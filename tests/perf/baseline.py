r"""錄一個版本在這台電腦上的效能基準（docs/refactor-port-plan.md 3.8）。

每首歌跑 N 次（預設 3）tests/perf/run_perf.py，同時每 0.25 秒取樣那個行程和它底下所有子孫行程的記憶體
（working set、private bytes；內嵌 Python 沒有 psutil，用 Windows API），有 nvidia-smi 時每秒記顯示卡記憶體與使用率
（整張卡，含其他程式；所以另外記 idle_before 時的值當底）。各階段取中位數，並列出最小、最大值看變異。

歌曲：
- 合成歌（一定會跑）：tests/private/numeric/inputs/sing.flac 接 7 次（約 3.6 分鐘），沒有授權問題，兩台電腦都能用；
- 真實歌曲：tests/private/songs/ 底下的音檔（自己擁有的歌，不進 git）。摘要只記 sha256、長度，不記檔名（檔名常是歌名）。
報告和 log 在 tests/private/upstream/<版本>/perf/<標籤>/（不進 git）；摘要寫到 tests/perf/baselines/<版本>-<標籤>.json（進 git）。
報告一定有「異常」（結束代碼、run.py 回報的問題、log 的 Traceback／改用 CPU／改在主程式裡執行／退回／卡住／逾時／
記憶體不足、子程式載入了別份程式），沒有就寫「無」；也列出執行條件和實際用的模型。

用法（repo 根目錄，用已安裝版本的內嵌 Python）：
  <python> tests/perf/baseline.py [git 參照，預設 v2.3.2] [--label=<這台電腦的名稱>] [--runs=3] [--quality=auto|standard]
有異常時結束代碼是 1。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT / "tests" / "numeric"), str(ROOT / "tests" / "contract")]

from baseline import log_alerts, machine  # noqa: E402  tests/numeric/baseline.py
from compare_upstream import OUT, _git, export  # noqa: E402

PRIVATE = ROOT / "tests" / "private"
MODELS = PRIVATE / "models"
SONGS = PRIVATE / "songs"
SUMMARY = HERE / "baselines"
AUDIO = (".flac", ".wav", ".mp3", ".m4a", ".ogg")
KEYS = ("ws_mb", "private_mb")


class _Mem(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class _Entry(ctypes.Structure):  # PROCESSENTRY32W
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def mem(pid: int) -> dict | None:
    """一個行程的記憶體（MB）；行程已經結束、打不開就是 None。"""
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000 | 0x0010, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ
    if not h:
        return None
    m = _Mem()
    m.cb = ctypes.sizeof(m)
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(wintypes.HANDLE(h), ctypes.byref(m), m.cb)
    k.CloseHandle(wintypes.HANDLE(h))
    return {"ws_mb": round(m.WorkingSetSize / 2**20), "private_mb": round(m.PrivateUsage / 2**20)} if ok else None


def descendants(root: int) -> list[tuple[int, str]]:
    """root 底下所有子孫行程的 (pid, 執行檔名)，用 Toolhelp32 行程快照。"""
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    e = _Entry()
    e.dwSize = ctypes.sizeof(e)
    kids: dict[int, list[tuple[int, str]]] = {}
    ok = k.Process32FirstW(wintypes.HANDLE(snap), ctypes.byref(e))
    while ok:
        kids.setdefault(e.th32ParentProcessID, []).append((e.th32ProcessID, e.szExeFile))
        ok = k.Process32NextW(wintypes.HANDLE(snap), ctypes.byref(e))
    k.CloseHandle(wintypes.HANDLE(snap))
    # shortcut: 父行程結束後 pid 被重用時，不相干的行程可能被算進來；取樣只有幾分鐘、子行程都由被測程式自己起，先不處理
    out, seen, todo = [], {root}, [root]
    while todo:
        for pid, exe in kids.get(todo.pop(), []):
            if pid not in seen:
                seen.add(pid)
                out.append((pid, exe))
                todo.append(pid)
    return out


def sample(root: int) -> dict:
    """主程式、子孫行程合計、兩者合計的記憶體。"""
    s = mem(root) or dict.fromkeys(KEYS, 0)
    kids = [(exe, m) for pid, exe in descendants(root) if (m := mem(pid))]
    c = {k: sum(m[k] for _, m in kids) for k in KEYS}
    return {"t": time.time(), "main": s, "children": c, "total": {k: s[k] + c[k] for k in KEYS},
            "procs": sorted({exe for exe, _ in kids})}


def gpu_sample() -> dict | None:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        used, util = (float(x) for x in r.stdout.strip().splitlines()[0].split(","))
        return {"t": time.time(), "mem_mb": used, "util": util}
    except Exception:  # noqa: BLE001  沒有 nvidia-smi 或讀不到：不記
        return None


def synth_song() -> Path:
    """sing.flac 接 7 次的合成歌（每次重做，內容固定）。"""
    import numpy as np
    import soundfile as sf

    src = PRIVATE / "numeric" / "inputs" / "sing.flac"
    dst = PRIVATE / "perf" / "synth_sing_x7.flac"
    dst.parent.mkdir(parents=True, exist_ok=True)
    y, sr = sf.read(str(src), dtype="float32", always_2d=True)
    sf.write(str(dst), np.concatenate([y] * 7), sr, subtype="PCM_24")
    return dst


def run_one(code: Path, song: Path, out: Path, quality: str) -> tuple[int, list[dict], list[dict], str]:
    log = out.with_suffix(".log")
    with open(log, "w", encoding="utf-8") as f:
        p = subprocess.Popen(
            [sys.executable, "-I", str(HERE / "run_perf.py"), str(code), str(song), str(MODELS), str(out),
             f"--quality={quality}"],
            stdout=f, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1"),
        )
        mems: list[dict] = []
        gpus: list[dict] = []
        stop = threading.Event()

        def gpu_loop():
            while not stop.wait(1.0):
                if (g := gpu_sample()) is not None:
                    gpus.append(g)

        th = threading.Thread(target=gpu_loop, daemon=True)
        th.start()
        while p.poll() is None:
            mems.append(sample(p.pid))
            time.sleep(0.25)
        stop.set()
        th.join(timeout=5)
    return p.returncode, mems, gpus, log.read_text("utf-8", errors="replace")


def per_phase(phases: list[dict], mems: list[dict], gpus: list[dict]) -> dict:
    out = {}
    for ph in phases:
        inside = [m for m in mems if ph["start"] <= m["t"] <= ph["end"]]
        g = [x for x in gpus if ph["start"] <= x["t"] <= ph["end"]]
        out[ph["name"]] = {
            "seconds": round(ph["end"] - ph["start"], 2),
            "ok": ph["ok"],
            "samples": len(inside),
            **{f"{grp}_{k}": max((m[grp][k] for m in inside), default=None) for grp in ("main", "children", "total")
               for k in KEYS},
            "procs": sorted({x for m in inside for x in m["procs"]}),
            "gpu_mem_mb": max((x["mem_mb"] for x in g), default=None),
            "gpu_util": max((x["util"] for x in g), default=None),
        }
    return out


def stats(vals: list) -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return "—"
    med = statistics.median(vals)
    return f"{med:g}" if len(vals) == 1 else f"{med:g}（{min(vals):g}～{max(vals):g}）"


def gpu_tasks(log: str) -> list[str]:
    """log 裡每個顯示卡子程式工作的時間，例如「separate 13.2」。"""
    return [f"{m[1]} {m[2]}" for m in re.finditer(r"\[gpu\] (\w+) 完成（([\d.]+) 秒）", log)]


def main() -> int:  # noqa: C901
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in sys.argv[1:] if a.startswith("--"))
    ref = args[0] if args else "v2.3.2"
    label = opt.get("label") or platform.node().lower()
    runs = int(opt.get("runs", 3))
    quality = opt.get("quality", "auto")
    if not (PRIVATE / "numeric" / "inputs" / "sing.flac").exists() or not MODELS.exists():
        print("找不到 tests/private/numeric/inputs/sing.flac 或 tests/private/models（見計畫 3.8）")
        return 2
    name, code = export(ref)
    base = OUT / name / "perf" / label
    base.mkdir(parents=True, exist_ok=True)
    songs = [("synth", synth_song())]
    songs += [(f"song{i + 1:02d}", p) for i, p in enumerate(sorted(x for x in SONGS.glob("*") if x.suffix.lower() in AUDIO))]
    problems: list[str] = []
    results: dict[str, dict] = {}
    first_log = ""
    first_run = None
    for sid, song in songs:
        sha = hashlib.sha256(song.read_bytes()).hexdigest()
        rec = {"sha256": sha, "source": "合成（sing.flac × 7）" if sid == "synth" else "tests/private/songs", "runs": []}
        for i in range(1, runs + 1):
            out = base / f"{sid}-run{i}"
            print(f"{sid} 第 {i} 次 → {out}", flush=True)
            rc, mems, gpus, log = run_one(code, song, out, quality)
            label_i = f"{sid} 第 {i} 次"
            if rc:
                problems.append(f"{label_i} run_perf.py 結束代碼 {rc}（log：{out.with_suffix('.log')}）")
            rj = json.loads((out / "run.json").read_text("utf-8")) if (out / "run.json").exists() else None
            if rj is None:
                problems.append(f"{label_i} 沒有產生 run.json，結果不完整")
            else:
                problems += [f"{label_i}：{p}" for p in rj["problems"]]
            problems += log_alerts(log.replace("[perf] 子程式載入", "[numeric] 子程式載入"), label_i)
            phases = json.loads((out / "phases.json").read_text("utf-8")) if (out / "phases.json").exists() else []
            rec["runs"].append({"rc": rc, "phases": per_phase(phases, mems, gpus), "gpu_tasks": gpu_tasks(log),
                                "run": rj})
            (out / "samples.json").write_text(json.dumps({"mem": mems, "gpu": gpus}), "utf-8")
            if first_run is None and rj is not None:
                first_run, first_log = rj, log
        results[sid] = rec
    mach = machine()
    tool = _git("rev-parse", "--short", "HEAD").strip()
    dirty = bool(_git("status", "--porcelain", "--", "tests").strip())
    env = (first_run or {}).get("environment") or {}
    probe = env.get("gpu_probe") or {}
    rep = [
        f"# 效能基準：{name}（{label}）",
        "",
        f"- 被測程式：`{name}`（{code}）",
        f"- 測試工具：commit `{tool}`" + ("，`tests/` 有未 commit 的修改" if dirty else ""),
        f"- Python：{sys.version.split()[0]}（{sys.executable}）",
        f"- 電腦：{mach['cpu']}（{mach['logical_cpus']} 個邏輯處理器）；{mach['ram_gb']} GB RAM；{mach['os']}",
        f"- 顯示卡：{mach['gpus']}；nvidia-smi：{mach['nvidia_smi']}",
        f"- 每首歌跑 {runs} 次；分離品質：{quality}；時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "- 數字是中位數（括號是最小～最大）。時間單位秒、記憶體 MB；記憶體是各階段裡取樣到的最大值"
        "（每 0.25 秒取樣，working set／private bytes）。main＝被測程式的主程式，children＝它底下所有子程式",
        "",
        "## 異常",
        "",
        *([f"- {p}" for p in problems] or ["無"]),
        "",
        "## 執行條件（和平常使用不同的地方）",
        "",
        *([f"- {c}" for c in (first_run or {}).get("conditions", [])] or ["（沒有 run.json，讀不到）"]),
        "",
        "## 實際使用的裝置與模型",
        "",
        f"- 顯示卡探測（子程式）：{json.dumps(probe, ensure_ascii=False) if probe else '沒有探測結果'}",
        "- " + ("分離、音高、語音辨識在顯示卡上跑" if probe.get("ok") else
                "沒有可用的顯示卡：log 裡的「[gpu] 顯示卡處理程式」實際上是在 CPU 上跑的子程式"),
        f"- ONNX Runtime provider：{env.get('onnx_providers')}",
        "- 套件：" + "、".join(f"{k} {v}" for k, v in env.items() if k not in ("onnx_providers", "gpu_probe", "executable")),
        f"- 音高模型：{'torchcrepe full（顯示卡）' if probe.get('ok') else 'torchcrepe tiny（CPU）'}（pitch_model＝auto）",
    ]
    for sid, rec in results.items():
        songs_used = {json.dumps((r["run"] or {}).get("song", {}), ensure_ascii=False) for r in rec["runs"]}
        rep += [f"- {sid}：" + "；".join(sorted(songs_used))]
    dev = sorted({ln.strip()[:160] for ln in first_log.splitlines()
                  if re.search(r"(?i)\[cuda\]|\[ctranslate2\].*warning|\[separate\]", ln)})
    rep += [f"- log：{d}" for d in dev[:10]]
    startup = [r["run"]["startup"] for rec in results.values() for r in rec["runs"] if r["run"]]
    rep += [
        "",
        "## 啟動",
        "",
        f"- import pipeline、share：{stats([s['import_pipeline_share'] for s in startup])} 秒；"
        f"import app：{stats([s['import_app'] for s in startup])} 秒（不含開伺服器、背景執行緒）",
    ]
    order = ["idle_before", "import", "separate", "pitch", "lyrics", "variant", "score", "export", "idle_after"]
    for sid, rec in results.items():
        dur = next(((r["run"] or {}).get("song", {}).get("duration") for r in rec["runs"] if r["run"]), None)
        rep += ["", f"## {sid}（{dur} 秒，sha256 {rec['sha256'][:12]}，{rec['source']}）", "",
                "| 階段 | 時間 | main ws | main private | children ws | children private | total private | 顯示卡記憶體 | 顯示卡使用率 |",
                "|---|---|---|---|---|---|---|---|---|"]
        for ph in order:
            vals = [r["phases"].get(ph) for r in rec["runs"] if r["phases"].get(ph)]
            if not vals:
                continue
            col = lambda k: stats([v[k] for v in vals])  # noqa: E731
            rep.append(f"| {ph} | {col('seconds')} | {col('main_ws_mb')} | {col('main_private_mb')} | "
                       f"{col('children_ws_mb')} | {col('children_private_mb')} | {col('total_private_mb')} | "
                       f"{col('gpu_mem_mb')} | {col('gpu_util')} |")
        rep += ["", "顯示卡子程式每個工作的時間（log 的「[gpu] … 完成」，每次一行）：", ""]
        rep += [f"- 第 {i} 次：{'、'.join(r['gpu_tasks']) or '（沒有）'}" for i, r in enumerate(rec["runs"], 1)]
    (base / "perf-report.md").write_text("\n".join(rep), "utf-8")
    SUMMARY.mkdir(exist_ok=True)
    summary = {
        "version": name,
        "label": label,
        "tool_commit": tool + ("+dirty" if dirty else ""),
        "machine": mach,
        "environment": {k: v for k, v in env.items() if k != "executable"},  # 不放含使用者名稱的路徑
        "conditions": (first_run or {}).get("conditions"),
        "quality": quality,
        "runs_per_song": runs,
        "problems": problems,
        "songs": {sid: {"sha256": rec["sha256"], "source": rec["source"],
                        "runs": [{"phases": r["phases"], "gpu_tasks": r["gpu_tasks"],
                                  "startup": (r["run"] or {}).get("startup"), "song": (r["run"] or {}).get("song")}
                                 for r in rec["runs"]]}
                  for sid, rec in results.items()},
    }
    out_json = SUMMARY / f"{name}-{label}.json"
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")
    print("\n".join(rep))
    print(f"報告：{base / 'perf-report.md'}；摘要：{out_json}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
