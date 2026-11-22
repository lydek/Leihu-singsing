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
