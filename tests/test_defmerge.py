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


# ---------------------------------------------------------------- Codex 審查（2026-11-24）r3


def test_insert_and_replace_at_same_point(tmp_path):
    """r3-1：a() 後面緊接 b()（沒有空行），上游在中間新增 c()、同時改 b()：原本留下舊版的 b() 蓋掉新版。"""
    old = "def a():\n    return 1\ndef b():\n    return 2\n"
    new = "def a():\n    return 1\n\n\ndef c():\n    return 3\ndef b():\n    return 22\n"
    _, out = merge(tmp_path, {"pipeline.py": old}, {"pipeline.py": new}, {"pipeline.py": old})
    got = out("pipeline.py")
    assert got.count("def b") == 1 and "return 22" in got and "return 2\n" not in got
    assert got.index("def a") < got.index("def c") < got.index("def b")


def test_consecutive_new_definitions_keep_order(tmp_path):
    """r3-2：連續新增的 A = 10、B = A + 1 原本反序，變成先用到 A。"""
    new = OLD + "\n\nA = 10\nB = A + 1\n"
    _, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": OLD})
    got = out("pipeline.py")
    assert got.index("A = 10") < got.index("B = A + 1")


def test_chained_assignment_is_manual(tmp_path):
    """r3-3：A = B = 1 原本被當成兩個定義、同一個範圍編輯兩次，甚至整行刪掉。"""
    old = OLD + "\n\nA = B = 1\n"
    new = OLD + "\n\nB = 2\n"
    r, _ = merge(tmp_path, {"pipeline.py": old}, {"pipeline.py": new}, {"pipeline.py": old})
    assert r["stats"]["manual"] >= 1 and "pipeline.py" not in r["files"]


@pytest.mark.parametrize("stmt", ["A, B = {}, 2", "CONFIG['x'] = {}"])
def test_unsupported_assignments_are_reported(tmp_path, stmt):
    """r3-4：解包指定、CONFIG["x"] = 兩邊都被忽略，上游改了卻回報零變更。"""
    old = OLD + "\n\nCONFIG = {}\n" + stmt.format(1) + "\n"
    new = old.replace(stmt.format(1), stmt.format(10))
    r, _ = merge(tmp_path, {"pipeline.py": old}, {"pipeline.py": new}, {"pipeline.py": old})
    assert any("模組層級" in x for x in r["report"]) and r["stats"]["manual"] >= 1


def test_same_file_duplicate_names_are_manual(tmp_path):
    """r3-5：同一個檔案 X = 1、SNAPSHOT = X、X = 2，上游只改第一個 X：原本改動直接消失。"""
    old = OLD + "\n\nX = 1\nSNAPSHOT = X\nX = 2\n"
    new = old.replace("X = 1\n", "X = 100\n")
    r, _ = merge(tmp_path, {"pipeline.py": old}, {"pipeline.py": new}, {"pipeline.py": old})
    assert r["stats"]["manual"] >= 1 and "pipeline.py" not in r["files"]


def test_removed_with_duplicate_in_fork_is_manual(tmp_path):
    """r3-6：上游刪除 b()，fork 兩個檔案都有 b()：原本靜默略過。"""
    new = OLD.replace("\n\ndef b():\n    return 2\n", "\n")
    r, _ = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new},
                 {"pipeline.py": OLD, "leihu/other.py": "def b():\n    return 2\n"})
    assert r["stats"]["manual"] == 1 and r["files"] == []


def test_removed_module_statement_and_import_are_reported(tmp_path):
    """r3-7：上游刪除 start_worker()、刪除 import：原本不報告，fork 照樣保留並執行。"""
    old = OLD.replace("import os\n", "import os\nimport side_effect\n") + "\nstart_worker()\n"
    r, _ = merge(tmp_path, {"pipeline.py": old}, {"pipeline.py": OLD}, {"pipeline.py": old})
    rep = "\n".join(r["report"])
    assert "模組層級刪除" in rep and "start_worker" in rep and "import side_effect" in rep


def test_import_for_moved_definition_is_manual(tmp_path):
    """r3-8：b() 搬到 leihu/core/b.py，上游新增 import re 並讓 b() 用 re：原本把 import 加在 pipeline.py。"""
    new = OLD.replace("import os\n", "import os\nimport re\n").replace("    return 2\n", "    return re.compile('x')\n")
    fork_main = OLD.replace("\n\ndef b():\n    return 2\n", "\n")
    r, _ = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new},
                 {"pipeline.py": fork_main, "leihu/core/b.py": "def b():\n    return 2\n"})
    assert any("leihu/core/b.py" in x and "import re" in x for x in r["report"] if x.startswith("手動"))
    assert "pipeline.py" not in r["files"]


def test_future_import_is_manual(tmp_path):
    """r3-9：上游新增 from __future__ import annotations，放在一般 import 後面是 SyntaxError。"""
    new = OLD.replace('"""上游 pipeline。"""\n', '"""上游 pipeline。"""\nfrom __future__ import annotations\n')
    r, _ = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": OLD})
    assert any("__future__" in x for x in r["report"] if x.startswith("手動"))


def test_import_used_before_insert_point_is_manual(tmp_path):
    """r3-9：import 加在最後一個 import 後面，前面的模組層級指定已經用到它。"""
    old = "import os\nPAT = None\nimport sys\n"
    new = "import os\nimport re\nPAT = re.compile('x')\nimport sys\n"
    r, _ = merge(tmp_path, {"pipeline.py": old}, {"pipeline.py": new}, {"pipeline.py": old})
    assert any("之前就用到 re" in x for x in r["report"])


def test_parenthesized_decorator_is_replaced_whole(tmp_path):
    """r3-10：`@(` 換行再寫裝飾器，AST 的行號不含 `@(`，原本替換後留下半個裝飾器。"""
    old = "def deco(f):\n    return f\n\n\n@(\n    deco\n)\ndef f():\n    return 1\n"
    new = "def deco(f):\n    return f\n\n\n@deco\ndef f():\n    return 2\n"
    _, out = merge(tmp_path, {"pipeline.py": old}, {"pipeline.py": new}, {"pipeline.py": old})
    got = out("pipeline.py")
    assert "@(" not in got and "@deco\ndef f():\n    return 2" in got
    compile(got, "pipeline.py", "exec")


def test_blueprint_rewrite_keeps_strings(tmp_path):
    """r3-11：Blueprint 改寫原本套在整段定義上，回傳的 "support@app.example" 會被改成 @bp。"""
    old = '@app.get("/a")\ndef a():\n    return "support@app.example"\n'
    new = old.replace("example", "example.tw")
    _, out = merge(tmp_path, {"app.py": old}, {"app.py": new},
                   {"app.py": "", "leihu/web/x.py": old.replace("@app.get", "@bp.get")})
    assert out("leihu/web/x.py") == '@bp.get("/a")\ndef a():\n    return "support@app.example.tw"\n'


def test_trailing_whitespace_in_string_counts(tmp_path):
    """r3-12：只改三引號字串裡的行尾空白，原本判定上游沒改。"""
    old = OLD + '\n\nTEXT = """first\nsecond"""\n'
    new = old.replace('"""first\n', '"""first  \n')
    r, out = merge(tmp_path, {"pipeline.py": old}, {"pipeline.py": new}, {"pipeline.py": old})
    assert r["stats"]["fast"] == 1 and '"""first  \nsecond"""' in out("pipeline.py")


def test_expect_compares_by_group_and_name(tmp_path):
    """r3-13：--expect 只用名稱：app.py 的 f 不同、pipeline.py 的 f 相同時原本回報沒有差異。"""
    app_old = '@app.get("/f")\ndef f():\n    return 1\n'
    app_new = app_old.replace("return 1", "return 2")
    pipe = "def f():\n    return 9\n"
    mine = app_old.replace("@app", "@bp")
    old = tree(tmp_path / "old", {"app.py": app_old, "pipeline.py": pipe})
    new = tree(tmp_path / "new", {"app.py": app_new, "pipeline.py": pipe})
    fork = tree(tmp_path / "fork", {"app.py": "", "leihu/web/x.py": mine, "pipeline.py": pipe})
    expect = tree(tmp_path / "expect", {"app.py": "", "leihu/web/x.py": mine.replace("return 1", "return 3"),
                                        "pipeline.py": pipe})
    r = D.run(old, new, fork, tmp_path / "out", expect=expect)
    assert r["diffs"] == ["app.py:f：內容不同（leihu/web/x.py）"]


def test_whole_file_merge_keeps_trailing_blank_lines(tmp_path):
    """r3-14：整檔合併原本去掉所有檔尾換行、最多補回一個。"""
    old = "a\nb\nc\nd\ne\n\n\n"
    _, out = merge(tmp_path, {"data.txt": old}, {"data.txt": old.replace("e\n", "E\n")},
                   {"data.txt": old.replace("a\n", "A\n")}, extra=["data.txt"])
    assert out("data.txt") == "A\nb\nc\nd\nE\n\n\n"


def test_version_followed_by_period():
    """r3-15：版號後面接句點（句尾）原本不換，結果仍是 2.3.5.，沒有 .1。"""
    assert D.unver("version 2.3.4.", "2.3.4") == f"version {D.VER_MARK}."
    assert D.unver("2.3.4.1.", "2.3.4") == f"{D.VER_MARK}."
    assert D.unver("2.3.40", "2.3.4") == "2.3.40"


def test_git_errors_are_not_missing_files():
    """r3-16：參照打錯、讀取失敗原本都當成檔案不存在，可能變成「上游刪掉所有定義」。"""
    import subprocess

    with pytest.raises(SystemExit):
        D.check_ref("no-such-ref-defmerge-test")
    assert D.show("HEAD", "no/such/file.py") is None
    with pytest.raises(subprocess.CalledProcessError):   # 不存在的參照：git 錯誤要丟出去
        D.show("no-such-ref-defmerge-test", "pipeline.py")


def test_mixed_alias_imports_sort():
    """r3-17：同時新增 import re 和 import re as regex，排序時 None 和字串比較丟 TypeError。"""
    assert D.import_lines({("import", "re", None), ("import", "re", "regex"),
                           ("from", "a", "x", None), ("from", "a", "x", "y")}) == [
        "import re", "import re as regex", "from a import x, x as y"]


def test_expect_both_deleted_is_same(tmp_path):
    """r3-18：定義正確刪除、預期也沒有，原本回報「合併結果沒有這個定義」。"""
    new = OLD.replace("\n\ndef b():\n    return 2\n", "\n")
    old_s, new_s = tree(tmp_path / "old", {"pipeline.py": OLD}), tree(tmp_path / "new", {"pipeline.py": new})
    fork, expect = tree(tmp_path / "fork", {"pipeline.py": OLD}), tree(tmp_path / "expect", {"pipeline.py": new})
    assert D.run(old_s, new_s, fork, tmp_path / "out", expect=expect)["diffs"] == []


# ---------------------------------------------------------------- 第 1 輪複審（2026-11-24）P3-10、P3-11


def test_version_mark_already_in_content_is_manual(tmp_path):
    """複審 P3-10（Codex r3-19）：內容裡本來就有版號記號，換回版號時會連它一起換掉，要手動。"""
    old = OLD + f'\n\nUSER_AGENT = "LeihuLiantai/2.3.4 {D.VER_MARK}"\n'
    new = old.replace("2.3.4", "2.3.5")
    r, out = merge(tmp_path, {"pipeline.py": old, "VERSION": "2.3.4\n"}, {"pipeline.py": new, "VERSION": "2.3.5\n"},
                   {"pipeline.py": old, "VERSION": "2.3.4.2\n"}, extra=["VERSION"])
    assert any("版號記號" in x and "USER_AGENT" in x for x in r["report"] if x.startswith("手動"))
    assert out("VERSION") == "2.3.5.1\n"   # 別的版號照常換


@pytest.mark.parametrize(("code", "want"), [
    ("@functools.cache(re)\ndef f():\n    return 1\n", True),         # 裝飾器載入時執行
    ("def f(x=re.compile('x')):\n    return x\n", True),            # 預設值載入時執行
    ("class C:\n    PAT = re.compile('x')\n", True),                 # 類別內容載入時執行
    ("class C(re.Pattern):\n    pass\n", True),                      # 基底
    ("def f():\n    return re.compile('x')\n", False),              # 函式內容要呼叫時才執行
    ("class C:\n    def m(self):\n        return re\n", False),    # 方法內容也是
])
def test_used_before_counts_load_time_parts(code, want):
    """複審 P3-11：原本整個略過函式和類別；裝飾器、預設參數、類別內容其實在載入時就會執行。"""
    text = "import os\n" + code + "import sys\n"
    assert D.used_before(text, "re", D.last_import_end(text)) is want


def test_merged_definition_using_import_at_load_is_manual(tmp_path):
    """Codex r6-4：依賴只出現在合併後定義的預設值（merged_uses_before），定義在最後一個 import 前面：
    新的 import 加在後面會 NameError，要列手動。原本的測試只走 used_before，守不住這一條。"""
    old = "import os\ndef f(x=None):\n    return x\nimport sys\n"
    new = "import os\nimport re\ndef f(x=re.compile('x')):\n    return x\nimport sys\n"
    r, _ = merge(tmp_path, {"pipeline.py": old}, {"pipeline.py": new}, {"pipeline.py": old})
    assert any("之前就用到 re" in x for x in r["report"] if x.startswith("手動"))


# ---------------------------------------------------------------- 第 2 輪審查（2026-11-24，PR #26）


def test_binary_file_in_git_ref_is_not_text():
    """第 2 輪 1：git() 用 text=True 時，Windows 上解碼失敗只在背景執行緒印 traceback、stdout 是 None，
    二進位檔被當成「上游刪掉了」。要丟 UnicodeDecodeError，file() 列為「不是文字檔」。"""
    with pytest.raises(UnicodeDecodeError):
        D.show("HEAD", "static/icon.png")
    head = D.Source(ref="HEAD")
    m = D.Merge(head, D.Source(ref="HEAD"), D.Source(ref="HEAD"))
    m.file("static/icon.png")
    assert m.report == ["手動    static/icon.png：不是文字檔"]


def test_fork_version_not_matching_old_upstream_is_manual(tmp_path):
    """第 2 輪 2：fork 的版號對不上舊上游（漏改成 2.3.3.1）時，三方合併保留 fork 的舊版號，原本回報成功。"""
    old = OLD + '\n\nVERSION = "2.3.4"\n'
    new = old.replace("2.3.4", "2.3.5")
    fork = old.replace('"2.3.4"', '"2.3.3.1"')
    r, _ = merge(tmp_path, {"pipeline.py": old, "VERSION": "2.3.4\n"}, {"pipeline.py": new, "VERSION": "2.3.5\n"},
                 {"pipeline.py": fork, "VERSION": "2.3.3.1\n"}, extra=["VERSION"])
    manual = [x for x in r["report"] if x.startswith("手動")]
    assert any("pipeline.py:VERSION" in x and "版號" in x for x in manual)
    assert any("VERSION：" in x and "版號" in x for x in manual)
    assert r["stats"]["version"] == 0 and r["files"] == []


def test_merge_file_failure_is_manual_not_empty(tmp_path):
    """第 2 輪 3：git merge-file 出錯（含 NUL 的檔案當成二進位）回傳 255、輸出空字串，原本整個檔案寫成空白。"""
    with pytest.raises(D.MergeFailed):
        D.merge3("a\x00b\n", "a\n", "c\n")
    r, _ = merge(tmp_path, {"data.txt": "a\nX\n"}, {"data.txt": "a\nY\n"}, {"data.txt": "a\x00b\nX\n"}, extra=["data.txt"])
    assert r["files"] == [] and r["stats"]["manual"] == 1


def test_merge_ignores_global_conflict_style(monkeypatch):
    """第 2 輪 3：merge.conflictStyle=diff3 多出 ||||||| 區塊，--expect 去掉衝突標記時會留下來。"""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "merge.conflictStyle")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "diff3")
    out, c = D.merge3("x = 1", "x = 0", "x = 2")
    assert c == 1 and "|||||||" not in out


def test_app_static_folder_uses_current_app(tmp_path):
    """第 2 輪 4：upstream_map 會把 app.static_folder、app.response_class 換成 current_app.*，defmerge 漏了：
    fork 沒改過的路由被當成改過（假衝突），上游新寫的用法照抄進 Blueprint 會 NameError。"""
    old = '@app.get("/a")\ndef a():\n    p = app.static_folder\n    return p\n'
    new = old.replace("return p", "return p + '/x'")
    fork = old.replace("@app.", "@bp.").replace("app.static_folder", "current_app.static_folder")
    r, out = merge(tmp_path, {"app.py": old}, {"app.py": new}, {"app.py": "", "leihu/web/x.py": fork})
    assert r["stats"]["fast"] == 1 and r["stats"]["conflict"] == 0
    assert "current_app.static_folder" in out("leihu/web/x.py") and " app.static_folder" not in out("leihu/web/x.py")


def test_conflicted_file_still_checked_for_missing_import(tmp_path, monkeypatch):
    """第 2 輪 5：同一個檔案裡 a 有衝突、b 乾淨合併並用到放進 common.py 的新函式：原本因為 SyntaxError 整個檔案略過。"""
    monkeypatch.setattr(D, "PLACE", {"helper": "leihu/web/common.py"})
    old = '@app.get("/a")\ndef a():\n    return 1\n\n\n@app.get("/b")\ndef b():\n    x = 1\n    return x\n'
    new = ('def helper():\n    return 1\n\n\n'
           + old.replace("return 1", "return 2").replace("return x", "return helper()"))
    fork = old.replace("@app.", "@bp.").replace("return 1", "return 10")
    r, _ = merge(tmp_path, {"app.py": old}, {"app.py": new},
                 {"app.py": "", "leihu/web/common.py": "X = 1\n", "leihu/web/x.py": fork})
    assert r["stats"]["conflict"] == 1
    assert any("leihu/web/x.py 用到 helper" in x for x in r["report"] if x.startswith("手動"))


def test_star_import_is_manual(tmp_path):
    """第 2 輪 6：from x import * 的名稱是 "*"，比不到用到的地方，原本自動加進 pipeline.py。"""
    new = OLD.replace("import os\n", "import os\nfrom helpers import *\n")
    r, _ = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": OLD})
    assert r["stats"]["imports"] == 0
    assert any("星號" in x for x in r["report"] if x.startswith("手動"))


@pytest.mark.parametrize("fork_has", ["from leihu.mine import bar\n", "def bar():\n    return 1\n"])
def test_new_import_shadowing_fork_name_is_manual(tmp_path, fork_has):
    """第 2 輪 7：fork 已經有同名的 import 或定義，上游加 from foo import bar 會把它蓋掉，要手動。"""
    new = OLD.replace("import os\n", "import os\nfrom foo import bar\n")
    fork = OLD.replace("import os\n", "import os\n" + fork_has)
    r, _ = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new}, {"pipeline.py": fork})
    assert r["stats"]["imports"] == 0
    assert any("同名" in x and "bar" in x for x in r["report"] if x.startswith("手動"))


def test_last_import_end_only_takes_leading_docstring():
    """第 2 輪 8：沒有 import 時只有第一個敘述是字串才算模組 docstring；後面的常數敘述不算。"""
    assert D.last_import_end("X = 1\n42\nY = 2\n") == 0
    assert D.last_import_end('"""說明。"""\nX = 1\n"字串"\n') == 1
    assert D.last_import_end("42\nX = 1\n") == 0


@pytest.mark.parametrize("argv", [["a", "b", "c", "--al", "--out=o"], ["a", "b", "c", "--alll", "--out=o"],
                                  ["a", "b", "c"], ["a", "b", "--out=o"]])
def test_cli_rejects_bad_options(argv):
    """第 2 輪 9：拼錯的選項原本被靜默忽略（--all 打錯就少合併其他檔案），缺 fork 參照要報錯。"""
    with pytest.raises(SystemExit) as e:
        D.parse_args(argv)
    assert e.value.code == 2


def test_cli_out_with_space():
    """第 2 輪 9：--out foo（沒有等號）原本寫進 ./1/。"""
    a = D.parse_args(["a", "b", "c", "--out", "foo", "--all"])
    assert (a.out, a.all, a.refs) == ("foo", True, ["a", "b", "c"])
    a = D.parse_args(["a", "b", "--tree=t", "--out=o", "--expect=e"])
    assert (a.tree, a.out, a.expect, a.all) == ("t", "o", "e", False)


# ---------------------------------------------------------------- 拆檔方式一：和 pipeline.py 共用命名空間（2026-11-24）

PART_HEAD = "# 從 pipeline.py 搬出\n\n\n"
FORK_MAIN = OLD.replace("\n\ndef b():\n    return 2\n", '\n\n_load_part("sep")\n')


def test_shared_part_import_goes_to_pipeline(tmp_path):
    """b() 用方式一搬到 leihu/core/sep.py（pipeline.py 有 _load_part("sep")），上游新增 import re 讓 b() 用：
    共用命名空間，import 照樣加在 pipeline.py，不是手動。"""
    new = OLD.replace("import os\n", "import os\nimport re\n").replace("    return 2\n", "    return re.compile('x')\n")
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new},
                   {"pipeline.py": FORK_MAIN, "leihu/core/sep.py": PART_HEAD + "def b():\n    return 2\n"})
    assert r["stats"]["manual"] == 0 and r["stats"]["imports"] == 1
    assert "import os\nimport re\n" in out("pipeline.py")
    assert "re.compile('x')" in out("leihu/core/sep.py")


def test_shared_part_new_definition_needs_no_import(tmp_path):
    """上游新增 helper（放在 sep.py 的 b 後面），pipeline.py 的 a() 改成呼叫它：同一個命名空間，不用 import。"""
    new = OLD.replace("    return x + y + z", "    return x + y + z + helper()") + "\n\ndef helper():\n    return 3\n"
    r, out = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new},
                   {"pipeline.py": FORK_MAIN, "leihu/core/sep.py": PART_HEAD + "def b():\n    return 2\n"})
    assert r["stats"]["manual"] == 0
    assert "def helper" in out("leihu/core/sep.py")


def test_core_module_without_load_call_is_still_separate(tmp_path):
    """leihu/core/ 裡沒有被 _load_part 載入的是方式二的模組：上游新增的 import 照舊要手動放進那個檔案。"""
    new = OLD.replace("import os\n", "import os\nimport re\n").replace("    return 2\n", "    return re.compile('x')\n")
    fork_main = OLD.replace("\n\ndef b():\n    return 2\n", "\n")
    r, _ = merge(tmp_path, {"pipeline.py": OLD}, {"pipeline.py": new},
                 {"pipeline.py": fork_main, "leihu/core/sep.py": "def b():\n    return 2\n"})
    assert any("leihu/core/sep.py" in x and "import re" in x for x in r["report"] if x.startswith("手動"))


def test_shared_part_loaded_before_import_point(tmp_path):
    """方式一的檔案在最後一個 import 之前就載入，裡面的定義載入時用到新的 import：加在後面會 NameError，要手動。"""
    old = "import os\nPAT = None\nimport sys\n"
    new = "import os\nimport re\nPAT = re.compile('x')\nimport sys\n"
    fork = 'import os\n_load_part("early")\nimport sys\n'
    r, _ = merge(tmp_path, {"pipeline.py": old}, {"pipeline.py": new},
                 {"pipeline.py": fork, "leihu/core/early.py": PART_HEAD + "PAT = None\n"})
    assert any("之前就用到 re" in x for x in r["report"] if x.startswith("手動"))


# ---------------------------------------------------------------- 逐個上游 commit 同步：--next（2026-11-28）


def _repo(root: Path, monkeypatch) -> callable:
    """暫存 git repo：舊上游 → 上游 3 個 commit；fork 從舊上游分出去。cwd 換到 repo。"""
    import subprocess

    def g(*args: str) -> str:
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                              capture_output=True, check=True).stdout.decode("utf-8").strip()

    root.mkdir()
    g("init", "-q", "-b", "up")
    g("config", "core.autocrlf", "false")
    monkeypatch.chdir(root)
    return g


def _commit(g, root: Path, text: str, msg: str) -> str:
    (root / "pipeline.py").write_text(text, "utf-8", newline="\n")
    g("add", "pipeline.py")
    g("commit", "-q", "-m", msg)
    return g("rev-parse", "HEAD")


def test_next_commit_skips_synced_trailers(tmp_path, monkeypatch):
    """trailer 用完整 sha 或至少 7 碼的縮寫都算同步過；都同步過時回傳 None。"""
    root = tmp_path / "r"
    g = _repo(root, monkeypatch)
    old = _commit(g, root, OLD, "old")
    u = [_commit(g, root, OLD.replace("return 2", f"return {i}"), f"u{i}") for i in (1, 2, 3)]
    assert D.next_commit(old, u[2], []) == (1, 3, u[0])
    assert D.next_commit(old, u[2], [u[0][:7]]) == (2, 3, u[1])
    assert D.next_commit(old, u[2], [u[0][:6]]) == (1, 3, u[0])   # 太短的縮寫不算
    assert D.next_commit(old, u[2], u) == (3, 3, None)


def test_next_merges_one_commit_into_worktree(tmp_path, monkeypatch, capsys):
    """--next：fork 改 a()、上游第 1 個 commit 改 b()，寫回工作目錄；commit 帶 trailer 之後換第 2 個；
    工作目錄有沒 commit 的修改時不跑。"""
    root = tmp_path / "r"
    g = _repo(root, monkeypatch)
    old = _commit(g, root, OLD, "old")
    u1 = _commit(g, root, OLD.replace("return 2", "return 22"), "u1")
    u2 = _commit(g, root, OLD.replace("return 2", "return 222"), "u2")
    g("switch", "-q", "-c", "fork", old)
    fork = OLD.replace("    x = 1\n", "    x = 10   # fork\n")
    _commit(g, root, fork, "fork")

    assert D.step(old, u2, None) == 0
    got = (root / "pipeline.py").read_text("utf-8")
    assert "x = 10   # fork" in got and "return 22\n" in got
    assert f'git commit -c {u1} --trailer "Upstream-commit: {u1}"' in capsys.readouterr().out
    with pytest.raises(SystemExit, match="還沒 commit"):
        D.step(old, u2, None)

    g("commit", "-q", "-a", "-C", u1, "--trailer", f"Upstream-commit: {u1}")
    assert g("log", "-1", "--format=%s") == "u1"   # -C 沿用上游的說明
    assert D.step(old, u2, None) == 0
    assert "return 222\n" in (root / "pipeline.py").read_text("utf-8")
    g("commit", "-q", "-a", "-m", f"u2\n\nUpstream-commit: {u2[:7]}")
    assert D.step(old, u2, None) == 0
    assert "都同步過了" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["a", "b", "--next", "--out=o"], ["a", "b", "--next", "--tree=t"],
                                  ["a", "b", "--next", "--all"], ["a", "b", "c", "--next"]])
def test_cli_next_rejects_other_modes(argv):
    with pytest.raises(SystemExit) as e:
        D.parse_args(argv)
    assert e.value.code == 2


def test_cli_next():
    a = D.parse_args(["a", "b", "--next", "--expect=e"])
    assert (a.next, a.refs, a.expect, a.out) == (True, ["a", "b"], "e", None)


def test_next_leaves_untracked_files_and_records_empty_sync(tmp_path, monkeypatch, capsys):
    """Codex 審查 P1：沒追蹤的檔案不能動（git checkout 救不回來）：舊的 b() 留在沒追蹤的 leihu/scratch.py，
    上游刪掉 b() 時不能刪到它；上游新增的檔案和沒追蹤的檔案同名時不寫出。
    P2：上游刪掉的 b() fork 早就刪了，合併沒有改動，要印 --allow-empty，commit 之後才會換下一個。"""
    root = tmp_path / "r"
    g = _repo(root, monkeypatch)
    old = _commit(g, root, OLD, "old")
    u1 = _commit(g, root, OLD.replace("\n\ndef b():\n    return 2\n", "\n"), "u1")
    (root / "static").mkdir()
    (root / "static" / "new.js").write_text("// 上游\n", "utf-8")
    g("add", "static/new.js")
    g("commit", "-q", "-m", "u2")
    u2 = g("rev-parse", "HEAD")
    g("switch", "-q", "-c", "fork", old)
    _commit(g, root, OLD.replace("\n\ndef b():\n    return 2\n", "\n"), "fork 刪掉 b")
    scratch = root / "leihu" / "scratch.py"
    scratch.parent.mkdir()
    scratch.write_bytes(b"def b():\n    return 2\n")
    (root / "static").mkdir()
    (root / "static" / "new.js").write_bytes(b"// fork \xe8\x87\xaa\xe5\xb7\xb1\xe7\x9a\x84\n")

    assert D.step(old, u2, None) == 0
    assert scratch.read_bytes() == b"def b():\n    return 2\n"
    assert f'git commit --allow-empty -c {u1} --trailer "Upstream-commit: {u1}"' in capsys.readouterr().out
    g("commit", "-q", "--allow-empty", "-C", u1, "--trailer", f"Upstream-commit: {u1}")

    assert D.step(old, u2, None) == 1
    assert any(x.startswith("手動") and "static/new.js" in x for x in capsys.readouterr().out.splitlines())
    assert (root / "static" / "new.js").read_bytes() == b"// fork \xe8\x87\xaa\xe5\xb7\xb1\xe7\x9a\x84\n"


def test_untracked_file_is_not_written_back(tmp_path):
    """Codex 審查 P1：寫回工作目錄時，git 沒追蹤的檔案（例如 fork 從 git 拿掉、只留在本機的副本）不能被三方合併改掉。"""
    old, new = tree(tmp_path / "old", {"static/x.js": "a\n"}), tree(tmp_path / "new", {"static/x.js": "b\n"})
    fork_dir = tmp_path / "fork"
    tree(fork_dir, {"static/x.js": "a\n"})
    r = D.run(old, new, D.Source(tree=fork_dir, tracked=set()), fork_dir, extra_files=["static/x.js"])
    assert (fork_dir / "static" / "x.js").read_text("utf-8") == "a\n"
    assert r["files"] == [] and any("git 沒追蹤" in x for x in r["report"])
