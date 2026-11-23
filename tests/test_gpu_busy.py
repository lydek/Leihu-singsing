"""pipeline.py 模組頂端缺 import subprocess（docs/next-version-plan.md 第 1 批）。

_running_apps 的 tasklist 備援、_sample_gpu_load 的 nvidia-smi 取樣都用到 subprocess，
NameError 被 except Exception 吞掉，這兩段從來沒有真的跑過。
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace


def test_running_apps_tasklist_fallback_runs(monkeypatch):
    """Windows API 查不到程式清單時改用 tasklist；修之前 NameError 被吞掉，回傳 None（查不到）。"""
    import pipeline as P

    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return SimpleNamespace(stdout='"OBS64.exe","1234","Console","1","100,000 K"\r\n')

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(P, "_process_names", lambda: None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert P._running_apps(["obs64.exe", "vtube studio.exe"]) == ["obs64.exe"]
    assert calls and calls[0][0] == "tasklist"


def test_sample_gpu_load_reads_nvidia_smi(monkeypatch):
    """nvidia-smi 的結果寫進 GPU_LOAD；修之前 NameError 被吞掉，GPU_LOAD 一直是 None。"""
    import gpu_worker as G
    import pipeline as P

    monkeypatch.setattr(G.POOL, "count", lambda: 0)  # 自己的顯示卡處理程式開著時不取樣
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: SimpleNamespace(stdout="7000, 8192, 90\n"))
    for k in ("free", "total", "util", "hot"):
        monkeypatch.setitem(P.GPU_LOAD, k, None if k != "hot" else 0)
    P._sample_gpu_load()
    assert P.GPU_LOAD["free"] == 1192 * 2**20
    assert P.GPU_LOAD["total"] == 8192 * 2**20
    assert P.GPU_LOAD["util"] == 90
    assert P.GPU_LOAD["hot"] == 1  # 90% ≥ GPU_BUSY_UTIL，連續兩次才算忙


def _drive(monkeypatch, samples):
    """依序餵 nvidia-smi 的結果（已用 MB, 使用率），每次都重新取樣，回傳每次 gpu_busy_reason 的結果。"""
    import gpu_worker as G
    import pipeline as P

    monkeypatch.setattr(G.POOL, "count", lambda: 0)
    it = iter(samples)
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kw: SimpleNamespace(stdout="{}, 8192, {}\n".format(*next(it))))
    for k, v in {"free": None, "total": None, "util": None, "hot": 0, "calm": 0, "busy": "",
                 "stalled_until": 0.0, "ok_at": 0.0, "err": ""}.items():
        monkeypatch.setitem(P.GPU_LOAD, k, v)
    got = []
    for _ in samples:
        P.GPU_LOAD["checked"] = 0.0   # 每次都當作超過 20 秒，重新取樣
        got.append(bool(P.gpu_busy_reason()))
    return got


def test_gpu_busy_does_not_flap_around_threshold(monkeypatch):
    """筆電 app.log：使用率在 85% 上下跳，判定忙碌後一次低於門檻就恢復，每 20 秒來回切換。
    判定忙碌後要連續 GPU_CALM_SAMPLES 次都低於門檻才恢復。"""
    import pipeline as P

    utils = [90, 90, 50, 90, 50, 90, 90, 50, 50, 90, 50, 50, 50, 50]
    got = _drive(monkeypatch, [(1000, u) for u in utils])
    assert got == [False, True, True, True, True, True, True, True, True, True, True, True, False, False]
    assert P.GPU_CALM_SAMPLES == 3


def test_gpu_busy_low_memory_also_needs_calm_samples(monkeypatch):
    """記憶體不足判定忙碌時同樣規則：記憶體回來、使用率也低，連續 3 次才恢復；還沒忙過就不會被擋。"""
    # 8192-6000 MB ≈ 2.3 GB < 3.5 GB 判定忙碌；記憶體回來後中間又不足一次，要重新數
    got = _drive(monkeypatch, [(1000, 10), (6000, 10), (1000, 10), (1000, 10), (6000, 10),
                               (1000, 10), (1000, 10), (1000, 10), (1000, 10)])
    assert got == [False, True, True, True, True, True, True, False, False]


def test_gpu_busy_expires_when_sampling_keeps_failing(monkeypatch, capsys):
    """Codex r1-2／r4-5：判定忙碌、一次正常取樣後 nvidia-smi 一直失敗（逾時、壞掉）：湊不到 3 次正常取樣，
    原本會一直沿用舊的「記憶體只剩…」、背景工作一直暫停。超過 GPU_STALE 沒有成功取樣就不再沿用；讀不到的原因記一次。"""
    import gpu_worker as G
    import pipeline as P

    clock = [1000.0]
    monkeypatch.setattr(P.time, "time", lambda: clock[0])
    monkeypatch.setattr(G.POOL, "count", lambda: 0)
    results = iter(["6000, 8192, 10\n", "1000, 8192, 10\n"])

    def fake_run(args, **kw):
        out = next(results, None)
        if out is None:
            raise subprocess.TimeoutExpired(args, 8)
        return SimpleNamespace(stdout=out)

    monkeypatch.setattr(subprocess, "run", fake_run)
    for k, v in {"free": None, "total": None, "util": None, "hot": 0, "calm": 0, "busy": "",
                 "stalled_until": 0.0, "ok_at": 0.0, "err": "", "checked": 0.0}.items():
        monkeypatch.setitem(P.GPU_LOAD, k, v)
    got = []
    for _ in range(12):   # 每次 30 秒
        got.append(bool(P.gpu_busy_reason()))
        clock[0] += 30
    assert got[:2] == [True, True]          # 記憶體不足 → 忙碌；一次正常取樣還不恢復
    assert got[-1] is False                 # 之後一直讀不到：超過 GPU_STALE 就不再沿用
    assert sum(got) <= 2 + P.GPU_STALE // 30 + 1
    out = capsys.readouterr().out
    assert out.count("讀不到顯示卡用量") == 1 and "TimeoutExpired" in out


def test_own_worker_skip_is_not_a_sampling_failure(monkeypatch):
    """練唱台自己的顯示卡處理程式開著時刻意不取樣：上一次的忙碌結果照用，不算過期。"""
    import gpu_worker as G
    import pipeline as P

    clock = [1000.0]
    monkeypatch.setattr(P.time, "time", lambda: clock[0])
    monkeypatch.setattr(G.POOL, "count", lambda: 1)
    for k, v in {"free": 1e9, "total": 8e9, "util": 10, "hot": 0, "calm": 0, "busy": "x",
                 "stalled_until": 0.0, "ok_at": 1000.0, "err": "", "checked": 0.0}.items():
        monkeypatch.setitem(P.GPU_LOAD, k, v)
    for _ in range(10):
        clock[0] += 30
        assert P.gpu_busy_reason()
