"""批次匯入的歌單解析（pipeline.read_sheet）與旋律譜匯出（score_sheet.render）。
從舊分支 2f3d2f2 搬來，import 改成目前的結構（refactor-port-plan.md G 組 #43）。"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

YT = "https://www.youtube.com/watch?v=ruy-QxDSKLg"


@pytest.fixture(scope="module")
def P(app_module):
    import pipeline

    return pipeline


@pytest.fixture(scope="module")
def sheet(app_module):
    import score_sheet

    return score_sheet


# ---------------------------------------------------------------- read_sheet：CSV


def test_csv_columns_key_language_url(P):
    csv = (
        "歌手,曲名,備註,語言,YT link\n"
        f"周杰倫,告白氣球,+2,國語,{YT}\n"
        ",,,,\n"  # 空列跳過，但列號照原本的表單算
        "鄧麗君,月亮代表我的心,=4,台語,\n"
        "只有歌手,,9,日文,看這裡 youtu.be/ruy-QxDSKLg\n"  # 超過 ±6 的 KEY 不採用；網址欄空的就找整列
    )
    got = P.read_sheet(csv.encode("utf-8-sig"), "歌單.csv")
    assert got == [
        {"line": "周杰倫 告白氣球", "artist": "周杰倫", "song": "告白氣球", "key": 2, "lang": "zh",
         "category": "中文歌", "url": YT, "row": 2},
        {"line": "鄧麗君 月亮代表我的心", "artist": "鄧麗君", "song": "月亮代表我的心", "key": 4, "lang": "zh",
         "category": "台語歌", "url": "", "row": 4},
        {"line": "只有歌手", "artist": "只有歌手", "song": "", "key": None, "lang": "ja", "category": "日文歌",
         "url": YT, "row": 5},
    ]  # fmt: skip


def test_csv_big5_and_tsv(P):
    big5 = "歌手,曲名\n張學友,吻別\n".encode("big5")
    assert [r["line"] for r in P.read_sheet(big5, "a.csv")] == ["張學友 吻別"]
    tsv = b"artist\ttitle\tkey\nA\tB\t-3\n"
    got = P.read_sheet(tsv, "a.txt")  # 有 tab、沒有逗號：當成 tab 分隔
    assert got[0]["line"] == "A B" and got[0]["key"] == -3


def test_csv_without_known_header_is_one_line_per_row(P):
    assert P.read_sheet("周杰倫 告白氣球\n\n鄧麗君,甜蜜蜜\n".encode(), "a.csv") == [
        {"line": "周杰倫 告白氣球"},
        {"line": "鄧麗君 甜蜜蜜"},
    ]
    assert P.read_sheet(b"", "a.csv") == []


def test_rows_capped_at_batch_max(P):
    body = "歌手,曲名\n" + "".join(f"A,{i}\n" for i in range(P.BATCH_MAX + 5))
    assert len(P.read_sheet(body.encode(), "a.csv")) == P.BATCH_MAX


@pytest.mark.parametrize(
    "name, data, msg",
    [
        ("a.xls", b"x", "舊版 Excel"),
        ("a.pdf", b"x", "只支援"),
        ("a.xlsx", b"not a zip", "讀不到這個 Excel 檔"),
    ],
)
def test_unsupported_or_broken_files(P, name, data, msg):
    with pytest.raises(P.UserError, match=msg):
        P.read_sheet(data, name)


# ---------------------------------------------------------------- read_sheet：xlsx

_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def _xlsx(rows_xml: str, shared: list[str] | None = None, sheet: str = "xl/worksheets/sheet1.xml") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if shared is not None:
            si = "".join(f"<si><t>{s}</t></si>" for s in shared)
            z.writestr("xl/sharedStrings.xml", f"<sst {_NS}>{si}</sst>")
        z.writestr(sheet, f"<worksheet {_NS}><sheetData>{rows_xml}</sheetData></worksheet>")
    return buf.getvalue()


def test_xlsx_shared_inline_number_and_sparse_cells(P):
    rows = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
        # 第 2 列空白（不在 XML 裡）；第 3 列 B 欄是 inlineStr、C 欄是數字
        '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3" t="inlineStr"><is><t>告白氣球</t></is></c>'
        '<c r="C3"><v>-2</v></c></row>'
        # 只有 C 欄：歌手、曲名都空，這列不算
        '<row r="4"><c r="C4"><v>1</v></c></row>'
        # 從 B 欄開始（A 欄缺）
        '<row r="5"><c r="B5" t="inlineStr"><is><t>只有曲名</t></is></c></row>'
    )
    got = P.read_sheet(_xlsx(rows, ["歌手", "曲名", "KEY", "周杰倫"]), "a.xlsx")
    assert [(r["artist"], r["song"], r["key"], r["row"]) for r in got] == [
        ("周杰倫", "告白氣球", -2, 3),
        ("", "只有曲名", None, 5),
    ]


def test_xlsx_uses_first_sheet_when_no_sheet1(P):
    rows = '<row r="1"><c r="A1" t="inlineStr"><is><t>周杰倫 告白氣球</t></is></c></row>'
    assert P.read_sheet(_xlsx(rows, sheet="xl/worksheets/sheet2.xml"), "a.xlsx") == [{"line": "周杰倫 告白氣球"}]


def test_xlsx_without_worksheet(P):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
    with pytest.raises(P.UserError, match="沒有工作表"):
        P.read_sheet(buf.getvalue(), "a.xlsx")


# ---------------------------------------------------------------- 旋律譜匯出（冒煙測試）


def _pitch_song(d, notes):
    d.mkdir(parents=True, exist_ok=True)
    (d / "pitch.json").write_text(json.dumps({"midi": [60.0, None], "hop": 0.01, "notes": notes}), "utf-8")


def test_render_sheet_pages_and_key(sheet, tmp_path):
    from PIL import Image

    # C 大調音階來回 8 遍（約 40 秒）
    scale = [60, 62, 64, 65, 67, 69, 71, 72]
    notes = [[i * 0.6, i * 0.6 + 0.5, scale[i % 8]] for i in range(64)]
    _pitch_song(tmp_path / "song", notes)
    lines = [
        {"t": 0.0, "end": 9.0, "text": "第一句歌詞", "p": "m",
         "chars": [{"c": c, "t": k * 1.5} for k, c in enumerate("第一句歌詞")]},
        {"t": 10.0, "end": 19.0, "text": "第二句", "p": "f", "chars": []},
        {"t": "壞掉的時間", "text": "略過"},
    ]  # fmt: skip
    out = tmp_path / "out"
    out.mkdir()
    (out / "old.png").write_bytes(b"x")  # 上次匯出的圖會先刪掉
    files, key = sheet.render(
        tmp_path / "song",
        {"title": "測試歌", "artist": "某人", "lines": lines, "notation": "both", "parts": True, "layout": "a4"},
        out,
    )
    assert key == "C"
    assert files and [p.name for p in files] == [f"{i + 1:02d}.png" for i in range(len(files))]
    assert sorted(p.name for p in out.iterdir()) == [p.name for p in files]
    with Image.open(files[0]) as im:
        assert im.size == sheet.LAYOUTS["a4"][:2]


def test_render_sheet_without_lyrics_and_transposed(sheet, tmp_path):
    scale = [60, 62, 64, 65, 67, 69, 71, 72]  # C 大調
    notes = [[i * 0.4, i * 0.4 + 0.3, scale[i % 8]] for i in range(32)]
    _pitch_song(tmp_path / "song", notes)
    files, key = sheet.render(tmp_path / "song", {"key": 2, "tonic": "auto", "layout": "不存在"}, tmp_path / "out")
    assert files and key == "D"  # 移調 +2：C 調變 D 調；不認得的版面用手機版
    from PIL import Image

    with Image.open(files[0]) as im:
        assert im.size == sheet.LAYOUTS["phone"][:2]


def test_render_sheet_without_melody(P, sheet, tmp_path):
    _pitch_song(tmp_path / "song", [[0.0, 0.01, 60]])  # 太短的音符不算
    with pytest.raises(P.UserError, match="還沒有旋律"):
        sheet.render(tmp_path / "song", {}, tmp_path / "out")


# ---------------------------------------------------------------- _chunks：長句切段（M6）


def _chunks_in_subprocess(line: dict, max_chars: int, max_span: float, notes: list | None = None) -> list:
    """在子行程裡跑：回歸時卡住的是子行程（30 秒逾時），不會拖住整個測試。環境變數沿用 conftest 設好的。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import json, sys, score_sheet as SS; "
        f"print(json.dumps([[c['t'], c['end'], ''.join(x['c'] for x in c['chars'])] "
        f"for c in SS._chunks(json.loads(sys.argv[1]), json.loads(sys.argv[2]), {max_chars}, {max_span})]))"
    )
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-c", code, json.dumps(line), json.dumps(notes or [])], cwd=root, env=os.environ.copy(),
                       capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_chunks_terminates_when_cut_equals_segment_start():
    """M6：切點剛好等於段落起點時，會切出空段、再處理同一段，一直重複。"""
    # 逐字時間早於句首（手動調整過時間常見）：兩個字都會被算成句首，句長超過 9 秒要切
    early = {"t": 0.0, "end": 12.0, "text": "ab", "chars": [{"c": "a", "t": -1.0}, {"c": "b", "t": -0.5}]}
    assert _chunks_in_subprocess(early, 20, 9.0) == [[0.0, 12.0, "ab"]]
    # 兩個字同一個時間：切到第二段時，第二個字的時間就是段落起點
    same = {"t": 0.0, "end": 12.0, "text": "abc",
            "chars": [{"c": "a", "t": 0.0}, {"c": "b", "t": 6.0}, {"c": "c", "t": 6.0}]}
    assert _chunks_in_subprocess(same, 1, 100.0) == [[0.0, 6.0, "a"], [6.0, 12.0, "bc"]]


def test_chunks_still_splits_long_line_between_chars():
    line = {"t": 0.0, "end": 12.0, "text": "abcd",
            "chars": [{"c": c, "t": t} for c, t in zip("abcd", (0.0, 3.0, 6.0, 9.0))]}
    assert _chunks_in_subprocess(line, 2, 100.0) == [[0.0, 6.0, "ab"], [6.0, 12.0, "cd"]]


def test_chunks_terminates_with_char_just_before_segment_start():
    """5.9995 秒的字比段落起點 6 秒早不到 1 毫秒，仍算進第二段；剩下同時間的字不能當切點。"""
    line = {"t": 0.0, "end": 12.0, "text": "abcd",
            "chars": [{"c": c, "t": t} for c, t in zip("abcd", (0.0, 5.9995, 6.0, 6.0))]}
    assert _chunks_in_subprocess(line, 1, 100.0) == [[0.0, 6.0, "a"], [6.0, 12.0, "bcd"]]


def test_chunks_falls_back_to_notes_when_char_cuts_cannot_advance():
    """字的時間都不能當切點時，改用音符之間切。"""
    line = {"t": 0.0, "end": 12.0, "text": "ab", "chars": [{"c": "a", "t": -1.0}, {"c": "b", "t": -0.5}]}
    assert _chunks_in_subprocess(line, 20, 9.0, [[6.0, 7.0, 60]]) == [[0.0, 6.0, "ab"], [6.0, 12.0, ""]]
