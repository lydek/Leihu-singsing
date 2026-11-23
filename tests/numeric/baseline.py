r"""錄一個版本在這台電腦上的數值基準（docs/refactor-port-plan.md 3.8）。

從 git 匯出指定的版本，用 run.py 處理合成素材和測試計畫指定的真實歌曲 N 次（次數寫在計畫裡；沒給計畫只跑合成素材 2 次），
然後寫報告：
- 執行條件：run.py 為了讓結果可重現而改掉的設定，逐項列出；
- 實際使用的裝置與模型：子程式每一步回報的裝置（child_hook.py 的 [device]）、各首歌實際用的分離模型、
  子程式載入的程式路徑；顯示卡探測結果只列出來，不拿來推定每一步用的裝置；
- 異常：結束代碼不是 0、run.py 回報的問題（素材 sha256 不符、模型檔被改、想對外連線、沒開過子程式）、
  被測程式 logs/ 裡的錯誤紀錄）、log 裡的 Traceback／改用 CPU／改用標準模型／改在主程式裡執行／退回／卡住／逾時／
  記憶體不足、子程式載入了別份程式、同一版各次輸出不完全相同（compare.exact，第 2 次起都和第 1 次比）。沒有就寫「無」；
- 第 1、2 次的差距表（compare.pair，參考用）與每次和標準答案的差距（compare.quality）；
- --against=<版本>：這次的第 1 次和那個版本用同一個計畫、同一個標籤錄的第 1 次逐檔核對（重構前後是否等價）。
報告和輸出在 tests/private/upstream/<版本>/numeric/<標籤>[-<計畫名稱>]/（不進 git）；
摘要（不含音檔）寫到 tests/numeric/baselines/<版本>-<標籤>[-<計畫名稱>].json（進 git，給另一台電腦、之後的重構版對照）。
同一時間只能錄一個基準（sandbox.acquire_recording）：別的基準還在跑就不開始。

用法（repo 根目錄，用已安裝版本的內嵌 Python）：
  <python> tests/numeric/baseline.py [git 參照，預設 v2.3.2] [--label=<這台電腦的名稱，例如 desktop-cpu>]
  [--plan=<測試計畫 JSON，見 plan.py>] [--against=<版本>] [--timeout=<每次執行的秒數上限，預設 7200>] [--device=auto|cpu] [--floor-version=<上游版本，預設 v2.3.2>]
--device=cpu：強制只用 CPU（CUDA_VISIBLE_DEVICES=-1），例如在有 NVIDIA 顯示卡的筆電上錄 CPU 基準；不能只靠 --label 區分。
穩定性：所有步驟實際都在 CPU 上（看 [device]）時，各次輸出要逐檔完全相同；有任何一步在顯示卡上時，
  浮點運算順序本來就可能不同，改用差距表的門檻判斷，逐檔核對只列出來參考。
--plan：計畫的 numeric 段落指定次數和真實歌曲（單執行緒，一首 3～4 分鐘的歌每次要 3～6 分鐘）。只跑 1 次時同一版的穩定性
  不判定（寫進執行條件），要靠 --against 和已知穩定的版本逐檔相同來確認。
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
import plan as PLAN  # noqa: E402
from compare_upstream import BASE, EXPORT_CLEANED, EXPORT_NOTES, private_copy, ref_name, write_summary, tools_dirty, LOG_ALERTS, OUT, _git, commit_of, export  # noqa: E402

INPUTS = ROOT / "tests" / "private" / "numeric" / "inputs"
MODELS = ROOT / "tests" / "private" / "models"
SUMMARY = HERE / "baselines"


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


KILL: dict = {"tree": None}  # 最後一次 kill_tree 記下的行程樹（沒確認停止時寫進 keep.json）


def kill_tree(p: subprocess.Popen) -> tuple[bool, str]:
    """結束整個行程樹：先記下所有子孫（PID＋建立時間），taskkill 和等待都有期限，最後逐一確認都停了。
    回傳 (確認全部停止, 說明)；說明要寫進報告。"""
    from sandbox import still_alive, tree_with_start

    tree = tree_with_start(p.pid)
    KILL["tree"] = tree
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
    if tree is None:
        return False, f"行程快照建不起來，無法確認子孫行程都停了（{note or 'taskkill 回報成功'}），沙盒保留不刪"
    for _ in range(20):
        left = still_alive(tree)
        if not left:
            return True, note
        time.sleep(0.5)
    return False, f"未確認全部停止：PID {left} 還在跑或查不到狀態（{note or 'taskkill 回報成功'}），沙盒保留不刪"


def run_logged(cmd: list[str], log: Path, timeout: float) -> tuple[int | None, list[str], bool]:
    """執行並把輸出寫進 log；超過 timeout 秒就結束整個行程樹（含顯示卡子程式），清掉它留下的暫存資料夾。
    回傳 (結束代碼，逾時時是 None；要寫進報告的問題；確認行程都停了)。沒確認停止時呼叫端要停止後面的執行。"""
    from sandbox import clean_stale, mark_keep

    with open(log, "w", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                             env=dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1"))
        try:
            return p.wait(timeout=timeout), [], True
        except subprocess.TimeoutExpired:
            stopped, note = kill_tree(p)
    head = (f"超過 {timeout:g} 秒沒有結束，" + ("已結束整個行程樹" if stopped else "嘗試結束行程樹")
            + f"，這次結果不完整（log：{log}）")
    with open(log, "a", encoding="utf-8") as f:  # 也寫回 log，事後看 log 就知道
        f.write(f"\n[baseline] {head}{'；' + note if note else ''}\n")
    # 沒確認全部停止時不清理，並在沙盒裡留 keep.json：子孫可能還在用沙盒（clean_stale 只看建立者，看不到孫行程）
    keep = [] if stopped else [mark_keep(log.read_text("utf-8", errors="replace"), KILL["tree"], note)]
    return None, [head, *([note] if note else []), *keep, *(clean_stale() if stopped else [])], stopped


# 被測程式的 app.log 每一行開頭有時間（leihu/logstamp.py，2.3.5.1 之後）；解析前去掉，舊版的 log 沒有也照樣讀
STAMP = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d ", re.M)


def unstamp(log: str) -> str:
    return STAMP.sub("", log)


def log_alerts(log: str, label: str) -> list[str]:
    out = []
    for line in log.splitlines():
        if re.match(r"\[(?:numeric|perf)\] 條件：", unstamp(line)):  # 工具自己印的執行條件（報告另列），不是被測程式的訊息
            continue
        for needle, what in LOG_ALERTS:
            if needle in line:
                out.append(f"{label} log（{what}）：{line.strip()[:200]}")
                break
    # 子程式做了某一步卻沒有 [device] 紀錄：實際裝置不明，CPU／顯示卡的穩定性判定也會選錯（時間前綴曾讓整份讀不到）
    out += [f"{label} {x[2:]}" for x in device_lines(log) if "未確認" in x]
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
    log = unstamp(log)
    seen = sorted({ln.strip()[len("[device] "):] for ln in log.splitlines() if ln.startswith("[device] ")})
    out = [f"- 實際裝置：{x}" for x in seen]
    for step in ("separate", "pitch", "whisper"):
        if f"[gpu] {step} 完成" in log and not any(x.startswith(step) for x in seen):
            out.append(f"- 實際裝置：{step} 未確認（子程式做了這一步，但沒有 [device] 紀錄）")
    if "改在主程式裡執行" in log:
        out.append("- 有工作改在主程式裡執行：那些工作的裝置沒有 [device] 紀錄（見異常）")
    return out


def valid_runs(base: Path, logs: dict) -> list[int]:
    """可以拿來比的那幾次：有 run.json、不是中途中斷、必要產物都在而且讀得了（問題已另外列進異常）。"""
    out = []
    for i in logs:
        f = base / f"run{i}" / "run.json"
        try:
            ok = f.exists() and not json.loads(f.read_text("utf-8")).get("incomplete") and not C.required(base / f"run{i}")
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            out.append(i)
    return out


FLOOR_PACKAGES = ("torch", "onnxruntime", "onnxruntime-gpu", "audio-separator", "faster-whisper", "ctranslate2",
                  "torchcrepe")


def floor_source(floor_path: Path, expect: dict, mode: str = "fixed",
                 quality: str | None = "standard") -> tuple[dict | None, list[str]]:
    """讀相似度下限，核對它能不能拿來判斷這一次：上游版本、固定條件（similarity.py --fixed）、標準模型、
    錄的時候沒有異常，以及電腦、套件版本、模型、素材都和這一次一樣。回傳 (下限, 不能用的原因)。"""
    if not floor_path.exists():
        return None, [f"顯示卡路徑沒有同一台電腦的相似度下限（{floor_path.name}），穩定性沒有判定："
                      "先在這台電腦用同一個 --label 跑 tests/numeric/similarity.py v2.3.2 --fixed"]
    f = json.loads(floor_path.read_text("utf-8"))
    why = []
    if f.get("mode") != mode:
        why.append(f"下限是 {f.get('mode') or 'normal'} 條件錄的，這次要的是 {mode}" +
                   ("（固定條件的下限要用 similarity.py --fixed 錄）" if mode == "fixed" else ""))
    if quality is not None and f.get("quality") != quality:
        why.append(f"下限的分離品質是 {f.get('quality')}，這次是 {quality}")
    if f.get("problems"):
        why.append(f"下限錄的時候有 {len(f['problems'])} 項異常")
    for key in ("cpu", "gpus"):
        if (f.get("machine") or {}).get(key) != (expect.get("machine") or {}).get(key):
            why.append(f"下限是在別台電腦錄的（{key} 不同）")
    fe, ee = f.get("environment") or {}, expect.get("environment") or {}
    why += [f"套件 {k} 版本不同：{fe.get(k)} vs {ee.get(k)}" for k in FLOOR_PACKAGES if fe.get(k) != ee.get(k)]
    if f.get("models") != expect.get("models"):
        why.append("模型檔的 sha256 和這一次不同")
    if (f.get("inputs") or {}).get("sha256") != (expect.get("inputs") or {}).get("sha256"):
        why.append("素材的 sha256 和這一次不同")
    if (f.get("songs") or {}) != (expect.get("songs") or {}):  # 計畫裡的歌曲編號要對應到同一首歌
        why.append("真實歌曲和錄下限時不同：編號對應的 sha256 不一樣")
    return f, [f"相似度下限 {floor_path.name} 不能用：{w}" for w in why]


def floor_check(rows: list[dict], floor_path: Path, expect: dict, mode: str = "fixed",
                quality: str | None = "standard") -> list[str]:
    """顯示卡路徑的穩定性：每一項不能比同一台電腦、同樣條件錄的相似度下限（上游自己跑多次的最差值）還差。
    下限不存在、不能用、沒有這一項都算沒判定（列進異常），固定門檻不拿來判斷；沒有數字的項目（相同／不同）
    是硬性條件，一定要通過。"""
    f, why = floor_source(floor_path, expect, mode, quality)
    if f is None or why:
        return why
    floor = f.get("floor", {})
    out = []
    for r in rows:
        if r.get("num") is None:
            if not r["ok"]:
                out.append(f"{r['item']} {r['value']}")
            continue
        f = floor.get(r["item"])
        vals = [float(v) for v in (f or {}).get("values", []) if v is not None and str(v) != "nan"]
        if not vals:
            out.append(f"相似度下限沒有「{r['item']}」，沒有判定")
            continue
        v = float(r["num"])
        higher = any(k in r["item"] for k in ("SDR", "一致率"))
        worst = min(vals) if higher else max(vals)
        if v != v or (v < worst if higher else v > worst):
            out.append(f"{r['item']} {r['value']}，比上游自己跑多次的最差值 {worst:g} 還差")
    return out


def device_report(logs: dict[str, str]) -> list[str]:
    """每一次各自的實際裝置；每次都一樣就只列一次並註明。"""
    per = {k: device_lines(v) for k, v in logs.items()}
    uniq = {tuple(v) for v in per.values()}
    if len(uniq) == 1:
        return [*next(iter(uniq)), f"- （{'、'.join(per)}的實際裝置都一樣）"]
    return [x for k, v in per.items() for x in (f"- {k}：", *[f"  {y}" for y in v])]


def final_devices(log: str) -> dict[str, str]:
    """child_hook 每個項目最後回報的實際裝置（「[device] <項目>：… 實際=<裝置>」，同一個項目後面的蓋掉前面的）。"""
    out: dict[str, str] = {}
    for ln in unstamp(log).splitlines():
        m = re.match(r"\[device\] (.+?)：.* 實際=(\S+)\s*$", ln)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def gpu_used(logs: dict[str, str]) -> bool:
    """有沒有任何一步最後實際在顯示卡上跑（要求值、中途失敗後退回 CPU 的嘗試都不算）。"""
    return any(dev.startswith("cuda") for log in logs.values() for dev in final_devices(log).values())


assert final_devices("[device] whisper 模型：device=cuda（要求 auto） 實際=cuda\n"
                     "[device] whisper 模型：device=cpu（要求 cpu） 實際=cpu\n") == {"whisper 模型": "cpu"}
assert gpu_used({"1": "11-24 01:36:19 [device] separate onnx x：要求 cuda 實際=cuda\n"})   # 帶時間的 log 也讀得到


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


def against_check(ref: str, sub: str, mine: Path, rj: dict, have_run1: bool) -> dict:
    """這次的第 1 次和另一個版本（同一個標籤、同一個計畫）錄的第 1 次逐檔核對：重構前後是否等價。
    素材、模型、真實歌曲、套件要相同才比；不同或缺檔都列為異常（不能當作等價）。"""
    _, other_name = ref_name(ref)
    other = OUT / other_name / "numeric" / sub / "run1"
    out = {"version": other_name, "rows": [], "problems": []}
    oj = C._json(other / "run.json")
    if not have_run1:
        out["problems"].append(f"這次的第 1 次沒有成功，沒有和 {other_name} 比")
    elif oj is None or oj.get("incomplete"):
        out["problems"].append(f"{other} 沒有完整的輸出：先用同一個 --label、--plan 錄 {ref}")
    else:
        for key, what in (("inputs", "素材"), ("models", "模型檔"), ("songs", "真實歌曲"), ("environment", "套件")):
            a, b = oj.get(key), rj.get(key)
            if key == "environment":  # 內嵌 Python 的路徑不比
                a, b = ({k: v for k, v in (x or {}).items() if k != "executable"} for x in (a, b))
            if a != b:
                out["problems"].append(f"和 {other_name} 的{what}不同，逐檔核對不能代表等價")
        out["problems"] += [f"{other_name} 的輸出不完整：{m}" for m in C.required(other)]
        out["rows"] = C.exact(other, mine)
        out["problems"] += [f"和 {other_name} 不完全相同：{r['item']} {r['value']}" for r in out["rows"] if not r["ok"]]
    return out


def main() -> int:  # noqa: C901
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in sys.argv[1:] if a.startswith("--"))
    ref = args[0] if args else "v2.3.2"
    label = opt.get("label") or platform.node().lower()
    if "songs" in opt or "runs" in opt:
        print("--songs、--runs 已改成測試計畫：--plan=<JSON>（次數和歌曲都寫在計畫裡，見 tests/numeric/plan.py）")
        return 2
    plan = PLAN.load(opt["plan"], "numeric") if opt.get("plan") else None
    runs = plan["runs"] if plan else 2
    if not (INPUTS / "manifest.json").exists() or not MODELS.exists():
        print(f"找不到 {INPUTS / 'manifest.json'} 或 {MODELS}：先跑 tests/numeric/make_inputs.py、複製模型（見計畫 3.8）")
        return 2
    from sandbox import acquire_recording

    notes = acquire_recording("numeric/baseline.py " + " ".join(sys.argv[1:]))
    sub = f"{label}-{plan['name']}" if plan else label  # 不同計畫的輸出、摘要不互相覆蓋
    name, code = export(ref)
    notes += EXPORT_CLEANED
    if plan:
        notes.append(f"測試計畫 {plan['name']}（{plan['path'].name}）：{runs} 次；真實歌曲 "
                     + f"{len(plan['songs'])} 首")
    if runs < 2:
        notes.append(f"只跑 {runs} 次（測試計畫指定）：同一版的穩定性沒有判定")
    base = OUT / name / "numeric" / sub
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
        rc, late, stopped = run_logged(
            [sys.executable, "-I", str(HERE / "run.py"), str(code), str(INPUTS), str(MODELS), str(out)]
            + ([f"--plan={plan['path']}", "--kind=numeric"] if plan else []) + [f"--device={opt.get('device', 'auto')}"],
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
        if not stopped:  # 殘留的行程可能還在用沙盒、吃 CPU／顯示卡：後面的執行不能信
            problems.append(f"第 {i} 次逾時後沒確認行程都停了，後面的執行取消")
            break
    ok_runs = valid_runs(base, logs)
    # 穩定性：第 2 次起每一次都和第 1 次比。全部在 CPU 上要逐檔完全相同；有用到顯示卡改看差距表的門檻
    on_gpu = gpu_used({f"第 {i} 次": logs[i] for i in ok_runs})
    rj0 = json.loads((base / f"run{ok_runs[0]}" / "run.json").read_text("utf-8")) if ok_runs else {}
    exact: dict[int, list] = {}
    pairs: dict[int, list] = {}
    for i in (i for i in ok_runs if i != 1 and 1 in ok_runs):
        try:
            exact[i], pairs[i] = C.exact(base / "run1", base / f"run{i}"), C.pair(base / "run1", base / f"run{i}")
        except Exception as e:  # noqa: BLE001  比對本身出錯也要寫出報告
            problems.append(f"第 1 次和第 {i} 次比對時出錯：{type(e).__name__}: {e}"[:300])
    # 下限一律用上游（--floor-version，預設 v2.3.2）在這台電腦錄的，不用被測版本自己的
    floor_path = SUMMARY / f"{opt.get('floor-version', BASE)}-{label}-similarity-fixed.json"
    if private_copy(floor_path).exists():  # 有真實歌曲時，逐首的下限只在本機的完整版
        floor_path = private_copy(floor_path)
    expect = {"machine": machine(), "environment": rj0.get("environment"), "models": rj0.get("models"),
              "inputs": rj0.get("inputs"), "songs": rj0.get("songs")}
    for i in exact:
        if not on_gpu:
            problems += [f"第 1 次和第 {i} 次不完全相同：{r['item']} {r['value']}" for r in exact[i] if not r["ok"]]
        else:
            problems += [f"第 1 次和第 {i} 次：{m}" for m in floor_check(pairs[i], floor_path, expect)]
    pair = pairs.get(2, [])
    if not exact and runs >= 2:
        problems.append("少於兩次有效的執行，沒有做穩定性比對")
    against: dict = {}
    if opt.get("against"):
        against = against_check(opt["against"], sub, base / "run1", rj0, 1 in ok_runs)
        problems += against["problems"]
    quality = {}
    for i in ok_runs:
        try:
            quality[i] = C.quality(base / f"run{i}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"第 {i} 次和標準答案比時出錯：{type(e).__name__}: {e}"[:300])
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
        *[f"- {c}" for c in notes],
        *([f"- {c}" for c in rj.get("conditions", [])] or ["（沒有 run.json，讀不到）"]),
        "",
        "## 實際使用的裝置與模型",
        "",
        *(used(base / f"run{ok_runs[0]}", {f"第 {i} 次": logs[i] for i in ok_runs}) if ok_runs
          else ["（沒有成功的執行）"]),
        "",
        "穩定性判斷：" + ("有步驟實際在顯示卡上跑：和同一台電腦錄的相似度下限比（逐檔核對、固定門檻只供參考）"
                          if on_gpu else "全部在 CPU 上，各次輸出要逐檔完全相同"),
        "",
        "## 同一版各次輸出逐檔核對（和第 1 次比）",
        "",
        *[x for i, rows in exact.items()
          for x in (f"第 {i} 次：{sum(r['ok'] for r in rows)}／{len(rows)} 個檔案完全相同", "", *C.table(rows), "")],
        *([] if exact else ["（沒有做）", ""]),
        *([f"## 和 {against['version']} 逐檔核對（兩邊的第 1 次）", "",
           f"{sum(r['ok'] for r in against['rows'])}／{len(against['rows'])} 個檔案完全相同", "",
           *C.table(against["rows"]), ""] if against else []),
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
        "conditions": notes + (rj.get("conditions") or []),
        "plan": {"name": plan["name"], "runs": runs, "songs": [s["id"] for s in plan["songs"]]} if plan else None,
        "commit": commit_of(code),
        "inputs": rj.get("inputs"),
        "songs": rj.get("songs"),  # 測試計畫的真實歌曲：編號 → sha256（不記檔名）
        "models": rj.get("models"),  # 模型檔 → sha256
        "problems": problems,
        "on_gpu": on_gpu,
        "exact": {str(i): rows for i, rows in exact.items()},
        "stability": pair,
        "against": {"version": against["version"], "rows": against["rows"]} if against else None,
        "quality": {str(i): q for i, q in quality.items()},
        "wall_seconds": wall,
    }
    out_json = SUMMARY / f"{name}-{sub}.json"
    why = write_summary(out_json, summary, tuple(s["id"] for s in plan["songs"]) if plan else ())
    if why:
        problems.append(why)
        (base / "numeric-report.md").write_text("\n".join(rep) + f"\n\n## 摘要沒有寫入\n\n- {why}\n", "utf-8")
        print(f"摘要沒有寫入（{out_json} 不是這一次的結果）：{why}")
    print("\n".join(rep))
    print(f"報告：{base / 'numeric-report.md'}；摘要：{out_json}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
