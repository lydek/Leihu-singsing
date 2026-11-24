"""第 7 批：顯示卡處理程式開不起來時交給 CPU 處理程式，不在伺服器裡做（next-version-plan.md 第 1 節）。

錯誤要讓使用者看得到、log 要記：log 有原因和 traceback、交給 CPU 的每個工作、10 分鐘期間的開始和結束；
歌曲的說明寫「顯示卡處理程式無法啟動，這首改用 CPU 處理：原因」；/api/status 帶 gpu_fallback；
兩種都開不起來時，歌曲的錯誤訊息是真正的原因。
"""

from __future__ import annotations

import json
import shutil
import sys
import time

import pytest

SID = "cpufb0001"
WHY = "顯示卡處理程式啟動失敗（代碼 1）：ModuleNotFoundError: No module named 'torch'"


@pytest.fixture
def G(app_module, monkeypatch):
    import gpu_worker as G

    monkeypatch.setattr(G, "_BROKEN", {"until": 0.0, "why": ""})
    monkeypatch.setattr(G.CURRENT, "cpu_fallback", None, raising=False)
    return G


def _gpu_fails(G, monkeypatch, calls):
    def gpu(name, *a, **k):
        calls.append(("gpu", name))
        raise G.WorkerFailed(WHY)

    def cpu(name, *a, **k):
        calls.append(("cpu", name, k.get("low")))
        return "cpu-result"

    monkeypatch.setattr(G.POOL, "call", gpu)
    monkeypatch.setattr(G.CPU_POOL, "call", cpu)


def test_worker_failed_goes_to_cpu_pool_not_server(G, monkeypatch, capsys):
    """原本改在伺服器裡做（_local），伺服器會載入 torch；改成交給 CPU 處理程式，log 寫原因、traceback、期間開始。"""
    calls = []
    _gpu_fails(G, monkeypatch, calls)
    before = set(sys.modules)
    assert G.call("pitch", "y16") == "cpu-result"
    assert calls == [("gpu", "pitch"), ("cpu", "pitch", True)]
    assert not ({"torch", "torchcrepe"} & (set(sys.modules) - before))
    assert not hasattr(G, "_local")
    log = capsys.readouterr()
    text = log.out + log.err
    assert "Traceback" in text and "WorkerFailed" in text
    assert "接下來 10 分鐘的工作改交給 CPU 處理程式" in text and "No module named 'torch'" in text
    assert "pitch 交給 CPU 處理程式（顯示卡處理程式無法啟動" in text
    assert G.CURRENT.cpu_fallback == WHY
    assert G.broken_info()["why"] == WHY and G.broken_info()["until"] > time.time()


def test_within_period_goes_straight_to_cpu(G, monkeypatch):
    calls = []
    _gpu_fails(G, monkeypatch, calls)
    G._BROKEN.update(until=time.time() + 600, why=WHY)
    assert G.call("separate", "m") == "cpu-result"
    assert calls == [("cpu", "separate", True)]   # 10 分鐘內不再開顯示卡處理程式


def test_period_end_is_logged_and_gpu_retried(G, monkeypatch, capsys):
    G._BROKEN.update(until=time.time() - 1, why=WHY)
    monkeypatch.setattr(G.POOL, "call", lambda name, *a, **k: "gpu-result")
    assert G.call("pitch", "y16") == "gpu-result"
    assert "10 分鐘到了，重新試著開顯示卡處理程式" in capsys.readouterr().out
    assert G.broken_info() is None and G._BROKEN["why"] == ""


def test_both_fail_reports_real_reason(G, monkeypatch, capsys):
    """兩種處理程式都開不起來：這首失敗，錯誤訊息是真正的原因（不是 SystemExit: 1 或「代碼 1」而已）。"""
    import pipeline as P

    def cpu(name, *a, **k):
        raise G.WorkerFailed("CPU 處理程式啟動失敗（代碼 1）：ImportError: DLL load failed while importing _C")

    monkeypatch.setattr(G.POOL, "call", lambda *a, **k: (_ for _ in ()).throw(G.WorkerFailed(WHY)))
    monkeypatch.setattr(G.CPU_POOL, "call", cpu)
    with pytest.raises(P.UserError) as e:
        G.call("whisper", "y16")
    msg = str(e.value)
    assert msg.startswith("顯示卡和 CPU 處理程式都無法啟動") and "DLL load failed" in msg and "No module named 'torch'" in msg
    assert capsys.readouterr().err.count("Traceback") >= 2   # 兩次失敗都有完整 traceback
    assert e.value.worker_unavailable
    assert G.CURRENT.cpu_fallback is None   # Agy 審查 2：CPU 也失敗的那首不寫「改用 CPU 處理」


def test_live_cpu_pool_start_failure_is_unavailable(G, monkeypatch):
    """直播中直接用 CPU 處理程式（cpu=True）、它開不起來：一樣是處理程式開不起來的錯誤，分離不會去重試、退回。"""
    import pipeline as P

    monkeypatch.setattr(G.CPU_POOL, "call",
                        lambda *a, **k: (_ for _ in ()).throw(G.WorkerFailed("CPU 處理程式啟動失敗（代碼 1）：ImportError: x")))
    with pytest.raises(P.UserError) as e:
        G.call("separate", "m", cpu=True)
    assert str(e.value).startswith("CPU 處理程式無法啟動") and e.value.worker_unavailable


def test_separation_fails_without_retry_when_no_worker(G, app_module, monkeypatch, tmp_path):
    """Agy 審查 1：兩種處理程式都開不起來時，分離原本會當成混合精度、高品質模型的問題重試，累加 _HQ_FAIL、寫「高品質
    人聲分離模型無法使用」，最後改跑聲像分離蓋掉分軌。現在第一次就直接失敗、顯示原因。"""
    import numpy as np

    import pipeline as P

    sid = "cpufb0002"
    d = P.LIB / sid
    d.mkdir(parents=True, exist_ok=True)
    try:
        (d / "meta.json").write_text(json.dumps({"id": sid, "steps": {"import": True}, "notes": []}), "utf-8")
        P.save_stem(d, "mix", np.full((2, 4410), 0.3, np.float32))
        calls, dsp, checked = [], [], []

        def _separate(model, *a, **k):
            calls.append(model)
            raise G._unavailable("顯示卡和 CPU 處理程式都無法啟動：探針")

        for name, val in [("module_status", lambda: {"uvr": True}), ("is_acapella", lambda d: False),
                          ("cuda_ok", lambda: True), ("hqv_ready", lambda: True), ("hq_ready", lambda: True),
                          ("ensure_model", lambda *a, **k: None), ("_autocast_ok", lambda: True),
                          ("_check_model_file", checked.append), ("_separate", _separate),
                          ("separate_dsp", lambda d, report: dsp.append(d)), ("_HQ_FAIL", {"vocal": 0, "kara": 0})]:
            monkeypatch.setattr(P, name, val)
        cfg = {"vocal_quality": "auto", "karaoke_quality": "auto",
               "separation_model": "std_vocal.onnx", "karaoke_model": "std_kara.onnx"}
        with pytest.raises(P.UserError, match="都無法啟動"):
            P.step_separate(d, cfg, lambda *a: None)
        assert calls == [P.HQ_VOCAL]   # 不改一般精度重試、不換標準模型
        assert dsp == [] and checked == [] and P._HQ_FAIL == {"vocal": 0, "kara": 0}
        assert not any("高品質" in n for n in P.read_meta(d)["notes"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cpu_pool_stall_is_not_reported_as_start_failure(G, monkeypatch):
    """CPU 處理程式開起來了、只是沒有回應（GpuStalled 是 WorkerFailed 的子類別）：照一般的卡住處理，不說「無法啟動」。"""
    monkeypatch.setattr(G.POOL, "call", lambda *a, **k: (_ for _ in ()).throw(G.WorkerFailed(WHY)))
    monkeypatch.setattr(G.CPU_POOL, "call", lambda *a, **k: (_ for _ in ()).throw(G.GpuStalled("CPU 處理程式 600 秒沒有回應")))
    with pytest.raises(G.GpuStalled):
        G.call("pitch", "y16")


def test_spawn_failure_message_has_child_traceback(G, tmp_path, monkeypatch):
    """子程式 import 失敗：錯誤訊息帶 traceback 的最後一行，不是只有「代碼 1」。用真的子程式。"""
    (tmp_path / "gpu_worker.py").write_text("raise ImportError('探針：載入失敗')\n", "utf-8")
    monkeypatch.setattr(G, "ROOT", tmp_path)   # 子程式的 cwd：import gpu_worker 會拿到這個壞掉的檔案
    monkeypatch.setattr(G, "_bind_to_job", lambda proc: None)
    pool = G.Pool(cpu=True)
    pool._start()
    try:
        with pytest.raises(G.WorkerFailed) as e:
            pool._spawn()
    finally:
        pool.listener.close()
    assert str(e.value).startswith("CPU 處理程式啟動失敗（代碼 1）：ImportError: 探針：載入失敗")
    assert not list(tmp_path.glob("leihu-spawn-*"))


def test_exit_reason(G, tmp_path):
    p = tmp_path / "e.txt"
    p.write_text("Traceback (most recent call last):\n  File \"x\"\nOSError: [WinError 126] 找不到模組\n\n", "utf-8")
    assert G._exit_reason(p) == "OSError: [WinError 126] 找不到模組" and not p.exists()
    assert G._exit_reason(tmp_path / "none.txt") == "沒有留下原因，詳細情形在 logs/app.log"


@pytest.fixture
def song(app_module):
    import pipeline as P

    d = P.LIB / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": SID, "steps": {}, "notes": ["顯示卡處理程式無法啟動，這首改用 CPU 處理：舊的",
                                                                             "別的說明"]}), "utf-8")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_job_writes_song_note_once(G, song, monkeypatch):
    """佇列做完這首後，把改用 CPU 的原因寫進歌曲的說明；同一種說明只留最新一則，其他說明不動。"""
    import pipeline as P

    q = P.Jobs(pipeline=True)

    def fn(report):
        G.CURRENT.cpu_fallback = WHY   # gpu_worker._cpu_fallback 做的事

    jid = q.submit("process", SID, fn, label="all")
    with q.lock:
        q.pending.clear()
        q.state[jid]["status"] = "running"
        q.running[jid] = "gpu"
    q._work(jid, fn)
    notes = P.read_meta(song)["notes"]
    assert notes == ["別的說明", f"顯示卡處理程式無法啟動，這首改用 CPU 處理：{WHY}"]
    assert G.CURRENT.cpu_fallback is None


def test_status_reports_fallback_only_while_active(G):
    import app

    c = app.app.test_client()
    assert "gpu_fallback" not in c.get("/api/status").get_json()   # 平常的回應不變（契約快照）
    G._BROKEN.update(until=time.time() + 600, why=WHY)
    got = c.get("/api/status").get_json()["gpu_fallback"]
    assert got["why"] == WHY and got["until"] > time.time()
