r"""拆 pipeline.py 的檢查（docs/pipeline-split-plan.md 第 3、7 節）。

- unresolved()：分出去的檔案（方式一，載入 pipeline 的命名空間）裡，每個作用域用到的全域名稱都要找得到。
  ruff 對這些檔案排除 F821，改用 symtable 逐層檢查：跨檔漏掉的名稱和函式裡的筆誤（例如 reslut）都抓得到。
- compare()：搬移前（git 參照）pipeline.py 的每個最上層定義，搬移後都還在。在 pipeline.py 或方式一的檔案裡，
  內容要逐字相同（只去掉頭尾空行，和 defmerge 的 norm 相同）；在別的 leihu 模組（方式二）裡，AST 要相同。

用法（repo 根目錄）：
  python tests/tools/split_check.py <搬移前的 git 參照>
結束代碼：有定義不見或內容不同是 1，否則 0。
"""
from __future__ import annotations

import ast
import builtins
import re
import subprocess
import symtable
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import defmerge as D
import split_moves as M

ROOT = Path(__file__).resolve().parents[2]


def part_names(pipeline_text: str) -> list[str]:
    """pipeline.py 依序載入的方式一檔案（模組層級的 _load_part("名稱") 呼叫，和 defmerge 用同一個規則）。"""
    return D.LOAD_RX.findall(pipeline_text)


def reassemble(pipeline_text: str, files: dict[str, str]) -> str:
    """把 pipeline.py 裡的每一行 _load_part("名稱") 換回那個檔案的內容（去掉 split_moves 加的檔頭）。"""
    def body(m: re.Match) -> str:
        text = files.get(f"leihu/core/{m.group(1)}.py", "")
        head = M.HEADER + "\n\n"
        return (text[len(head):] if text.startswith(head) else text).rstrip("\n")
    return D.LOAD_RX.sub(body, pipeline_text)


def unresolved(src: str, filename: str, known: set[str]) -> list[str]:
    """src 裡每個作用域參照到、卻不是在那個作用域或外層函式綁定的名稱中，known、builtins、src 自己模組層級都沒有的。"""
    top = symtable.symtable(src, filename, "exec")
    defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
    ok = known | defined | set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__builtins__"}
    missing: set[str] = set()

    def walk(t: symtable.SymbolTable) -> None:
        for s in t.get_symbols():
            if not s.is_referenced():
                continue
            if t.get_type() == "module":
                if not (s.is_assigned() or s.is_imported()) and s.get_name() not in ok:
                    missing.add(s.get_name())
            elif s.is_global() and s.get_name() not in ok:
                missing.add(s.get_name())
        for c in t.get_children():
            walk(c)

    walk(top)
    return sorted(missing)


def compare(base: str, files: dict[str, str], parts: set[str], base_files: dict[str, str] | None = None) -> list[str]:
    """base：搬移前的 pipeline.py；base_files：搬移前已經分出去的方式一檔案（路徑 → 內容）；files：搬移後的
    pipeline.py、leihu/*.py、leihu/core/*.py；parts：搬移後方式一的檔案路徑。回傳問題清單（空的就是都對）。
    搬移前的 pipeline.py 先把它自己的 _load_part 換回當時的檔案內容，之前的步驟搬走的定義也一起比。
    每個定義都還在 pipeline.py 或方式一的檔案時（這一步沒有方式二的搬移），另外把搬移後的 _load_part 也換回內容，
    整份和搬移前逐字比對：定義以外的模組層級敘述（try、if、運算式）和順序也要相同（Agy 審查 1）。"""
    base = reassemble(base, base_files or {})
    problems = _compare_defs(base, files, parts)
    only_method1 = all(f == "pipeline.py" or f in parts for f in _homes(base, files))
    if not problems and only_method1 and D.norm(reassemble(files["pipeline.py"], files)) != D.norm(base):
        problems.append("把 _load_part 換回原本的內容後，整份和搬移前不同（定義以外的敘述或順序變了）")
    return problems


def _homes(base: str, files: dict[str, str]) -> set[str]:
    """搬移前的定義現在在哪些檔案。"""
    names = set(D.defs(base))
    return {f for f, text in files.items() if names & set(D.defs(text))}


def _compare_defs(base: str, files: dict[str, str], parts: set[str]) -> list[str]:
    where: dict[str, list[tuple[str, str]]] = {}
    for f, text in files.items():
        for nm, ds in D.defs(text).items():
            where.setdefault(nm, []).extend((f, x.src) for x in ds)
    problems = []
    for nm, ds in D.defs(base).items():
        got = where.get(nm, [])
        if len(ds) > 1 or len(got) > 1:
            if [D.norm(x.src) for x in ds] != [D.norm(s) for _, s in got]:
                problems.append(f"{nm}：有兩個以上同名定義，內容或數目和搬移前不同")
            continue
        if not got:
            problems.append(f"{nm}：搬移後找不到")
            continue
        f, src = got[0]
        if f == "pipeline.py" or f in parts:
            if D.norm(src) != D.norm(ds[0].src):
                problems.append(f"{nm}：內容和搬移前不同（{f}）")
        elif ast.dump(ast.parse(src)) != ast.dump(ast.parse(ds[0].src)):
            problems.append(f"{nm}：AST 和搬移前不同（{f}）")
    return problems


def tree_files(root: Path) -> dict[str, str]:
    out = {"pipeline.py": (root / "pipeline.py").read_bytes().decode("utf-8").replace("\r\n", "\n")}
    for pat in ("leihu/*.py", "leihu/core/*.py"):
        for p in sorted(root.glob(pat)):
            out[p.relative_to(root).as_posix()] = p.read_bytes().decode("utf-8").replace("\r\n", "\n")
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    def show(path: str) -> str:
        return subprocess.run(["git", "show", f"{sys.argv[1]}:{path}"], capture_output=True, check=True,
                              cwd=ROOT).stdout.decode("utf-8").replace("\r\n", "\n")
    base = show("pipeline.py")
    base_files = {f"leihu/core/{n}.py": show(f"leihu/core/{n}.py") for n in part_names(base)}
    files = tree_files(ROOT)
    parts = {f"leihu/core/{n}.py" for n in part_names(files["pipeline.py"])}
    problems = compare(base, files, parts, base_files)
    print("\n".join(problems) or f"{len(D.defs(reassemble(base, base_files)))} 個定義都在，內容和搬移前相同")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
