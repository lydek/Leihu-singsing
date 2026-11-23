"""上游 2.3.5 帶進來的問題，fork 的修正（Codex 審查 2026-11-24 的 C 組，docs/next-version-plan.md 的 fork 修改表）。"""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

SID = "up235test1"
N = 4410


@pytest.fixture
def song(app_module):
    import pipeline as P

    d = P.LIB / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": SID, "steps": {"import": True},
                                             "notes": ["AI 分離失敗，改用聲像分離：上一次的", "別的說明"]}), "utf-8")
    P.save_stem(d, "mix", np.full((2, N), 0.3, np.float32))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _fake_separate(P, monkeypatch, vocal_model: str, karaoke: str, fail_note: bool):
    def separate_uvr(d, cfg, report):
        if fail_note:
            P._hq_fail_note(d, "人聲", RuntimeError("模型載入失敗"))
        P.update_meta(d, vocal_model=vocal_model, karaoke=karaoke)
        for name in ("lead", "back", "inst"):
            P.save_stem(d, name, np.full((2, N), 0.1, np.float32))

    monkeypatch.setattr(P, "separate_uvr", separate_uvr)
    monkeypatch.setattr(P, "module_status", lambda: {"uvr": True})
    monkeypatch.setattr(P, "is_acapella", lambda d: False)
    monkeypatch.setattr(P, "cuda_ok", lambda: True)


def test_hq_failure_note_survives_step_separate(song, monkeypatch):
    """r2-4／r5-2：2.3.5 的「高品質人聲分離模型無法使用…」在 step_separate 收尾時被「刪掉含『分離』的說明」清掉。
    上一次分離的說明在開始時清掉，這次寫的要留著。"""
    import pipeline as P

    _fake_separate(P, monkeypatch, "mdx", "std_kara.onnx", fail_note=True)
    P.step_separate(song, {"vocal_quality": "auto", "karaoke_quality": "standard"}, lambda *a: None)
    notes = P.read_meta(song)["notes"]
    assert any(n.startswith("高品質人聲分離模型無法使用") for n in notes)
    assert not any("上一次的" in n for n in notes)
    assert "別的說明" in notes


@pytest.mark.parametrize(("vocal_model", "karaoke", "want_light"), [
    ("bs_roformer", "HQ", False),     # 要求的高品質都用到了：補做完成
    ("mdx", "HQ", True),              # 人聲高品質失敗改標準
    ("bs_roformer", "std", True),     # 主唱高品質失敗改標準
])
def test_standard_fallback_is_not_a_finished_upgrade(song, monkeypatch, vocal_model, karaoke, want_light):
    """r5-3：補做高品質時高品質失敗改用標準模型，step_separate 原本回 False，呼叫端清掉 live_light、之後不再補做。"""
    import pipeline as P

    kara = P.HQ_KARAOKE if karaoke == "HQ" else "std_kara.onnx"
    _fake_separate(P, monkeypatch, vocal_model, kara, fail_note=False)
    got = P.step_separate(song, {"vocal_quality": "auto", "karaoke_quality": "auto"}, lambda *a: None)
    assert got is want_light


def test_standard_quality_requested_is_not_degraded(song, monkeypatch):
    import pipeline as P

    _fake_separate(P, monkeypatch, "mdx", "std_kara.onnx", fail_note=False)
    assert P.step_separate(song, {"vocal_quality": "standard", "karaoke_quality": "standard"}, lambda *a: None) is False
