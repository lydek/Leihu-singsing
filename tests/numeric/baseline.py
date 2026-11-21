r"""錄一個版本在這台電腦上的數值基準（docs/refactor-port-plan.md 3.8）。

從 git 匯出指定的版本，用 run.py 處理合成素材 N 次（預設 2），然後寫報告：
- 執行條件：run.py 為了讓結果可重現而改掉的設定，逐項列出；
- 實際使用的裝置與模型：子程式每一步回報的裝置（child_hook.py 的 [device]）、各首歌實際用的分離模型、
  子程式載入的程式路徑；顯示卡探測結果只列出來，不拿來推定每一步用的裝置；
- 異常：結束代碼不是 0、run.py 回報的問題（素材 sha256 不符、模型檔被改、想對外連線、沒開過子程式）、
  被測程式 logs/ 裡的錯誤紀錄）、log 裡的 Traceback／改用 CPU／改用標準模型／改在主程式裡執行／退回／卡住／逾時／
  記憶體不足、子程式載入了別份程式、同一版各次輸出不完全相同（compare.exact，第 2 次起都和第 1 次比）。沒有就寫「無」；
- 第 1、2 次的差距表（compare.pair，參考用）與每次和標準答案的差距（compare.quality）。
報告和輸出在 tests/private/upstream/<版本>/numeric/<標籤>/（不進 git）；
摘要（不含音檔）寫到 tests/numeric/baselines/<版本>-<標籤>.json（進 git，給另一台電腦、之後的重構版對照）。

用法（repo 根目錄，用已安裝版本的內嵌 Python）：
  <python> tests/numeric/baseline.py [git 參照，預設 v2.3.2] [--label=<這台電腦的名稱，例如 desktop-cpu>] [--runs=2] [--songs]
  [--timeout=<每次執行的秒數上限，預設 7200>] [--device=auto|cpu]
--device=cpu：強制只用 CPU（CUDA_VISIBLE_DEVICES=-1），例如在有 NVIDIA 顯示卡的筆電上錄 CPU 基準；不能只靠 --label 區分。
穩定性：所有步驟實際都在 CPU 上（看 [device]）時，各次輸出要逐檔完全相同；有任何一步在顯示卡上時，
  浮點運算順序本來就可能不同，改用差距表的門檻判斷，逐檔核對只列出來參考。
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
from compare_upstream import EXPORT_NOTES, write_summary, tools_dirty, LOG_ALERTS, OUT, _git, commit_of, export  # noqa: E402

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


TIMEOUT = 7200  # 秒：每一次執行的上限（--timeout 可改）；推論卡住時不會一直等下去


def kill_tree(p: subprocess.Popen) -> tuple[bool, str]:
    """結束整個行程樹：先記下所有子孫（PID＋建立時間），taskkill 和等待都有期限，最後逐一確認都停了。
    回傳 (確認全部停止, 說明)；說明要寫進報告。"""
    from sandbox import still_alive, tree_with_start

    tree = tree_with_start(p.pid)
    try:
        r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        note = "" if r.returncode == 0 else f"taskkill 結束代碼 {r.returncode}：{(r.stdout + r.stderr).strip()[:200]}"
    except subprocess.TimeoutExpired:
        note = "taskkill 60 秒沒有結束"
    try:
        p.wait(timeout=60)
    except subprocess.TimeoutExpired:
        pass
    for _ in range(20):
        left = still_alive(tree)
        if not left:
            return True, note
        time.sleep(0.5)
    return False, f"未確認全部停止：PID {left} 還在跑（{note or 'taskkill 回報成功'}），沙盒保留不刪"


def run_logged(cmd: list[str], log: Path, timeout: float) -> tuple[int | None, list[str]]:
    """執行並把輸出寫進 log；超過 timeout 秒就結束整個行程樹（含顯示卡子程式），清掉它留下的暫存資料夾。
    回傳 (結束代碼，逾時時是 None；要寫進報告的問題)。"""
    from sandbox import clean_stale

    with open(log, "w", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                             env=dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1"))
        try:
            return p.wait(timeout=timeout), []
        except subprocess.TimeoutExpired:
            stopped, note = kill_tree(p)
    head = (f"超過 {timeout:g} 秒沒有結束，" + ("已結束整個行程樹" if stopped else "嘗試結束行程樹")
            + f"，這次結果不完整（log：{log}）")
    with open(log, "a", encoding="utf-8") as f:  # 也寫回 log，事後看 log 就知道
        f.write(f"\n[baseline] {head}{'；' + note if note else ''}\n")
    # 沒確認全部停止時不清理：子孫可能還在用沙盒（clean_stale 只看建立者，看不到孫行程）
    return None, [head, *([note] if note else []), *(clean_stale() if stopped else [])]


def log_alerts(log: str, label: str) -> list[str]:
    out = []
    for line in log.splitlines():
        if re.match(r"\[(?:numeric|perf)\] 條件：", line):  # 工具自己印的執行條件（報告另列），不是被測程式的訊息
            continue
        for needle, what in LOG_ALERTS:
            if needle in line:
                out.append(f"{label} log（{what}）：{line.strip()[:200]}")
                break
    loaded = [("gpu_worker.py", m.group(1).strip()) for m in re.finditer(r"\[(?:numeric|perf)\] 子程式載入 (.+)", log)]
    loaded += [("pipeline.py", m.group(1).strip()) for m in re.finditer(r"\[source\] pipeline (.+)", log)]
    roots = re.findall(r"\[(?:numeric|perf)\] 沙盒 (.+)", log)
    root = roots[0].strip() if roots else None
    if loaded and root is None:
        out.append(f"{label} log 裡沒有這次的沙盒路徑，子程式來源無法核對")
    for want, p in loaded:  # 一定要是這一次自己的沙盒（不是別的 session 的同名資料夾）
        if root is None or os.path.normcase(p) != os.path.normcase(os.path.join(root, want)):
            out.append(f"{label} 子程式載入了這次沙盒以外的程式：{p}")
    if "[gpu] " in log and not any(w == "pipeline.py" for w, _ in loaded):
        out.append(f"{label} 子程式沒有回報 pipeline 的來源（child_hook 沒有生效）")
    return out


def device_lines(log: str) -> list[str]:
    """子程式回報的每一步實際裝置（child_hook.py）；有做的步驟卻沒有回報就寫「未確認」。"""
    seen = sorted({ln.strip()[len("[device] "):] for ln in log.splitlines() if ln.startswith("[device] ")})
    out = [f"- 實際裝置：{x}" for x in seen]
    for step in ("separate", "pitch", "whisper"):
        if f"[gpu] {step} 完成" in log and not any(x.startswith(step) for x in seen):
            out.append(f"- 實際裝置：{step} 未確認（子程式做了這一步，但沒有 [device] 紀錄）")
    if "改在主程式裡執行" in log:
        out.append("- 有工作改在主程式裡執行：那些工作的裝置沒有 [device] 紀錄（見異常）")
    return out


def device_report(logs: dict[str, str]) -> list[str]:
    """每一次各自的實際裝置；每次都一樣就只列一次並註明。"""
    per = {k: device_lines(v) for k, v in logs.items()}
    uniq = {tuple(v) for v in per.values()}
    if len(uniq) == 1:
        return [*next(iter(uniq)), f"- （{'、'.join(per)}的實際裝置都一樣）"]
    return [x for k, v in per.items() for x in (f"- {k}：", *[f"  {y}" for y in v])]


def gpu_used(logs: dict[str, str]) -> bool:
    """有沒有任何一步實際在顯示卡上跑（看 [device] 紀錄）。"""
    return any("cuda" in ln.lower() for log in logs.values() for ln in log.splitlines() if ln.startswith("[device] "))


def used(run_dir: Path, logs: dict[str, str]) -> list[str]:
    """實際使用的裝置與模型（裝置逐次列；模型、套件看第一次成功的那次）。"""
    log = "\n".join(logs.values())
    rj = json.loads((run_dir / "run.json").read_text("utf-8"))
    env = rj["environment"]
    probe = env.get("gpu_probe") or {}
    lines = [
        f"- 顯示卡探測（子程式）：{json.dumps(probe, ensure_ascii=False) if probe else '沒有探測結果'}"
        "（只代表探測用的小運算能不能在顯示卡上跑；每一步實際用的裝置看下面的「實際裝置」）",
        *device_report(logs),
        f"- ONNX Runtime 可用的 provider：{env.get('onnx_providers')}",
        "- 套件：" + "、".join(f"{k} {v}" for k, v in env.items() if k not in ("onnx_providers", "gpu_probe", "executable")),
        f"- 顯示卡子程式開了 {rj['worker_spawns']} 次（log 的「[gpu] 顯示卡處理程式」是子程式的名稱，不代表一定用顯示卡）",
    ]
    for sid in C.songs(run_dir, run_dir):
        m = C._json(run_dir / sid / "meta.json") or {}
        lines.append(f"- {sid}：separation＝{m.get('separation')}、vocal_model＝{m.get('vocal_model')}、karaoke＝{m.get('karaoke')}")
    loads = sorted(set(re.findall(r"\[numeric\] 子程式載入 (.+)", log)))
    lines.append(f"- 子程式載入的 gpu_worker：{'、'.join(p.strip() for p in loads) or '（log 裡沒有）'}")
    dev = sorted({ln.strip()[:160] for ln in log.splitlines()
                  if re.search(r"(?i)\[lyrics\]|\[cuda\]|\[separate\]|\[ctranslate2\].*warning", ln)})
    lines += [f"- log：{d}" for d in dev[:12]]
    return lines


def main() -> int:  # noqa: C901
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in sys.argv[1:] if a.startswith("--"))
    ref = args[0] if args else "v2.3.2"
    label = opt.get("label") or platform.node().lower()
    runs = int(opt.get("runs", 2))
    if runs < 2:
        print("--runs 至少要 2：數值基準要同一版跑兩次以上，逐檔核對確認結果穩定")
        return 2
    if not (INPUTS / "manifest.json").exists() or not MODELS.exists():
        print(f"找不到 {INPUTS / 'manifest.json'} 或 {MODELS}：先跑 tests/numeric/make_inputs.py、複製模型（見計畫 3.8）")
        return 2
    name, code = export(ref)
    base = OUT / name / "numeric" / label
    base.mkdir(parents=True, exist_ok=True)
    problems: list[str] = list(EXPORT_NOTES)
    logs: dict[int, str] = {}
    wall: dict[int, float] = {}
    for i in range(1, runs + 1):
        out = base / f"run{i}"
        if out.exists():
            import shutil

            shutil.rmtree(out)
        print(f"第 {i} 次：{name} → {out}", flush=True)
        t0 = time.time()
        rc, late = run_logged(
            [sys.executable, "-I", str(HERE / "run.py"), str(code), str(INPUTS), str(MODELS), str(out)]
            + ([f"--songs={SONGS}"] if "songs" in opt else []) + [f"--device={opt.get('device', 'auto')}"],
            base / f"run{i}.log", float(opt.get("timeout", TIMEOUT)),
        )
        wall[i] = round(time.time() - t0, 1)
        logs[i] = (base / f"run{i}.log").read_text("utf-8", errors="replace")
        problems += [f"第 {i} 次：{p}" for p in late]
        if rc:
            problems.append(f"第 {i} 次 run.py 結束代碼 {rc}（log：{base / f'run{i}.log'}）")
        if (out / "run.json").exists():
            problems += [f"第 {i} 次：{p}" for p in json.loads((out / "run.json").read_text("utf-8"))["problems"]]
        else:
            problems.append(f"第 {i} 次沒有產生 run.json，結果不完整")
        problems += log_alerts(logs[i], f"第 {i} 次")
        if (out / "run.json").exists():
            problems += [f"第 {i} 次輸出不完整：{p}" for p in C.required(out)]
    ok_runs = [i for i in logs if (base / f"run{i}" / "run.json").exists()
               and not json.loads((base / f"run{i}" / "run.json").read_text("utf-8")).get("incomplete")]
    # 穩定性：第 2 次起每一次都和第 1 次比。全部在 CPU 上要逐檔完全相同；有用到顯示卡改看差距表的門檻
    on_gpu = gpu_used({f"第 {i} 次": logs[i] for i in ok_runs})
    exact = {i: C.exact(base / "run1", base / f"run{i}") for i in ok_runs if i != 1 and 1 in ok_runs}
    pairs = {i: C.pair(base / "run1", base / f"run{i}") for i in exact}
    for i in exact:
        rows, what = (pairs[i], "超過差距門檻") if on_gpu else (exact[i], "不完全相同")
        problems += [f"第 1 次和第 {i} 次{what}：{r['item']} {r['value']}" for r in rows if not r["ok"]]
    pair = pairs.get(2, [])
    if runs >= 2 and not exact:
        problems.append("少於兩次成功的執行，沒有做穩定性比對")
    quality = {i: C.quality(base / f"run{i}") for i in ok_runs}
    rj = json.loads((base / f"run{ok_runs[0]}" / "run.json").read_text("utf-8")) if ok_runs else {}
    mach = machine()
    tool = _git("rev-parse", "--short", "HEAD").strip()
    dirty = tools_dirty()

    rep = [
        f"# 數值基準：{name}（{label}）",
        "",
        f"- 被測程式：`{name}`（commit {commit_of(code)}；{code}）",
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
        *(used(base / f"run{ok_runs[0]}", {f"第 {i} 次": logs[i] for i in ok_runs}) if ok_runs
          else ["（沒有成功的執行）"]),
        "",
        "穩定性判斷：" + ("有步驟在顯示卡上跑，用差距表的門檻（逐檔核對只供參考）" if on_gpu
                          else "全部在 CPU 上，各次輸出要逐檔完全相同"),
        "",
        "## 同一版各次輸出逐檔核對（和第 1 次比）",
        "",
        *[x for i, rows in exact.items()
          for x in (f"第 {i} 次：{sum(r['ok'] for r in rows)}／{len(rows)} 個檔案完全相同", "", *C.table(rows), "")],
        *([] if exact else ["（沒有做）", ""]),
        "## 第 1、2 次的差距表（參考：逐檔核對通過時這裡一定全部通過）",
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
        "commit": commit_of(code),
        "inputs": rj.get("inputs"),
        "songs": rj.get("songs"),  # run.py --songs 的真實歌曲：匿名編號 → sha256（不記檔名）
        "models": rj.get("models"),  # 模型檔 → sha256
        "problems": problems,
        "on_gpu": on_gpu,
        "exact": {str(i): rows for i, rows in exact.items()},
        "stability": pair,
        "quality": {str(i): q for i, q in quality.items()},
        "wall_seconds": wall,
    }
    out_json = SUMMARY / f"{name}-{label}.json"
    why = write_summary(out_json, summary)
    if why:
        problems.append(why)
        (base / "numeric-report.md").write_text("\n".join(rep) + f"\n\n## 摘要沒有寫入\n\n- {why}\n", "utf-8")
    print("\n".join(rep))
    print(f"報告：{base / 'numeric-report.md'}；摘要：{out_json}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
