"""tests/tools/defmerge.py：以最上層定義為單位三方合併上游的改動（docs/upstream-sync-research.md 方向 A）。

舊上游、新上游、fork 都用暫存資料夾（Source(tree=...)），不需要 git 參照。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import defmerge as D

OLD = '''"""上游 pipeline。"""
import os

X = 1


def a():
    x = 1
    y = 2
    z = 3
    return x + y + z


def b():
    return 2
'''


def tree(root: Path, files: dict[str, str], crlf: bool = False) -> D.Source:
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    return D.Source(tree=root)


def merge(tmp_path, old: dict, new: dict, fork: dict, crlf: bool = False, extra=None):
    r = D.run(tree(tmp_path / "old", old), tree(tmp_path / "new", new), tree(tmp_path / "fork", fork, crlf),
              tmp_path / "out", extra_files=extra)
    return r, (lambda name: (tmp_path / "out" / name).read_bytes().decode("utf-8"))


def test_fork_unchanged_definition_is_replaced(tmp_path):
    new = OLD.replace("return 2", "return 22")
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": OLD})
    assert r["stats"]["fast"] == 1 and r["stats"]["conflict"] == 0
    assert out("pipeline.py") == new


def test_three_way_keeps_both_sides(tmp_path):
    """fork 改函式開頭、上游改結尾：兩邊都留下，沒有衝突標記。"""
    fork = OLD.replace("    x = 1\n", "    x = 10   # fork\n")
    new = OLD.replace("    return x + y + z", "    return x * y * z")
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": fork})
    assert r["stats"]["merged"] == 1
    got = out("pipeline.py")
    assert "x = 10   # fork" in got and "return x * y * z" in got and "<<<<<<<" not in got


def test_same_lines_conflict(tmp_path):
    fork = OLD.replace("    y = 2\n", "    y = 20\n")
    new = OLD.replace("    y = 2\n", "    y = 200\n")
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": fork})
    assert r["stats"]["conflict"] == 1
    got = out("pipeline.py")
    assert "<<<<<<< fork" in got and "y = 20\n" in got and "y = 200\n" in got and ">>>>>>> 新上游" in got


def test_comment_only_fork_change_is_not_overwritten(tmp_path):
    """fork 只補了註解也算改過：要三方合併，不能被上游直接蓋掉（原型第一版的錯誤）。"""
    fork = OLD.replace("def a():\n", "def a():\n    # fork 的說明\n")
    new = OLD.replace("    return x + y + z", "    return x * y * z")
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": fork})
    assert r["stats"]["merged"] == 1
    got = out("pipeline.py")
    assert "# fork 的說明" in got and "return x * y * z" in got


def test_adjacent_lines_conflict_like_git(tmp_path):
    """git merge-file 把相鄰的改動也當成衝突（和 git 合併相同）：fork 在 def 下一行加註解、上游改再下一行。"""
    fork = OLD.replace("def b():\n", "def b():\n    # fork\n")
    new = OLD.replace("return 2", "return 22")
    r, _ = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": fork})
    assert r["stats"]["conflict"] == 1


def test_definition_moved_to_another_file(tmp_path):
    """fork 把 b 搬到 leihu/core/b.py：照樣在那個檔案合併。"""
    fork_main = OLD.replace("\n\ndef b():\n    return 2\n", "\n")
    new = OLD.replace("return 2", "return 22")
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new},
                   {"pipeline.py": fork_main, "leihu/core/b.py": "def b():\n    return 2\n"})
    assert r["files"] == ["leihu/core/b.py"]
    assert "return 22" in out("leihu/core/b.py")


def test_new_definition_goes_after_previous_neighbor(tmp_path):
    new = OLD + "\n\ndef c():\n    return 3\n"
    fork_main = OLD.replace("\n\ndef b():\n    return 2\n", "\n")
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new},
                   {"pipeline.py": fork_main, "leihu/core/b.py": "def b():\n    return 2\n\n\ndef d():\n    pass\n"})
    assert r["stats"]["added"] == 1
    assert out("leihu/core/b.py") == "def b():\n    return 2\n\n\ndef c():\n    return 3\n\n\ndef d():\n    pass\n"


def test_place_overrides_neighbor(tmp_path, monkeypatch):
    """PLACE 登記的新定義放指定的檔案：有鄰居在那個檔案就接在後面，沒有就放檔案最後。"""
    monkeypatch.setattr(D, "PLACE", {"c": "leihu/common.py", "re:helper_.*": "leihu/common.py"})
    new = OLD + "\n\ndef c():\n    return 3\n\n\ndef helper_x():\n    pass\n"
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new},
                   {"pipeline.py": OLD, "leihu/common.py": "def shared():\n    pass\n"})
    assert r["stats"]["added"] == 2
    got = out("leihu/common.py")
    assert got.index("def shared") < got.index("def c") and "def helper_x" in got
    assert r["files"] == ["leihu/common.py"]   # 沒有放進 pipeline.py


def test_new_definition_used_from_other_file_needs_import(tmp_path, monkeypatch):
    """2.3.5 的 submit_unfinished：放進 common.py，上游也改了用到它的路由，那個檔案要 import（列為手動）。"""
    monkeypatch.setattr(D, "PLACE", {"helper": "leihu/web/common.py"})
    old = '@app.get("/a")\ndef a():\n    x = 1\n    return x\n'
    new = 'def helper():\n    return 1\n\n\n' + old.replace("return x", "return helper()")
    r, _ = merge(tmp_path, {"app.py": old}, {"app.py": new},
                 {"app.py": "", "leihu/web/common.py": "X = 1\n", "leihu/web/x.py": old.replace("@app.", "@bp."),
                  "leihu/web/y.py": "import pipeline as P\n\n\ndef y():\n    return P.helper\n"})
    assert any(line.startswith("手動") and "leihu/web/x.py 用到 helper" in line for line in r["report"])
    assert not any("leihu/web/y.py" in line for line in r["report"])   # P.helper 不用 import


def test_removed_definition(tmp_path):
    new = OLD.replace("\n\ndef b():\n    return 2\n", "\n")
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": OLD})
    assert r["stats"]["removed"] == 1 and "def b" not in out("pipeline.py")
    fork = OLD.replace("return 2", "return 2  # fork 改過")
    r, _ = merge(tmp_path / "2", {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": fork})
    assert r["stats"]["removed"] == 0 and r["stats"]["manual"] == 1


def test_renamed_definition(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "RENAMES", {"a": "fork_a"})
    fork = OLD.replace("def a():", "def fork_a():")
    new = OLD.replace("    return x + y + z", "    return x * y * z")
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": fork})
    assert r["stats"]["merged"] == 1   # 名稱那一行 fork 改了、內容上游改了：三方合併
    got = out("pipeline.py")
    assert "def fork_a():" in got and "return x * y * z" in got and "def a():" not in got


def test_duplicate_names_are_manual(tmp_path):
    new = OLD.replace("return 2", "return 22")
    r, _ = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new},
                 {"pipeline.py": OLD, "leihu/other.py": "def b():\n    return 2\n"})
    assert r["stats"]["manual"] == 1 and r["files"] == []


def test_crlf_fork_keeps_line_endings(tmp_path):
    """2.3.5 的 upgrade_separation：工作目錄是 CRLF、patch 是 LF 就套不上；定義合併照常，寫出保留 CRLF。"""
    fork = OLD.replace("    x = 1\n", "    x = 10\n")
    new = OLD.replace("    return x + y + z", "    return x * y * z")
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": fork}, crlf=True)
    assert r["stats"]["merged"] == 1
    got = out("pipeline.py")
    assert "\r\n" in got and "\n" not in got.replace("\r\n", "")
    assert "x = 10\r\n" in got and "return x * y * z" in got


def test_version_rule(tmp_path):
    """3.15：版號一律是「新上游版本＋.1」，版號本身不衝突；fork 在同一行的註解、網址（F1）照樣留著。"""
    old = OLD + '\n\nVERSION = "2.3.4"\nUSER_AGENT = "LeihuLiantai/2.3.4 (+https://github.com/LeihuTW/x)"\n'
    new = old.replace("2.3.4", "2.3.5")
    fork = (old.replace('"2.3.4"', '"2.3.4.2"   # fork 序號').replace("/2.3.4", "/2.3.4.2")
            .replace("LeihuTW", "lydek"))
    r, out = merge(tmp_path, {"pipeline.py": old, "VERSION": "2.3.4\n"}, {"pipeline.py": new, "VERSION": "2.3.5\n"},
                   {"pipeline.py": fork, "VERSION": "2.3.4.2\n"}, extra=["VERSION"])
    assert r["stats"]["version"] == 3 and r["stats"]["conflict"] == 0
    got = out("pipeline.py")
    assert 'VERSION = "2.3.5.1"   # fork 序號' in got
    assert '"LeihuLiantai/2.3.5.1 (+https://github.com/lydek/x)"' in got
    assert out("VERSION") == "2.3.5.1\n"
    assert D.unver("2.3.50 2.3.5 2.3.5.2", "2.3.5") == f"2.3.50 {D.VER_MARK} {D.VER_MARK}"


def test_new_imports(tmp_path):
    """上游新增的 import：pipeline.py 已經有就略過，沒有就加在最後一個 import 後面；app.py 要手動。"""
    new = OLD.replace("import os\n", "import os\nimport re\nimport subprocess\n")
    fork = OLD.replace("import os\n", "import os\nimport subprocess\n")
    r, out = merge(tmp_path, {"pipeline.py": OLD, "app.py": "import os\n"},
                   {"pipeline.py": new, "app.py": "import os\nimport json\n"},
                   {"pipeline.py": fork, "app.py": "import os\n"})
    got = out("pipeline.py")
    assert got.count("import subprocess") == 1 and "import subprocess\nimport re\n" in got
    assert r["stats"]["imports"] == 1
    assert any("app.py 新增的 import" in x and "import json" in x for x in r["report"])
    assert D.import_lines({("from", "a", "x", None), ("from", "a", "y", "z")}) == ["from a import x, y as z"]


def test_app_routes_use_blueprint_style(tmp_path):
    """上游 app.py 的 @app.get 換成 fork 的 @bp.get 再比較：fork 沒改過的路由直接換。"""
    old = '@app.get("/a")\ndef a():\n    return 1\n'
    new = old.replace("return 1", "return 2")
    r, out = merge(tmp_path, {"app.py": old}, {"app.py": new},
                   {"app.py": "", "leihu/web/x.py": old.replace("@app.", "@bp.")})
    assert r["stats"]["fast"] == 1
    assert out("leihu/web/x.py") == '@bp.get("/a")\ndef a():\n    return 2\n'


def test_other_files_whole_file_merge(tmp_path):
    old = "a\nb\nc\nd\ne\n"
    files = {"gpu_worker.py": old, "same.py": old}
    r, out = merge(tmp_path, files, {"gpu_worker.py": old.replace("e\n", "E\n"), "same.py": old.replace("a\n", "A\n")},
                   {"gpu_worker.py": old.replace("a\n", "A\n"), "same.py": old}, extra=["gpu_worker.py", "same.py"])
    assert out("gpu_worker.py") == "A\nb\nc\nd\nE\n"   # 兩邊都留
    assert out("same.py") == "A\nb\nc\nd\ne\n"         # fork 沒改：直接換
    assert r["stats"]["merged"] == 1 and r["stats"]["fast"] == 1


@pytest.mark.parametrize(("old", "new", "fork", "want"), [
    (None, "x\n", None, "新增"),
    (None, "x\n", "y\n", "手動"),
    ("x\n", None, "x\n", "手動"),
    ("x\n", "y\n", None, "手動"),
])
def test_other_files_added_or_removed(tmp_path, old, new, fork, want):
    def files(t):
        return {} if t is None else {"f.txt": t}
    r, _ = merge(tmp_path, files(old), files(new), files(fork), extra=["f.txt"])
    assert r["report"][-1].startswith(want)
