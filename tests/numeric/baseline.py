r"""錄一個版本在這台電腦上的數值基準（docs/refactor-port-plan.md 3.8）。

從 git 匯出指定的版本，用 run.py 處理合成素材 N 次（預設 2），然後寫報告：
- 執行條件：run.py 為了讓結果可重現而改掉的設定，逐項列出；
- 實際使用的裝置與模型：顯示卡探測結果、ONNX 可用的 provider、各首歌實際用的分離模型、音高模型、子程式載入的程式路徑；
- 異常：結束代碼不是 0、run.py 回報的問題（素材 sha256 不符、模型檔被改、想對外連線、沒開過子程式）、
  log 裡的 Traceback／改用 CPU／改在主程式裡執行／退回／卡住／逾時／記憶體不足、子程式載入了別份程式、
  同一版兩次結果超過門檻。沒有就寫「無」；
- 兩次結果的差距（compare.pair）與每次和標準答案的差距（compare.quality）。
報告和輸出在 tests/private/upstream/<版本>/numeric/<標籤>/（不進 git）；
摘要（不含音檔）寫到 tests/numeric/baselines/<版本>-<標籤>.json（進 git，給另一台電腦、之後的重構版對照）。

用法（repo 根目錄，用已安裝版本的內嵌 Python）：
  <python> tests/numeric/baseline.py [git 參照，預設 v2.3.2] [--label=<這台電腦的名稱，例如 desktop-cpu>] [--runs=2] [--songs]
--songs：tests/private/songs/ 的真實歌曲也一起跑（run.py --songs；單執行緒，一首 3～4 分鐘的歌每次要幾分鐘）。
有異常時結束代碼是 1。
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / "tests" / "contract")]

import compare as C  # noqa: E402
from compare_upstream import LOG_ALERTS, OUT, _git, export  # noqa: E402

INPUTS = ROOT / "tests" / "private" / "numeric" / "inputs"
MODELS = ROOT / "tests" / "private" / "models"
SUMMARY = HERE / "baselines"
SONGS = ROOT / "tests" / "private" / "songs"


def machine() -> dict:
    ps = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; (Get-CimInstance Win32_Processor).Name; "
        "(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }) -join '、'; "
        "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    cpu, gpus, ram = ([*r.stdout.strip().splitlines(), "", "", ""])[:3]
    info = {"os": platform.platform(), "cpu": cpu.strip(), "gpus": gpus.strip(), "ram_gb": ram.strip(),
            "logical_cpus": os.cpu_count()}
    try:
        q = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version,power.limit",
                            "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
        info["nvidia_smi"] = q.stdout.strip() or q.stderr.strip()
    except Exception as e:  # noqa: BLE001
        info["nvidia_smi"] = f"沒有 nvidia-smi（{type(e).__name__}）"
    return info


def log_alerts(log: str, label: str) -> list[str]:
    out = []
    for line in log.splitlines():
        for needle, what in LOG_ALERTS:
            if needle in line:
                out.append(f"{label} log（{what}）：{line.strip()[:200]}")
                break
    for m in re.finditer(r"\[numeric\] 子程式載入 (.+)", log):
        p = m.group(1).strip()
        if "leihu-numeric-" not in p or not p.endswith(os.path.join("app", "gpu_worker.py")):
            out.append(f"{label} 子程式載入了暫存資料夾以外的程式：{p}")
    return out


def used(run_dir: Path, log: str) -> list[str]:
    """實際使用的裝置與模型。"""
    rj = json.loads((run_dir / "run.json").read_text("utf-8"))
    env = rj["environment"]
    probe = env.get("gpu_probe") or {}
    gpu = bool(probe.get("ok"))
    lines = [
        f"- 顯示卡探測（子程式）：{json.dumps(probe, ensure_ascii=False) if probe else '沒有探測結果'}",
        "- " + ("分離、音高、語音辨識在顯示卡上跑" if gpu else
                "沒有可用的顯示卡：log 裡的「[gpu] 顯示卡處理程式」實際上是在 CPU 上跑的子程式"),
        f"- ONNX Runtime provider：{env.get('onnx_providers')}",
        "- 套件：" + "、".join(f"{k} {v}" for k, v in env.items() if k not in ("onnx_providers", "gpu_probe", "executable")),
        f"- 音高模型：{'torchcrepe full（顯示卡）' if gpu else 'torchcrepe tiny（CPU）'}（pitch_model＝auto 時由顯示卡探測決定）",
        f"- 顯示卡子程式開了 {rj['worker_spawns']} 次",
    ]
    for sid in C.SONGS:
        m = C._json(run_dir / sid / "meta.json") or {}
        lines.append(f"- {sid}：separation＝{m.get('separation')}、vocal_model＝{m.get('vocal_model')}、karaoke＝{m.get('karaoke')}")
    loads = sorted(set(re.findall(r"\[numeric\] 子程式載入 (.+)", log)))
    lines.append(f"- 子程式載入的 gpu_worker：{'、'.join(p.strip() for p in loads) or '（log 裡沒有）'}")
    dev = sorted({ln.strip()[:160] for ln in log.splitlines() if re.search(r"(?i)execution provider|\[lyrics\]|\[cuda\]|\[ctranslate2\].*warning", ln)})
    lines += [f"- log：{d}" for d in dev[:12]]
    return lines


def main() -> int:  # noqa: C901
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in sys.argv[1:] if a.startswith("--"))
    ref = args[0] if args else "v2.3.2"
    label = opt.get("label") or platform.node().lower()
    runs = int(opt.get("runs", 2))
    if not (INPUTS / "manifest.json").exists() or not MODELS.exists():
        print(f"找不到 {INPUTS / 'manifest.json'} 或 {MODELS}：先跑 tests/numeric/make_inputs.py、複製模型（見計畫 3.8）")
        return 2
    name, code = export(ref)
    base = OUT / name / "numeric" / label
    base.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    logs: dict[int, str] = {}
    wall: dict[int, float] = {}
    for i in range(1, runs + 1):
        out = base / f"run{i}"
        if out.exists():
            import shutil

            shutil.rmtree(out)
        print(f"第 {i} 次：{name} → {out}", flush=True)
        t0 = time.time()
        with open(base / f"run{i}.log", "w", encoding="utf-8") as f:
            r = subprocess.run(
                [sys.executable, "-I", str(HERE / "run.py"), str(code), str(INPUTS), str(MODELS), str(out)]
                + ([f"--songs={SONGS}"] if "songs" in opt else []),
                stdout=f, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1"),
            )
        wall[i] = round(time.time() - t0, 1)
        logs[i] = (base / f"run{i}.log").read_text("utf-8", errors="replace")
        if r.returncode:
            problems.append(f"第 {i} 次 run.py 結束代碼 {r.returncode}（log：{base / f'run{i}.log'}）")
        if (out / "run.json").exists():
            problems += [f"第 {i} 次：{p}" for p in json.loads((out / "run.json").read_text("utf-8"))["problems"]]
        else:
            problems.append(f"第 {i} 次沒有產生 run.json，結果不完整")
        problems += log_alerts(logs[i], f"第 {i} 次")
    ok_runs = [i for i in logs if (base / f"run{i}" / "run.json").exists()]
    pair = C.pair(base / "run1", base / "run2") if {1, 2} <= set(ok_runs) else []
    problems += [f"同一版兩次結果超過門檻：{r['item']} {r['value']}（門檻 {r['limit']}）" for r in pair if not r["ok"]]
    if runs >= 2 and not pair:
        problems.append("少於兩次成功的執行，沒有做穩定性比對")
    quality = {i: C.quality(base / f"run{i}") for i in ok_runs}
    rj = json.loads((base / f"run{ok_runs[0]}" / "run.json").read_text("utf-8")) if ok_runs else {}
    mach = machine()
    tool = _git("rev-parse", "--short", "HEAD").strip()
    dirty = bool(_git("status", "--porcelain", "--", "tests").strip())

    rep = [
        f"# 數值基準：{name}（{label}）",
        "",
        f"- 被測程式：`{name}`（{code}）",
        f"- 測試工具：commit `{tool}`" + ("，`tests/` 有未 commit 的修改" if dirty else ""),
        f"- Python：{sys.version.split()[0]}（{sys.executable}）",
        f"- 電腦：{mach['cpu']}（{mach['logical_cpus']} 個邏輯處理器）；{mach['ram_gb']} GB RAM；{mach['os']}",
        f"- 顯示卡：{mach['gpus']}；nvidia-smi：{mach['nvidia_smi']}",
        f"- 素材：{INPUTS}（語音：{(rj.get('inputs') or {}).get('tts_voice')}）",
        f"- 執行次數：{runs}；每次總時間：" + "、".join(f"{v} 秒" for v in wall.values())
        + f"；時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 異常",
        "",
        *([f"- {p}" for p in problems] or ["無"]),
        "",
        "## 執行條件（和平常使用不同的地方）",
        "",
        *([f"- {c}" for c in rj.get("conditions", [])] or ["（沒有 run.json，讀不到）"]),
        "",
        "## 實際使用的裝置與模型",
        "",
        *(used(base / f"run{ok_runs[0]}", logs[ok_runs[0]]) if ok_runs else ["（沒有成功的執行）"]),
        "",
        "## 同一版兩次結果的差距",
        "",
        *(C.table(pair) if pair else ["（沒有做）"]),
        "",
    ]
    for i, q in quality.items():
        rep += [f"## 第 {i} 次和標準答案的差距", "", *C.table(q, with_limit=False), ""]
    (base / "numeric-report.md").write_text("\n".join(rep), "utf-8")
    SUMMARY.mkdir(exist_ok=True)
    summary = {
        "version": name,
        "label": label,
        "tool_commit": tool + ("+dirty" if dirty else ""),
        "machine": mach,
        # 不放 Python 的完整路徑（含使用者名稱）；報告裡有
        "environment": {k: v for k, v in (rj.get("environment") or {}).items() if k != "executable"},
        "conditions": rj.get("conditions"),
        "inputs": rj.get("inputs"),
        "problems": problems,
        "stability": pair,
        "quality": {str(i): q for i, q in quality.items()},
        "wall_seconds": wall,
    }
    out_json = SUMMARY / f"{name}-{label}.json"
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")
    print("\n".join(rep))
    print(f"報告：{base / 'numeric-report.md'}；摘要：{out_json}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
