"""tests/tools/upstream_map.py：同步上游時把 app.py、pipeline.py 的 hunk 對應到現在的檔案（refactor-port-plan.md 3.21）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import upstream_map as U

SRC = """import os

X = 1


@app.get("/a")
def a():
    return 1


def b():
    return 2
"""


def test_spans_include_decorators_and_assignments():
    assert U.spans(SRC) == [(3, 3, "X"), (6, 8, "a"), (11, 12, "b")]


def test_split_hunks_reads_new_ranges_and_lines():
    diff = 'diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -6,2 +6,3 @@ x\n @app.get("/a")\n+# 新\n def a():\n@@ -12 +13,0 @@\n-    return 2\n'
    hs = U.split_hunks(diff)
    assert [(h["s"], h["n"]) for h in hs] == [(6, 3), (13, 0)]
    assert hs[0]["lines"] == [' @app.get("/a")', "+# 新", " def a():"]


def test_target_of_by_enclosing_name():
    idx = {"a": ["leihu/web/system.py"], "b": ["leihu/web/live.py"], "X": ["leihu/web/common.py"]}
    sp = U.spans(SRC)
    assert U.target_of({"s": 7, "n": 1}, sp, idx) == ("leihu/web/system.py", "a")
    assert U.target_of({"s": 1, "n": 1}, sp, idx)[0] is None  # import：模組層級，手動
    assert U.target_of({"s": 8, "n": 4}, sp, idx)[0] is None  # 橫跨 a、b，兩個檔案：手動
    assert U.target_of({"s": 7, "n": 1}, sp, {})[0] is None  # 名稱現在找不到：手動


def test_target_of_only_looks_where_the_source_file_was_split_to():
    sp = U.spans(SRC)
    # app.py 的路由和 pipeline 的同名函式不衝突
    idx = {"a": ["leihu/web/lyrics.py", "pipeline.py"], "b": ["pipeline.py"]}
    assert U.target_of({"s": 7, "n": 1}, sp, idx, "app.py") == ("leihu/web/lyrics.py", "a")
    assert U.target_of({"s": 7, "n": 1}, sp, idx, "pipeline.py") == ("pipeline.py", "a")
    # pipeline.py 上游新增的定義預設留在 pipeline.py；app.py 新增的要手動決定放哪個 Blueprint
    assert U.target_of({"s": 11, "n": 2}, sp, {}, "pipeline.py")[0] == "pipeline.py"
    assert U.target_of({"s": 11, "n": 2}, sp, {}, "app.py")[0] is None
    # 新增的定義接在搬走的定義旁邊：手動
    assert U.target_of({"s": 8, "n": 4}, sp, {"a": ["leihu/pitch.py"]}, "pipeline.py")[0] is None


def test_retarget_rewrites_app_decorators_only_for_app_py():
    lines = [
        ' @app.get("/x")',
        "+@app.errorhandler(E)",
        "-@app.before_request",
        '     return send_from_directory(app.static_folder, "i")',
        " current_app.static_folder",
    ]
    assert U.retarget(lines, "app.py") == [
        ' @bp.get("/x")',
        "+@bp.app_errorhandler(E)",
        "-@bp.before_app_request",
        '     return send_from_directory(current_app.static_folder, "i")',
        " current_app.static_folder",
    ]
    assert U.retarget(lines, "pipeline.py") == lines


def test_current_index_knows_split_locations():
    idx = U.current_index()
    assert idx["setlist_clear"] == ["leihu/web/live.py"]
    assert idx["main"] == ["app.py"]
    assert idx["clean_pitch"] == ["leihu/pitch.py"]
    assert idx["parse_lrc"] == ["leihu/lyrics_text.py"]
    assert idx["process_song"] == ["pipeline.py"]
