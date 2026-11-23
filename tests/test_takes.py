"""docs/next-version-plan.md 第 2 批：M7 與 9 的試聽檔。評分、Claude 建議、試聽在處理期間錄音被刪掉，
不能再寫回 JSON 或 wav，不然留下孤兒檔（錄音清單裡出現沒有錄音檔的紀錄）。"""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

SID, TID = "taketest01", "20261123-1200-abcd"


@pytest.fixture
def take(app_module):
    import soundfile as sf

    import pipeline as P

    d = P.LIB / SID
    td = d / "takes"
    td.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": SID, "title": "t"}), "utf-8")
    (td / f"{TID}.json").write_text(json.dumps({"id": TID, "status": "scoring", "acc": 0.5, "mean": 0.0,
                                                "spread": 0.0, "key": 0, "speed": 1.0}), "utf-8")
    sf.write(str(td / f"{TID}.wav"), np.zeros(1600, dtype=np.float32), 16000)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _delete(app_module):
    assert app_module.app.test_client().delete(f"/api/songs/{SID}/takes/{TID}").status_code == 200


def _leftovers(d):
    return sorted(p.name for p in (d / "takes").iterdir())


def test_score_take_does_not_recreate_deleted_take(app_module, take, monkeypatch):
    """M7：評分期間錄音被刪掉，評分結束時不能再寫出 JSON。"""
    import pipeline as P

    def read_audio(*a, **k):
        _delete(app_module)  # 讀完錄音、還在分析時使用者按了刪除
        return np.zeros(1600, dtype=np.float32)

    monkeypatch.setattr(P, "read_audio", read_audio)
    monkeypatch.setattr(P, "track_pitch", lambda *a, **k: np.full(400, 60.0, dtype=np.float32))
    monkeypatch.setattr(P, "load_pitch", lambda d: (np.full(2000, 60.0, dtype=np.float32), []))
    monkeypatch.setattr(P, "onset_offset", lambda *a, **k: None)
    monkeypatch.setattr(P, "lyric_lines", lambda d: [])
    P.score_take(take, TID, lambda *a: None)
    assert _leftovers(take) == []


def test_claude_coach_does_not_recreate_deleted_take(app_module, take, monkeypatch):
    """M7：等 Claude 回覆期間錄音被刪掉，建議照樣回傳（已經付費），但不寫回 JSON。"""
    import claude_api as C

    def ask_text(*a, **k):
        _delete(app_module)
        return "多練高音", {"cost": 0}

    monkeypatch.setattr(C, "ask_text", ask_text)
    r = app_module.app.test_client().post(f"/api/songs/{SID}/claude/coach", json={"take": TID})
    assert r.status_code == 200
    assert r.get_json()["text"] == "多練高音"
    assert _leftovers(take) == []


def test_preview_after_take_deleted_returns_404(app_module, take, monkeypatch):
    """9：混音期間錄音被刪掉，不寫試聽檔、回 404。"""
    import share as SH
    from leihu.web import takes

    def mix_take(*a, **k):
        _delete(app_module)
        return np.zeros((2, 100), dtype=np.float32), 44100, None

    monkeypatch.setattr(takes, "_need_variant", lambda d, sid, tid: None)
    monkeypatch.setattr(SH, "mix_take", mix_take)
    r = app_module.app.test_client().post(f"/api/songs/{SID}/takes/{TID}/preview", json={})
    assert r.status_code == 404
    assert _leftovers(take) == []


def test_mixprep_after_take_deleted_returns_404(app_module, take, monkeypatch, tmp_path):
    """9：處理試聽用人聲期間錄音被刪掉，不寫暫存人聲、回 404。"""
    import soundfile as sf

    import pipeline as P
    import share as SH
    from leihu.web import takes

    back = tmp_path / "back.wav"
    sf.write(str(back), np.zeros((100, 2), dtype=np.float32), 44100)

    def vocal_track(*a, **k):
        _delete(app_module)
        return np.zeros((2, 100), dtype=np.float32)

    monkeypatch.setattr(takes, "_need_variant", lambda d, sid, tid: None)
    monkeypatch.setattr(SH, "load_take", lambda d, tid: {})
    monkeypatch.setattr(SH, "variant_needed", lambda d, info: (0, 1.0))
    monkeypatch.setattr(P, "stem_path", lambda vd, name: back)
    monkeypatch.setattr(SH, "vocal_track", vocal_track)
    r = app_module.app.test_client().post(f"/api/songs/{SID}/takes/{TID}/mixprep", json={})
    assert r.status_code == 404
    assert _leftovers(take) == []


def test_batch_delete_removes_all_take_files(app_module, take):
    """批次刪除和單筆刪除刪掉同樣的檔案（JSON、錄音、暫存人聲、試聽檔）。"""
    td = take / "takes"
    (td / f"{TID}.vocal_v2_1_light.wav").write_bytes(b"x")
    (td / f"{TID}.preview.wav").write_bytes(b"x")
    r = app_module.app.test_client().post(f"/api/songs/{SID}/takes/delete", json={"ids": [TID]})
    assert r.get_json() == {"deleted": 1}
    assert _leftovers(take) == []


@pytest.mark.parametrize("route", ["preview", "mixprep"])
def test_take_deleted_while_reading_returns_404(app_module, take, monkeypatch, tmp_path, route):
    """Codex r4-6：錄音在讀取階段（mix_take／vocal_track 讀 JSON、WAV 時）被刪掉，原本 FileNotFoundError 由 S15 回 500。"""
    import soundfile as sf

    import pipeline as P
    import share as SH
    from leihu.web import takes

    back = tmp_path / "back.wav"
    sf.write(str(back), np.zeros((100, 2), dtype=np.float32), 44100)

    def reads_deleted(*a, **k):
        _delete(app_module)
        raise FileNotFoundError(2, "No such file", f"{TID}.wav")

    monkeypatch.setattr(takes, "_need_variant", lambda d, sid, tid: None)
    monkeypatch.setattr(SH, "mix_take", reads_deleted)
    monkeypatch.setattr(SH, "load_take", lambda d, tid: {})
    monkeypatch.setattr(SH, "variant_needed", lambda d, info: (0, 1.0))
    monkeypatch.setattr(P, "stem_path", lambda vd, name: back)
    monkeypatch.setattr(SH, "vocal_track", reads_deleted)
    r = app_module.app.test_client().post(f"/api/songs/{SID}/takes/{TID}/{route}", json={})
    assert r.status_code == 404


def test_other_missing_file_is_still_an_error(app_module, take, monkeypatch):
    """錄音還在、是別的檔案不見（例如伴奏）：照原本的錯誤處理（500 + log），不能說成錄音不見了。"""
    import share as SH
    from leihu.web import takes

    def missing_stem(*a, **k):
        raise FileNotFoundError(2, "No such file", "back.flac")

    monkeypatch.setattr(takes, "_need_variant", lambda d, sid, tid: None)
    monkeypatch.setattr(SH, "mix_take", missing_stem)
    r = app_module.app.test_client().post(f"/api/songs/{SID}/takes/{TID}/preview", json={})
    assert r.status_code == 500
