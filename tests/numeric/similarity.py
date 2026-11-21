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
有異常時結束代碼是 1。
"""

from __future__ import annotations

import itertools
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / "tests" / "contract")]

import compare as C  # noqa: E402
from baseline import INPUTS, MODELS, SUMMARY, log_alerts, machine, used  # noqa: E402
from compare_upstream import tools_dirty, OUT, _git, commit_of, export  # noqa: E402

SONGS = ROOT / "tests" / "private" / "songs"


def number(r: dict) -> float | None:
    """row 的 num（沒有四捨五入）；沒有 num 的項目（例如「相同／不同」）：相同是 0、不同是 1。"""
    if r.get("num") is not None:
        return float("inf") if r["num"] == "inf" else float(r["num"])
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
    if not (INPUTS / "manifest.json").exists() or not MODELS.exists():
        print(f"找不到 {INPUTS / 'manifest.json'} 或 {MODELS}（見計畫 3.8）")
        return 2
    has_songs = SONGS.exists() and any(x.suffix.lower() in C.AUDIO for x in SONGS.iterdir())  # fetch.py 等不算
    name, code = export(ref)
    base = OUT / name / "similarity" / label
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
        cmd = [sys.executable, "-I", str(HERE / "run.py"), str(code), str(INPUTS), str(MODELS), str(out), "--normal",
               f"--quality={quality}"] + ([f"--songs={SONGS}"] if has_songs else [])
        t0 = time.time()
        with open(base / f"run{i}.log", "w", encoding="utf-8") as f:
            r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                               env=dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1"))
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
    if len(ok_runs) < 2:
        problems.append("少於兩次成功的執行，沒有相似度可以算")
    pairs = {f"{i}-{j}": C.pair(base / f"run{i}", base / f"run{j}") for i, j in itertools.combinations(ok_runs, 2)}
    items: dict[str, dict] = {}
    for rows in pairs.values():
        for r in rows:
            it = items.setdefault(r["item"], {"values": [], "raw": [], "limit": r["limit"], "over": 0})
            it["values"].append(number(r))
            it["raw"].append(r["value"])
            it["over"] += 0 if r["ok"] else 1
    quality_runs = {i: C.quality(base / f"run{i}") for i in ok_runs}
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
        f"# 相似度基準：{name}（{label}）",
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
        "- 執行緒數、torchcrepe 隨機抖動照平常（run.py --normal）",
        "",
        "## 實際使用的裝置與模型",
        "",
        *(used(base / f"run{ok_runs[0]}", logs[ok_runs[0]]) if ok_runs else ["（沒有成功的執行）"]),
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
        "commit": commit_of(code),
        "inputs": rj.get("inputs"),
        "models": rj.get("models"),
        "songs": rj.get("songs"),
        "problems": problems,
        "pairs": pairs,
        "floor": {k: {"values": ["inf" if x == float("inf") else x for x in v["values"]], "limit": v["limit"],
                      "over": v["over"]} for k, v in items.items()},  # inf 不是標準 JSON
        "truth": {str(i): q for i, q in quality_runs.items()},
        "wall_seconds": wall,
    }
    out_json = SUMMARY / f"{name}-{label}-similarity.json"
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), "utf-8")
    print("\n".join(rep))
    print(f"報告：{base / 'similarity-report.md'}；摘要：{out_json}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
