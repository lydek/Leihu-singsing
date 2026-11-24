"""拆 pipeline.py 的載入機制與檢查（docs/pipeline-split-plan.md 第 3、5、7 節，步驟 0）。"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "tools"))
import defmerge as D  # noqa: E402
import split_check as C  # noqa: E402
import split_moves as M  # noqa: E402


def _text(p: Path) -> str:
    return p.read_bytes().decode("utf-8").replace("\r\n", "\n")


# ---------------------------------------------------------------- 載入機制


def test_load_part_shares_pipeline_namespace(app_module, tmp_path, monkeypatch):
    """載入的檔案等於寫在 pipeline.py 裡：同一個命名空間、替換 P.<名稱> 有效、型別註記不在載入時執行、
    traceback 指到分出去的檔案、不動 pipeline 的 __doc__。"""
    import pipeline as P

    (tmp_path / "probe.py").write_text(
        "# 說明用註解\n"
        "PROBE_X = _probe_helper()\n\n\n"
        "def probe_f() -> NotDefinedAnywhere:\n"   # 沒有 __future__ annotations 的話，定義時就 NameError
        "    return PROBE_X + _probe_helper()\n\n\n"
        "def probe_boom():\n"
        "    raise RuntimeError('probe')\n", "utf-8")
    monkeypatch.setattr(P, "PARTS_DIR", tmp_path)
    monkeypatch.setattr(P, "_PARTS", [])
    monkeypatch.setattr(P, "_probe_helper", lambda: 1, raising=False)
    doc = P.__doc__
    try:
        P._load_part("probe")
        assert P._PARTS == ["probe"] and P.PROBE_X == 1
        assert P.probe_f.__module__ == "pipeline"
        monkeypatch.setattr(P, "_probe_helper", lambda: 10)
        assert P.probe_f() == 11
        assert P.__doc__ == doc
        with pytest.raises(RuntimeError) as e:
            P.probe_boom()
        assert Path(e.traceback[-1].path).name == "probe.py"
    finally:
        for n in ("PROBE_X", "probe_f", "probe_boom"):
            P.__dict__.pop(n, None)


def test_part_header_is_a_comment():
    """分出去的檔案開頭如果是字串，exec 時會蓋掉 pipeline 的 __doc__，所以 split_moves 寫的是註解。"""
    g = {"__doc__": "pipeline"}
    exec(compile('"""part"""\nX = 1\n', "part.py", "exec"), g)  # noqa: S102
    assert g["__doc__"] == "part"
    assert all(line.startswith("#") for line in M.HEADER.splitlines())


def test_loaded_parts_match_pipeline_calls(app_module):
    import pipeline as P

    assert C.part_names(_text(ROOT / "pipeline.py")) == P._PARTS


# ---------------------------------------------------------------- 名稱檢查


def test_unresolved_finds_missing_names_and_typos():
    src = ("X = helper()\n\n\n"
           "def f(a):\n    result = a\n    return reslut + len(a) + Y + X\n\n\n"
           "def g():\n    v = 1\n\n    def h():\n        return v\n    return h\n\n\n"
           "class Cls:\n    Z = W\n\n    def m(self):\n        return [q for q in range(3)] + other\n")
    assert C.unresolved(src, "part.py", {"helper", "Y"}) == ["W", "other", "reslut"]


def test_parts_resolve_all_names(app_module):
    """每個分出去的檔案，每個作用域用到的全域名稱都在 pipeline 的命名空間或 builtins 裡（取代 ruff 的 F821）。"""
    import pipeline as P

    for name in P._PARTS:
        p = P.PARTS_DIR / f"{name}.py"
        assert C.unresolved(_text(p), str(p), set(vars(P))) == [], name


def test_pipeline_names_complete(app_module):
    """pipeline.py 和分出去的檔案的每個最上層名稱，P.<名稱> 都拿得到；分出去的函式、類別 __module__ 仍是 pipeline；
    同一個名稱不會同時定義在兩個檔案。"""
    import pipeline as P

    files = {"pipeline.py": _text(ROOT / "pipeline.py")}
    files |= {f"leihu/core/{n}.py": _text(P.PARTS_DIR / f"{n}.py") for n in P._PARTS}
    seen: dict[str, str] = {}
    for f, text in files.items():
        for nm in D.defs(text):
            assert nm not in seen or seen[nm] == f, f"{nm} 同時在 {seen.get(nm)} 和 {f}"
            seen[nm] = f
            assert hasattr(P, nm), f"P.{nm}（{f}）"
            if f != "pipeline.py" and isinstance(getattr(P, nm), type(test_pipeline_names_complete) | type):
                assert getattr(P, nm).__module__ == "pipeline", nm


# ---------------------------------------------------------------- 搬移腳本與 AST 比對

SAMPLE = ('"""模組說明。"""\nimport os\n\n# ---- 區塊\n\n'
          "def a():\n    return 1\n\n\n"
          "@staticmethod\ndef b():\n    return 2\n\n\n"
          "try:\n    a()\nexcept Exception:\n    pass\n\n\n"
          "def c():\n    return b()\n\n\n"
          "def d():\n    return 4\n")


def test_move_cuts_contiguous_block():
    new, part = M.move(SAMPLE, "x", "b", "c")
    assert '_load_part("x")' in new and "def b" not in new and "def a" in new and "def d" in new
    assert new.index('_load_part("x")') < new.index("def d")
    block = SAMPLE[SAMPLE.index("@staticmethod"):SAMPLE.index("    return b()") + len("    return b()")]
    assert part == M.HEADER + "\n\n" + block + "\n"
    assert C.compare(SAMPLE, {"pipeline.py": new, "leihu/core/x.py": part}, {"leihu/core/x.py"}) == []


@pytest.mark.parametrize(("args", "msg"), [
    (("x", "nope", "c"), "找不到"), (("x", "c", "b"), "前面"), (("x-y", "b", "c"), "識別字")])
def test_move_rejects_bad_ranges(args, msg):
    with pytest.raises(M.MoveError, match=msg):
        M.move(SAMPLE, *args)


def test_compare_reports_changed_and_missing():
    new, part = M.move(SAMPLE, "x", "b", "c")
    assert C.compare(SAMPLE, {"pipeline.py": new, "leihu/core/x.py": part.replace("return 2", "return 3")},
                     {"leihu/core/x.py"}) == ["b：內容和搬移前不同（leihu/core/x.py）"]
    assert C.compare(SAMPLE, {"pipeline.py": new}, set()) == ["b：搬移後找不到", "c：搬移後找不到"]
    # 方式二的模組只比 AST：註解、排版不同不算
    mod = "def b():   # 方式二\n    return (2)\n"
    assert C.compare("def b():\n    return 2\n", {"pipeline.py": "", "leihu/m.py": mod}, set()) == []


def test_move_and_load_real_pipeline(tmp_path):
    """在 pipeline.py 的副本上真的搬一段（分類：會讀執行中重新指派的 LIB），import 起來、名稱檢查、AST 比對都要對。"""
    shutil.copy2(ROOT / "pipeline.py", tmp_path / "pipeline.py")
    shutil.copytree(ROOT / "leihu", tmp_path / "leihu", ignore=shutil.ignore_patterns("__pycache__"))
    base = _text(ROOT / "pipeline.py")
    dest = M.run(tmp_path, "categories", "_cats_path", "save_categories")
    assert dest == tmp_path / "leihu" / "core" / "categories.py"
    files = C.tree_files(tmp_path)
    assert C.compare(base, files, {"leihu/core/categories.py"}) == []
    code = ("import pipeline as P, split_check as C\n"
            "from pathlib import Path\n"
            "assert P._PARTS == ['categories'], P._PARTS\n"
            "assert P._cats_path.__module__ == 'pipeline'\n"
            "P.LIB = Path('elsewhere')\n"
            "assert P._cats_path() == Path('elsewhere') / 'categories.json'\n"
            "p = P.PARTS_DIR / 'categories.py'\n"
            "assert C.unresolved(p.read_text('utf-8'), str(p), set(vars(P))) == []\n"
            "print('ok')\n")
    env = dict(os.environ, LIANTAI_DATA=str(tmp_path / "data"), PYTHONPATH=str(ROOT / "tests" / "tools"))
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True)
    assert r.returncode == 0 and r.stdout.decode().strip() == "ok", r.stderr.decode("utf-8", "replace")[-800:]
    ast.parse(dest.read_text("utf-8"))


def test_installer_packs_parts():
    """安裝檔要帶 leihu/core/（方式一的檔案不在就啟動失敗）；leihu.nsi 用 File /r 整個 payload，不用改。"""
    build = (ROOT / "installer" / "build.sh").read_text("utf-8")
    assert "cp ../leihu/core/*.py payload/leihu/core/" in build
