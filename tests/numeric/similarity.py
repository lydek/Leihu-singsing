r"""錄一個版本在這台電腦上的相似度基準（docs/refactor-port-plan.md 3.8）：上游自己在平常條件下跑多次，輸出差多少。

數值基準（baseline.py）固定單執行緒、關掉隨機抖動，讓同一版逐位元組相同，用來確認「行為沒變」。
第二階段的效能調整一定會讓輸出不同，只能要求「相似」；可以接受的差距不能比上游自己跑多次的差距還嚴，
所以這裡用平常的條件（run.py --normal：執行緒數照平常、torchcrepe 隨機抖動開著）跑 N 次（預設 3），
每兩次比一次（compare.pair），記下每個項目的中位數和最差值，當作第二階段門檻的下限。
也記每次和合成素材標準答案的差距（compare.quality）的範圍。

歌曲：合成素材（tests/private/numeric/inputs）一定會跑；tests/private/songs/ 有音檔時一起跑（run.py --songs），
摘要只記 sha256，不記檔名。報告和輸出在 tests/private/upstream/<版本>/similarity/<標籤>/（不進 git），
摘要寫到 tests/numeric/baselines/<版本>-<標籤>-similarity.json（進 git）。
報告一定有「異常」一節（結束代碼、run.py 回報的問題、log 的 Traceback／改用 CPU／改在主程式裡執行／退回／卡住／逾時／
記憶體不足、子程式載入了別份程式），沒有就寫「無」。兩次之間超過數值基準門檻不算異常（這裡就是要量它），會列在表裡。

用法（repo 根目錄，用已安裝版本的內嵌 Python）：
  <python> tests/numeric/similarity.py [git 參照，預設 v2.3.2] [--label=<這台電腦的名稱>] [--runs=3] [--quality=standard|auto]
  [--timeout=<每次執行的秒數上限，預設 7200>] [--device=auto|cpu] [--fixed]
--fixed：用和數值基準一樣的固定條件（單執行緒、關抖動、固定亂數種子）跑，記下的差距給顯示卡的數值基準當下限；
  摘要是 <版本>-<標籤>-similarity-fixed.json。不加是平常條件（第二階段效能調整的門檻下限）。
有異常時結束代碼是 1。
"""

from __future__ import annotations

import itertools
import json
import platform
import re
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / "tests" / "contract")]

import compare as C  # noqa: E402
import numpy as np  # noqa: E402
from baseline import INPUTS, MODELS, SUMMARY, TIMEOUT, log_alerts, machine, run_logged, used, valid_runs  # noqa: E402
from compare_upstream import EXPORT_NOTES, write_summary, tools_dirty, OUT, _git, commit_of, export  # noqa: E402

SONGS = ROOT / "tests" / "private" / "songs"


def number(r: dict) -> float | None:
    """row 的 num（沒有四捨五入）；沒有 num 的項目（例如「相同／不同」）：相同是 0、不同是 1。"""
    if r.get("num") is not None:
        return float(r["num"])  # "inf"、"-inf"、"nan" 也能轉
    if r.get("ok") is not None and r["limit"] in ("相同", "都是 done"):
        return 0.0 if r["ok"] else 1.0
    v = r["value"]
    if isinstance(v, dict):
        return 1.0
    if v == "相同":
        return 0.0
    if m := re.fullmatch(r"(-?[\d.]+) vs (-?[\d.]+)", str(v)):
        return abs(float(m[1]) - float(m[2]))
    m = re.match(r"(-?inf|-?[\d.]+)", str(v))
    return float(m[1]) if m else None


# 數值越大越好的項目（SDR、一致率）：最差值取最小；其他取最大
HIGHER_BETTER = ("SDR", "一致率")


def main() -> int:  # noqa: C901
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in sys.argv[1:] if a.startswith("--"))
    ref = args[0] if args else "v2.3.2"
    label = opt.get("label") or platform.node().lower()
    runs = int(opt.get("runs", 3))
    if runs < 2:
        print("--runs 至少要 2：相似度要兩兩比較")
        return 2
    quality = opt.get("quality", "standard")
    fixed = "fixed" in opt  # 固定條件（和數值基準一樣）：顯示卡數值基準的下限要用這種錄的
    if not (INPUTS / "manifest.json").exists() or not MODELS.exists():
        print(f"找不到 {INPUTS / 'manifest.json'} 或 {MODELS}（見計畫 3.8）")
        return 2
    has_songs = SONGS.exists() and any(x.suffix.lower() in C.AUDIO for x in SONGS.iterdir())  # fetch.py 等不算
    name, code = export(ref)
    base = OUT / name / ("similarity-fixed" if fixed else "similarity") / label  # 兩種模式不互相覆蓋
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
        cmd = [sys.executable, "-I", str(HERE / "run.py"), str(code), str(INPUTS), str(MODELS), str(out),
               *([] if fixed else ["--normal"]),
               f"--quality={quality}", f"--device={opt.get('device', 'auto')}"] + ([f"--songs={SONGS}"] if has_songs else [])
        t0 = time.time()
        rc, late, stopped = run_logged(cmd, base / f"run{i}.log", float(opt.get("timeout", TIMEOUT)))
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
        if (out / "run.json").exists():  # 缺檔是執行失敗，不是自然差異
            problems += [f"第 {i} 次輸出不完整：{p}" for p in C.required(out)]
        if not stopped:  # 殘留的行程可能還在用沙盒、吃 CPU：後面的執行不能信
            problems.append(f"第 {i} 次逾時後沒確認行程都停了，後面的執行取消")
            break
    ok_runs = valid_runs(base, logs)
    if len(ok_runs) < 2:
        problems.append("少於兩次成功的執行，沒有相似度可以算")
    pairs = {}
    for i, j in itertools.combinations(ok_runs, 2):
        try:
            pairs[f"{i}-{j}"] = C.pair(base / f"run{i}", base / f"run{j}")
        except Exception as e:  # noqa: BLE001  比對本身出錯也要寫出報告
            problems.append(f"第 {i} 次和第 {j} 次比對時出錯：{type(e).__name__}: {e}"[:300])
    items: dict[str, dict] = {}
    for rows in pairs.values():
        for r in rows:
            it = items.setdefault(r["item"], {"values": [], "raw": [], "limit": r["limit"], "over": 0})
            it["values"].append(number(r))
            it["raw"].append(r["value"])
            it["over"] += 0 if r["ok"] else 1
    for rows in pairs.values():  # 結構壞掉、缺檔、算不出來不是自然差異
        problems += [f"{r['item']}：{r['value']}" for r in rows if not r["ok"] and r.get("num") is None
                     and r["limit"] in ("必要欄位都在、型別正確", "有音高", "取樣率、聲道、長度相同", "兩邊都有", "")]
    for item, it in items.items():  # 算不出來的值（NaN）不是自然差異
        if any(v is not None and v != v for v in it["values"]):
            problems.append(f"{item} 有算不出來的值（NaN），這一項的下限無效")
    quality_runs = {}
    for i in ok_runs:
        try:
            quality_runs[i] = C.quality(base / f"run{i}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"第 {i} 次和標準答案比時出錯：{type(e).__name__}: {e}"[:300])
    rj = json.loads((base / f"run{ok_runs[0]}" / "run.json").read_text("utf-8")) if ok_runs else {}
    mach = machine()
    tool = _git("rev-parse", "--short", "HEAD").strip()
    dirty = tools_dirty()

    def fmt(x: float | None) -> str:
        return "—" if x is None else ("inf" if x == float("inf") else f"{x:g}")

    table = ["| 項目 | 中位數 | 最差 | 數值基準的門檻 | 超過門檻的組數 |", "|---|---|---|---|---|"]
    for item, it in items.items():
        vals = [v for v in it["values"] if v is not None]
        worst = (min(vals) if any(k in item for k in HIGHER_BETTER) else max(vals)) if vals else None
        med = statistics.median(vals) if vals else None
        table.append(f"| {item} | {fmt(med)} | {fmt(worst)} | {it['limit']} | {it['over']}／{len(it['values'])} |")
    qitems: dict[str, list] = {}
    for q in quality_runs.values():
        for r in q:
            qitems.setdefault(r["item"], []).append(str(r["value"]))
    qtable = ["| 項目 | " + " | ".join(f"第 {i} 次" for i in quality_runs) + " |",
              "|---|" + "---|" * len(quality_runs)]
    qtable += [f"| {k} | " + " | ".join(v) + " |" for k, v in qitems.items()]
    rep = [
        f"# 相似度基準：{name}（{label}，{'固定條件' if fixed else '平常條件'}）",
        "",
        f"- 被測程式：`{name}`（commit {commit_of(code)}；{code}）",
        f"- 測試工具：commit `{tool}`" + ("，`tests/` 有未 commit 的修改" if dirty else ""),
        f"- Python：{sys.version.split()[0]}（{sys.executable}）",
        f"- 電腦：{mach['cpu']}（{mach['logical_cpus']} 個邏輯處理器）；{mach['ram_gb']} GB RAM；{mach['os']}",
        f"- 顯示卡：{mach['gpus']}；nvidia-smi：{mach['nvidia_smi']}",
        f"- 素材：{INPUTS}（語音：{(rj.get('inputs') or {}).get('tts_voice')}）；真實歌曲："
        + (f"{len(rj.get('songs') or {})} 首（{SONGS}）" if has_songs else "沒有（tests/private/songs/ 是空的）"),
        f"- 執行次數：{runs}（兩兩比較 {len(pairs)} 組）；每次總時間：" + "、".join(f"{v} 秒" for v in wall.values())
        + f"；時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 異常",
        "",
        *([f"- {p}" for p in problems] or ["無"]),
        "",
        "## 執行條件（和平常使用不同的地方）",
        "",
        *([f"- {c}" for c in rj.get("conditions", [])] or ["（沒有 run.json，讀不到）"]),
        *([] if fixed else ["- 執行緒數、torchcrepe 隨機抖動、亂數種子照平常（run.py --normal）"]),
        "",
        "## 實際使用的裝置與模型",
        "",
        *(used(base / f"run{ok_runs[0]}", {f"第 {i} 次": logs[i] for i in ok_runs}) if ok_runs
          else ["（沒有成功的執行）"]),
        "",
        "## 上游自己跑多次的差距（第二階段門檻的下限）",
        "",
        "「最差」：SDR、一致率取最小，其他取最大。音符數、歌詞行數是兩次的差；meta 不同記 1。"
        "超過數值基準門檻不算異常：門檻是給固定條件用的，這裡量的是平常條件下的自然差異。",
        "",
        *table,
        "",
        "## 每次和標準答案的差距",
        "",
        *qtable,
        "",
    ]
    (base / "similarity-report.md").write_text("\n".join(rep), "utf-8")
    SUMMARY.mkdir(exist_ok=True)
    summary = {
        "version": name,
        "label": label,
        "tool_commit": tool + ("+dirty" if dirty else ""),
        "machine": mach,
        "environment": {k: v for k, v in (rj.get("environment") or {}).items() if k != "executable"},
        "conditions": rj.get("conditions"),
        "quality": quality,
        "mode": "fixed" if fixed else "normal",
        "commit": commit_of(code),
        "inputs": rj.get("inputs"),
        "models": rj.get("models"),
        "songs": rj.get("songs"),
        "problems": problems,
        "pairs": pairs,
        "floor": {k: {"values": [x if x is None or np.isfinite(x) else str(x) for x in v["values"]],
                      "limit": v["limit"], "over": v["over"]} for k, v in items.items()},  # inf、nan 不是標準 JSON
        "truth": {str(i): q for i, q in quality_runs.items()},
        "wall_seconds": wall,
    }
    out_json = SUMMARY / f"{name}-{label}-similarity{'-fixed' if fixed else ''}.json"
    why = write_summary(out_json, summary)
    if why:
        problems.append(why)
        (base / "similarity-report.md").write_text("\n".join(rep) + f"\n\n## 摘要沒有寫入\n\n- {why}\n", "utf-8")
        print(f"摘要沒有寫入（{out_json} 不是這一次的結果）：{why}")
    print("\n".join(rep))
    print(f"報告：{base / 'similarity-report.md'}；摘要：{out_json}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
