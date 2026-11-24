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

ROOT = Path(__file__).resolve().parents[2]
LOAD_RX = re.compile(r'(?m)^_load_part\("(\w+)"\)\s*$')


def part_names(pipeline_text: str) -> list[str]:
    """pipeline.py 依序載入的方式一檔案（模組層級的 _load_part("名稱") 呼叫）。"""
    return LOAD_RX.findall(pipeline_text)


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


def compare(base: str, files: dict[str, str], parts: set[str]) -> list[str]:
    """base：搬移前的 pipeline.py；files：搬移後的 pipeline.py、leihu/*.py、leihu/core/*.py（路徑 → 內容）；
    parts：方式一的檔案路徑。回傳問題清單（空的就是都對）。"""
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
    base = subprocess.run(["git", "show", f"{sys.argv[1]}:pipeline.py"], capture_output=True, check=True,
                          cwd=ROOT).stdout.decode("utf-8").replace("\r\n", "\n")
    files = tree_files(ROOT)
    parts = {f"leihu/core/{n}.py" for n in part_names(files["pipeline.py"])}
    problems = compare(base, files, parts)
    print("\n".join(problems) or f"{len(D.defs(base))} 個定義都在，內容和搬移前相同")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
