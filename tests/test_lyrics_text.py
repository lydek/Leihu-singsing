"""歌詞文字處理（leihu/lyrics_text.py 與 pipeline 的語言、繁簡函式）、Claude 價格（claude_api.price_for）。
從舊分支 2f3d2f2 搬來，import 改成目前的結構（refactor-port-plan.md G 組 #43）。"""

from __future__ import annotations

import pytest

from leihu import lyrics_text as T


@pytest.fixture(scope="module")
def P(app_module):
    import pipeline

    return pipeline


@pytest.fixture(scope="module")
def claude_api(app_module):
    import claude_api

    return claude_api


# ---------------------------------------------------------------- LRC／SRT


def test_parse_lrc_sorts_repeats_and_strips_word_timing():
    got = T.parse_lrc("[00:05.00][00:01.00]重複\n無時間\n[00:03.00]<00:03.10>逐<00:03.20>字\n\n")
    assert got == [
        {"t": 1.0, "text": "重複"},  # 一行多個時間標記：每個時間各一句，依時間排序
        {"t": 3.0, "text": "逐字"},  # Enhanced LRC 的逐字時間拿掉
        {"t": 5.0, "text": "重複"},
        {"t": None, "text": "無時間"},  # 沒有時間的放最後，空行不算
    ]


def test_lrc_round_trip_and_time_format():
    lines = [{"t": 65.5, "text": "一"}, {"t": 0.0, "text": "二"}, {"t": None, "text": "三"}]
    lrc = T.to_lrc(lines)
    assert lrc == "[01:05.50]一\n[00:00.00]二\n三\n"
    assert T.parse_lrc(lrc) == [lines[1], lines[0], lines[2]]
    assert T.fmt_lrc_time(-3) == "00:00.00"  # 負的時間當成 0


def test_srt_to_lrc_joins_lines_and_drops_tags():
    srt = "1\n00:00:01,500 --> 00:00:03,000\n第一句\n第二行\n\n2\n00:01:02,25 --> 00:01:04,000\n<i>下一句</i>\n\n3\n沒有時間\n"
    assert T.srt_to_lrc(srt) == "[00:01.50]第一句 第二行\n[01:02.25]下一句\n"


def test_clean_lrc_drops_credits_title_and_tags():
    raw = "[ti:歌]\n[00:01.00]作詞：某人\n[00:02.00]周杰倫 - 告白氣球\n\n[00:03.00]塞納河畔\n"
    assert T.clean_lrc(raw, "告白氣球", "周杰倫") == "[00:03.00]塞納河畔"


def test_strip_hallucinations_keeps_real_lines(P):
    assert P.strip_hallucinations("[00:01.00]你好\n[00:02.00]請不吝點贊訂閱\n") == "[00:01.00]你好\n"
    assert P.strip_hallucinations("[00:02.00]Thanks for watching\n") == ""


# ---------------------------------------------------------------- 語言與繁簡


def test_is_japanese_needs_enough_kana(P):
    assert P.is_japanese("君の名は。あなたへ")
    assert not P.is_japanese("漢" * 20 + "の")  # 中文裡夾一個假名不算日文
    assert not P.is_japanese("")


@pytest.mark.parametrize(
    "text, lang", [("あいうえお", "ja"), ("사랑해", "ko"), ("愛你", "zh"), ("love", "en"), ("123", "")]
)
def test_guess_language(P, text, lang):
    assert P.guess_language(text) == lang


def test_to_taiwan_only_converts_glyphs(P):
    assert P.to_taiwan("爱情来了") == "愛情來了"
    assert P.to_taiwan("信息") == "信息"  # s2tw：不換成台灣慣用語（s2twp 會變「資訊」）
    assert P.to_taiwan("君の名は来た") == "君の名は来た"  # 日文不轉


def test_search_key_normalizes_width_case_and_script(P):
    assert P.search_key("ＡＢＣ 愛情") == "abc 爱情"
    assert P.search_key("") == ""
    assert T.to_simplified("愛情") == "爱情"


def test_similarity_ignores_script_spacing_and_tags():
    assert T.similarity("塞納河畔左岸的咖啡", "[00:01.00]塞纳河畔 左岸的咖啡\n") == 1.0
    assert T.similarity("短", "短") is None  # 辨識結果太短不比
    assert T.similarity("塞納河畔左岸的咖啡", "") is None
    assert 0 < T.similarity("塞納河畔左岸的咖啡", "塞納河畔") < 1


# ---------------------------------------------------------------- 搜尋字串


def test_lyric_queries_prefers_youtube_metadata():
    assert T.lyric_queries({"title": "x", "yt_track": "告白氣球", "yt_artist": "周杰倫"}) == ["告白氣球 周杰倫", "x"]


def test_lyric_queries_strips_noise_and_brackets():
    got = T.lyric_queries({"title": "周杰倫 Jay Chou【告白氣球 Love Confession】Official MV"})
    assert got == ["告白氣球 Love Confession 周杰倫 Jay Chou", "告白氣球 Love Confession", "周杰倫 Jay Chou"]
    assert len(T.lyric_queries({"title": "a【b】【c】【d】【e】"})) <= 3


# ---------------------------------------------------------------- 轉出名稱


def test_pipeline_reexports_same_objects(P):
    """步驟 4 搬出去的名稱，pipeline 轉出的是同一個物件（P.<名稱>、gpu_worker 的用法不變）。"""
    from leihu import pitch

    exports = (
        (T, ("_CREDIT", "_NOISE", "_norm", "align_to_recognized", "clean_lrc", "fmt_lrc_time", "lyric_queries",
             "parse_lrc", "similarity", "srt_to_lrc", "to_lrc", "to_simplified")),
        (pitch, ("HOP", "HOP_S", "PITCH_SR", "bridge_gaps", "clean_pitch", "fix_octaves", "octave_low_score",
                 "segment_notes", "trim_glides")),
    )  # fmt: skip
    for mod, names in exports:
        for name in names:
            assert getattr(P, name) is getattr(mod, name), name


# ---------------------------------------------------------------- Claude 價格


def test_price_table_longer_prefix_first(claude_api):
    """前綴比對取第一個符合的：較長的前綴（claude-sonnet-5-5）要排在較短的（claude-sonnet-5）前面，否則永遠比不到。"""
    names = [p for p, _, _ in claude_api.PRICES]
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            assert not b.startswith(a), f"{b} 排在 {a} 後面，永遠比不到"


def test_price_for_matches_by_prefix(claude_api):
    for prefix, pin, pout in claude_api.PRICES:
        assert claude_api.price_for(prefix + "-20261101", 10) == (pin, pout)
    assert claude_api.price_for("gpt-4o", 10) is None
    assert claude_api.price_for("", 10) is None


def test_price_for_haiku_long_prompt_tier(claude_api):
    base = claude_api.price_for("claude-haiku-5-5", 100_000)
    assert claude_api.price_for("claude-haiku-5-5", 100_001) == (0.50, 2.50) != base
