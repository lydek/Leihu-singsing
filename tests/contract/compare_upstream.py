r"""上游（基準）與目前這一邊的契約比對（docs/refactor-port-plan.md 3.7）。

用同一組契約案例（test_contract.py 的 CASES）在沙盒裡跑基準版本（serve_upstream.py），
逐案例和目前這一邊的快照（snapshots/contract.json）比 JSON 結構，依 RULES 把差異分成「預期」和「未預期」，
報告寫到 tests/private/upstream/<名稱>/contract-report.md。有未預期差異或異常時結束代碼是 1。

用法（repo 根目錄）：python tests/contract/compare_upstream.py [git 參照，預設 v2.3.2] [--twice]
--record：錄 contract.json。用 serve_upstream.py 跑指定的已 commit 參照，得到候選結果；
  快照已經存在時，再現場跑 --base=<參照>（預設 v2.3.2）比對，所有差異都符合 RULES 才覆寫，否則 exit 1、保留原快照。
  快照還不存在時（第一次建立基準）直接寫入。只能用在步驟 1 之前（serve_upstream.py 要改得到 app 的背景函式）。
--rebase：和 --record 一起用，重建基準本身（參照就是 --base，例如測試工具改了快照格式）：和舊快照的差異列在報告裡，
  不擋寫入。沒有 --rebase 時，重錄基準本身遇到未登記的差異一樣不寫入。
--twice：同一個版本連跑兩次，結果要完全相同（穩定性檢查）；不同就列為異常，--record 也不寫入。
報告一定有「異常與沙盒條件」一節：伺服器 log 的 Traceback、顯示卡退回／改用 CPU 的訊息、沙盒替換了什麼、
哪些替換目標在這個版本找不到、對外連線、沒有案例的路由、兩次結果不同的案例。沒有就寫「無」。
--leak：漏移檢查（upstream-sync-plan.md 第 7 節步驟 5）。前端與安裝檔和上游比只能剩 OWN_CHANGES；
  上游在 --since..<參照> 之間新增到 app.py、pipeline.py 的名稱，重構版都要找得到（刻意不移植的列在 NOT_PORTED）。
  --since=<參照> 預設是上次的同步點（git merge-base HEAD <參照>）；已經 merge -s ours 過的版本要自己給（例如 v2.1.0 是 v2.0.0）。
--code=<資料夾>：不從 git 匯出，直接比對這份程式碼（名稱用資料夾名稱；例如檢查比對工具抓不抓得到故意改過的地方）。
數值比對是另一支工具：tests/numeric/baseline.py（舊的 --numeric 依賴 2.0.0 重構版的 ml-worker，已拿掉）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT), str(ROOT / "tests")]

from test_contract import CASES, SNAPSHOT, run_contract  # noqa: E402

OUT = ROOT / "tests" / "private" / "upstream"

BASE = "v2.3.2"  # 第一階段的基準（refactor-port-plan.md 3.8）

# 預期差異：(名稱, 說明, 判斷函式(案例, 差異字串) -> bool)。差異字串的格式見 diff()：
# "+ /路徑=值"（基準沒有、目前有）、"- /路徑=值"（基準有、目前沒有）、"~ /路徑: 基準 -> 目前"
# 從空的開始：舊分支的 S15、D1、R2、D2/D4 是 2.0.0 重構版的差異，D1 還用案例名稱字首整個放行，留著會蓋掉真的差異。
# 每加一條都要寫明問題編號和原因；判斷式只放行那個差異本身，不要用案例名稱整個放行。
RULES: list[tuple[str, str, object]] = []

# 刻意不移植的上游名稱 → 原因（refactor 從 v2.3.2 開始，目前沒有）
NOT_PORTED: dict[str, str] = {}
# 目前這一邊對前端、安裝檔的改動：(檔案, 改動行要符合的 regex)；和上游比，其他改動行都算漏移
FRONT = ["static", "hotkeys.py", "VERSION", "installer/leihu.nsi"]
OWN_CHANGES: list[tuple[str, str]] = []

# 伺服器 log 裡代表「這次執行條件和平常不同」的訊息：一律列進報告，不然結果會被誤解
LOG_ALERTS = [
    ("Traceback", "Python 例外"),
    ("改在主程式裡執行", "顯示卡子程式開不起來，改在主程式裡做"),
    ("改用 CPU", "改用 CPU"),
    ("退回", "退回較低品質的模型或做法"),
    ("改用標準模型", "高品質模型失敗，改用標準模型"),
    ("卡住", "偵測到卡住"),
    ("逾時", "逾時"),
    ("out of memory", "記憶體不足"),
    ("暫存資料夾刪不掉", "測試工具的暫存資料夾沒有刪乾淨"),
]
# 案例本身故意造成的 log 訊息：(訊息片段, 原因)。報告照樣列出，但不算異常
LOG_EXPECTED = [
    ("[live] 直播模式開著", "案例 status_live（GET /api/status?live=1）故意開直播模式，之後新的分離、音高分析改用 CPU"),
]
_NAME_PATTERNS = (
    r"^def (\w+)",
    r"^class (\w+)",
    r"^([A-Z_][A-Z0-9_]+) =",
    r'^\s+"(\w+)": ',
    r'@app\.(?:get|post|patch|delete)\("([^"]+)"',
    r"^    def (\w+)",
)


# 測試工具有沒有未 commit 的修改：不算錄出來的結果（快照、摘要），不然先錄的那份會讓後面每份都標成有修改
TOOL_PATHS = ("--", "tests", ":!tests/contract/snapshots", ":!tests/numeric/baselines", ":!tests/perf/baselines",
              ":!tests/private")


def scrub(x):
    """要進 git 的摘要：把 repo、暫存資料夾、使用者資料夾的絕對路徑換成 <repo>、<tmp>、<home>（含使用者名稱）。
    報告（tests/private/）保留完整路徑。"""
    if isinstance(x, dict):
        return {scrub(k): scrub(v) for k, v in x.items()}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    if not isinstance(x, str):
        return x
    places = [(str(ROOT), "<repo>"), (tempfile.gettempdir(), "<tmp>"), (os.path.expanduser("~"), "<home>")]
    for p, tag in sorted(places, key=lambda t: -len(t[0])):  # 長的先換（repo 可能在 home 底下）
        for form in {p, p.replace("\\", "/"), p.replace("\\", "\\\\")}:
            x = re.sub(re.escape(form), tag, x, flags=re.IGNORECASE)
    return x


def tools_dirty() -> bool:
    return bool(_git("status", "--porcelain", *TOOL_PATHS).strip())


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    ).stdout


def leak_check(ref: str, since: str | None) -> list[str]:
    """回傳漏移的項目（空的表示沒有）。"""
    problems = []
    cur = None
    for line in _git("diff", "-U0", ref, "--", *FRONT).splitlines():  # 和工作目錄比：移植完、提交前就要能檢查
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            cur = line[6:] if line.startswith("+++ b/") else cur or line[6:]
            continue
        if line.startswith(("+++", "---", "@@", "diff ", "index ", "new file", "deleted file")) or line[:1] not in (
            "+",
            "-",
        ):
            continue
        if not any(f == cur and re.fullmatch(rx, line) for f, rx in OWN_CHANGES):
            problems.append(f"前端／安裝檔 {cur}：{line[:150]}")
    base = since or _git("merge-base", "HEAD", ref).strip()
    if _git("rev-parse", base).strip() == _git("rev-parse", ref).strip():
        return problems  # 上次同步點就是這個版本：後端沒有新的上游改動要檢查
    added = [
        ln[1:]
        for ln in _git("diff", base, ref, "--", "app.py", "pipeline.py").splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    ]
    names = {m.group(1) for ln in added for rx in _NAME_PATTERNS if (m := re.search(rx, ln))}
    files = [f for f in _git("ls-files").split() if f.endswith(".py") and not f.startswith("tests/")]
    code = "".join((ROOT / f).read_text("utf-8") for f in files if (ROOT / f).exists())
    for n in sorted(names):
        if n not in code and n not in NOT_PORTED:
            problems.append(f"後端：上游新增的 {n} 在重構版找不到")
    return problems


def export(ref: str) -> tuple[str, Path]:
    """每次都從 git 重新匯出到這個行程自己的資料夾 OUT/<名稱>/exports/<pid>-<時間>（不沿用、不共用：
    解壓中斷、被改過、tag 改指別的 commit、同時跑的另一個工具刪掉，都不會測到錯的程式），行程結束時刪掉。
    完整 commit 寫在 <code>/.leihu-commit，報告用 commit_of() 讀。"""
    full = subprocess.run(["git", "rev-parse", f"{ref}^{{commit}}"], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()
    tag = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", full], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    name = tag or full[:7]
    import atexit
    import time

    fresh = OUT / name / "exports" / f"{os.getpid()}-{time.time_ns()}"
    fresh.mkdir(parents=True)
    atexit.register(shutil.rmtree, fresh, True)  # 匯出的程式碼裡沒有 junction，可以直接刪
    blob = subprocess.run(["git", "archive", "--format=tar", full], cwd=ROOT, capture_output=True, check=True).stdout
    with tempfile.TemporaryFile() as f:
        f.write(blob)
        f.seek(0)
        with tarfile.open(fileobj=f) as t:
            t.extractall(fresh, filter="data")
    (fresh / ".leihu-commit").write_text(full, "utf-8")
    return name, fresh


def commit_of(code: Path) -> str:
    """export() 匯出的完整 commit；--code 指定的資料夾沒有這個檔，回傳說明。"""
    f = code / ".leihu-commit"
    return f.read_text("utf-8").strip() if f.exists() else "（不是 export() 匯出的，commit 不明）"


def diff(old, new, path: str, out: list[str]) -> None:
    """old＝重構版快照、new＝上游。"""
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(old.keys() | new.keys()):
            p = f"{path}/{k}"
            if k not in new:
                out.append(f"+ {p}={json.dumps(old[k], ensure_ascii=False)[:120]}")
            elif k not in old:
                out.append(f"- {p}={json.dumps(new[k], ensure_ascii=False)[:120]}")
            else:
                diff(old[k], new[k], p, out)
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (a, b) in enumerate(zip(old, new, strict=False)):
            diff(a, b, f"{path}[{i}]", out)
    elif type(old) is not type(new) or old != new:  # True == 1、1 == 1.0 在 Python 成立，JSON 契約卻不同
        out.append(
            f"~ {path}: {json.dumps(new, ensure_ascii=False)[:120]} -> {json.dumps(old, ensure_ascii=False)[:120]}"
        )


def classify(case: str, d: str) -> str | None:
    for name, _, pred in RULES:
        if pred(case, d):
            return name
    return None


_IDS = {
    "new": "x",
    "old": "x",
    "done": "x",
    "pending": "x",
    "job": "0123456789",
    "take_new": "x",
    "template": "x",
    "uploaded": "x",
    "yt_song": "x",
}


def run_once(name: str, code: Path, tag: str) -> dict:
    """用 serve_upstream.py 跑一次 CASES。回傳結果、伺服器 log、沙盒紀錄、路由清單；log 另存在 OUT/name。"""
    tmp = Path(tempfile.mkdtemp(prefix="leihu-upstream-"))
    shim, routes = tmp / "shim.log", tmp / "routes.json"
    try:
        result = run_contract(
            tmp,
            CASES,
            env_extra={
                "PYTHONDONTWRITEBYTECODE": "1",
                "LEIHU_SHIM_LOG": str(shim),
                "LEIHU_ROUTES_OUT": str(routes),
            },
            server=[sys.executable, str(HERE / "serve_upstream.py")],
            # 不複製使用者設定：config.json 的 library_dir、models_dir 會把資料導到沙盒外（fixture 已經寫好測試設定）
            setup=lambda data: shutil.copytree(code, data, dirs_exist_ok=True, ignore=shutil.ignore_patterns("config.json")),
        )
        log = (tmp / "server.log").read_text("utf-8", errors="replace")
        notes = [json.loads(x) for x in shim.read_text("utf-8").splitlines() if x] if shim.exists() else []
        rules = json.loads(routes.read_text("utf-8")) if routes.exists() else None
    finally:
        if (tmp / "server.log").exists():
            shutil.copy2(tmp / "server.log", OUT / name / f"server-{tag}.log")
        shutil.rmtree(tmp, ignore_errors=True)
    (OUT / name / f"contract-{tag}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    return {"result": result, "log": log, "notes": notes, "routes": rules}


def uncovered(routes: list[str] | None) -> list[str]:
    """CASES 沒有打到的路由（規則字串＋方法），排除 /static 和 /api/debug/stacks。拿不到路由清單時回傳說明。"""
    from test_contract import _fill
    from werkzeug.routing import Map, Rule

    if routes is None:
        return ["沙盒沒有寫出路由清單（routes.json），涵蓋率沒有檢查"]
    want = {r for r in routes if not r.startswith(("/static/", "/api/debug/stacks "))}
    by_rule: dict[str, set[str]] = {}
    for r in want:
        rule, method = r.rsplit(" ", 1)
        by_rule.setdefault(rule, set()).add(method)
    adapter = Map([Rule(rule, methods=ms, endpoint=rule) for rule, ms in by_rule.items()]).bind("127.0.0.1")
    hit = set()
    for _, method, path, _ in CASES:
        try:
            hit.add(f"{adapter.match(_fill(path, _IDS).split('?')[0], method=method)[0]} {method}")
        except Exception:  # noqa: S112  打到不存在的路由（404 案例）不算涵蓋
            continue
    return sorted(want - hit)


def alerts(run: dict, label: str, known: list[str]) -> list[str]:
    """一次執行裡所有「條件和平常不同」的地方。LOG_EXPECTED 符合的訊息放進 known（照樣列在報告）。"""
    out = []
    for line in run["log"].splitlines():
        why = next((w for n, w in LOG_EXPECTED if n in line), None)
        if why:
            known.append(f"{label} 伺服器 log：{line.strip()[:200]}（{why}）")
            continue
        for needle, what in LOG_ALERTS:
            if needle in line:
                out.append(f"{label} 伺服器 log（{what}）：{line.strip()[:200]}")
                break
    out += [f"{label} 沙盒：{n['msg']}" for n in run["notes"] if n["kind"] == "missing"]
    for case, rec in run["result"].items():
        for k, v in rec["files"].items():
            if "net_blocked" in k:
                out.append(f"{label} 案例 {case} 想對外連線（已擋下）：{json.dumps(v, ensure_ascii=False)[:200]}")
            if k.endswith("spawn.log"):
                out += [f"{label} 案例 {case} 開了沒有分類的外部程式：{e}" for e in v if e.get("kind") == "spawn"]
    out += [f"{label} 沒有案例的路由：{r}" for r in uncovered(run["routes"])]
    return out


def raw_diff(a: dict, b: dict) -> dict[str, list[str]]:
    """兩份結果逐案例的差異，不套用 RULES（同一版跑兩次的穩定性檢查用）。"""
    a, b = (json.loads(json.dumps(x, ensure_ascii=False)) for x in (a, b))
    out: dict[str, list[str]] = {}
    for case in sorted(a.keys() | b.keys()):
        if case not in a or case not in b:
            out[case] = ["只有一次有這個案例"]
            continue
        d: list[str] = []
        diff(a[case], b[case], "", d)
        if d:
            out[case] = d
    return out


def compare(snap: dict, result: dict) -> tuple[list[str], dict, dict]:
    """snap＝目前這一邊、result＝基準。回傳（完全相同的案例、預期差異、未預期差異）。案例集合不同也算未預期。"""
    same, expected, unexpected = [], {}, {}
    snap, result = (json.loads(json.dumps(x, ensure_ascii=False)) for x in (snap, result))  # 兩邊都用 JSON 的型別（tuple→list）
    for case in sorted(snap.keys() | result.keys()):
        if case not in snap or case not in result:
            unexpected.setdefault(case, []).append("只有" + ("基準" if case in result else "目前這一邊") + "有這個案例")
            continue
        out: list[str] = []
        diff(snap[case], result[case], "", out)
        if not out:
            same.append(case)
        for d in out:
            rule = classify(case, d)
            (expected.setdefault(rule, []) if rule else unexpected.setdefault(case, [])).append(
                f"{case}: {d}" if rule else d
            )
    return same, expected, unexpected


def meta(name: str, code: Path, runs: int) -> list[str]:
    """報告開頭：被測程式、測試工具、執行環境。"""
    import platform

    tool = _git("rev-parse", "--short", "HEAD").strip()
    dirty = tools_dirty()
    return [
        f"- 被測程式：`{name}`（commit {commit_of(code)}；{code}）",
        f"- 測試工具：commit `{tool}`" + ("，`tests/` 有未 commit 的修改" if dirty else ""),
        f"- Python：{sys.version.split()[0]}（{sys.executable}）",
        f"- 電腦：{platform.platform()}；{platform.processor() or '（CPU 型號讀不到）'}",
        f"- 執行次數：{runs}；時間：{__import__('time').strftime('%Y-%m-%d %H:%M:%S')}",
        "- 沙盒條件：背景工作不執行、不連網、不開外部程式、固定為沒有可用的顯示卡；完整清單在下方「沙盒替換」",
    ]


def main() -> int:  # noqa: C901
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = {a.split("=", 1)[0]: (a.split("=", 1)[1] if "=" in a else "") for a in sys.argv[1:] if a.startswith("--")}
    if "--code" in opt:
        code = Path(opt["--code"]).resolve()
        name = code.name
        (OUT / name).mkdir(parents=True, exist_ok=True)
    else:
        name, code = export(args[0] if args else BASE)
    runs = [run_once(name, code, "run1")]
    if "--twice" in opt:
        runs.append(run_once(name, code, "run2"))
    result = runs[0]["result"]

    known: list[str] = []
    problems = alerts(runs[0], "第 1 次", known)
    if len(runs) > 1:
        problems += alerts(runs[1], "第 2 次", known)
        for c, ds in raw_diff(runs[1]["result"], result).items():  # 同一版：不套用 RULES，要完全相同
            problems.append(f"兩次結果不同的案例 {c}：{ds[0][:200]}")
    stubs = [n["msg"] for n in runs[0]["notes"] if n["kind"] in ("stub", "forced")]

    recording = "--record" in opt
    if recording and SNAPSHOT.exists():
        base_name, base_code = export(opt.get("--base") or BASE)
        if base_name == name:
            snap, base_result = json.loads(SNAPSHOT.read_text("utf-8")), result  # 重錄基準本身：和舊快照比
        else:
            base_run = run_once(base_name, base_code, "base")
            problems += alerts(base_run, f"基準 {base_name}", known)
            snap, base_result = result, base_run["result"]
    elif recording:
        snap, base_result = result, result  # 第一次建立基準
    else:
        if not SNAPSHOT.exists():
            print(f"還沒有快照 {SNAPSHOT}：先用 --record 錄一次")
            return 1
        snap, base_result = json.loads(SNAPSHOT.read_text("utf-8")), result
    same, expected, unexpected = compare(snap, base_result)

    title = f"錄製 {name} 的契約快照" if recording else f"基準 {name} 與目前快照的契約比對"
    lines = [
        f"# {title}",
        "",
        *meta(name, code, len(runs)),
        "",
        f"案例 {len(snap)} 個：完全相同 {len(same)}、有差異 {len(snap) - len(same)}；"
        f"未預期差異的案例 {len(unexpected)} 個。",
        "",
        "## 異常",
        "",
        *([f"- {p}" for p in problems] or ["無"]),
        "",
        "## 案例本身造成的訊息（不算異常）",
        "",
        *([f"- {k}" for k in known] or ["無"]),
        "",
    ]
    if unexpected:
        lines += ["## 未預期的差異", ""]
        for case, ds in unexpected.items():
            lines += [f"### {case}", "", "```", *ds, "```", ""]
    if expected:
        lines += ["## 預期的差異", ""]
        for rule, ds in expected.items():
            desc = next(r[1] for r in RULES if r[0] == rule)
            lines += [
                f"### {rule}：{desc}（{len(ds)} 項）",
                "",
                "```",
                *ds[:8],
                *(["……"] if len(ds) > 8 else []),
                "```",
                "",
            ]
    lines += ["## 沙盒替換", "", *[f"- {s}" for s in stubs], ""]

    # 重錄基準本身（--record 的參照就是 --base）而且明確加了 --rebase：和舊快照的差異只供參考（工具改了快照格式）
    rebase = recording and "--rebase" in opt and SNAPSHOT.exists() and (opt.get("--base") or BASE) == name
    if recording and "--rebase" in opt and not rebase:
        problems.append("--rebase 只能用在重錄基準本身（參照要等於 --base），這次沒有效果")
    blocking = bool(problems) or (bool(unexpected) and not rebase)
    if recording:
        if blocking:
            lines += ["**沒有寫入快照**：有異常或未登記的差異，先處理再錄。", ""]
        else:
            SNAPSHOT.parent.mkdir(exist_ok=True)
            with open(SNAPSHOT, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
            lines += [f"已寫入 {SNAPSHOT.relative_to(ROOT).as_posix()}。", ""]
    report = OUT / name / "contract-report.md"
    report.write_text("\n".join(lines), "utf-8")
    print(
        f"契約{'錄製' if recording else '比對'}：相同 {len(same)}、有差異 {len(snap) - len(same)}、"
        f"未預期 {len(unexpected)} 個案例、異常 {len(problems)} 項"
        + ("（未寫入快照）" if recording and blocking else "")
        + f"；報告：{report}"
    )
    rc = 1 if blocking else 0
    if "--leak" in opt:
        problems = leak_check(args[0] if args else BASE, opt.get("--since") or None)
        body = "\n".join(f"- {p}" for p in problems) or "沒有漏移。"
        kept = "\n".join(f"- `{k}`：{v}" for k, v in NOT_PORTED.items())
        (OUT / name / "leak-report.md").write_text(
            f"# 上游 {name} 漏移檢查\n\n{body}\n\n刻意不移植：\n\n{kept}\n", "utf-8"
        )
        print(f"漏移檢查：{len(problems)} 項；報告：{OUT / name / 'leak-report.md'}")
        rc = max(rc, 1 if problems else 0)
    return rc


if __name__ == "__main__":
    sys.exit(main())
