"""docs/next-version-plan.md 第 4 批：下載與取消。

S8（yt-dlp 的 stderr、逾時、取消時結束）、38 的 .partK、36 的分段下載 206 空資料、ensure_model_data 的鎖、
YouTube 讓位途中按停止、佇列的繼續／讓位／顯示卡記憶體不夠重試看停止、匯出影片中途停止。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

# ---------------------------------------------------------------- S8：yt-dlp

FAKE_YTDLP = r'''
import os, sys, time
out = sys.argv[sys.argv.index("-o") + 1].replace("%(ext)s", "wav")
mode = os.environ["FAKE_YTDLP_MODE"]
if mode == "flood":   # 錯誤訊息寫滿 stderr 的管線緩衝區，之後才在 stdout 印東西
    sys.stderr.write("x" * 300_000 + "\n"); sys.stderr.flush()
    print("TITLE 測試歌曲", flush=True)
    open(out, "wb").write(b"RIFF")
else:                 # 一直下載不完；每 0.1 秒寫一次心跳，停了就代表行程結束了
    beat = os.environ["FAKE_YTDLP_BEAT"]
    open(beat, "w").write(str(time.time()))
    if mode == "hang-prog":
        print("PROG  10.0%", flush=True)
    for _ in range(600):
        open(beat, "w").write(str(time.time()))
        time.sleep(0.1)
'''


@pytest.fixture
def fake_ytdlp(tmp_path, monkeypatch, app_module):
    import pipeline as P

    pkg = tmp_path / "fake" / "yt_dlp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", "utf-8")
    (pkg / "__main__.py").write_text(FAKE_YTDLP, "utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "fake"))
    monkeypatch.setenv("FAKE_YTDLP_BEAT", str(tmp_path / "beat"))
    monkeypatch.setattr(P, "ffmpeg_path", lambda: sys.executable)
    monkeypatch.setattr(P, "_upgrade_ytdlp", lambda: None)
    d = tmp_path / "song"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"id": "ytsong0001"}), "utf-8")
    return d


def _beating(path, wait=1.0) -> bool:
    before = path.read_text()
    time.sleep(wait)
    return path.read_text() != before


def test_s8_stderr_flood_does_not_deadlock(fake_ytdlp, monkeypatch):
    """stderr 和 stdout 分開讀時，stderr 寫滿管線緩衝區，yt-dlp 卡在寫 stderr、我們卡在讀 stdout，互相等。"""
    import pipeline as P

    monkeypatch.setenv("FAKE_YTDLP_MODE", "flood")
    t = threading.Thread(target=P.step_youtube, args=(fake_ytdlp, "https://youtu.be/x", lambda *a: None), daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive(), "step_youtube 卡住了"
    assert (fake_ytdlp / "original.wav").exists()


def test_s8_cancel_kills_ytdlp(fake_ytdlp, monkeypatch):
    """按停止（report 丟 JobCancelled）後 yt-dlp 不能留在背景繼續下載。"""
    import pipeline as P

    monkeypatch.setenv("FAKE_YTDLP_MODE", "hang-prog")

    def report(step, p):
        if "%" in step:
            raise P.JobCancelled()

    with pytest.raises(P.JobCancelled):
        P.step_youtube(fake_ytdlp, "https://youtu.be/x", report)
    assert not _beating(fake_ytdlp.parent / "beat")


def test_s8_timeout_stops_ytdlp(fake_ytdlp, monkeypatch):
    """網路卡住時不會一直占著下載佇列：超過整體逾時就結束 yt-dlp、回報失敗。"""
    import pipeline as P

    monkeypatch.setenv("FAKE_YTDLP_MODE", "hang")
    monkeypatch.setattr(P, "YT_TIMEOUT", 1)
    t0 = time.time()
    with pytest.raises(P.UserError, match="沒有完成"):
        P.step_youtube(fake_ytdlp, "https://youtu.be/x", lambda *a: None)
    assert time.time() - t0 < 20
    assert not _beating(fake_ytdlp.parent / "beat")


# ---------------------------------------------------------------- 分段下載


class _Resp:
    def __init__(self, reads, status=206):
        self.status, self.reads = status, iter(reads)

    def read(self, n):
        r = next(self.reads, b"")
        return r() if callable(r) else r

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


BIG = 16 << 20  # 16 MB 以上才走分段下載


class _Stop(Exception):
    pass


def test_206_without_data_waits_before_retry(tmp_path, monkeypatch, app_module):
    """回 206 後一個位元組都沒給：原本不停頓地重連，每條下載執行緒佔滿一個核心。"""
    import pipeline as P

    calls = []
    monkeypatch.setattr(P, "_open", lambda url, a=0, b=None, timeout=30: calls.append(a) or _Resp([]))
    t0 = time.time()

    def progress(got, size, speed):
        if time.time() - t0 > 1:
            raise _Stop()

    with pytest.raises(_Stop):
        P.fetch_file(["https://example/a"], tmp_path / "f.bin", BIG, progress=progress, conns=1)
    assert len(calls) <= 3


def test_no_write_after_stop(tmp_path, monkeypatch, app_module):
    """讀一段的期間按了停止：讀完的那段不寫進 .partK。"""
    import pipeline as P

    reading = threading.Event()

    def slow():
        reading.set()
        time.sleep(1.0)
        return b"b" * 1000

    monkeypatch.setattr(P, "_open", lambda url, a=0, b=None, timeout=30: _Resp([b"a" * 1000, slow]))
    dst = tmp_path / "f.bin"

    def progress(got, size, speed):
        if reading.is_set():  # 第二段還在讀的時候按停止（寫入有緩衝，不能用檔案大小判斷）
            raise _Stop()

    with pytest.raises(_Stop):
        P.fetch_file(["https://example/a"], dst, BIG, progress=progress, conns=1)
    assert (tmp_path / "f.bin.part0").read_bytes() == b"a" * 1000


def test_next_download_waits_for_stuck_thread(tmp_path, monkeypatch, app_module):
    """取消時 5 秒內沒結束的下載執行緒（卡在 read）還開著 .partK：下次下載同一個檔案前先等它結束。"""
    import pipeline as P

    stuck = threading.Event()

    def very_slow():
        stuck.set()
        time.sleep(6.5)
        return b"b" * 1000

    monkeypatch.setattr(P, "_open", lambda url, a=0, b=None, timeout=30: _Resp([b"a" * 1000, very_slow]))
    dst = tmp_path / "f.bin"

    def progress(got, size, speed):
        if stuck.is_set():
            raise _Stop()

    with pytest.raises(_Stop):
        P.fetch_file(["https://example/a"], dst, BIG, progress=progress, conns=1)
    left = P._LINGER.get(str(dst), [])
    assert left and all(t.is_alive() for t in left)
    monkeypatch.setattr(P, "_open", lambda url, a=0, b=None, timeout=30: _Resp([b"c" * (BIG - a)]))
    P.fetch_file(["https://example/a"], dst, BIG, conns=1)
    assert not any(t.is_alive() for t in left)
    assert dst.stat().st_size == BIG
    assert str(dst) not in P._LINGER


# ---------------------------------------------------------------- ensure_model_data 的鎖


def test_ensure_model_data_runs_one_at_a_time(tmp_path, monkeypatch, app_module):
    """背景下載高品質模型和處理歌曲同時呼叫：小檔的暫存檔名固定，同時下載會互相蓋掉。"""
    import pipeline as P

    body = b'{"ok": true}'
    monkeypatch.setattr(P, "MODELS", tmp_path)
    monkeypatch.setattr(P, "MODEL_DATA", {"d.json": (len(body), hashlib.sha256(body).hexdigest(), ["https://x"])})
    active, peak = [0], [0]
    gate = threading.Lock()

    def fetch(urls, p, size, sha):
        with gate:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.5)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        with gate:
            active[0] -= 1

    monkeypatch.setattr(P, "fetch_file", fetch)
    ts = [threading.Thread(target=P.ensure_model_data) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert peak[0] == 1


# ---------------------------------------------------------------- 佇列：繼續、讓位、停止


def _running_job(q, fn=lambda report: None, label="all", **flags):
    jid = q.submit("process", "queuesong1", fn, label=label)
    with q.lock:
        q.pending[:] = [(j, f) for j, f in q.pending if j != jid]
        q.state[jid].update(status="running", **flags)
        q.running[jid] = "gpu"
    return jid


def test_resume_clears_pause_of_running_job(app_module):
    """按了全部暫停、工作走到下一個檢查點前又按繼續：不用讓位重做。"""
    import pipeline as P

    q = P.Jobs(live_safe=True)
    jid = _running_job(q)
    q.set_paused(True)
    assert q.get(jid).get("pause")
    q.set_paused(False)
    assert not q.get(jid).get("pause")


def test_deferred_job_with_cancel_is_not_requeued(app_module):
    """讓位的同時按了停止：照停止處理，不排回佇列重跑。"""
    import pipeline as P

    def fn(report):
        raise P.JobDeferred("paused")

    q = P.Jobs()
    jid = _running_job(q, fn, cancel=True)
    q._work(jid, fn)
    assert q.get(jid)["status"] == "cancelled"
    assert all(j != jid for j, _ in q.pending)


def test_oom_retry_with_cancel_is_not_requeued(app_module):
    """兩首同時處理、顯示卡記憶體不夠時排回最前面重做；同時按了停止就不重做。"""
    import pipeline as P

    def fn(report):
        raise RuntimeError("CUDA out of memory")

    q = P.Jobs(pipeline=True)
    _running_job(q, label="youtube")
    jid = _running_job(q, fn, cancel=True)
    q._work(jid, fn)
    assert q.get(jid)["status"] == "cancelled"
    assert all(j != jid for j, _ in q.pending)


def test_youtube_deferral_keeps_cancelled_state(app_module, monkeypatch):
    """YouTube 下載讓位途中按了停止：停止的路由已經標成已取消，不能再改回排隊下載
    （否則歌名停在「排隊下載」，重開程式還會自動重新下載）。"""
    import shutil

    import gpu_worker as G
    import pipeline as P

    sid = "ytcancel01"
    d = P.LIB / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": sid, "dl_state": "cancelled"}), "utf-8")

    def step_youtube(d, url, report):
        raise P.JobDeferred("paused")

    def check():
        raise P.JobCancelled()

    monkeypatch.setattr(P, "step_youtube", step_youtube)
    monkeypatch.setattr(G.CURRENT, "check", check, raising=False)
    try:
        with pytest.raises(P.JobDeferred):
            P.youtube_job(sid, "https://youtu.be/x")(lambda *a: None)
        assert P.read_meta(d).get("dl_state") == "cancelled"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 匯出影片中途停止


def test_export_video_cancel_stops_ffmpeg_and_removes_partial(tmp_path, monkeypatch, app_module):
    import soundfile as sf

    import pipeline as P
    import share as SH

    if not P.ffmpeg_path():
        pytest.skip("沒有 ffmpeg")

    class Painter:
        def __init__(self, *a):
            pass

        def frame(self, t):
            return np.zeros((SH.H, SH.W, 3), dtype=np.uint8)

    procs = []
    real_popen = subprocess.Popen

    def popen(*a, **k):
        procs.append(real_popen(*a, **k))
        return procs[-1]

    def report(step, p):
        raise P.JobCancelled()

    audio = tmp_path / "a.wav"
    sf.write(str(audio), np.zeros((44100 * 2, 2), dtype=np.float32), 44100)
    out = tmp_path / "out.mp4"
    monkeypatch.setattr(SH, "VideoPainter", Painter)
    monkeypatch.setattr(SH, "set_font_family", lambda *a: None)
    monkeypatch.setattr(SH.subprocess, "Popen", popen)
    with pytest.raises(P.JobCancelled):
        SH.export_video(tmp_path, {}, {"dur": 2.0}, "t", audio, out, report)
    assert procs and procs[0].poll() is not None
    assert not out.exists()
