"""docs/next-version-plan.md 第 2 批：刪除處理中的歌（D2、D4、28 的 A2、A5）、換 KEY 等分軌（D6、68）、
step_import 補鎖。D1–D6 指 step2-plan.md（git show 3d0fd5f:docs/step2-plan.md）。

測試時 LIANTAI_NO_JOBS=1：佇列只排隊、不執行，「執行中」由 _running 直接改狀態。
"""

from __future__ import annotations

import json
import shutil
import sys
import threading

import pytest


@pytest.fixture
def queues(app_module, monkeypatch):
    """共用的 jobs、vjobs 換成空的狀態，測試之間不互相影響。"""
    from leihu.web import common

    for q in (common.jobs, common.vjobs):
        monkeypatch.setattr(q, "state", {})
        monkeypatch.setattr(q, "pending", [])
        monkeypatch.setattr(q, "running", {})
    return common.jobs, common.vjobs


@pytest.fixture
def song(app_module):
    import pipeline as P

    made = []

    def make(sid: str) -> object:
        d = P.LIB / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps({"id": sid, "title": sid}), "utf-8")
        made.append(d)
        return d

    yield make
    for d in made:
        shutil.rmtree(d, ignore_errors=True)


def _running(q, kind: str, sid: str, label: str) -> str:
    jid = q.submit(kind, sid, lambda report: None, label=label)
    with q.lock:
        q.pending[:] = [(j, f) for j, f in q.pending if j != jid]
        q.state[jid]["status"] = "running"
        q.running[jid] = "gpu"
    return jid


# ---------------------------------------------------------------- D2、D4：刪除歌曲


@pytest.mark.parametrize("busy", ["jobs", "vjobs"])
def test_delete_running_song_is_refused_and_queue_untouched(app_module, queues, song, busy):
    """D2：有工作在執行時刪掉資料夾，那個工作接著會寫出殘缺的 meta.json。28 的 A5：拒絕時排隊中的工作照舊。"""
    jobs, vjobs = queues
    d = song("delbusy01")
    if busy == "jobs":
        _running(jobs, "process", "delbusy01", "all")
        waiting = vjobs.submit("variant", "delbusy01", lambda report: None, label="2:1.0")
    else:
        _running(vjobs, "variant", "delbusy01", "2:1.0")
        waiting = jobs.submit("process", "delbusy01", lambda report: None, label="pitch")
    r = app_module.app.test_client().delete("/api/songs/delbusy01")
    assert r.status_code == 400
    assert "正在處理" in r.get_json()["error"]
    assert d.exists()
    q = vjobs if busy == "jobs" else jobs
    assert q.get(waiting)["status"] == "queued"


def test_delete_cancels_queued_jobs(app_module, queues, song):
    """D4：刪掉的歌的排隊工作改成已取消，不再出現在 /api/status 的工作清單。"""
    jobs, vjobs = queues
    d = song("delqueue01")
    a = jobs.submit("process", "delqueue01", lambda report: None, label="all")
    b = vjobs.submit("variant", "delqueue01", lambda report: None, label="2:1.0")
    other = jobs.submit("process", "delother01", lambda report: None, label="all")
    r = app_module.app.test_client().delete("/api/songs/delqueue01")
    assert r.status_code == 200
    assert not d.exists()
    assert jobs.get(a)["status"] == "cancelled"
    assert vjobs.get(b)["status"] == "cancelled"
    assert jobs.get(other)["status"] == "queued"  # 別首歌不受影響
    assert [j["id"] for j in jobs.active() + vjobs.active()] == [other]


def test_cancelled_song_jobs_leave_the_queue(app_module, queues, monkeypatch):
    """Codex r4-1：cancel_song 只改狀態、留在 pending：一小時後 submit 清掉它的 state，
    order()、active()、upcoming() 丟 KeyError（排在擋住的工作後面時，_next 碰不到它）。"""
    import pipeline as P

    jobs, _ = queues
    _running(jobs, "process", "longsong01", "all")
    jobs.submit("process", "waiting01", lambda report: None, label="all")
    gone = jobs.submit("process", "deleted01", lambda report: None, label="all")
    with jobs.lock:
        jobs.cancel_song("deleted01")
    assert gone not in [j for j, _ in jobs.pending]
    t0 = P.time.time()
    monkeypatch.setattr(P.time, "time", lambda: t0 + 3700)   # 超過一小時再排一個新工作
    jobs.submit("process", "later01", lambda report: None, label="all")
    assert gone not in jobs.state
    assert list(jobs.order()) and jobs.active() and jobs.upcoming("all", 5) == ["waiting01", "later01"]


def test_finished_state_still_in_queue_is_not_purged(app_module, queues, monkeypatch):
    """清一小時前的工作紀錄時，還在 pending 的不清（防其他取消路徑留下同樣的情形）。"""
    import pipeline as P

    jobs, _ = queues
    jid = jobs.submit("process", "stuck01", lambda report: None, label="all")
    jobs.update(jid, status="cancelled")   # update 會補 finished，但沒有從 pending 拿掉
    t0 = P.time.time()
    monkeypatch.setattr(P.time, "time", lambda: t0 + 3700)
    jobs.submit("process", "later02", lambda report: None, label="all")
    assert jid in jobs.state
    jobs.order()


def test_delete_while_prefetching_is_refused(app_module, queues, song):
    """28 的 A2：預先下載不是佇列裡的工作，刪掉資料夾後下載會再寫出殘缺的歌。"""
    import pipeline as P

    d = song("delprefetch1")
    lock = P._dl_lock("delprefetch1")
    lock.acquire()
    try:
        r = app_module.app.test_client().delete("/api/songs/delprefetch1")
    finally:
        lock.release()
    assert r.status_code == 400
    assert "下載" in r.get_json()["error"]
    assert d.exists()
    assert app_module.app.test_client().delete("/api/songs/delprefetch1").status_code == 200


# ---------------------------------------------------------------- D6、68：換 KEY 等分軌


@pytest.mark.parametrize(
    "label", ["all", "youtube", "upgrade", "fine", "max", "leadfix", "import,separate,pitch,lyrics", "separate,pitch"]
)
def test_variant_waits_for_stem_writing_job(queues, label):
    """D6：同一首歌正在分軌、匯入時換 KEY，移調會讀到寫到一半的分軌。68：主唱補回也會重寫分軌。
    要等的那首先跳過，別首的移調照常開始；分軌做完後輪到它。"""
    jobs, vjobs = queues
    writer = _running(jobs, "process", "stemsong01", label)
    x = vjobs.submit("variant", "stemsong01", lambda report: None, label="2:1.0")
    y = vjobs.submit("variant", "othersong1", lambda report: None, label="2:1.0")
    assert vjobs._next(False, 1)[0] == y
    assert vjobs._next(False, 1)[0] is None
    assert vjobs.get(x)["status"] == "queued"
    jobs.update(writer, status="done")
    jobs.running.pop(writer)
    assert vjobs._next(False, 1)[0] == x


@pytest.mark.parametrize(("kind", "label"), [("process", "pitch"), ("process", "lyrics"), ("process", "pitch,lyrics"),
                                             ("take", "20261123-1200-abcd"), ("export", "mixdown")])
def test_variant_does_not_wait_for_jobs_that_keep_stems(queues, kind, label):
    """只重新分析旋律、歌詞，或評分、匯出，不寫分軌：移調照常開始。"""
    jobs, vjobs = queues
    _running(jobs, kind, "stemsong01", label)
    x = vjobs.submit("variant", "stemsong01", lambda report: None, label="2:1.0")
    assert vjobs._next(False, 1)[0] == x


def test_queued_stem_job_does_not_block_variant(queues):
    """還在排隊的分軌不擋移調（還沒開始寫）。"""
    jobs, vjobs = queues
    jobs.submit("process", "stemsong01", lambda report: None, label="all")
    x = vjobs.submit("variant", "stemsong01", lambda report: None, label="2:1.0")
    assert vjobs._next(False, 1)[0] == x


# ---------------------------------------------------------------- step_import 補鎖


def test_step_import_keeps_steps_written_meanwhile(tmp_path, monkeypatch, app_module):
    """step_import 在鎖外讀出 steps、再交給 update_meta：中間別人寫進 steps 的會被舊值蓋掉。"""
    import numpy as np
    import soundfile as sf

    import pipeline as P

    d = tmp_path / "song"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"id": "importlock", "steps": {}}), "utf-8")
    sf.write(str(d / "original.wav"), np.zeros((4410, 2), dtype=np.float32), P.SR)

    def other_writer():  # 另一個執行緒照正確的做法（鎖內讀改寫）記下 separate
        with P._META_LOCK:
            steps = P.read_meta(d).get("steps", {})
            steps["separate"] = True
            P.update_meta(d, steps=steps)

    real_read = P.read_meta
    other = threading.Thread(target=other_writer)

    def read_meta(path):
        meta = real_read(path)
        if sys._getframe(1).f_code.co_name == "step_import" and other.ident is None:
            other.start()  # step_import 讀出 steps 之後才插進來
            other.join(0.5)  # 修好後 step_import 拿著鎖，other 要等它寫完
        return meta

    monkeypatch.setattr(P, "read_meta", read_meta)
    P.step_import(d, lambda *a: None)
    other.join(5)
    assert real_read(d)["steps"] == {"import": True, "separate": True}


def test_delete_moves_folder_while_holding_queue_locks(app_module, queues, song, monkeypatch):
    """Codex r4-2：確認沒有執行中的工作後就放開佇列鎖、再 rmtree：空窗裡新的工作可以開始讀寫刪到一半的歌。
    改成拿著鎖時先把資料夾移走（新的工作找不到這首歌），rmtree 在鎖外做。"""
    from leihu.web import library

    jobs, vjobs = queues
    d = song("delrace001")
    seen = {}
    real_replace, real_rmtree = library.os.replace, library.shutil.rmtree

    def replace(a, b):
        seen["replace_locked"] = jobs.lock.locked() and vjobs.lock.locked()
        return real_replace(a, b)

    def rmtree(p, **kw):
        seen["rmtree_locked"] = jobs.lock.locked()
        return real_rmtree(p, **kw)

    monkeypatch.setattr(library.os, "replace", replace)
    monkeypatch.setattr(library.shutil, "rmtree", rmtree)
    r = app_module.app.test_client().delete("/api/songs/delrace001")
    assert r.status_code == 200 and not d.exists()
    assert seen == {"replace_locked": True, "rmtree_locked": False}
    import pipeline as P
    assert not any((P.LIB / ".trash").iterdir())


def test_delete_failure_keeps_queued_jobs(app_module, queues, song, monkeypatch):
    """Codex r4-3：刪除失敗（檔案被其他程式開著）時，排隊的工作原本已經全部取消、不會恢復。"""
    from leihu.web import library

    jobs, vjobs = queues
    d = song("delfail001")
    a = jobs.submit("process", "delfail001", lambda report: None, label="all")
    b = vjobs.submit("variant", "delfail001", lambda report: None, label="2:1.0")

    def locked(src, dst):
        raise PermissionError(32, "另一個程式正在使用此檔案")

    monkeypatch.setattr(library.os, "replace", locked)
    r = app_module.app.test_client().delete("/api/songs/delfail001")
    assert r.status_code == 400 and "刪不掉" in r.get_json()["error"]
    assert d.exists()
    assert jobs.get(a)["status"] == "queued" and vjobs.get(b)["status"] == "queued"


def test_waiting_for_stems_is_not_reported_as_live_hold(app_module):
    """Codex r4-8：所有排隊的工作都因為等分軌（wait_for）被跳過時，_next 原本也回報「只剩直播中暫停的工作」，
    _run 就把「等這首歌的分軌處理完」改寫成「直播中暫停（直播中）」。只有真的因為直播跳過才算。"""
    import pipeline as P

    q = P.Jobs(wait_for=lambda st: st.get("song") == "busysong1")
    jid = q.submit("variant", "busysong1", lambda r: None, label="2:1.0")
    with q.lock:
        got, fn, held = q._next(hold=False, slots=1)
    assert got is None and held is False
    assert q.state[jid]["step"] == "等這首歌的分軌處理完"
    q2 = P.Jobs()
    q2.submit("process", "bgsong001", lambda r: None, label="upgrade")
    with q2.lock:
        assert q2._next(hold=True, slots=1)[2] is True   # 直播中跳過背景工作：照舊回報
