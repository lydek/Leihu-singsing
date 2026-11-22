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


# ---------------------------------------------------------------- octave_low_score、fix_octaves


def tone(midi: float, frames: int, harmonics=(1, 2, 3, 4)) -> np.ndarray:
    """frames 格長、基頻是 midi 的聲音，只含指定的泛音（第 k 泛音 = k 倍基頻）。"""
    f = 440.0 * 2 ** ((midi - 69) / 12)
    t = np.arange(frames * PT.HOP) / PT.PITCH_SR
    return sum(np.sin(2 * np.pi * k * f * t) for k in harmonics).astype(np.float32)


def test_octave_low_score_tells_real_low_note_from_octave_up():
    low = tone(60, 40)  # 真的是 C4：第 1～4 泛音都有
    up = tone(72, 40, (1, 2))  # 其實是 C5：C4 的第 1、3 泛音位置沒有能量
    no_fundamental = tone(60, 40, (2, 3, 4))  # （第 1＋3）÷（第 2＋4）＝ 1 ÷ 2
    assert np.isclose(PT.octave_low_score(low, 0, 40, 60), 1.0, rtol=0.02)
    assert PT.octave_low_score(up, 0, 40, 60) <= 0.12
    assert np.isclose(PT.octave_low_score(no_fundamental, 0, 40, 60), 0.5, rtol=0.02)


def test_octave_low_score_none_when_too_short_or_silent():
    assert PT.octave_low_score(tone(60, 40), 0, 2, 60) is None  # 320 個取樣，不到 400
    assert PT.octave_low_score(np.zeros(40 * PT.HOP, np.float32), 0, 40, 60) is None


# 四段音：前後都是 C4（60），第三段（90～125 格、35 格長）是候選音。候選音不算句尾短尾音（超過 30 格），
# 要有「比周圍小聲」或泛音證據才會移
A, B = 90, 125


def melody(target: float) -> np.ndarray:
    return arr((60, 40), (NAN, 5), (60, 40), (NAN, 5), (target, B - A), (NAN, 5), (60, 40))


def level(weak: bool) -> np.ndarray:
    rms = np.ones(len(melody(60)), np.float32)
    rms[A:B] = 0.1 if weak else 1.0
    return rms


def audio(seg: np.ndarray) -> np.ndarray:
    y = np.zeros(len(melody(60)) * PT.HOP, np.float32)
    y[A * PT.HOP : B * PT.HOP] = seg
    return y


def test_fix_octaves_needs_evidence_to_move():
    m = melody(72)
    assert same(PT.fix_octaves(m), m)  # 沒有音量、沒有聲音：看不出來，不移
    assert same(PT.fix_octaves(m, level(False)), m)  # 音量和周圍一樣：不移
    got = PT.fix_octaves(m, level(True))
    assert same(got, melody(60))  # 比周圍小聲：移回低八度
    assert same(m[A:B], np.full(B - A, 72))  # 不改輸入


def test_fix_octaves_uses_harmonics():
    m = melody(72)
    # 泛音顯示低八度的音確實存在：音量正常也移
    assert same(PT.fix_octaves(m, level(False), audio(tone(60, B - A))), melody(60))
    # 泛音顯示真的是高音：比周圍小聲也保留
    assert same(PT.fix_octaves(m, level(True), audio(tone(72, B - A, (1, 2)))), m)


def test_fix_octaves_moves_low_note_up():
    m = melody(48)
    assert same(PT.fix_octaves(m, level(True)), melody(60))  # 往下差八度又比較小聲：往上移
    # 泛音顯示低音確實存在：保留
    assert same(PT.fix_octaves(m, level(True), audio(tone(48, B - A))), m)
    # 泛音顯示低音位置沒有能量（其實是高八度）：音量正常也移
    assert same(PT.fix_octaves(m, level(False), audio(tone(60, B - A, (1, 2)))), melody(60))


def test_fix_octaves_diff_and_level_boundaries():
    """差 10～14 個半音才算八度錯（移 12、保留零頭）；比周圍小聲要低於一半。"""
    for target, want in ((45.99, 45.99), (46, 58), (50, 62), (50.01, 50.01), (70, 58), (74, 62), (74.01, 74.01)):
        assert same(PT.fix_octaves(melody(target), level(True)), melody(want))
    for lvl, want in ((0.49, 60), (0.5, 72)):
        rms = level(False)
        rms[A:B] = lvl
        assert same(PT.fix_octaves(melody(72), rms), melody(want))


def test_fix_octaves_score_thresholds(monkeypatch):
    """泛音分數：往下移（候選音偏高）要 ≥ 0.35 才確定、≤ 0.12 就否決；往上移相反。中間看音量。"""
    y = audio(np.zeros((B - A) * PT.HOP, np.float32))
    cases = [  # 分數, 偏高時（音量正常、小聲）移不移, 偏低時（音量正常、小聲）移不移
        (0.12, (False, False), (True, True)),
        (0.120001, (False, True), (False, True)),
        (0.349999, (False, True), (False, True)),
        (0.35, (True, True), (False, False)),
    ]
    for score, up, down in cases:
        monkeypatch.setattr(PT, "octave_low_score", lambda *a, s=score: s)
        for target, moves in ((72, up), (48, down)):
            for weak, move in zip((False, True), moves):
                got = PT.fix_octaves(melody(target), level(weak), y)
                assert same(got, melody(60 if move else target)), (score, target, weak)


def test_fix_octaves_short_tail_after_long_note():
    """句尾短尾音（不超過 30 格，緊接在比它兩倍還長的音後面）：沒有音量、聲音也移。"""
    m = arr((60, 40), (NAN, 5), (60, 41), (NAN, 5), (72, 20), (NAN, 5), (60, 40))
    want = arr((60, 40), (NAN, 5), (60, 41), (NAN, 5), (60, 20), (NAN, 5), (60, 40))
    assert same(PT.fix_octaves(m), want)
    # 尾音和周圍（60）差 24，和前一個音（72）差 12：照前一個音移
    m = arr((60, 100), (NAN, 5), (72, 61), (NAN, 5), (84, 20))
    assert same(PT.fix_octaves(m), arr((60, 100), (NAN, 5), (72, 61), (NAN, 5), (72, 20)))


def test_fix_octaves_context_is_length_weighted_and_needs_30_frames():
    # 周圍是 1 段 100 格的 60 和 2 段 35 格的 72：依長度加權的中位數是 60（一般中位數會是 72）
    m = arr((60, 100), (NAN, 5), (72, 35), (NAN, 5), (72, 35), (NAN, 5), (72, 35))
    rms = np.ones(len(m), np.float32)
    rms[105:140] = 0.1
    want = m.copy()
    want[105:140] = 60
    assert same(PT.fix_octaves(m, rms), want)
    # 2 秒內的周圍加起來不到 30 格：不移（空隙 51 格，避免周圍的短音被當成句尾短尾音）
    for last, moved in ((14, 72), (15, 60)):
        m = arr((72, 35), (NAN, 51), (60, 15), (NAN, 51), (60, last))
        rms = np.ones(len(m), np.float32)
        rms[:35] = 0.1
        want = m.copy()
        want[:35] = moved
        assert same(PT.fix_octaves(m, rms), want)


def test_fix_octaves_second_pass_and_moves_each_run_once():
    # 第一輪只修得到最後一段（48），中段（72）的周圍要等最後一段修好才變準
    m = arr((60, 35), (NAN, 5), (72, 35), (NAN, 5), (48, 35))
    rms = np.ones(len(m), np.float32)
    rms[40:115] = 0.1
    assert same(PT.fix_octaves(m, rms), arr((60, 35), (NAN, 5), (60, 35), (NAN, 5), (60, 35)))
    # 小聲的尾音第一輪移到 60 之後，第二輪和周圍（加權中位數 72）比又差八度：每段只移一次，不會被移回去
    m = arr((72, 80), (NAN, 5), (60, 80), (NAN, 5), (72, 20), (NAN, 5), (84, 80))
    rms = np.ones(len(m), np.float32)
    rms[170:190] = 0.1
    assert same(PT.fix_octaves(m, rms), arr((72, 80), (NAN, 5), (60, 80), (NAN, 5), (60, 20), (NAN, 5), (84, 80)))


def test_fix_octaves_keeps_long_notes_and_few_runs():
    """候選音都比周圍小聲，換成別的長度就會被移。"""
    long = arr((60, 40), (NAN, 5), (72, 61), (NAN, 5), (60, 40))  # 超過 60 格的音不看
    two = arr((60, 41), (NAN, 5), (72, 20))  # 不到 3 段不看（不然算句尾短尾音）
    for x, a in ((long, 45), (two, 46)):
        rms = np.ones(len(x), np.float32)
        rms[a:] = 0.1
        rms[a + 61 + 5 :] = 1.0  # long 的最後一段回到正常音量
        assert same(PT.fix_octaves(x, rms), x)


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
