r"""以「最上層定義」為單位，把上游 app.py、pipeline.py 的改動三方合併進 fork（docs/upstream-sync-research.md 方向 A）。

upstream_map.py 是「hunk 落在哪個定義 → 那個名稱現在在哪個檔案 → 改寫 patch 路徑」，fork 改過的定義 patch 就套不上（.rej）。
這支改成對每個上游改過的定義做三方合併：
  base = 舊上游的定義、theirs = 新上游的定義、ours = fork 目前的定義（在哪個檔案都可以）
用 `git merge-file` 合併；兩邊改到同一段才衝突。
- 上游新增的定義：PLACE 有登記就放那個檔案，沒有就放在它前一個鄰居（上游的順序）現在所在的檔案、接在後面。
- 上游刪掉的定義：fork 沒改過才刪，改過列為「手動」。
- 版號（VERSIONED 的名稱、VERSION 檔）：版號先換成記號再三方合併（版號本身不會衝突，fork 的註解、網址照樣留著），
  合併後版號一律是 3.15 的「新上游版本＋.1」。
- 模組層級的 import：上游新增的 import，fork 同一組檔案裡已經有就略過；沒有的話，IMPORT_TARGET 有登記就加進那個檔案，
  沒登記列為「手動」。其他模組層級的敘述（if 區塊、運算式）只列出來，手動處理。
- 其他檔案（gpu_worker.py、static/* 等）：整個檔案三方合併（fork 和舊上游相同就直接換）。

用法（repo 根目錄，要先 git fetch upstream --tags）：
  python tests/tools/defmerge.py <舊上游> <新上游> <fork 的參照> --out=<資料夾> [--expect=<參照>] [--all]
    寫出合併後的檔案到 <資料夾>（不動工作目錄），印出報告。
    --expect：和這個參照（例如手動同步後的 commit）依名稱比較，列出合併結果不同的定義（驗證用）。
    --all：app.py、pipeline.py 以外上游改過的檔案也整個檔案三方合併。
  python tests/tools/defmerge.py <舊上游> <新上游> --tree=<資料夾> --out=<資料夾>
    fork 用一個資料夾（例如模擬拆檔後的副本），不用 git 參照；寫出時保留原本的換行（CRLF／LF）。
結束代碼：有衝突或要手動處理的是 1，否則 0。

限制：類別整個當成一個定義合併（不拆到方法）；名稱改了的定義要登記在 RENAMES；同名定義在 fork 有兩個以上時列為「手動」；
fork 為了排版把上游的整段縮排，三方合併會把整段當成改過（研究文件第 6 節）。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# 上游檔案 → fork 裡可能放這些定義的檔案（glob，相對 repo 根目錄）
GROUPS = {
    "app.py": ["app.py", "leihu/web/*.py"],
    "pipeline.py": ["pipeline.py", "leihu/*.py", "leihu/core/*.py"],
}
RENAMES: dict[str, str] = {}   # 上游名稱 → fork 名稱（改名時登記）
# 上游新增的定義要放的檔案：名稱，或 "re:<模式>"。沒登記的放在前一個鄰居所在的檔案
PLACE: dict[str, str] = {
    "submit_unfinished": "leihu/web/common.py",   # 2.3.5：背景、system、library 共用
    # 2.3.3：佇列保存與還原是啟動時的背景工作（app.main 呼叫）
    **dict.fromkeys(["_QUEUE_READY", "_queue_snapshot", "_restorable", "_resubmit", "queue_saver", "restore_queue"],
                    "leihu/web/background.py"),
}
VERSIONED = {"VERSION", "USER_AGENT"}   # 3.15：內容照新上游、版號改成「上游版本＋.1」
VERSION_FILES = {"VERSION"}
IMPORT_TARGET = {"pipeline.py": "pipeline.py"}   # 上游新增的 import 要加進哪個檔案；app.py 拆成很多檔案，要手動放


# ---------------------------------------------------------------- 讀檔
def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout


def show(ref: str, path: str) -> str | None:
    try:
        return git("show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None


class Source:
    """一份程式：git 參照或資料夾。讀出來一律是 LF；資料夾記下原本的換行，寫出時照原樣。"""

    def __init__(self, ref: str | None = None, tree: Path | None = None):
        self.ref, self.tree = ref, tree
        self.eols: dict[str, str] = {}

    def files(self, pattern: str) -> list[str]:
        if self.tree:
            return sorted(p.relative_to(self.tree).as_posix() for p in self.tree.glob(pattern) if p.is_file())
        out = git("ls-tree", "-r", "--name-only", self.ref).splitlines()
        rx = re.compile("^" + re.escape(pattern).replace(r"\*", "[^/]*") + "$")
        return [f for f in out if rx.match(f)]

    def read(self, path: str) -> str | None:
        if self.tree:
            p = self.tree / path
            if not p.exists():
                return None
            text = p.read_bytes().decode("utf-8")
        else:
            text = show(self.ref, path)
            if text is None:
                return None
        if "\r\n" in text:
            self.eols[path] = "\r\n"
        return text.replace("\r\n", "\n")

    def eol(self, path: str) -> str:
        return self.eols.get(path, "\n")


# ---------------------------------------------------------------- 定義與模組層級
DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)


def defs(text: str | None) -> dict[str, tuple[int, int, str]]:
    """名稱 → (開始行, 結束行, 原始碼)；行號從 0 起、結束不含。包含裝飾器。"""
    out: dict[str, tuple[int, int, str]] = {}
    if not text:
        return out
    lines = text.split("\n")
    for n in ast.parse(text).body:
        names = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [n.name]
        elif isinstance(n, ast.Assign):
            names = [t.id for t in n.targets if isinstance(t, ast.Name)]
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names = [n.target.id]
        if not names:
            continue
        a = (n.decorator_list[0].lineno if getattr(n, "decorator_list", None) else n.lineno) - 1
        b = n.end_lineno
        for nm in names:
            out[nm] = (a, b, "\n".join(lines[a:b]))
    return out


def others(text: str | None) -> list[str]:
    """不是定義、也不是 import 的模組層級敘述（if、運算式）的原始碼。"""
    if not text:
        return []
    lines = text.split("\n")
    return ["\n".join(lines[n.lineno - 1:n.end_lineno]) for n in ast.parse(text).body
            if not isinstance(n, (*DEF_NODES, ast.Import, ast.ImportFrom))]


def import_keys(text: str | None) -> set[tuple]:
    """模組層級 import 的每一個名稱：("import", 模組, 別名) 或 ("from", 模組, 名稱, 別名)。"""
    out: set[tuple] = set()
    if not text:
        return out
    for n in ast.parse(text).body:
        if isinstance(n, ast.Import):
            out |= {("import", a.name, a.asname) for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mod = "." * n.level + (n.module or "")
            out |= {("from", mod, a.name, a.asname) for a in n.names}
    return out


def import_lines(keys: set[tuple]) -> list[str]:
    def alias(name, asname):
        return f"{name} as {asname}" if asname else name
    out = [f"import {alias(k[1], k[2])}" for k in sorted(k for k in keys if k[0] == "import")]
    froms: dict[str, list[str]] = {}
    for k in sorted(k for k in keys if k[0] == "from"):
        froms.setdefault(k[1], []).append(alias(k[2], k[3]))
    return out + [f"from {m} import {', '.join(ns)}" for m, ns in froms.items()]


def last_import_end(text: str) -> int:
    """最後一個模組層級 import 的下一行（行號從 0 起）；沒有 import 時放在 docstring 和 __future__ 之後的開頭。"""
    end = 0
    for n in ast.parse(text).body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            end = n.end_lineno
        elif end == 0 and isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant):
            end = n.end_lineno   # 模組 docstring
    return end


# ---------------------------------------------------------------- 規則
def to_fork_style(src: str, origin: str) -> str:
    """上游 app.py 的寫法換成 fork 的 Blueprint 寫法（和 upstream_map 相同）。"""
    if origin != "app.py":
        return src
    src = re.sub(r"@app\.errorhandler", "@bp.app_errorhandler", src)
    src = re.sub(r"@app\.before_request", "@bp.before_app_request", src)
    return re.sub(r"@app\.", "@bp.", src)


VER_MARK = "@@DEFMERGE_VERSION@@"   # 不能用 NUL：git merge-file 會當成二進位檔


def unver(src: str, ver: str) -> str:
    """把版號（含 fork 的第 4 段，例如 2.3.4、2.3.4.1、2.3.4.2）換成記號，三方合併時版號就不會衝突。
    只換完整的版號：2.3.5 不會換到 2.3.50。"""
    return re.sub(rf"(?<![\d.]){re.escape(ver)}(\.\d+)?(?![\d.])", VER_MARK, src)


def place_of(name: str) -> str | None:
    if name in PLACE:
        return PLACE[name]
    return next((f for k, f in PLACE.items() if k.startswith("re:") and re.fullmatch(k[3:], name)), None)


def norm(s: str) -> str:
    """比對用：只去掉行尾空白和頭尾空行。註解要算進去，不然 fork 只補了註解的定義會被當成沒改過、直接被上游蓋掉。"""
    return "\n".join(line.rstrip() for line in s.strip("\n").split("\n"))


def merge3(ours: str, base: str, theirs: str) -> tuple[str, int]:
    """git merge-file 三方合併：回傳 (結果, 衝突數)。"""
    with tempfile.TemporaryDirectory() as d:
        p = {k: Path(d) / k for k in ("ours", "base", "theirs")}
        for k, v in (("ours", ours), ("base", base), ("theirs", theirs)):
            p[k].write_bytes((v + "\n").encode("utf-8"))
        r = subprocess.run(["git", "merge-file", "-p", "-L", "fork", "-L", "舊上游", "-L", "新上游",
                            str(p["ours"]), str(p["base"]), str(p["theirs"])], capture_output=True)
        return r.stdout.decode("utf-8").replace("\r\n", "\n").rstrip("\n"), r.returncode


# ---------------------------------------------------------------- 合併
class Merge:
    """一次同步：依序處理每一組（app.py、pipeline.py）和其他檔案，最後寫出。"""

    def __init__(self, old: Source, new: Source, fork: Source):
        self.old, self.new, self.fork = old, new, fork
        self.stats = dict.fromkeys(("changed", "fast", "merged", "conflict", "added", "removed", "manual",
                                    "version", "imports"), 0)
        self.report: list[str] = []
        self.edits: dict[str, list[tuple[int, int, str]]] = {}   # 檔案 → [(開始, 結束, 新內容)]
        self.texts: dict[str, str] = {}
        self.whole: dict[str, str] = {}   # 整個檔案換掉的
        self.touched: set[str] = set()   # 這次改到的 fork 名稱（驗證用）
        self.placed: list[tuple[str, str]] = []   # 上游新增的定義放在哪個檔案
        vold, vnew = old.read("VERSION"), new.read("VERSION")
        self.oldver = vold.strip() if vold else None
        self.upver = vnew.strip() if vnew else None

    def vmerge(self, ours: str, base: str, theirs: str) -> tuple[str, int]:
        """3.15：版號不管三方合併的結果，一律是「新上游版本＋.1」；版號以外照常三方合併（fork 的註解、網址會留著）。"""
        o, b, t = unver(ours, self.oldver), unver(base, self.oldver), unver(theirs, self.upver)
        merged, c = (t, 0) if norm(o) == norm(b) else merge3(o, b, t)
        return merged.replace(VER_MARK, self.upver + ".1"), c

    def note(self, kind: str, msg: str, stat: str | None = None) -> None:
        self.report.append(f"{kind:4}  {msg}")
        if stat:
            self.stats[stat] += 1

    # ---- 一組檔案
    def group(self, origin: str, pats: list[str]) -> None:
        old_t, new_t = self.old.read(origin), self.new.read(origin)
        old_d, new_d = defs(old_t), defs(new_t)
        self.build_index(pats)
        order = list(new_d)
        for name, (_, _, new_src) in new_d.items():
            base = old_d.get(name)
            if base and norm(base[2]) == norm(new_src):
                continue   # 上游沒改
            self.touched.add(RENAMES.get(name, name))
            theirs = to_fork_style(new_src, origin)
            if base is None:
                self.added(origin, name, theirs, order)
            else:
                self.changed(origin, name, to_fork_style(base[2], origin), theirs)
        for name in old_d:
            if name not in new_d:
                self.removed(origin, name, to_fork_style(old_d[name][2], origin))
        self.imports(origin, old_t, new_t)
        o_old = others(old_t)
        for s in others(new_t):
            if s not in o_old:
                self.note("手動", f"{origin} 模組層級新增／改動：{s.splitlines()[0][:100]}", "manual")

    def build_index(self, pats: list[str]) -> None:
        """fork 這一組檔案的定義：名稱 → [(檔案, 開始行, 結束行, 原始碼)]。"""
        self.index: dict[str, list[tuple[str, int, int, str]]] = {}
        for pat in pats:
            for f in self.fork.files(pat):
                t = self.fork.read(f)
                if t is None:
                    continue
                self.texts[f] = t
                for nm, (a, b, src) in defs(t).items():
                    self.index.setdefault(nm, []).append((f, a, b, src))

    def where(self, name: str):
        hits = self.index.get(RENAMES.get(name, name), [])
        return hits[0] if len(hits) == 1 else (None if not hits else "ambiguous")

    def located(self, name: str) -> bool:
        return self.where(name) not in (None, "ambiguous")

    def added(self, origin: str, name: str, theirs: str, order: list[str]) -> None:
        if self.where(name) is not None:
            self.note("手動", f"{origin}:{name}：上游新增，但 fork 已經有同名定義", "manual")
            return
        prev = [n for n in reversed(order[:order.index(name)]) if self.located(n)]
        target = place_of(name)
        if target is not None:
            if target not in self.texts:
                self.note("手動", f"{origin}:{name}：PLACE 指定的 {target} 不存在", "manual")
                return
            anchor = next((n for n in prev if self.where(n)[0] == target), None)
            at = self.where(anchor)[2] if anchor else len(self.texts[target].rstrip("\n").split("\n"))
        elif prev:
            anchor = prev[0]
            target, _, at, _ = self.where(anchor)
        else:
            self.note("手動", f"{origin}:{name}：上游新增，找不到前面的鄰居", "manual")
            return
        self.edits.setdefault(target, []).append((at, at, "\n\n" + theirs))   # 和前一個定義隔兩個空行
        self.placed.append((name, target))
        self.note("新增", f"{origin}:{name} → {target}（放在{f'{anchor} 後面' if anchor else '檔案最後'}）", "added")

    def changed(self, origin: str, name: str, base: str, theirs: str) -> None:
        self.stats["changed"] += 1
        hit = self.where(name)
        if hit is None:
            self.note("手動", f"{origin}:{name}：fork 找不到這個定義", "manual")
            return
        if hit == "ambiguous":
            self.note("手動", f"{origin}:{name}：fork 有兩個以上同名定義", "manual")
            return
        f, a, b, ours = hit
        if name in VERSIONED and self.upver and self.oldver:
            merged, c = self.vmerge(ours, base, theirs)
            kind, stat = ("版號", "version") if c == 0 else (f"衝突 {c} 處", "conflict")
        elif norm(ours) == norm(base):
            merged, kind, stat = theirs, "直接換", "fast"
        else:
            merged, c = merge3(ours, base, theirs)
            kind, stat = ("合併", "merged") if c == 0 else (f"衝突 {c} 處", "conflict")
        self.edits.setdefault(f, []).append((a, b, merged))
        self.note(kind, f"{origin}:{name} → {f}", stat)

    def removed(self, origin: str, name: str, base: str) -> None:
        hit = self.where(name)
        if hit in (None, "ambiguous"):
            return
        f, a, b, ours = hit
        self.touched.add(RENAMES.get(name, name))
        if norm(ours) == norm(base):
            self.edits.setdefault(f, []).append((a, b, ""))
            self.note("刪除", f"{origin}:{name}（{f}）", "removed")
        else:
            self.note("手動", f"{origin}:{name}：上游刪掉了，但 fork 改過（{f}）", "manual")

    def imports(self, origin: str, old_t: str | None, new_t: str | None) -> None:
        """IMPORT_TARGET 的檔案已經有就略過，沒有就加進去；沒登記的（app.py 拆成很多檔案）要手動放進用到的檔案。"""
        missing = import_keys(new_t) - import_keys(old_t)
        target = IMPORT_TARGET.get(origin)
        if target in self.texts:
            missing -= import_keys(self.texts[target])
        if not missing:
            return
        lines = import_lines(missing)
        if target in self.texts:
            at = last_import_end(self.texts[target])
            self.edits.setdefault(target, []).append((at, at, "\n".join(lines)))
            self.stats["imports"] += len(lines)
            self.note("import", f"{origin} → {target}：{'；'.join(lines)}")
        else:
            self.note("手動", f"{origin} 新增的 import 要放進用到的檔案：{'；'.join(lines)}", "manual")

    # ---- 其他檔案：整個檔案三方合併
    def file(self, path: str) -> None:
        try:
            o, n, f = self.old.read(path), self.new.read(path), self.fork.read(path)
        except UnicodeDecodeError:
            self.note("手動", f"{path}：不是文字檔", "manual")
            return
        if path in VERSION_FILES and None not in (o, n, f) and self.upver and self.oldver:
            merged, c = self.vmerge(f.rstrip("\n"), o.rstrip("\n"), n.rstrip("\n"))
            self.whole[path] = merged + ("\n" if n.endswith("\n") else "")
            self.note("版號" if c == 0 else f"衝突 {c} 處", path, "version" if c == 0 else "conflict")
        elif n is None:
            same = f is not None and o is not None and norm(f) == norm(o)
            self.note("手動", f"{path}：上游刪掉了" + ("（fork 沒改過，可以刪）" if same else ""), "manual")
        elif o is None:
            if f is None:
                self.whole[path] = n
                self.note("新增", path, "added")
            else:
                self.note("手動", f"{path}：上游新增，但 fork 已經有這個檔案", "manual")
        elif f is None:
            self.note("手動", f"{path}：fork 沒有這個檔案", "manual")
        elif norm(f) == norm(o):
            self.whole[path] = n
            self.note("直接換", path, "fast")
        else:
            merged, c = merge3(f.rstrip("\n"), o.rstrip("\n"), n.rstrip("\n"))
            self.whole[path] = merged + ("\n" if n.endswith("\n") else "")
            self.note("合併" if c == 0 else f"衝突 {c} 處", path, "merged" if c == 0 else "conflict")

    # ---- 寫出與驗證
    def write(self, out_dir: Path) -> list[str]:
        for f, es in self.edits.items():
            lines = self.texts[f].split("\n")
            for a, b, new_src in sorted(es, key=lambda e: -e[0]):
                lines[a:b] = new_src.split("\n") if new_src else []
            self.whole[f] = "\n".join(lines)
        self.check_uses()
        out_dir.mkdir(parents=True, exist_ok=True)
        for f, text in self.whole.items():
            p = out_dir / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.replace("\n", self.fork.eol(f)).encode("utf-8"))
        return sorted(self.whole)

    def check_uses(self) -> None:
        """上游新增的定義放進別的檔案時，用到它的檔案要 import（2.3.5 的 submit_unfinished：background、system、
        library 都要 from .common import）。只列出來，不自動加：import 要和那個檔案原本的寫法排在一起。"""
        for name, target in self.placed:
            for f in sorted(self.texts):
                if f == target:
                    continue
                text = self.whole.get(f, self.texts[f])
                if not re.search(rf"(?<![.\w]){re.escape(name)}\b", text):   # P.名稱 這種用法不用 import
                    continue
                try:
                    if name in defs(text) or any((k[3] or k[2]) == name if k[0] == "from" else (k[2] or k[1]) == name
                                                 for k in import_keys(text)):
                        continue
                except SyntaxError:   # 有衝突標記：衝突已經列在報告裡
                    continue
                self.note("手動", f"{f} 用到 {name}（放在 {target}），要加 import", "manual")

    def compare(self, expect: Source, out_dir: Path) -> list[str]:
        """依名稱和預期的結果比較（不管放在哪個檔案）：內容不同、或位置不同。"""
        def index_of(read, files):
            idx = {}
            for f in files:
                t = read(f)
                if t is None:
                    continue
                # 有衝突的地方先取 fork 那一側才能解析；衝突本身已經列在報告裡
                t = re.sub(r"<<<<<<< fork\n(.*?)=======\n.*?>>>>>>> 新上游\n?", r"\1", t.replace("\r\n", "\n"), flags=re.S)
                for nm, (_, _, src) in defs(t).items():
                    idx[nm] = (f, src)
            return idx
        fork_files = [f for pats in GROUPS.values() for pat in pats for f in self.fork.files(pat)]
        got = index_of(lambda f: (out_dir / f).read_text("utf-8") if (out_dir / f).exists() else self.fork.read(f),
                       fork_files)
        want = index_of(expect.read, [f for pats in GROUPS.values() for pat in pats for f in expect.files(pat)])
        diffs = []
        for nm in sorted(self.touched):
            g, w = got.get(nm), want.get(nm)
            if not g or not w:
                diffs.append(f"{nm}：{'合併結果' if not g else '預期'}沒有這個定義")
            elif norm(g[1]) != norm(w[1]):
                diffs.append(f"{nm}：內容不同（{g[0]}）")
            elif g[0] != w[0]:
                diffs.append(f"{nm}：位置不同（合併放 {g[0]}，預期在 {w[0]}）")
        return diffs


def run(old: Source, new: Source, fork: Source, out_dir: Path, expect: Source | None = None,
        extra_files: list[str] | None = None) -> dict:
    m = Merge(old, new, fork)
    for origin, pats in GROUPS.items():
        m.group(origin, pats)
    for path in extra_files or []:
        m.file(path)
    files = m.write(out_dir)
    diffs = m.compare(expect, out_dir) if expect else []
    return {"stats": m.stats, "report": m.report, "diffs": diffs, "files": files}


def changed_files(old_ref: str, new_ref: str) -> list[str]:
    """上游兩版之間改過的檔案（app.py、pipeline.py 以外）。"""
    out = git("diff", "--name-only", old_ref, new_ref).splitlines()
    return [f for f in out if f not in GROUPS]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in sys.argv[1:] if a.startswith("--"))
    if len(args) < 2 or "out" not in opt or ("tree" not in opt and len(args) < 3):
        print(__doc__)
        return 2
    fork = Source(tree=Path(opt["tree"])) if "tree" in opt else Source(ref=args[2])
    expect = Source(ref=opt["expect"]) if "expect" in opt else None
    extra = changed_files(args[0], args[1]) if "all" in opt else None
    r = run(Source(ref=args[0]), Source(ref=args[1]), fork, Path(opt["out"]), expect, extra)
    print("\n".join(r["report"]))
    print("統計：", r["stats"])
    print("寫出：", r["files"])
    if expect:
        print("和", opt["expect"], "不同的定義：", r["diffs"] or "無")
    return 1 if r["stats"]["conflict"] or r["stats"]["manual"] else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
