r"""以「最上層定義」為單位，把上游 app.py、pipeline.py 的改動三方合併進 fork（docs/upstream-sync-research.md 方向 A）。

upstream_map.py 是「hunk 落在哪個定義 → 那個名稱現在在哪個檔案 → 改寫 patch 路徑」，fork 改過的定義 patch 就套不上（.rej）。
這支改成對每個上游改過的定義做三方合併：
  base = 舊上游的定義、theirs = 新上游的定義、ours = fork 目前的定義（在哪個檔案都可以）
用 `git merge-file` 合併；兩邊改到同一段（或相鄰的行）才衝突。
- 上游新增的定義：PLACE 有登記就放那個檔案，沒有就放在它前一個鄰居（上游的順序）現在所在的檔案、接在後面；
  連續新增的照上游的順序放在一起。
- 上游刪掉的定義：fork 沒改過才刪，改過、或 fork 有兩個以上同名的列為「手動」。
- 版號（VERSIONED 的名稱、VERSION 檔）：版號先換成記號再三方合併（版號本身不會衝突，fork 的註解、網址照樣留著），
  合併後版號一律是 3.15 的「新上游版本＋.1」。
- 模組層級的 import：上游新增的，IMPORT_TARGET 的檔案已經有就略過、沒有就加在最後一個 import 後面；用到它的定義在
  別的檔案、__future__、或加的位置之前就用到的，列為「手動」。上游刪掉的 import 和其他模組層級的敘述（if 區塊、
  運算式、解包指定、`X[...] =` 這類指定）的新增、改動、刪除都列為「手動」。
- 其他檔案（gpu_worker.py、static/* 等）：整個檔案三方合併（fork 和舊上游相同就直接換），檔尾的空行照原樣。
- 同一個敘述指定好幾個名稱（`A = B = 1`）、同一個檔案有兩個以上同名定義：不自動處理，列為「手動」。

用法（repo 根目錄，要先 git fetch upstream --tags）：
  python tests/tools/defmerge.py <舊上游> <新上游> <fork 的參照> --out=<資料夾> [--expect=<參照>] [--all]
    寫出合併後的檔案到 <資料夾>（不動工作目錄），印出報告。
    --expect：和這個參照（例如手動同步後的 commit）依「來源檔案＋名稱」比較，列出合併結果不同的定義（驗證用）。
    --all：app.py、pipeline.py 以外上游改過的檔案也整個檔案三方合併。
  python tests/tools/defmerge.py <舊上游> <新上游> --tree=<資料夾> --out=<資料夾>
    fork 用一個資料夾（例如模擬拆檔後的副本），不用 git 參照；寫出時保留原本的換行（CRLF／LF）。
結束代碼：有衝突或要手動處理的是 1，否則 0；參照不存在等 git 錯誤直接中止。

限制：類別整個當成一個定義合併（不拆到方法）；名稱改了的定義要登記在 RENAMES；
fork 為了排版把上游的整段縮排，三方合併會把整段當成改過（研究文件第 6 節）。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
from itertools import pairwise
from pathlib import Path
from typing import NamedTuple

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
    # 第 2 輪審查 1：在主執行緒解碼。text=True 時 Windows 在背景執行緒解碼，二進位檔的 UnicodeDecodeError 只印出來、
    # stdout 變成 None，會被當成檔案不存在
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout.decode("utf-8")


def check_ref(ref: str) -> None:
    """參照打錯時直接中止（不然會被當成上游把檔案、定義都刪掉了）。"""
    try:
        git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except subprocess.CalledProcessError:
        raise SystemExit(f"找不到 git 參照：{ref}") from None


def show(ref: str, path: str) -> str | None:
    """參照裡這個檔案的內容；只有確定樹裡沒有這個路徑才回傳 None，其他 git 錯誤照樣丟出去。"""
    try:
        return git("show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        if not git("ls-tree", "--name-only", ref, "--", path).strip():
            return None
        raise


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
class Def(NamedTuple):
    a: int          # 開始行（從 0 起，含裝飾器）
    b: int          # 結束行（不含）
    src: str
    multi: bool     # 同一個敘述指定好幾個名稱（A = B = 1）：不能各自編輯


def _def_names(n) -> list[str] | None:
    """這個最上層敘述定義的名稱；不是「單純定義」的回傳 None（交給 others，列為手動）。"""
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [n.name]
    if isinstance(n, ast.Assign) and all(isinstance(t, ast.Name) for t in n.targets):
        return [t.id for t in n.targets]
    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
        return [n.target.id]
    return None


def _start(n, lines: list[str], prev_end: int) -> int:
    """敘述的開始行（從 0 起），含裝飾器。`@(` 換行再寫裝飾器時，AST 的行號不含 `@(` 那一行，往上找到 @。"""
    if not getattr(n, "decorator_list", None):
        return n.lineno - 1
    k = min(d.lineno for d in n.decorator_list) - 1
    while k > prev_end and not lines[k].lstrip().startswith("@"):
        k -= 1
    return k


def defs(text: str | None) -> dict[str, list[Def]]:
    """名稱 → 這個名稱的每一個定義（同一個檔案可能有兩個以上同名的）。"""
    out: dict[str, list[Def]] = {}
    if not text:
        return out
    lines = text.split("\n")
    prev_end = -1
    for n in ast.parse(text).body:
        names = _def_names(n)
        if names:
            a, b = _start(n, lines, prev_end), n.end_lineno
            for nm in names:
                out.setdefault(nm, []).append(Def(a, b, "\n".join(lines[a:b]), len(names) > 1))
        prev_end = n.end_lineno - 1
    return out


def others(text: str | None) -> list[str]:
    """不是單純定義、也不是 import 的模組層級敘述（if、運算式、解包指定、`X[...] =`）的原始碼。"""
    if not text:
        return []
    lines = text.split("\n")
    return ["\n".join(lines[n.lineno - 1:n.end_lineno]) for n in ast.parse(text).body
            if _def_names(n) is None and not isinstance(n, (ast.Import, ast.ImportFrom))]


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


def bound_name(k: tuple) -> str:
    """import 之後可以用的名稱。"""
    if k[0] == "import":
        return k[2] or k[1].split(".")[0]
    return k[3] or k[2]


def _key_order(k: tuple) -> tuple:
    return tuple("" if x is None else x for x in k)   # 有別名、沒別名的混在一起排序時 None 不能和字串比


def import_lines(keys: set[tuple]) -> list[str]:
    def alias(name, asname):
        return f"{name} as {asname}" if asname else name
    out = [f"import {alias(k[1], k[2])}" for k in sorted((k for k in keys if k[0] == "import"), key=_key_order)]
    froms: dict[str, list[str]] = {}
    for k in sorted((k for k in keys if k[0] == "from"), key=_key_order):
        froms.setdefault(k[1], []).append(alias(k[2], k[3]))
    return out + [f"from {m} import {', '.join(ns)}" for m, ns in froms.items()]


def last_import_end(text: str) -> int:
    """最後一個模組層級 import 的下一行（行號從 0 起）；沒有 import 時放在模組 docstring 之後。"""
    end = 0
    for n in ast.parse(text).body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            end = n.end_lineno
        elif end == 0 and isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant):
            end = n.end_lineno   # 模組 docstring
    return end


def uses_at_load(n: ast.AST, name: str) -> bool:
    """這個模組層級敘述在載入時就會用到 name 嗎。函式只看裝飾器、預設值、型別註記（內容要呼叫時才執行）；
    類別看裝飾器、基底、keywords 和類別內容（類別內容載入時就執行，裡面的方法照函式的規則）。
    有 from __future__ import annotations 時註記不會執行，這裡照樣算（寧可多列手動）。"""
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = n.args
        parts = [*n.decorator_list, *a.defaults, *a.kw_defaults, n.returns,
                 *(x.annotation for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg) if x)]
    elif isinstance(n, ast.ClassDef):
        return any(uses_at_load(p, name) for p in (*n.decorator_list, *n.bases, *n.keywords, *n.body))
    else:
        parts = [n]
    return any(isinstance(x, ast.Name) and x.id == name for p in parts if p is not None for x in ast.walk(p))


def used_before(text: str, name: str, line: int) -> bool:
    """在 line（從 0 起）之前的模組層級敘述（不含 import）載入時有沒有用到 name（複審 P3-11：函式的裝飾器、
    預設值和類別內容也算）。"""
    for n in ast.parse(text).body:
        if n.lineno - 1 >= line:
            break
        if not isinstance(n, (ast.Import, ast.ImportFrom)) and uses_at_load(n, name):
            return True
    return False


# ---------------------------------------------------------------- 規則
def to_fork_style(src: str, origin: str) -> str:
    """上游 app.py 的寫法換成 fork 的 Blueprint 寫法（和 upstream_map 相同）。裝飾器只改那一行（字串、註解不動）；
    app.static_folder、app.response_class 換成 current_app.*（第 2 輪審查 4：原本漏了，Blueprint 裡沒有 app）。"""
    if origin != "app.py":
        return src
    src = re.sub(r"(?m)^(\s*)@app\.errorhandler", r"\1@bp.app_errorhandler", src)
    src = re.sub(r"(?m)^(\s*)@app\.before_request", r"\1@bp.before_app_request", src)
    src = re.sub(r"(?m)^(\s*)@app\.", r"\1@bp.", src)
    return re.sub(r"\bapp\.(static_folder|response_class)\b", r"current_app.\1", src)


VER_MARK = "@@DEFMERGE_VERSION@@"   # 不能用 NUL：git merge-file 會當成二進位檔


def unver(src: str, ver: str) -> str:
    """把版號（含 fork 的第 4 段，例如 2.3.4、2.3.4.1、2.3.4.2）換成記號，三方合併時版號就不會衝突。
    只換完整的版號：2.3.5 不會換到 2.3.50；後面接句點（句尾）照樣換。"""
    return re.sub(rf"(?<![\d.]){re.escape(ver)}(\.\d+)?(?!\.?\d)", VER_MARK, src)


def place_of(name: str) -> str | None:
    if name in PLACE:
        return PLACE[name]
    return next((f for k, f in PLACE.items() if k.startswith("re:") and re.fullmatch(k[3:], name)), None)


def norm(s: str) -> str:
    """比對用：只去掉頭尾空行。行尾空白、註解都要算（三引號字串裡的行尾空白是資料；fork 只補註解也算改過）。"""
    return s.strip("\n")


class MergeFailed(Exception):
    """git merge-file 執行失敗（不是有衝突）：例如內容有 NUL 被當成二進位檔。"""


def merge3(ours: str, base: str, theirs: str, exact: bool = False) -> tuple[str, int]:
    """git merge-file 三方合併：回傳 (結果, 衝突數)。exact：整個檔案合併用，內容照原樣、不補也不去掉檔尾換行。
    執行失敗丟 MergeFailed（第 2 輪審查 3：結束代碼 255、輸出是空字串，原本會把定義或整個檔案寫成空白）。
    衝突標記固定用 merge 樣式：使用者設了 diff3／zdiff3 會多出 ||||||| 區塊，--expect 取 fork 那一側時去不乾淨。"""
    with tempfile.TemporaryDirectory() as d:
        p = {k: Path(d) / k for k in ("ours", "base", "theirs")}
        for k, v in (("ours", ours), ("base", base), ("theirs", theirs)):
            p[k].write_bytes((v if exact else v + "\n").encode("utf-8"))
        r = subprocess.run(["git", "-c", "merge.conflictStyle=merge", "merge-file", "-p",
                            "-L", "fork", "-L", "舊上游", "-L", "新上游",
                            str(p["ours"]), str(p["base"]), str(p["theirs"])], capture_output=True)
        if r.returncode < 0 or r.returncode > 127:   # 衝突數最多回報到 127
            err = r.stderr.decode("utf-8", errors="replace").strip()
            raise MergeFailed(f"git merge-file 失敗（代碼 {r.returncode}）：{err or '沒有訊息'}")
        out = r.stdout.decode("utf-8").replace("\r\n", "\n")
        return (out if exact else out.rstrip("\n")), r.returncode


# ---------------------------------------------------------------- 合併
class Merge:
    """一次同步：依序處理每一組（app.py、pipeline.py）和其他檔案，最後寫出。"""

    def __init__(self, old: Source, new: Source, fork: Source):
        self.old, self.new, self.fork = old, new, fork
        self.stats = dict.fromkeys(("changed", "fast", "merged", "conflict", "added", "removed", "manual",
                                    "version", "imports"), 0)
        self.report: list[str] = []
        self.edits: dict[str, list[tuple[int, int, str]]] = {}   # 檔案 → [(開始, 結束, 新內容)]；開始＝結束是插入
        self.texts: dict[str, str] = {}
        self.whole: dict[str, str] = {}   # 整個檔案換掉的
        self.touched: set[tuple[str, str]] = set()   # 這次改到的 (來源檔案, fork 名稱)（驗證用）
        self.placed: list[tuple[str, str]] = []   # 上游新增的定義放在哪個檔案
        self.dest: dict[str, str] = {}   # 合併後的定義 → 放在哪個檔案（import 依實際位置檢查）
        self.code: dict[str, str] = {}   # 合併後的定義 → 原始碼
        vold, vnew = old.read("VERSION"), new.read("VERSION")
        self.oldver = vold.strip() if vold else None
        self.upver = vnew.strip() if vnew else None

    def vmerge(self, ours: str, base: str, theirs: str) -> tuple[str | None, int | str]:
        """3.15：版號不管三方合併的結果，一律是「新上游版本＋.1」；版號以外照常三方合併（fork 的註解、網址會留著）。
        不能自動換時回傳 (None, 原因)，要手動：內容裡本來就有 VER_MARK（換回版號會連它一起換掉，Codex r3-19）；
        三份裡有一份找不到該有的版號（fork 漏改、帶後綴，第 2 輪審查 2：三方合併會保留 fork 的舊版號、還回報成功）。"""
        if any(VER_MARK in s for s in (ours, base, theirs)):
            return None, f"內容裡本來就有版號記號 {VER_MARK}，不能自動換版號"
        o, b, t = unver(ours, self.oldver), unver(base, self.oldver), unver(theirs, self.upver)
        missing = [w for w, s, v in (("fork", o, self.oldver), ("舊上游", b, self.oldver), ("新上游", t, self.upver))
                   if VER_MARK not in s]
        if missing:
            return None, f"{'、'.join(missing)}找不到版號 {self.oldver}／{self.upver}，不能自動換版號"
        merged, c = (t, 0) if norm(o) == norm(b) else merge3(o, b, t)
        if c == 0 and VER_MARK not in merged:
            return None, "合併後找不到版號，不能自動換版號"
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
        self.dest, self.code, self.pos = {}, {}, {}   # 這一組合併後的定義：檔案、原始碼、位置（import 只看同一組用到的）
        order = list(new_d)
        for name, news in new_d.items():
            self.one(origin, name, news, old_d.get(name, []), order)
        for name, olds in old_d.items():
            if name not in new_d:
                self.removed(origin, name, olds)
        self.imports(origin, old_t, new_t)
        self.module_stmts(origin, old_t, new_t)

    def one(self, origin: str, name: str, news: list[Def], olds: list[Def], order: list[str]) -> None:
        """上游的一個名稱：沒改就略過，新增、改過就交給 added／changed。"""
        if len(news) > 1 or len(olds) > 1 or news[0].multi or (olds and olds[0].multi):
            if not olds or [x.src for x in olds] != [x.src for x in news]:
                self.note("手動", f"{origin}:{name}：上游同一個檔案有兩個以上同名定義，或和別的名稱寫在同一個指定裡", "manual")
            return
        new_src = news[0].src
        if olds and norm(olds[0].src) == norm(new_src):
            return   # 上游沒改
        self.touched.add((origin, RENAMES.get(name, name)))
        theirs = to_fork_style(new_src, origin)
        if not olds:
            self.added(origin, name, theirs, order)
        else:
            self.changed(origin, name, to_fork_style(olds[0].src, origin), theirs)

    def module_stmts(self, origin: str, old_t: str | None, new_t: str | None) -> None:
        """模組層級的其他敘述：新增、改動、刪除都列為手動。"""
        o_old, o_new = others(old_t), others(new_t)
        for s in o_new:
            if s not in o_old:
                self.note("手動", f"{origin} 模組層級新增／改動：{s.splitlines()[0][:100]}", "manual")
        for s in o_old:
            if s not in o_new:
                self.note("手動", f"{origin} 模組層級刪除（fork 裡的要自己刪）：{s.splitlines()[0][:100]}", "manual")

    def build_index(self, pats: list[str]) -> None:
        """fork 這一組檔案的定義：名稱 → [(檔案, 定義)]。"""
        self.index: dict[str, list[tuple[str, Def]]] = {}
        for pat in pats:
            for f in self.fork.files(pat):
                t = self.fork.read(f)
                if t is None:
                    continue
                self.texts[f] = t
                for nm, ds in defs(t).items():
                    self.index.setdefault(nm, []).extend((f, x) for x in ds)

    def where(self, name: str):
        """(檔案, 定義)；找不到是 None，有兩個以上、或和別的名稱寫在同一個指定裡是 "ambiguous"。"""
        hits = self.index.get(RENAMES.get(name, name), [])
        if not hits:
            return None
        return hits[0] if len(hits) == 1 and not hits[0][1].multi else "ambiguous"

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
            at = self.where(anchor)[1].b if anchor else len(self.texts[target].rstrip("\n").split("\n"))
        elif prev:
            anchor = prev[0]
            target, d = self.where(anchor)
            at = d.b
        else:
            self.note("手動", f"{origin}:{name}：上游新增，找不到前面的鄰居", "manual")
            return
        self.edits.setdefault(target, []).append((at, at, "\n\n" + theirs))   # 和前一個定義隔兩個空行
        self.placed.append((name, target))
        self.dest[name], self.code[name], self.pos[name] = target, theirs, at
        self.note("新增", f"{origin}:{name} → {target}（放在{f'{anchor} 後面' if anchor else '檔案最後'}）", "added")

    def changed(self, origin: str, name: str, base: str, theirs: str) -> None:
        self.stats["changed"] += 1
        hit = self.where(name)
        if hit is None:
            self.note("手動", f"{origin}:{name}：fork 找不到這個定義", "manual")
            return
        if hit == "ambiguous":
            self.note("手動", f"{origin}:{name}：fork 有兩個以上同名定義，或和別的名稱寫在同一個指定裡", "manual")
            return
        f, d = hit
        try:
            r = self._merge_def(origin, name, d, base, theirs)
        except MergeFailed as e:
            self.note("手動", f"{origin}:{name}：{e}", "manual")
            return
        if r is None:
            return
        merged, kind, stat = r
        self.edits.setdefault(f, []).append((d.a, d.b, merged))
        self.dest[name], self.code[name], self.pos[name] = f, merged, d.a
        self.note(kind, f"{origin}:{name} → {f}", stat)

    def _merge_def(self, origin: str, name: str, d: Def, base: str, theirs: str) -> tuple[str, str, str] | None:
        """(合併結果, 報告的種類, 統計欄位)；要手動時已經記在報告裡，回傳 None。"""
        if name in VERSIONED and self.upver and self.oldver:
            if VER_MARK not in unver(theirs, self.upver):
                self.note("手動", f"{origin}:{name}：新上游的這個定義裡找不到版號 {self.upver}", "manual")
                return None
            merged, c = self.vmerge(d.src, base, theirs)
            if merged is None:
                self.note("手動", f"{origin}:{name}：{c}", "manual")
                return None
            return (merged, "版號", "version") if c == 0 else (merged, f"衝突 {c} 處", "conflict")
        if norm(d.src) == norm(base):
            return theirs, "直接換", "fast"
        merged, c = merge3(d.src, base, theirs)
        return (merged, "合併", "merged") if c == 0 else (merged, f"衝突 {c} 處", "conflict")

    def removed(self, origin: str, name: str, olds: list[Def]) -> None:
        hit = self.where(name)
        if hit is None:
            return
        self.touched.add((origin, RENAMES.get(name, name)))
        if hit == "ambiguous" or len(olds) > 1 or olds[0].multi:
            self.note("手動", f"{origin}:{name}：上游刪掉了，fork 有兩個以上同名、或和別的名稱寫在同一個指定裡", "manual")
            return
        f, d = hit
        if norm(d.src) == norm(to_fork_style(olds[0].src, origin)):
            self.edits.setdefault(f, []).append((d.a, d.b, ""))
            self.note("刪除", f"{origin}:{name}（{f}）", "removed")
        else:
            self.note("手動", f"{origin}:{name}：上游刪掉了，但 fork 改過（{f}）", "manual")

    def imports(self, origin: str, old_t: str | None, new_t: str | None) -> None:
        """上游新增的 import：放進 IMPORT_TARGET（已經有就略過）；用到它的定義在別的檔案、__future__、或加的位置之前
        就用到的要手動。上游刪掉的 import 列出來（可能有副作用，不自動刪）。"""
        old_k, new_k = import_keys(old_t), import_keys(new_t)
        for k in sorted(old_k - new_k, key=_key_order):
            self.note("手動", f"{origin} 上游刪掉了 import（fork 裡的要自己看要不要刪）：{import_lines({k})[0]}", "manual")
        target = IMPORT_TARGET.get(origin)
        auto, manual = set(), []
        for k in sorted(new_k - old_k, key=_key_order):
            add, why = self.import_plan(k, target)
            if add:
                auto.add(k)
            manual += [(k, w) for w in why]
        for k, why in manual:
            self.note("手動", f"{origin} 新增的 import（{why}）：{import_lines({k})[0]}", "manual")
        if auto:
            lines = import_lines(auto)
            at = last_import_end(self.texts[target])
            self.edits.setdefault(target, []).append((at, at, "\n".join(lines)))
            self.stats["imports"] += len(lines)
            self.note("import", f"{origin} → {target}：{'；'.join(lines)}")

    def import_plan(self, k: tuple, target: str | None) -> tuple[bool, list[str]]:
        """一個上游新增的 import：(要不要自動加進 target, 要手動處理的原因)。"""
        name = bound_name(k)
        if k[1] == "__future__":
            return False, ["__future__ 要放在檔案最前面"]
        if name == "*":   # 第 2 輪審查 6：看不出匯入哪些名稱，就找不到哪些檔案用到
            return False, ["星號 import 看不出哪些檔案用到"]
        users = sorted({f for nm, f in self.dest.items() if re.search(rf"(?<![.\w]){re.escape(name)}\b", self.code[nm])})
        elsewhere = [f for f in users if f != target]
        why = [f"用到它的定義在 {'、'.join(elsewhere)}"] if elsewhere else []
        if target not in self.texts or (users and target not in users):
            return False, why or ["要放進用到的檔案"]
        text = self.texts[target]
        if k in import_keys(text):
            return False, why
        at = last_import_end(text)
        if used_before(text, name, at) or self.merged_uses_before(target, name, at):
            return False, [*why, f"{target} 在 import 的位置之前就用到 {name}"]
        return True, why

    def merged_uses_before(self, target: str, name: str, at: int) -> bool:
        """這次合併進 target、放在 import 位置之前的模組層級定義，載入時有沒有用到 name（uses_at_load）。"""
        for nm, f in self.dest.items():
            if f != target or self.pos[nm] >= at:
                continue
            try:
                node = ast.parse(self.code[nm]).body[0]
            except (SyntaxError, IndexError):   # 有衝突標記：衝突已經列在報告裡
                continue
            if uses_at_load(node, name):
                return True
        return False

    # ---- 其他檔案：整個檔案三方合併
    def file(self, path: str) -> None:
        try:
            self._file(path)
        except MergeFailed as e:
            self.note("手動", f"{path}：{e}（這個檔案沒有寫出）", "manual")

    def _file(self, path: str) -> None:
        try:
            o, n, f = self.old.read(path), self.new.read(path), self.fork.read(path)
        except UnicodeDecodeError:
            self.note("手動", f"{path}：不是文字檔", "manual")
            return
        if path in VERSION_FILES and None not in (o, n, f) and self.upver and self.oldver:
            merged, c = self.vmerge(f.rstrip("\n"), o.rstrip("\n"), n.rstrip("\n"))
            if merged is None:
                self.note("手動", f"{path}：{c}", "manual")
                return
            self.whole[path] = merged + ("\n" if n.endswith("\n") else "")
            self.note("版號" if c == 0 else f"衝突 {c} 處", path, "version" if c == 0 else "conflict")
        elif n is None:
            same = f is not None and o is not None and f == o
            self.note("手動", f"{path}：上游刪掉了" + ("（fork 沒改過，可以刪）" if same else ""), "manual")
        elif o is None:
            if f is None:
                self.whole[path] = n
                self.note("新增", path, "added")
            else:
                self.note("手動", f"{path}：上游新增，但 fork 已經有這個檔案", "manual")
        elif f is None:
            self.note("手動", f"{path}：fork 沒有這個檔案", "manual")
        elif f == o:
            self.whole[path] = n
            self.note("直接換", path, "fast")
        else:
            merged, c = merge3(f, o, n, exact=True)
            self.whole[path] = merged
            self.note("合併" if c == 0 else f"衝突 {c} 處", path, "merged" if c == 0 else "conflict")

    # ---- 寫出與驗證
    def apply(self, f: str, es: list[tuple[int, int, str]]) -> str | None:
        """把一個檔案的編輯套上去。同一個位置的插入照加入的順序合成一塊；替換範圍互相重疊時不套用（回傳 None）。"""
        repl = sorted((e for e in es if e[0] != e[1]), key=lambda e: e[0])
        if any(y[0] < x[1] for x, y in pairwise(repl)):
            return None
        inserts: dict[int, list[str]] = {}
        for a, _, s in (e for e in es if e[0] == e[1]):
            inserts.setdefault(a, []).append(s)
        ops = [(a, b, s, 0) for a, b, s in repl] + [(a, a, "".join(ss), 1) for a, ss in inserts.items()]
        # 從後面往前套；同一個起點先替換、再插入（插入就落在替換後的內容前面，不會被替換範圍蓋到）
        lines = self.texts[f].split("\n")
        for a, b, s, _ in sorted(ops, key=lambda e: (-e[0], e[3])):
            lines[a:b] = s.split("\n") if s else []
        return "\n".join(lines)

    def write(self, out_dir: Path) -> list[str]:
        for f, es in self.edits.items():
            text = self.apply(f, es)
            if text is None:
                self.note("手動", f"{f}：有兩個編輯的範圍重疊，這個檔案沒有寫出", "manual")
                continue
            self.whole[f] = text
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
                    has = self._has_name(text, name)
                except SyntaxError:
                    self.note("手動", f"{f} 用到 {name}（放在 {target}），檔案解析不了，自己確認要不要 import", "manual")
                    continue
                if not has:
                    self.note("手動", f"{f} 用到 {name}（放在 {target}），要加 import", "manual")

    @staticmethod
    def _has_name(text: str, name: str) -> bool:
        """這個檔案自己定義或 import 了 name 嗎。有衝突標記時取 fork 那一側再解析（第 2 輪審查 5：原本整個檔案略過，
        同一個檔案裡別的定義用到新名稱、缺 import 就漏報）。"""
        try:
            ast.parse(text)
        except SyntaxError:
            text = ours_side(text)
        return name in defs(text) or any(bound_name(k) == name for k in import_keys(text))

    def compare(self, expect: Source, out_dir: Path) -> list[str]:
        """依「來源檔案＋名稱」和預期的結果比較（不管放在哪個檔案）：內容不同、或位置不同。
        只比定義的內容和所在檔案，不比定義之間的順序和模組層級敘述（那些要看合併結果本身）。"""
        def got_read(f):
            return (out_dir / f).read_text("utf-8") if (out_dir / f).exists() else self.fork.read(f)
        diffs = []
        for origin in GROUPS:
            got, want = _index_of(got_read, self.fork.files, origin), _index_of(expect.read, expect.files, origin)
            for o, nm in sorted(self.touched):
                if o == origin:
                    d = _diff_one(got.get(nm, []), want.get(nm, []))
                    if d:
                        diffs.append(f"{origin}:{nm}：{d}")
        return diffs


def ours_side(text: str) -> str:
    """衝突的地方只留 fork 那一側（merge3 固定用 merge 樣式的衝突標記）。"""
    return re.sub(r"<<<<<<< fork\n(.*?)=======\n.*?>>>>>>> 新上游\n?", r"\1", text, flags=re.S)


def _index_of(read, files_of, origin: str) -> dict[str, list[tuple[str, str]]]:
    """這一組檔案的定義：名稱 → [(檔案, 原始碼)]（--expect 用）。"""
    idx: dict[str, list[tuple[str, str]]] = {}
    for pat in GROUPS[origin]:
        for f in files_of(pat):
            t = read(f)
            if t is None:
                continue
            # 有衝突的地方先取 fork 那一側才能解析；衝突本身已經列在報告裡
            for nm, ds in defs(ours_side(t.replace("\r\n", "\n"))).items():
                idx.setdefault(nm, []).extend((f, x.src) for x in ds)
    return idx


def _diff_one(g: list[tuple[str, str]], w: list[tuple[str, str]]) -> str | None:
    if not g and not w:
        return None   # 兩邊都刪掉了
    if len(g) > 1 or len(w) > 1:
        return "有兩個以上同名定義，沒辦法比"
    if not g or not w:
        return f"{'合併結果' if not g else '預期'}沒有這個定義"
    if norm(g[0][1]) != norm(w[0][1]):
        return f"內容不同（{g[0][0]}）"
    if g[0][0] != w[0][0]:
        return f"位置不同（合併放 {g[0][0]}，預期在 {w[0][0]}）"
    return None


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
    for ref in [*args[:2], *([] if "tree" in opt else [args[2]]), *([opt["expect"]] if "expect" in opt else [])]:
        check_ref(ref)
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
