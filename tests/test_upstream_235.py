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


def _fake_models(P, monkeypatch, fail=(), hq_fail=None):
    """真的跑 separate_uvr，只換掉模型推論：fail 裡的高品質模型（"vocal"／"kara"）載入失敗。"""
    import soundfile as sf

    monkeypatch.setattr(P, "module_status", lambda: {"uvr": True})
    monkeypatch.setattr(P, "is_acapella", lambda d: False)
    monkeypatch.setattr(P, "cuda_ok", lambda: True)
    monkeypatch.setattr(P, "hqv_ready", lambda: True)
    monkeypatch.setattr(P, "hq_ready", lambda: True)
    monkeypatch.setattr(P, "ensure_model", lambda *a, **k: None)
    monkeypatch.setattr(P, "_autocast_ok", lambda: False)
    monkeypatch.setattr(P, "_check_model_file", lambda name: None)
    monkeypatch.setattr(P, "fix_lead_gaps", lambda d: None)
    monkeypatch.setattr(P, "harmony_ratio", lambda d: None)
    monkeypatch.setattr(P, "_HQ_FAIL", dict(hq_fail or {"vocal": 0, "kara": 0}))

    def _separate(model, inp, tmp, *a):
        if (model == P.HQ_VOCAL and "vocal" in fail) or (model == P.HQ_KARAOKE and "kara" in fail):
            raise RuntimeError("模型載入失敗")
        outs = []
        for kind in ("Vocals", "Instrumental"):
            out = tmp / f"{model}_({kind}).wav"
            sf.write(str(out), np.full((N, 2), 0.1, np.float32), P.SR)
            outs.append(out)
        return outs

    monkeypatch.setattr(P, "_separate", _separate)


CFG = {"vocal_quality": "auto", "karaoke_quality": "auto",
       "separation_model": "std_vocal.onnx", "karaoke_model": "std_kara.onnx"}


@pytest.mark.parametrize(("fail", "want_light"), [
    ((), False),           # 要求的高品質都用到了：補做完成
    (("vocal",), True),    # 這次人聲高品質失敗改標準
    (("kara",), True),     # 這次主唱高品質失敗改標準
])
def test_standard_fallback_is_not_a_finished_upgrade(song, monkeypatch, fail, want_light):
    """r5-3：補做高品質時高品質失敗改用標準模型，step_separate 原本回 False，呼叫端清掉 live_light、之後不再補做。"""
    import pipeline as P

    _fake_models(P, monkeypatch, fail)
    assert P.step_separate(song, dict(CFG), lambda *a: None) is want_light


def test_hq_disabled_this_run_is_not_a_finished_upgrade(song, monkeypatch):
    """複審 P2-1：高品質這次執行已停用（連續失敗 2 次），直接用標準模型，也不算補做完成。"""
    import pipeline as P

    _fake_models(P, monkeypatch, hq_fail={"vocal": 2, "kara": 0})
    assert P.hq_disabled(CFG)
    assert P.step_separate(song, dict(CFG), lambda *a: None) is True


def test_failures_of_model_no_longer_wanted_do_not_count(song, monkeypatch):
    """Codex r6-1：人聲高品質失敗兩次後改成「標準」、主唱維持高品質：原本 hq_disabled 只看失敗次數，背景補做整批跳過；
    分離用足目前要求的品質也回報「沒用到要求的品質」，live_light 清不掉。"""
    import pipeline as P

    _fake_models(P, monkeypatch, hq_fail={"vocal": 2, "kara": 0})
    cfg = dict(CFG, vocal_quality="standard")
    assert not P.hq_disabled(cfg)
    assert P.step_separate(song, cfg, lambda *a: None) is False
    assert P.hq_disabled(CFG)   # 人聲還要高品質：照樣停用


def test_standard_quality_requested_is_not_degraded(song, monkeypatch):
    """複審 P2-1：設定本來就要標準模型，不是「沒用到要求的品質」（原本的副本判斷靠設定重算）。"""
    import pipeline as P

    _fake_models(P, monkeypatch)
    cfg = dict(CFG, vocal_quality="standard", karaoke_quality="standard")
    assert P.step_separate(song, cfg, lambda *a: None) is False


@pytest.mark.parametrize(("fail", "keep"), [((), False), (("vocal",), True)])
def test_process_song_keeps_live_light_on_fallback(song, monkeypatch, fail, keep):
    """複審 P2-1：補做（process_song）途中高品質失敗改標準，live_light 要留著；都用到高品質才清掉。"""
    import pipeline as P

    _fake_models(P, monkeypatch, fail)
    P.update_meta(song, live_light=True)
    monkeypatch.setattr(P, "load_config", lambda: dict(CFG))
    monkeypatch.setattr(P, "live_light_cfg", lambda cfg: None)
    monkeypatch.setattr(P, "measure_stem_loudness", lambda d: None)
    P.process_song(SID, ["separate"])(lambda *a: None)
    assert bool(P.read_meta(song).get("live_light")) is keep


@pytest.mark.parametrize(("fail", "keep"), [((), False), (("kara",), True)])
def test_fine_job_keeps_live_light_on_fallback(song, monkeypatch, fail, keep):
    """複審 P2-1：精細處理（高品質模型都在時等於補做）途中高品質失敗改標準，不算補做完成。"""
    import pipeline as P

    _fake_models(P, monkeypatch, fail)
    P.update_meta(song, live_light=True)
    monkeypatch.setattr(P, "load_config", lambda: dict(CFG))
    monkeypatch.setattr(P, "step_pitch", lambda d, cfg, report: None)
    P.fine_job(SID)(lambda *a: None)
    assert bool(P.read_meta(song).get("live_light")) is keep


class _Stop(BaseException):
    pass


def _upgrade_once(monkeypatch, disabled_at_schedule: bool, disabled_at_run: bool):
    """跑一輪 live_light_upgrade：回傳排了幾個工作、排進去的工作執行時有沒有真的處理。"""
    import pipeline as P
    from leihu.web import background as B

    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) > 1:   # 第一次是開程式後的等待，第二次是一輪結束
            raise _Stop()

    state = {"disabled": disabled_at_schedule}
    submitted, ran = [], []
    monkeypatch.setattr(B.time, "sleep", sleep)
    monkeypatch.setattr(P, "load_config", lambda: {})
    monkeypatch.setattr(P, "live_status", lambda cfg: {"hold": False})
    monkeypatch.setattr(P, "hq_models_ready", lambda cfg: True)
    monkeypatch.setattr(P, "hq_disabled", lambda cfg: state["disabled"])
    monkeypatch.setattr(P, "list_songs", lambda: [{"id": SID, "live_light": True}])
    monkeypatch.setattr(P, "process_song", lambda sid, steps: lambda report: ran.append(sid))
    monkeypatch.setattr(B, "prerender_key", lambda sid: None)
    monkeypatch.setattr(B.jobs, "find", lambda *a: None)
    monkeypatch.setattr(B.jobs, "submit", lambda kind, sid, fn, label: submitted.append(fn))
    with pytest.raises(_Stop):
        B.live_light_upgrade()
    state["disabled"] = disabled_at_run
    for fn in submitted:
        fn(lambda *a: None)
    return len(submitted), ran


@pytest.mark.parametrize(("at_schedule", "at_run", "want"), [
    (False, False, (1, [SID])),   # 高品質可用：照常補做
    (True, True, (0, [])),        # 高品質這次執行已停用：不排（原本每次開程式都整批用標準模型重做）
    (False, True, (1, [])),       # 排隊期間停用了：執行時跳過，標記留著
])
def test_live_light_upgrade_skips_when_hq_disabled(app_module, monkeypatch, at_schedule, at_run, want):
    """複審 P2-1：高品質一直失敗的電腦上，live_light_upgrade 不要每次開程式都把所有 live_light 的歌用標準模型重做。"""
    assert _upgrade_once(monkeypatch, at_schedule, at_run) == want


def test_bad_model_resets_only_its_own_download_state(app_module, monkeypatch, tmp_path):
    """r5-1：主唱模型檢出壞掉時，人聲模型的下載還在跑：只重設主唱的狀態，人聲照樣是 downloading（搬移才會等它）。"""
    import pipeline as P

    monkeypatch.setattr(P, "MODELS", tmp_path)
    (tmp_path / "uvr").mkdir()
    (tmp_path / "uvr" / P.HQ_KARAOKE).write_bytes(b"broken")
    monkeypatch.setattr(P, "_MODEL_CHECKED", set())
    monkeypatch.setitem(P.HQV_DL, "state", "downloading")
    monkeypatch.setitem(P.HQ_DL, "state", "ready")
    P._check_model_file(P.HQ_KARAOKE)
    assert not (tmp_path / "uvr" / P.HQ_KARAOKE).exists()
    assert P.HQ_DL["state"] == "idle"
    assert P.HQV_DL["state"] == "downloading"


@pytest.fixture
def library_songs(app_module, monkeypatch):
    """重做全部歌曲用：兩首沒分離完的歌，佇列換成空的。"""
    import pipeline as P
    from leihu.web import common

    for q in (common.jobs, common.vjobs):
        monkeypatch.setattr(q, "state", {})
        monkeypatch.setattr(q, "pending", [])
        monkeypatch.setattr(q, "running", {})
    monkeypatch.setattr(P, "hqv_ready", lambda: True)
    monkeypatch.setattr(P, "vocal_model", lambda cfg: P.HQ_VOCAL)
    songs = [{"id": f"unfin0000{i}", "steps": {"import": True}, "created": i} for i in (1, 2)]
    monkeypatch.setattr(P, "list_songs", lambda: songs)
    for s in songs:
        (P.LIB / s["id"]).mkdir(parents=True, exist_ok=True)
        (P.LIB / s["id"] / "meta.json").write_text(json.dumps(s), "utf-8")
    yield common.jobs, [s["id"] for s in songs]
    for s in songs:
        shutil.rmtree(P.LIB / s["id"], ignore_errors=True)


def test_redo_all_includes_recently_failed_song(library_songs):
    """r5-4：jobs.active() 也包含最近 10 分鐘做完、失敗的工作；剛失敗的歌原本被當成處理中而跳過。"""
    jobs, (failed, queued) = library_songs
    j1 = jobs.submit("process", failed, lambda r: None, label="all")
    jobs.update(j1, status="error", error="x")
    jobs.pending[:] = [(j, f) for j, f in jobs.pending if j != j1]   # 測試時佇列不執行：當成已經做完、失敗
    jobs.submit("process", queued, lambda r: None, label="all")   # 這首真的還在排
    import app

    r = app.app.test_client().post("/api/upgrade-separation")
    assert r.status_code == 200 and r.get_json()["unfinished"] == 1
    assert [jobs.state[j]["song"] for j, _ in jobs.pending] == [queued, failed]


def test_redo_all_unfinished_pauses_while_live(library_songs, monkeypatch):
    """r5-5：重做全部歌曲批次補做的工作（原本標籤 separate,pitch）不在 LIVE_BACKGROUND，直播中照樣開始；
    改用 unfinished 標籤，直播中跳過、重開後也照樣是 unfinished。單首優先處理照舊直播中也做。"""
    import pipeline as P
    from leihu.web import background, common

    jobs, (a, b) = library_songs
    import app

    app.app.test_client().post("/api/upgrade-separation")
    labels = [jobs.state[j]["label"] for j, _ in jobs.pending]
    assert labels == ["unfinished", "unfinished"]
    assert "unfinished" in P.LIVE_BACKGROUND
    jid, fn, only_held = jobs._next(hold=True, slots=1)
    assert jid is None and only_held is True        # 直播中：兩個都跳過
    assert background._restorable("unfinished")
    jobs.pending.clear()
    jobs.state.clear()   # 當成重開練唱台：佇列是空的，從 queue.json 排回去
    background._resubmit(a, "unfinished")
    assert [jobs.state[j]["label"] for j, _ in jobs.pending] == ["unfinished"]
    common.submit_unfinished(b, ["separate", "pitch"])   # 單首優先處理：標籤照舊
    assert jobs.state[jobs.pending[-1][0]]["label"] == "separate,pitch"


def test_prioritize_queued_unfinished_runs_while_live(library_songs):
    """複審 P3-4：直播中對已經被「重做全部歌曲」排進去的歌按「優先處理」，原本只移到最前面、標籤仍是
    unfinished，直播中照樣跳過。改成取消後用步驟標籤重排。"""
    jobs, (_, b) = library_songs
    import app

    client = app.app.test_client()
    client.post("/api/upgrade-separation")
    old = next(j for j, _ in jobs.pending if jobs.state[j]["song"] == b)
    r = client.post(f"/api/songs/{b}/prioritize")
    assert r.status_code == 200 and r.get_json()["what"] == "process"
    new = r.get_json()["job"]
    assert jobs.state[old]["status"] == "cancelled"
    assert jobs.state[new]["label"] == "separate,pitch"
    jid, _, _ = jobs._next(hold=True, slots=1)
    assert jid == new   # 直播中照樣開始


def test_reprioritize_leaves_started_job_alone(library_songs):
    """複審 P3-4：取消 unfinished 工作時它剛好開始了，不能把執行中的工作停掉、也不重排。"""
    import pipeline as P
    from leihu.web import system

    jobs, (_, b) = library_songs
    jid = jobs.submit("process", b, lambda r: None, label="unfinished")
    jobs.state[jid]["status"] = "running"
    with pytest.raises(P.UserError, match="正在處理中"):   # Codex r6-2：也不能往下補排一份
        system._reprioritize_unfinished(b, jid)
    assert jobs.state[jid]["status"] == "running" and not jobs.state[jid].get("cancel")


def test_double_prioritize_makes_one_replacement(library_songs, monkeypatch):
    """Codex r6-2：連按兩次優先處理：第一次取消 unfinished、還沒重排時第二次進來看不到工作，又補排一份，
    同一首歌排了兩個 separate,pitch。"""
    import threading

    import app
    from leihu.web import system

    jobs, (_, b) = library_songs
    app.app.test_client().post("/api/upgrade-separation")
    entered, go = threading.Event(), threading.Event()
    real = system.submit_unfinished

    def slow_submit(sid, steps, label=None):
        entered.set()
        go.wait(2)   # 第一次取消舊的、還沒重排
        return real(sid, steps, label)

    monkeypatch.setattr(system, "submit_unfinished", slow_submit)
    codes = []

    def press():
        codes.append(app.app.test_client().post(f"/api/songs/{b}/prioritize").status_code)

    t1 = threading.Thread(target=press)
    t1.start()
    assert entered.wait(2)
    t2 = threading.Thread(target=press)
    t2.start()
    t2.join(0.3)
    go.set()
    t1.join(5)
    t2.join(5)
    mine = [j for j in jobs.state.values() if j["song"] == b and j["status"] == "queued"]
    assert codes == [200, 200] and [j["label"] for j in mine] == ["separate,pitch"]


def test_prioritize_started_between_check_and_cancel(library_songs, monkeypatch):
    """Codex r6-2：看的時候還在排隊、取消前剛好開始了：回「這首正在處理中」，不能再補排一份。"""
    import app
    from leihu.web import system

    jobs, (_, b) = library_songs
    app.app.test_client().post("/api/upgrade-separation")
    real_cancel = jobs.cancel

    def cancel(jid, queued_only=False):
        jobs.state[jid]["status"] = "running"   # 佇列剛好開始做
        return real_cancel(jid, queued_only=queued_only)

    monkeypatch.setattr(system.jobs, "cancel", cancel)
    r = app.app.test_client().post(f"/api/songs/{b}/prioritize")
    assert r.status_code == 400 and r.get_json()["error"] == "這首正在處理中"
    assert [j["label"] for j in jobs.state.values() if j["song"] == b] == ["unfinished"]
