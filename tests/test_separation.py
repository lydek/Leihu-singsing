"""docs/next-version-plan.md 第 5 批：分離的退回。

22：第一段分離（人聲／伴奏）也有兩層退回（混合精度失敗改一般精度、高品質失敗改標準模型；高品質退回 2.3.5 上游已做，
fork 只改「一般精度成功才標記混合精度不能用」和重疊處理的記憶體不夠）；停止、讓位不改跑聲像分離；
auto 模式顯示卡記憶體不到 6 GB 用標準模型。36：和別首重疊處理時顯示卡記憶體不夠，交給佇列一次一首重做，不自己退回。
3.6：長歌的旋律分析逾時依音訊長度放寬。

都是用假的分離（_separate）測流程；顯示卡上真的記憶體不夠、6 GB 門檻要在筆電實測。
"""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

SID = "septest001"
N = 4410


@pytest.fixture
def song(app_module):
    import pipeline as P

    d = P.LIB / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": SID, "steps": {"import": True}}), "utf-8")
    P.save_stem(d, "mix", np.full((2, N), 0.3, np.float32))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_sep(monkeypatch, app_module):
    """假的 _separate：fail(model, autocast) 回傳要丟的例外（或 None）；記下每次呼叫的模型與混合精度。"""
    import soundfile as sf

    import pipeline as P

    calls, rules = [], {}

    def _separate(model, inp, tmp, overlap, autocast, label, report, start, span, cpu=False):
        calls.append((model, autocast))
        e = rules.get("fail", lambda m, a: None)(model, autocast)
        if e:
            raise e
        out = []
        for kind in ("Vocals", "Instrumental"):
            p = tmp / f"{model}_{len(calls)}_({kind}).wav"
            sf.write(str(p), np.full((N, 2), 0.1, np.float32), P.SR)
            out.append(p)
        return out

    monkeypatch.setattr(P, "_separate", _separate)
    monkeypatch.setattr(P, "ensure_model", lambda name, progress=None: None)
    monkeypatch.setattr(P, "vocal_model", lambda cfg: P.HQ_VOCAL)
    monkeypatch.setattr(P, "karaoke_model", lambda cfg: cfg["karaoke_model"])
    monkeypatch.setattr(P, "_autocast_ok", lambda: True)
    monkeypatch.setattr(P, "fix_lead_gaps", lambda d: 0.0)
    monkeypatch.setattr(P, "harmony_ratio", lambda d: None)
    monkeypatch.setitem(P._AUTOCAST, "bad", False)
    # 2.3.5：高品質失敗次數是整個程式共用的，不重設的話前一個測試的失敗會讓之後的 vocal_model 改用標準模型
    monkeypatch.setattr(P, "_HQ_FAIL", {"vocal": 0, "kara": 0})
    monkeypatch.setattr(P, "_check_model_file", lambda name: None)
    return calls, rules


CFG = {"separation_model": "std_vocal.onnx", "karaoke_model": "std_kara.onnx"}


def _overlapping(monkeypatch, on):
    import gpu_worker as G

    monkeypatch.setattr(G.CURRENT, "overlapping", lambda: on, raising=False)


# ---------------------------------------------------------------- 22：第一段的兩層退回


def test_first_stage_hq_failure_falls_back_to_standard(song, fake_sep, monkeypatch):
    """高品質人聲分離（BS-Roformer）失敗：改用標準模型，不要整首退回聲像分離。"""
    import pipeline as P

    calls, rules = fake_sep
    rules["fail"] = lambda m, a: RuntimeError("模型載入失敗") if m == P.HQ_VOCAL else None
    _overlapping(monkeypatch, False)
    P.separate_uvr(song, dict(CFG), lambda *a: None)
    assert [m for m, _ in calls[:3]] == [P.HQ_VOCAL, P.HQ_VOCAL, "std_vocal.onnx"]
    meta = P.read_meta(song)
    assert meta["vocal_model"] == "mdx"
    assert any(n.startswith("高品質人聲分離模型無法使用") for n in meta.get("notes", []))   # 2.3.5：歌曲清單上看得到原因
    assert P._HQ_FAIL["vocal"] == 1


def test_first_stage_autocast_failure_retries_full_precision(song, fake_sep, monkeypatch):
    """混合精度在這張顯示卡上出問題：之後都改用一般精度，這首用高品質重分一次。"""
    import pipeline as P

    calls, rules = fake_sep
    rules["fail"] = lambda m, a: RuntimeError("cuDNN error") if (m == P.HQ_VOCAL and a) else None
    _overlapping(monkeypatch, False)
    P.separate_uvr(song, dict(CFG), lambda *a: None)
    assert calls[:2] == [(P.HQ_VOCAL, True), (P.HQ_VOCAL, False)]
    assert P._AUTOCAST["bad"] is True
    assert P.read_meta(song)["vocal_model"] == "bs_roformer"


def test_first_stage_oom_does_not_mark_autocast_bad(song, fake_sep, monkeypatch):
    """記憶體不夠不是混合精度的問題：不能把混合精度標成壞掉，直接退回標準模型。"""
    import pipeline as P

    calls, rules = fake_sep
    rules["fail"] = lambda m, a: RuntimeError("CUDA out of memory") if m == P.HQ_VOCAL else None
    _overlapping(monkeypatch, False)
    P.separate_uvr(song, dict(CFG), lambda *a: None)
    assert P._AUTOCAST["bad"] is False
    assert [m for m, _ in calls[:2]] == [P.HQ_VOCAL, "std_vocal.onnx"]


def test_first_stage_cancel_is_not_a_failure(song, fake_sep, monkeypatch):
    import pipeline as P

    calls, rules = fake_sep
    rules["fail"] = lambda m, a: P.JobDeferred("paused")
    with pytest.raises(P.JobDeferred):
        P.separate_uvr(song, dict(CFG), lambda *a: None)
    assert len(calls) == 1 and P._AUTOCAST["bad"] is False


def test_step_separate_does_not_fall_back_to_dsp_on_cancel(song, monkeypatch, app_module):
    """停止、讓位不是分離失敗：原本會改跑聲像分離（載入 librosa、scipy，還寫「AI 分離失敗」的提示）。"""
    import pipeline as P

    def separate_uvr(d, cfg, report):
        raise P.JobCancelled()

    dsp = []
    monkeypatch.setattr(P, "module_status", lambda: {"uvr": True})
    monkeypatch.setattr(P, "separate_uvr", separate_uvr)
    monkeypatch.setattr(P, "separate_dsp", lambda d, report: dsp.append(d))
    with pytest.raises(P.JobCancelled):
        P.step_separate(song, dict(CFG), lambda *a: None)
    assert dsp == []


# ---------------------------------------------------------------- 36：重疊處理時記憶體不夠交給佇列


@pytest.mark.parametrize("stage", ["vocal", "karaoke"])
def test_overlap_oom_is_left_to_the_queue(song, fake_sep, monkeypatch, stage):
    """和別首同時分離時記憶體不夠：不自己退回標準模型，丟給佇列一次一首重做（原本兩首都變成標準品質）。"""
    import pipeline as P

    calls, rules = fake_sep
    monkeypatch.setattr(P, "karaoke_model", lambda cfg: P.HQ_KARAOKE)
    target = P.HQ_VOCAL if stage == "vocal" else P.HQ_KARAOKE
    rules["fail"] = lambda m, a: RuntimeError("CUDA out of memory") if m == target else None
    _overlapping(monkeypatch, True)
    with pytest.raises(RuntimeError, match="out of memory"):
        P.separate_uvr(song, dict(CFG), lambda *a: None)
    assert all(m != "std_vocal.onnx" and m != "std_kara.onnx" for m, _ in calls)
    assert P._HQ_FAIL == {"vocal": 0, "kara": 0}   # 不算高品質模型失敗（2.3.5 連續 2 次就不用）


def test_step_separate_leaves_overlap_oom_to_the_queue(song, monkeypatch, app_module):
    import pipeline as P

    def separate_uvr(d, cfg, report):
        raise RuntimeError("CUDA out of memory")

    dsp = []
    monkeypatch.setattr(P, "module_status", lambda: {"uvr": True})
    monkeypatch.setattr(P, "separate_uvr", separate_uvr)
    monkeypatch.setattr(P, "separate_dsp", lambda d, report: dsp.append(d))
    _overlapping(monkeypatch, True)
    with pytest.raises(RuntimeError):
        P.step_separate(song, dict(CFG), lambda *a: None)
    assert dsp == []
    _overlapping(monkeypatch, False)   # 沒有重疊時照舊退回聲像分離
    P.step_separate(song, dict(CFG), lambda *a: None)
    assert dsp == [song]


def test_lyrics_overlap_oom_is_left_to_the_queue(song, monkeypatch, app_module):
    """歌詞辨識時記憶體不夠也一樣：重疊處理時丟給佇列重做，不寫「辨識沒有完成」。"""
    import pipeline as P

    def step_lyrics(d, cfg, report):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(P, "step_lyrics", step_lyrics)
    monkeypatch.setattr(P, "search_lyrics_later", lambda *a, **k: None)
    _overlapping(monkeypatch, True)
    with pytest.raises(RuntimeError):
        P.process_song(SID, ["lyrics"])(lambda *a: None)
    _overlapping(monkeypatch, False)
    P.process_song(SID, ["lyrics"])(lambda *a: None)
    assert any("辨識沒有完成" in n for n in P.read_meta(song)["notes"])


def test_jobs_tells_whether_running_alongside(app_module):
    """佇列放一個 overlapping() 給分離用：和別首同時在跑、而且這首還沒因為記憶體不夠重做過。"""
    import gpu_worker as G
    import pipeline as P

    seen = []
    q = P.Jobs(pipeline=True)

    def fn(report):
        seen.append(G.CURRENT.overlapping())

    for flags in ({}, {"oom_retry": True}):
        other = q.submit("process", "other00001", lambda r: None, label="youtube")
        jid = q.submit("process", SID, fn, label="all")
        with q.lock:
            q.pending.clear()
            for j in (other, jid):
                q.state[j]["status"] = "running"
                q.running[j] = "gpu"
            q.state[jid].update(flags)
        q._work(jid, fn)
        q.running.clear()
    assert seen == [True, False]


# ---------------------------------------------------------------- 22：auto 模式的 6 GB 門檻


@pytest.mark.parametrize(("vram", "hq"), [(4e9, False), (8e9, True), (None, True)])
def test_auto_quality_checks_vram(monkeypatch, app_module, vram, hq):
    """auto 模式：顯示卡記憶體不到 6 GB 就用標準模型（BS-Roformer 放不下）；還不知道記憶體大小時照舊。"""
    import pipeline as P

    started = []
    monkeypatch.setattr(P, "_HQ_FAIL", {"vocal": 0, "kara": 0})
    monkeypatch.setattr(P, "cuda_ok", lambda: True)
    monkeypatch.setattr(P, "gpu_info", lambda block=True: {"ok": True, "vram": vram} if vram else None)
    monkeypatch.setattr(P, "hqv_ready", lambda: True)
    monkeypatch.setattr(P, "hq_ready", lambda: True)
    monkeypatch.setattr(P, "start_hqv_download", lambda: started.append("v"))
    monkeypatch.setattr(P, "start_hq_download", lambda: started.append("k"))
    cfg = dict(CFG, vocal_quality="auto", karaoke_quality="auto")
    assert (P.vocal_model(cfg) == P.HQ_VOCAL) is hq
    assert (P.karaoke_model(cfg) == P.HQ_KARAOKE) is hq
    assert P.wants_hq(cfg) is hq
    assert started == []
    high = dict(cfg, vocal_quality="high")   # 使用者明確選高品質：照用
    assert P.vocal_model(high) == P.HQ_VOCAL


def test_hq_fail_count_still_applies_with_vram_check(monkeypatch, app_module):
    """6 GB 門檻和 2.3.5 的「這次執行高品質連續失敗 2 次先不用」兩個條件都要成立才用高品質。"""
    import pipeline as P

    monkeypatch.setattr(P, "cuda_ok", lambda: True)
    monkeypatch.setattr(P, "gpu_info", lambda block=True: {"ok": True, "vram": 8e9})
    monkeypatch.setattr(P, "hqv_ready", lambda: True)
    monkeypatch.setattr(P, "hq_ready", lambda: True)
    monkeypatch.setattr(P, "_HQ_FAIL", {"vocal": 2, "kara": 1})
    cfg = dict(CFG, vocal_quality="auto", karaoke_quality="high")
    assert P.vocal_model(cfg) == "std_vocal.onnx"
    assert P.karaoke_model(cfg) == P.HQ_KARAOKE


# ---------------------------------------------------------------- 3.6：長歌的旋律分析逾時


def test_pitch_stall_grows_with_audio_length(app_module):
    """旋律分析沒有中間進度：固定 600 秒時，長歌在 CPU 上會被當成卡住。放寬成音訊長度的 3 倍。"""
    import gpu_worker as G

    short = np.zeros(16000 * 60, np.float32)
    long = np.zeros(16000 * 400, np.float32)
    assert G._stall("pitch", (short,)) == G.STALL["pitch"]
    assert G._stall("pitch", (long,)) == 1200
    assert G._stall("separate", (long,)) == G.STALL["separate"]


# ---------------------------------------------------------------- Codex 審查補的情境


class GpuStalled(Exception):   # 和 gpu_worker.GpuStalled 同名（pipeline 用類別名稱判斷）
    pass


def test_both_precisions_failing_does_not_mark_autocast_bad(song, fake_sep, monkeypatch):
    """模型壞掉之類的錯誤，混合精度和一般精度都會失敗：不是混合精度的問題，不能讓之後每首都停用混合精度。"""
    import pipeline as P

    calls, rules = fake_sep
    rules["fail"] = lambda m, a: RuntimeError("模型檔損壞") if m == P.HQ_VOCAL else None
    _overlapping(monkeypatch, False)
    P.separate_uvr(song, dict(CFG), lambda *a: None)
    assert P._AUTOCAST["bad"] is False
    assert [m for m, _ in calls[:3]] == [P.HQ_VOCAL, P.HQ_VOCAL, "std_vocal.onnx"]


def test_second_stage_uses_full_precision_after_first_stage_retry(song, fake_sep, monkeypatch):
    """第一段改用一般精度成功後，這首的第二段也用一般精度（不要再失敗一次、重分一次）。"""
    import pipeline as P

    calls, rules = fake_sep
    monkeypatch.setattr(P, "karaoke_model", lambda cfg: P.HQ_KARAOKE)
    rules["fail"] = lambda m, a: RuntimeError("cuDNN error") if (m == P.HQ_VOCAL and a) else None
    _overlapping(monkeypatch, False)
    P.separate_uvr(song, dict(CFG), lambda *a: None)
    assert calls == [(P.HQ_VOCAL, True), (P.HQ_VOCAL, False), (P.HQ_KARAOKE, False)]


@pytest.mark.parametrize("stage", ["vocal", "karaoke"])
def test_gpu_stall_is_left_to_step_separate(song, fake_sep, monkeypatch, stage):
    """顯示卡沒有進度（GpuStalled）：不當成高品質失敗在顯示卡上改用標準模型，交給 step_separate 改用 CPU、設 live_light。"""
    import pipeline as P

    calls, rules = fake_sep
    monkeypatch.setattr(P, "karaoke_model", lambda cfg: P.HQ_KARAOKE)
    target = P.HQ_VOCAL if stage == "vocal" else P.HQ_KARAOKE
    rules["fail"] = lambda m, a: GpuStalled("120 秒沒有回應") if m == target else None
    _overlapping(monkeypatch, False)
    with pytest.raises(GpuStalled):
        P.separate_uvr(song, dict(CFG), lambda *a: None)
    assert [m for m, _ in calls].count(target) == 1
    assert P._AUTOCAST["bad"] is False


def test_overlap_judgement_survives_other_song_finishing(song, monkeypatch, app_module):
    """分離那層判定是重疊處理時記憶體不夠，往上丟的途中另一首剛好做完：step_separate 不能改跑聲像分離，
    佇列也要照樣排回去重做（原本 len(running) 變成 1，直接標成錯誤）。"""
    import pipeline as P

    _overlapping(monkeypatch, True)
    err = RuntimeError("CUDA out of memory")
    assert P._overlap_oom(err)
    _overlapping(monkeypatch, False)   # 另一首做完了

    def separate_uvr(d, cfg, report):
        raise err

    dsp = []
    monkeypatch.setattr(P, "module_status", lambda: {"uvr": True})
    monkeypatch.setattr(P, "separate_uvr", separate_uvr)
    monkeypatch.setattr(P, "separate_dsp", lambda d, report: dsp.append(d))
    with pytest.raises(RuntimeError):
        P.step_separate(song, dict(CFG), lambda *a: None)
    assert dsp == []

    q = P.Jobs(pipeline=True)

    def fn(report):
        raise err

    jid = q.submit("process", SID, fn, label="all")
    with q.lock:
        q.pending.clear()
        q.state[jid]["status"] = "running"
        q.running[jid] = "gpu"   # 只剩這首在跑
    monkeypatch.setattr(P, "free_gpu", lambda: None)
    q._work(jid, fn)
    st = q.get(jid)
    assert st["status"] == "queued" and st.get("oom_retry")


# ---------------------------------------------------------------- Codex 審查（2026-11-24）r2-1～r2-3


def test_overlap_still_counts_right_after_other_song_finished(app_module, monkeypatch):
    """r2-1：OOM 從子程式傳回來前另一首剛好做完：第一次判定時只剩一首，原本會當成一般的高品質失敗。
    別首做完後 OVERLAP_GRACE 內仍算重疊；過了就不算。"""
    import pipeline as P

    q = P.Jobs(pipeline=True)
    other = q.submit("process", "other00001", lambda r: None, label="youtube")
    jid = q.submit("process", SID, lambda r: None, label="all")
    with q.lock:
        q.pending.clear()
        for j in (other, jid):
            q.state[j]["status"] = "running"
            q.running[j] = "gpu"
    q._work(other, lambda r: None)   # 另一首做完：finally 從 running 拿掉、記下 overlap_end
    assert list(q.running) == [jid]
    assert q._overlapping(jid) is True
    t0 = P.time.time()
    monkeypatch.setattr(P.time, "time", lambda: t0 + P.OVERLAP_GRACE + 1)
    assert q._overlapping(jid) is False


def test_oom_retry_waits_for_all_running_jobs(app_module):
    """r2-2：記憶體不夠重做的工作，要等上一首的旋律分析（post）也做完才開始，才是真的一次一首。"""
    import pipeline as P

    q = P.Jobs(pipeline=True)
    prev = q.submit("process", "other00001", lambda r: None, label="all")
    with q.lock:
        q.pending.clear()
        q.state[prev]["status"] = "running"
        q.running[prev] = "post"
    retry = {"kind": "process", "label": "all", "song": SID, "oom_retry": True}
    assert q._can_start(dict(retry, oom_retry=False), 2) is True   # 一般工作可以和旋律分析重疊
    assert q._can_start(retry, 2) is False
    q.running.clear()
    assert q._can_start(retry, 2) is True


def test_lyrics_passes_raise_oom_only_when_overlapping(song, monkeypatch, app_module):
    """r2-3：歌詞辨識在子程式裡遇到 OOM 會自己改跑 CPU，佇列不知道要降並行；重疊處理時要求子程式直接丟出來。"""
    import gpu_worker as G
    import pipeline as P

    seen = []

    def fake_call(name, *args, progress=None, **kw):
        seen.append(kw.get("raise_oom"))
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(G, "call", fake_call)
    monkeypatch.setattr(P, "module_status", lambda: {"whisper": True})
    monkeypatch.setattr(P, "analysis_vocal", lambda d: np.zeros(16000, np.float32))
    for on in (True, False):
        _overlapping(monkeypatch, on)
        with pytest.raises(RuntimeError):
            P.step_lyrics(song, dict(CFG), lambda *a: None)
    assert seen == [True, False]


def test_whisper_worker_raises_oom_when_asked(monkeypatch):
    """r2-3：子程式的 t_whisper：raise_oom 為真時 OOM 直接丟出去，不改用 CPU 重跑。"""
    import types

    import gpu_worker as G

    class Model:
        model = types.SimpleNamespace(unload_model=lambda: None)

        def transcribe(self, *a, **k):
            raise RuntimeError("CUDA failed with error out of memory")

    P = types.SimpleNamespace(_whisper_model=lambda cfg, raise_oom=False: Model(),
                              pick_language=lambda *a: ("ja", ""), PITCH_SR=16000)
    with pytest.raises(RuntimeError, match="out of memory"):
        G.t_whisper(P, lambda *a: None, np.zeros(16000, np.float32), {}, "", False, raise_oom=True)
