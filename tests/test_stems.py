"""docs/next-version-plan.md 第 6a 批：分軌一致性（69、70）。

KEY 版本跟著原調分軌失效（含資料夾刪不掉時的 .stale 記號）、主唱與合音兩軌一起換、分軌改了要重新分析旋律
（pitch_stale）、補做高品質時顯示卡又卡住不清掉 live_light、伺服器讀自己的分軌不退回 audioread。
"""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

SID = "stemtest01"
N = 4410


@pytest.fixture
def song(app_module):
    import pipeline as P

    d = P.LIB / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": SID, "steps": {"import": True, "separate": True, "pitch": True},
                                             "separation": "uvr"}), "utf-8")
    for name, v in (("lead", 0.1), ("back", 0.2), ("inst", 0.3), ("mix", 0.4)):
        P.save_stem(d, name, np.full((2, N), v, dtype=np.float32))
    vd = P.variant_dir(d, 2, 1.0)
    vd.mkdir()
    for s in P.STEMS:
        P.save_stem(vd, s, np.zeros((2, N), dtype=np.float32))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _level(P, d, name):
    return round(float(P.read_audio(P.stem_path(d, name)).mean()), 2)


def _fake_gap(monkeypatch, P):
    """_lead_gap_gain 判定一半的合音要搬回主唱。"""
    monkeypatch.setattr(P, "_lead_gap_gain", lambda lead, back, sr, spans: (np.full(N, 0.5, np.float32), 12.0,
                                                                           {"moved_lines": 2, "weak": False}))
    monkeypatch.setattr(P, "_split_note", lambda d, stats: None)


# ---------------------------------------------------------------- KEY 版本跟著失效


def test_lead_fix_invalidates_key_versions(song, monkeypatch):
    """主唱補回改了分軌：舊的 KEY 版本是用修正前的分軌做的，之後移調、匯出不能再用。"""
    import pipeline as P

    _fake_gap(monkeypatch, P)
    assert P.variant_ready(song, 2, 1.0)
    assert P.fix_lead_gaps(song) > 0
    assert not P.variant_ready(song, 2, 1.0)
    assert P.read_meta(song).get("pitch_stale") is True


def test_undeletable_key_version_is_marked_stale(song, monkeypatch, app_module):
    """Windows 上檔案正在被讀時資料夾刪不掉：留記號，variant_ready、音訊路由不把它當成有效版本，step_variant 整份重做。"""
    import pipeline as P

    monkeypatch.setattr(P.shutil, "rmtree", lambda *a, **k: None)
    P.invalidate_variants(song)
    vd = P.variant_dir(song, 2, 1.0)
    assert vd.exists() and (vd / P.VARIANT_STALE).exists()
    assert not P.variant_ready(song, 2, 1.0)
    c = app_module.app.test_client()
    assert c.get(f"/api/songs/{SID}/audio/lead?key=2&speed=1").status_code == 404
    assert c.get(f"/api/songs/{SID}/audio/mix?key=2&speed=1").status_code == 200  # 原調的 mix 照常

    redone = []
    monkeypatch.setattr(P, "shift_piece", lambda y, sr, k, s, a, b: (redone.append(1) or y[:, max(0, a - sr):b + sr], max(0, a - sr)))
    monkeypatch.setattr(P, "live_status", lambda *a: {"hold": False})
    P.step_variant(song, 2, 1.0, lambda *a: None)
    assert len(redone) == len(P.STEMS)  # 三軌都重做，不是「檔案已經在」就跳過
    assert not (vd / P.VARIANT_STALE).exists()
    assert P.variant_ready(song, 2, 1.0)
    assert _level(P, vd, "lead") == 0.1  # 用的是現在的原調分軌


# ---------------------------------------------------------------- 兩軌一起換


def test_lead_fix_replaces_both_stems_or_neither(song, monkeypatch):
    """主唱、合音分開存時，第二軌失敗會留下一新一舊；要一起換，失敗就換回原本的。"""
    import pipeline as P

    _fake_gap(monkeypatch, P)
    real = P.write_wav

    def write_wav(path, y, sr=P.SR):
        if path.name.startswith("back"):
            raise OSError("磁碟滿了")
        real(path, y, sr)

    monkeypatch.setattr(P, "write_wav", write_wav)
    with pytest.raises(OSError):
        P.fix_lead_gaps(song)
    assert (_level(P, song, "lead"), _level(P, song, "back")) == (0.1, 0.2)
    assert not list(song.glob("*.tmp.*")) and not list(song.glob("*.bak"))


def test_replace_stems_restores_on_failed_swap(song, monkeypatch):
    """暫存檔都寫好、換到第二軌才失敗：已經換掉的第一軌也要換回原本的。"""
    import pipeline as P

    real = P._replace_retry

    def replace(src, dst):
        if dst.name == "back.flac" and src.name.startswith("back.tmp"):
            raise PermissionError("檔案被佔用")
        real(src, dst)

    monkeypatch.setattr(P, "_replace_retry", replace)
    with pytest.raises(PermissionError):
        P._replace_stems(song, {"lead": np.full((2, N), 0.6, np.float32), "back": np.full((2, N), 0.7, np.float32)})
    assert (_level(P, song, "lead"), _level(P, song, "back")) == (0.1, 0.2)
    assert not list(song.glob("*.tmp.*")) and not list(song.glob("*.bak"))


def test_replace_stems_success(song):
    import pipeline as P

    P._replace_stems(song, {"lead": np.full((2, N), 0.6, np.float32), "back": np.full((2, N), 0.7, np.float32)})
    assert (_level(P, song, "lead"), _level(P, song, "back")) == (0.6, 0.7)
    assert not list(song.glob("*.tmp.*")) and not list(song.glob("*.bak"))


# ---------------------------------------------------------------- pitch_stale


def test_step_separate_marks_pitch_stale_and_invalidates(song, monkeypatch):
    """重新分離後旋律還是舊分軌的；中途停止的話要記得補做。"""
    import pipeline as P

    monkeypatch.setattr(P, "module_status", lambda: {"uvr": True})
    monkeypatch.setattr(P, "separate_uvr", lambda d, cfg, report: None)
    monkeypatch.setattr(P, "is_acapella", lambda d: False)
    P.step_separate(song, {}, lambda *a: None)
    assert P.read_meta(song).get("pitch_stale") is True
    assert not P.variant_ready(song, 2, 1.0)


def test_step_pitch_clears_pitch_stale(song, monkeypatch):
    import pipeline as P

    P.update_meta(song, pitch_stale=True)
    monkeypatch.setattr(P, "analysis_vocal", lambda d: np.zeros(1600, np.float32))
    monkeypatch.setattr(P, "track_pitch", lambda y, cfg=None, strict=False: np.full(100, 60.0, np.float32))
    P.step_pitch(song, {}, lambda *a: None)
    assert "pitch_stale" not in P.read_meta(song)


def test_lead_fix_job_redoes_pitch_when_stale(song, monkeypatch):
    """上次搬回主唱後還沒分析完旋律就停止：這次就算沒有要搬的，也要重新分析旋律。"""
    import pipeline as P

    P.update_meta(song, pitch_stale=True)
    done = []
    monkeypatch.setattr(P, "fix_lead_gaps", lambda d: 0.0)
    monkeypatch.setattr(P, "step_pitch", lambda d, cfg, report: done.append(d))
    P.lead_fix_job(SID)(lambda *a: None)
    assert done == [song]


@pytest.mark.parametrize("checked", [True, False])
def test_background_retries_stale_pitch_once(song, monkeypatch, app_module, checked):
    """背景檢查遇到旋律待重做的歌排一次工作補做；同一次開程式不會一直重排。
    checked=False：主唱補回也還沒檢查過（lead_fix_needed 為真），補做失敗後不能繞去一般檢查又排一次。"""
    import pipeline as P
    from leihu.web import background, common

    extra = {"lead_fix": P.LEAD_FIX_V, "lead_fix_lrc": P._lrc_stamp(song)} if checked else {}
    P.update_meta(song, pitch_stale=True, **extra)
    monkeypatch.setattr(background, "_PITCH_RETRIED", set())
    monkeypatch.setattr(common.jobs, "state", {})
    monkeypatch.setattr(common.jobs, "pending", [])
    monkeypatch.setattr(P, "live_status", lambda *a: {"hold": False})
    monkeypatch.setattr(background.time, "sleep", lambda s: None)
    monkeypatch.setattr(P, "list_songs", lambda: [P.read_meta(song)])
    monkeypatch.setattr(P, "_lead_gap_gain", lambda *a: (np.ones(10), 5.0, {"moved_lines": 1, "weak": False}))
    background._lead_fix_scan()
    assert [j["label"] for j in common.jobs.state.values()] == ["leadfix"]
    common.jobs.state.clear()
    common.jobs.pending.clear()
    background._lead_fix_scan()   # 補做失敗了：pitch_stale 還在
    assert common.jobs.state == {}


def test_background_skips_stale_pitch_without_stems(song, monkeypatch, app_module):
    """還沒分離完的新歌（匯入時分離失敗）不用補旋律，不要每次開程式都排一個會失敗的工作。"""
    import pipeline as P
    from leihu.web import background, common

    meta = P.read_meta(song)
    meta["steps"]["separate"] = False
    P.write_meta(song, dict(meta, pitch_stale=True))
    monkeypatch.setattr(background, "_PITCH_RETRIED", set())
    monkeypatch.setattr(common.jobs, "state", {})
    monkeypatch.setattr(common.jobs, "pending", [])
    monkeypatch.setattr(P, "list_songs", lambda: [P.read_meta(song)])
    background._lead_fix_scan()
    assert common.jobs.state == {}


# ---------------------------------------------------------------- Codex 審查補的情境


def test_lead_fix_marks_before_replacing(song, monkeypatch):
    """換分軌途中失敗（或換完後清理舊 WAV 失敗）也不能跳過標記：KEY 版本失效、旋律待重做要在改分軌之前記下。"""
    import pipeline as P

    _fake_gap(monkeypatch, P)

    def boom(*a, **k):
        raise OSError("換到一半失敗")

    monkeypatch.setattr(P, "_replace_stems", boom)
    with pytest.raises(OSError):
        P.fix_lead_gaps(song)
    assert not P.variant_ready(song, 2, 1.0)
    assert P.read_meta(song).get("pitch_stale") is True


def test_step_separate_marks_before_writing(song, monkeypatch):
    """分離中途失敗：分軌可能已經寫了一部分，也要先標記。"""
    import pipeline as P

    def boom(d, report):
        raise RuntimeError("分離失敗")

    monkeypatch.setattr(P, "module_status", lambda: {"uvr": False})
    monkeypatch.setattr(P, "separate_dsp", boom)
    with pytest.raises(RuntimeError):
        P.step_separate(song, {}, lambda *a: None)
    assert not P.variant_ready(song, 2, 1.0)
    assert P.read_meta(song).get("pitch_stale") is True


def test_invalidate_raises_when_it_cannot_mark(song, monkeypatch):
    """資料夾刪不掉、記號也寫不進去：不能把舊版本當成有效，主唱補回要在改分軌之前停下。"""
    import pathlib

    import pipeline as P

    _fake_gap(monkeypatch, P)
    monkeypatch.setattr(P.shutil, "rmtree", lambda *a, **k: None)
    real = pathlib.Path.write_text

    def write_text(self, *a, **k):
        if self.name == P.VARIANT_STALE:
            raise PermissionError("磁碟滿了")
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "write_text", write_text)
    with pytest.raises(OSError):
        P.fix_lead_gaps(song)
    assert (_level(P, song, "lead"), _level(P, song, "back")) == (0.1, 0.2)


def test_replace_stems_keeps_backup_when_restore_fails(song, monkeypatch):
    """第二軌換不了、第一軌換回也失敗：原本的第一軌留在 .bak，不能被刪掉。"""
    import pipeline as P

    real = P._replace_retry

    def replace(src, dst):
        if src.name.endswith(".bak") or (dst.name == "back.flac" and src.name.startswith("back.tmp")):
            raise PermissionError("檔案被佔用")
        real(src, dst)

    monkeypatch.setattr(P, "_replace_retry", replace)
    with pytest.raises(PermissionError):
        P._replace_stems(song, {"lead": np.full((2, N), 0.6, np.float32), "back": np.full((2, N), 0.7, np.float32)})
    import soundfile as sf

    bak = song / "lead.flac.bak"
    assert bak.exists()
    assert round(float(sf.read(str(bak), dtype="float32")[0].mean()), 2) == 0.1
    assert _level(P, song, "back") == 0.2


def test_replace_stems_ignores_old_wav_cleanup_failure(song, monkeypatch):
    """原本是舊的 WAV：兩軌新的 FLAC 都換好了，刪舊 WAV 被佔用不能讓呼叫端以為沒換成。"""
    import pathlib

    import pipeline as P

    for name in ("lead", "back"):
        (song / f"{name}.flac").unlink()
        P.write_wav(song / f"{name}.wav", np.full((2, N), 0.1 if name == "lead" else 0.2, np.float32))
    real = pathlib.Path.unlink

    def unlink(self, *a, **k):
        if self.suffix == ".wav" and self.stem in ("lead", "back"):
            raise PermissionError("檔案被佔用")
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    P._replace_stems(song, {"lead": np.full((2, N), 0.6, np.float32), "back": np.full((2, N), 0.7, np.float32)})
    assert (_level(P, song, "lead"), _level(P, song, "back")) == (0.6, 0.7)


def test_queued_mixdown_checks_stale_when_it_runs(song, monkeypatch, app_module):
    """mixdown 送出時 KEY 版本還有效，排在重新分離後面；執行時已經失效，不能匯出舊分軌。"""
    import pipeline as P
    from leihu.web import common

    monkeypatch.setattr(common.jobs, "state", {})
    monkeypatch.setattr(common.jobs, "pending", [])
    r = app_module.app.test_client().post(f"/api/songs/{SID}/mixdown?key=2&speed=1", json={})
    assert r.status_code == 200
    (_, fn), = common.jobs.pending
    monkeypatch.setattr(P.shutil, "rmtree", lambda *a, **k: None)
    P.invalidate_variants(song)
    with pytest.raises(P.UserError, match="過期"):
        fn(lambda *a: None)


# ---------------------------------------------------------------- live_light


def test_upgrade_keeps_live_light_when_gpu_stalls(song, monkeypatch):
    """補做高品質時顯示卡又被佔滿、中途改用 CPU 標準模型：還是輕量版，live_light 要留著，之後再補做。"""
    import pipeline as P

    P.update_meta(song, live_light=True)

    def step_separate(d, cfg, report):
        P.update_meta(d, live_light=True)  # 卡住改用 CPU 時 step_separate 自己標的
        return True

    monkeypatch.setattr(P, "live_light_cfg", lambda cfg: None)
    monkeypatch.setattr(P, "step_separate", step_separate)
    monkeypatch.setattr(P, "measure_stem_loudness", lambda d: None)
    P.process_song(SID, ["separate"])(lambda *a: None)
    assert P.read_meta(song).get("live_light") is True


def test_fine_job_keeps_live_light_when_gpu_stalls(song, monkeypatch):
    """精細處理（高品質模型都在時等於補做）中途改用 CPU 標準模型：不算補做完成。"""
    import pipeline as P

    P.update_meta(song, live_light=True)

    def step_separate(d, cfg, report):
        P.update_meta(d, live_light=True)
        return True

    monkeypatch.setattr(P, "hq_models_ready", lambda cfg: True)
    monkeypatch.setattr(P, "step_separate", step_separate)
    monkeypatch.setattr(P, "step_pitch", lambda d, cfg, report: None)
    P.fine_job(SID)(lambda *a: None)
    assert P.read_meta(song).get("live_light") is True


def test_step_separate_reports_light_fallback(song, monkeypatch):
    import pipeline as P

    class GpuStalled(Exception):
        pass

    stall = [True]

    def separate_uvr(d, cfg, report):
        if stall[0] and not cfg.get("_cpu"):
            raise GpuStalled()   # 顯示卡卡住：step_separate 改用 CPU＋標準模型重做

    monkeypatch.setattr(P, "module_status", lambda: {"uvr": True})
    monkeypatch.setattr(P, "separate_uvr", separate_uvr)
    monkeypatch.setattr(P, "is_acapella", lambda d: False)
    assert P.step_separate(song, {}, lambda *a: None) is True
    stall[0] = False
    assert P.step_separate(song, {}, lambda *a: None) is False


# ---------------------------------------------------------------- 讀自己的分軌


def test_read_audio_without_fallback_raises_on_bad_file(tmp_path, app_module):
    """soundfile 讀不了時，librosa 會退回 audioread（可能讀出殘缺的音訊、不報錯）；讀自己的分軌要直接報錯。"""
    import soundfile as sf

    import pipeline as P

    bad = tmp_path / "lead.flac"
    bad.write_bytes(b"fLaC" + b"\x00" * 64)
    with pytest.raises(sf.SoundFileRuntimeError):
        P.read_audio(bad, fallback=False)


def test_read_audio_without_fallback_matches_default(song):
    """同一個檔案兩種讀法數值完全相同（只差讀不了時要不要退回）。"""
    import pipeline as P

    p = P.stem_path(song, "lead")
    for kw in ({}, {"sr": P.PITCH_SR, "mono": True}):
        assert np.array_equal(P.read_audio(p, **kw), P.read_audio(p, fallback=False, **kw))


# ---------------------------------------------------------------- Codex 第二輪


def test_running_variant_finishing_after_stems_changed_stays_stale(song, monkeypatch):
    """移調已經讀了舊分軌在算，這時分軌被換掉（資料夾刪不掉、留了 .stale）：移調做完不能把記號清掉。"""
    import pipeline as P

    vd = P.variant_dir(song, 2, 1.0)
    P.update_meta(song)
    (vd / P.VARIANT_STALE).write_text("1", "utf-8")  # 先讓它整份重做
    monkeypatch.setattr(P, "live_status", lambda *a: {"hold": False})
    changed = []

    def shift_piece(y, sr, k, s, a, b):
        if not changed:   # 算到一半：重新分離寫了新的分軌，KEY 版本資料夾刪不掉
            changed.append(1)
            monkeypatch.setattr(P.shutil, "rmtree", lambda *a, **kw: None)
            P._replace_stems(song, {"lead": np.full((2, N), 0.6, np.float32), "back": np.full((2, N), 0.7, np.float32)})
            P.invalidate_variants(song)
        sa = max(0, a - sr)
        return y[:, sa:b + sr], sa

    monkeypatch.setattr(P, "shift_piece", shift_piece)
    P.step_variant(song, 2, 1.0, lambda *a: None)
    assert not P.variant_ready(song, 2, 1.0)


def test_compact_keeps_newer_flac_over_leftover_wav(song):
    """主唱補回換好了新的 FLAC，舊 WAV 刪不掉留著；之後「轉成 FLAC 節省空間」不能拿舊 WAV 蓋掉新分軌。"""
    import pipeline as P

    P.write_wav(song / "lead.wav", np.full((2, N), 0.1, np.float32))
    P.save_stem(song, "lead", np.full((2, N), 0.6, np.float32))
    P.write_wav(song / "lead.wav", np.full((2, N), 0.1, np.float32))  # save_stem 刪掉了，再放回去當作刪不掉的舊檔
    P.compact_song(song)
    assert _level(P, song, "lead") == 0.6
    assert not (song / "lead.wav").exists()


def test_replace_stems_ignores_backup_cleanup_failure(song, monkeypatch):
    """兩軌都換好了，.bak 被防毒暫時佔用刪不掉：不能回報成失敗（呼叫端會跳過完成記錄和旋律分析）。"""
    import pathlib

    import pipeline as P

    real = pathlib.Path.unlink

    def unlink(self, *a, **k):
        if self.name.endswith(".bak"):
            raise PermissionError("檔案被佔用")
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    P._replace_stems(song, {"lead": np.full((2, N), 0.6, np.float32), "back": np.full((2, N), 0.7, np.float32)})
    assert (_level(P, song, "lead"), _level(P, song, "back")) == (0.6, 0.7)
