r"""把 pipeline.py 裡一段連續的最上層定義搬到 leihu/core/<名稱>.py（docs/pipeline-split-plan.md 方式一）。

從 <第一個名稱> 的開頭（含裝飾器）到 <最後一個名稱> 的結尾，中間的註解、空行、其他模組層級敘述一起搬，原樣不改；
pipeline.py 原本的位置換成一行 `_load_part("<名稱>")`，載入時就在原來的位置執行。分出去的檔案開頭是註解
（不能是字串：exec 時會蓋掉 pipeline 的 __doc__）。換行（CRLF／LF）照 pipeline.py。
寫成腳本是為了能重跑：同一個搬移可以套到舊的 commit 上，做模擬同步（計畫第 7 節）。

用法（repo 根目錄，或 --root 指定的資料夾）：
  python tests/tools/split_moves.py <檔名，例如 jobs> <第一個名稱> <最後一個名稱> [--root=<資料夾>]
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import defmerge as D

HEADER = ("# 從 pipeline.py 搬出（tests/tools/split_moves.py，docs/pipeline-split-plan.md 方式一）。\n"
          "# pipeline._load_part 把這個檔案載入 pipeline 的命名空間，等於這段程式寫在 pipeline.py 裡；不能單獨 import。\n")


class MoveError(Exception):
    pass


def _one(ds: dict, name: str):
    got = ds.get(name)
    if not got:
        raise MoveError(f"pipeline.py 裡找不到 {name}")
    if len(got) > 1 or got[0].multi:
        raise MoveError(f"{name} 有兩個以上同名定義，或和別的名稱寫在同一個指定裡")
    return got[0]


def move(text: str, part: str, first: str, last: str) -> tuple[str, str]:
    """回傳 (新的 pipeline.py, 分出去的檔案)。text 是 LF。"""
    if not part.isidentifier():
        raise MoveError(f"檔名要是識別字：{part}")
    ds = D.defs(text)
    a, b = _one(ds, first).a, _one(ds, last).b
    if b <= a:
        raise MoveError(f"{last} 在 {first} 前面")
    lines = text.split("\n")
    body = ast.parse(text).body
    # 範圍的兩端要剛好是敘述的邊界（不能切在某個敘述的中間）
    if any(n.lineno - 1 < a < n.end_lineno or n.lineno - 1 < b < n.end_lineno for n in body):
        raise MoveError("範圍切到敘述的中間")
    block = "\n".join(lines[a:b])
    new = "\n".join([*lines[:a], f'_load_part("{part}")', *lines[b:]])
    out = HEADER + "\n\n" + block + "\n"
    ast.parse(new), ast.parse(out)   # 兩邊都要能解析
    return new, out


def run(root: Path, part: str, first: str, last: str) -> Path:
    src = root / "pipeline.py"
    raw = src.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in raw else "\n"
    dest = root / "leihu" / "core" / f"{part}.py"
    if dest.exists():
        raise MoveError(f"{dest} 已經存在")
    new, out = move(raw.replace("\r\n", "\n"), part, first, last)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(out.replace("\n", eol).encode("utf-8"))
    src.write_bytes(new.replace("\n", eol).encode("utf-8"))
    return dest


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="split_moves.py", description=__doc__, allow_abbrev=False,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("part", help="分出去的檔名（leihu/core/<名稱>.py）")
    p.add_argument("first", help="第一個搬走的最上層名稱")
    p.add_argument("last", help="最後一個搬走的最上層名稱")
    p.add_argument("--root", default=".", help="repo 根目錄（預設目前的資料夾）")
    a = p.parse_args()
    try:
        dest = run(Path(a.root), a.part, a.first, a.last)
    except MoveError as e:
        print(f"沒有搬：{e}")
        return 1
    print(f"已搬到 {dest}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
