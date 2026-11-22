r"""同步上游時，把上游對 app.py、pipeline.py 的改動對應到 refactor 現在的檔案（refactor-port-plan.md 3.21）。

E 組步驟 1 把 app.py 拆到 leihu/web/，步驟 4 把 pipeline.py 的一部分搬到 leihu/；refactor 要求線性歷史，
不能 merge 上游，cherry-pick 上游改 app.py 的 commit 一定會衝突。程式是原封搬的，函式名稱沒變，
所以用「hunk 落在上游哪個最上層定義裡」→「那個名稱現在定義在哪個檔案」來對應。

用法（repo 根目錄，要先 git fetch upstream --tags）：
  python tests/tools/upstream_map.py v2.3.2 v2.3.3                # 列出每個 commit 的每個 hunk 要改哪個檔案
  python tests/tools/upstream_map.py v2.3.2 v2.3.3 --patch=<資料夾>  # 另外寫出 patch（每個 commit 最多兩個）

<序號>-<sha>.other.patch 是其他檔案的改動，用 git apply --index --3way 套；<序號>-<sha>.split.patch 是
app.py、pipeline.py 的 hunk，用 git apply --index --reject --recount 套。split.patch 裡 app.py 的 hunk 改寫成目標檔案的路徑，並把 @app.X 換成 @bp.X（errorhandler、before_request 用 app_ 版本）、
app.static_folder／app.response_class 換成 current_app.*；其他檔案原樣。行號是上游的，git apply 會依內容找位置。
落在模組層級（import、常數）或名稱已經不在的 hunk 不放進 patch，列在報告裡手動處理。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPLIT = ("app.py", "pipeline.py")  # 拆過的檔案；其他檔案的改動原樣套用
# 拆過的檔案的程式現在可能在哪裡（同名的定義只在這個範圍裡找：app.py 的 furigana 路由和 pipeline.furigana 不同）
MOVED_TO = {
    "app.py": ("app.py", "leihu/web/"),
    "pipeline.py": ("pipeline.py", "leihu/pitch.py", "leihu/lyrics_text.py"),
}
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$")
APP_RULES = [  # 步驟 1 搬移時唯一改過的寫法（3.19）
    (re.compile(r"@app\.errorhandler\b"), "@bp.app_errorhandler"),
    (re.compile(r"@app\.before_request\b"), "@bp.before_app_request"),
    (re.compile(r"@app\."), "@bp."),
    (re.compile(r"\bapp\.(static_folder|response_class)\b"), r"current_app.\1"),
]


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    ).stdout


def _names(node: ast.stmt) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def spans(src: str) -> list[tuple[int, int, str]]:
    """最上層定義的 (開始行, 結束行, 名稱)；開始行含 decorator。"""
    out = []
    for n in ast.parse(src).body:
        start = min([n.lineno, *(d.lineno for d in getattr(n, "decorator_list", []))])
        out += [(start, n.end_lineno, nm) for nm in _names(n)]
    return out


def current_index(root: Path = ROOT) -> dict[str, list[str]]:
    """名稱 → 現在定義它的檔案（app.py、pipeline.py、leihu/ 底下）。"""
    idx: dict[str, list[str]] = {}
    for p in [root / "app.py", root / "pipeline.py", *sorted((root / "leihu").rglob("*.py"))]:
        for _, _, nm in spans(p.read_text("utf-8")):
            idx.setdefault(nm, []).append(p.relative_to(root).as_posix())
    return idx


def split_hunks(diff: str) -> list[dict]:
    """單一檔案的 unified diff → hunk 清單（header、新檔的行範圍、內容行）。"""
    out, cur = [], None
    for line in diff.split("\n"):
        m = HUNK.match(line)
        if m:
            cur = {
                "header": line,
                "s": int(m.group(3)),
                "n": int(m.group(4) if m.group(4) is not None else 1),
                "lines": [],
            }
            out.append(cur)
        elif cur is not None and line[:1] in (" ", "+", "-", "\\"):
            cur["lines"].append(line)
    return out


def target_of(
    h: dict, up_spans: list[tuple[int, int, str]], idx: dict[str, list[str]], src: str = "app.py"
) -> tuple[str | None, str]:
    """回傳 (目標檔案或 None, 說明)。src 是上游的檔案（app.py 或 pipeline.py）。"""
    s = h["s"]
    e = s + max(h["n"], 1) - 1
    names = sorted({nm for a, b, nm in up_spans if a <= e and s <= b})
    if not names:
        return None, "模組層級（import、常數、註解）：手動"
    where = {nm: [f for f in idx.get(nm, []) if f.startswith(MOVED_TO[src])] for nm in names}
    files = sorted({f for fs in where.values() for f in fs})
    gone = [nm for nm, fs in where.items() if not fs]
    if gone and src == "pipeline.py" and files in ([], ["pipeline.py"]):
        # pipeline.py 只搬出不常改的區塊：上游新增的定義預設留在 pipeline.py
        return "pipeline.py", f"{', '.join(names)}（{', '.join(gone)} 是上游新增的，放在 pipeline.py）"
    if gone:
        return None, f"{', '.join(gone)} 現在找不到（上游新增的定義，或已改名）：手動決定放哪個檔案"
    if len(files) != 1:
        return None, f"{', '.join(names)} 分在 {', '.join(files)}：手動"
    return files[0], ", ".join(names)


def retarget(lines: list[str], src: str) -> list[str]:
    if src != "app.py":
        return lines
    out = []
    for ln in lines:
        body = ln[1:]
        for rx, rep in APP_RULES:
            body = rx.sub(rep, body)
        out.append(ln[:1] + body)
    return out


def trim_blank_context(lines: list[str]) -> list[str]:
    """去掉頭尾的空白上下文行：搬到別的檔案後，函式可能在檔案開頭或結尾，上游 hunk 邊上的空行不存在。
    行數和 header 對不上，所以 split.patch 要用 git apply --recount。"""
    s, e = 0, len(lines)
    while s < e and lines[s] == " ":
        s += 1
    while e > s and lines[e - 1] == " ":
        e -= 1
    return lines[s:e]


def commit_patch(c: str, idx: dict[str, list[str]]) -> tuple[str, str, list[str]]:
    """一個上游 commit → (其他檔案的 patch, 改好路徑的 patch, 報告行)。
    其他檔案的 patch 有 blob index，用 git apply --3way 套（fork 自己改過的地方會變成衝突）；
    改好路徑的 hunk 沒有對應的 blob，不能 3-way，用 git apply --reject --recount。"""
    report, parts = [], []
    other = git("diff", "--binary", f"{c}^", c, "--", ".", *(f":!{f}" for f in SPLIT))
    for f in SPLIT:
        diff = git("diff", "-U3", f"{c}^", c, "--", f)
        if not diff.strip():
            continue
        up = spans(git("show", f"{c}:{f}"))
        for h in split_hunks(diff):
            tgt, why = target_of(h, up, idx, f)
            report.append(f"  {f} {h['header'].split(' @@')[0]} @@ → {tgt or '（不放進 patch）'}：{why}")
            if tgt:
                parts.append(
                    "\n".join(
                        [
                            f"diff --git a/{tgt} b/{tgt}",
                            f"--- a/{tgt}",
                            f"+++ b/{tgt}",
                            h["header"],
                            *(trim_blank_context(retarget(h["lines"], f)) if tgt != f else h["lines"]),
                        ]
                    )
                )
    return other, ("\n".join(parts) + "\n") if parts else "", report


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    if len(args) != 2:
        print(__doc__)
        return 2
    base, ref = args
    idx = current_index()
    out = Path(opt["patch"]) if "patch" in opt else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    commits = git("rev-list", "--reverse", "--no-merges", f"{base}..{ref}").split()
    for i, c in enumerate(commits, 1):
        print(f"{i:02d} {git('show', '-s', '--format=%h %s', c).strip()}")
        other, split, report = commit_patch(c, idx)
        print("\n".join(report) or "  （沒有改 app.py、pipeline.py）")
        if out:
            for suffix, text in (("other", other), ("split", split)):
                if text.strip():
                    (out / f"{i:02d}-{c[:7]}.{suffix}.patch").write_text(text, "utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
