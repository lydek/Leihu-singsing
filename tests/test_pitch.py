"""音高曲線的後處理（leihu/pitch.py）。記錄目前的行為，之後同步上游或調整時才看得出改了什麼。
一格是 HOP_S（10 ms）；曲線用 MIDI 音高，NaN 是沒有聲音。"""

from __future__ import annotations

import numpy as np

from leihu import pitch as PT

NAN = np.nan


def arr(*parts) -> np.ndarray:
    """arr((60, 5), (NAN, 3)) → 5 格 60 接 3 格 NaN。"""
    return np.concatenate([np.full(n, v, dtype=np.float32) for v, n in parts])


def same(a, b) -> bool:
    return np.allclose(a, b, equal_nan=True)


def test_constants():
    assert PT.PITCH_SR == 16000 and PT.HOP == 160 and PT.HOP_S == 0.01


# ---------------------------------------------------------------- bridge_gaps


def test_bridge_gaps_fills_short_gap_between_close_pitches():
    got = PT.bridge_gaps(arr((60, 3), (NAN, 4), (61, 3)))
    assert not np.isnan(got).any()
    assert same(got[3:7], np.linspace(60, 61, 6)[1:-1])  # 線性連起來


def test_bridge_gaps_leaves_long_gap_jump_and_edges():
    long_gap = arr((60, 3), (NAN, 13), (60, 3))  # 超過 12 格
    jump = arr((60, 3), (NAN, 4), (62, 3))  # 前後差超過 1 個半音
    edges = arr((NAN, 3), (60, 3), (NAN, 3))  # 頭尾的空白沒有兩邊可以接
    for x in (long_gap, jump, edges):
        assert same(PT.bridge_gaps(x), x)
    src = arr((60, 3), (NAN, 4), (61, 3))
    PT.bridge_gaps(src)
    assert np.isnan(src[3:7]).all()  # 不改輸入


# ---------------------------------------------------------------- clean_pitch


def test_clean_pitch_fixes_octave_jump_and_spike():
    f0 = arr((60, 10))
    f0[4] = 72  # 單格跳高八度：拉回原八度
    f0[7] = 64  # 偏離中位數超過 2.5 個半音：換成中位數
    got = PT.clean_pitch(f0)
    assert same(got, arr((60, 10)))
    assert got.dtype == np.float32


def test_clean_pitch_octave_fix_keeps_offset():
    """跳一個八度又差 1 個半音：先移回原八度（保留那 1 個半音），不是直接換成中位數。"""
    f0 = arr((60, 15))
    f0[4] = 73
    f0[10] = 47
    expected = arr((60, 15))
    expected[4] = 61
    expected[10] = 59
    assert same(PT.clean_pitch(f0), expected)


def test_clean_pitch_drops_runs_shorter_than_6():
    got = PT.clean_pitch(arr((60, 5), (NAN, 2), (62, 6)))
    assert same(got, arr((NAN, 7), (62, 6)))


# ---------------------------------------------------------------- trim_glides


def test_trim_glides_cuts_fast_slide_into_note():
    f0 = np.concatenate([np.array([50, 52, 54, 56, 58], dtype=np.float32), arr((60, 20))])
    got = PT.trim_glides(f0)
    assert np.isnan(got[:5]).all()  # 滑入的部分去掉
    assert same(got[5:], arr((60, 20)))  # 穩定的音保留


def test_trim_glides_keeps_steady_note_and_drops_tiny_segment():
    steady = arr((NAN, 2), (60, 20), (NAN, 2))
    assert same(PT.trim_glides(steady), steady)
    assert np.isnan(PT.trim_glides(arr((60, 5)))).all()  # 修完不到 6 格就整段去掉


# ---------------------------------------------------------------- segment_notes


def test_segment_notes_splits_on_pitch_change():
    # 0.5 秒 C4 接 0.5 秒 E4：偏離連續 5 格才切，切點往回算 4 格
    assert PT.segment_notes(arr((60, 50), (64, 50))) == [[0.0, 0.5, 60], [0.5, 1.0, 64]]


def test_segment_notes_drops_short_and_low_notes():
    f0 = arr((60, 7), (NAN, 3), (30, 20), (NAN, 3), (62, 10))
    # 7 格（0.07 秒）太短、MIDI 30 太低都不算；最後一段 0.10 秒保留
    assert PT.segment_notes(f0) == [[0.33, 0.43, 62]]
