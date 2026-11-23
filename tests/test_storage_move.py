"""docs/next-version-plan.md 第 6b 批：搬移歌曲庫（69、70）。

搬移開始前已經在寫的（HTTP 寫入請求、背景工作、KEY 轉換）要等它們寫完才複製，搬移中不再有新的寫入；
排隊中被停止要恢復原本的暫停狀態；搬移中開歌曲、記演唱次數只讀；排隊中的工作開始時才取路徑；
接用現有歌曲庫也要等 KEY 佇列；搬移中不開始背景下載模型。
"""

from __future__ import annotations

import json
import shutil
import threading
import time

import pytest

SID = "movetest01"


@pytest.fixture
def queues(app_module, monkeypatch):
    from leihu.web import common

    for q in (common.jobs, common.vjobs):
        monkeypatch.setattr(q, "state", {})
        monkeypatch.setattr(q, "pending", [])
        monkeypatch.setattr(q, "running", {})
    return common.jobs, common.vjobs


@pytest.fixture
def song(app_module):
    import pipeline as P

    d = P.LIB / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": SID, "title": "t", "steps": {"import": True}, "lufs": -14.0,
                                             "last_opened": 0}), "utf-8")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def moving(app_module, monkeypatch):
    """搬移中（測試結束一定恢復）。直接設 STORAGE，舊程式上也跑得起來（等的人每秒會重新檢查）。"""
    import pipeline as P

    monkeypatch.setitem(P.STORAGE, "moving", True)
    return P


# ---------------------------------------------------------------- 寫入者計數


def test_writer_blocks_while_moving(moving):
    with pytest.raises(moving.UserError), moving.library_writer(wait=False):
        pass


def test_http_write_request_counts_as_writer(song, monkeypatch, app_module):
    """搬移開始前已經進來的寫入請求算寫入者：搬移要等它做完才複製。"""
    import pipeline as P

    seen = []
    real = P.update_meta

    def update_meta(d, **kw):
        seen.append(P._LIB_WRITERS[0])
        return real(d, **kw)

    monkeypatch.setattr(P, "update_meta", update_meta)
    assert app_module.app.test_client().patch(f"/api/songs/{SID}", json={"artist": "a"}).status_code == 200
    assert seen == [1]
    assert P._LIB_WRITERS[0] == 0


def test_http_write_request_blocked_while_moving(song, moving, app_module):
    r = app_module.app.test_client().patch(f"/api/songs/{SID}", json={"artist": "a"})
    assert r.status_code == 400
    assert "搬移" in r.get_json()["error"]


def test_frozen_waits_for_writers(queues, app_module):
    """搬移前等正在寫的做完；搬移期間新的寫入者要等搬完。計數最後要回到原本的值。"""
    import pipeline as P
    from leihu.web import storage

    start = P._LIB_WRITERS[0]
    entered, release = threading.Event(), threading.Event()
    late_in = threading.Event()
    order = []

    def writer():
        with P.library_writer():
            entered.set()
            release.wait(5)
            order.append("writer done")

    def late():
        with P.library_writer():
            late_in.set()

    t = threading.Thread(target=writer)
    t.start()
    assert entered.wait(5)
    cm = storage._library_frozen(lambda *a: None, 10)   # 要留著參考：被回收時 finally 會提早解除搬移
    frozen = threading.Thread(target=lambda: cm.__enter__() or order.append("move"))
    try:
        frozen.start()
        time.sleep(0.3)
        assert order == []   # 還有人在寫，搬移在等
        release.set()
        frozen.join(5)
        assert order == ["writer done", "move"]
        lt = threading.Thread(target=late)
        lt.start()
        assert not late_in.wait(0.3)   # 搬移中新的寫入者在等
    finally:
        release.set()
        if order[-1:] == ["move"]:
            cm.__exit__(None, None, None)
    assert late_in.wait(5)
    lt.join(5)
    t.join(5)
    assert P._LIB_WRITERS[0] == start


def test_frozen_gives_up_after_timeout(queues, app_module):
    """等太久就放棄搬移、歌曲庫不動（原本 KEY 轉換等 600 秒後照樣開始搬）。"""
    import pipeline as P
    from leihu.web import storage

    _, vjobs = queues
    jid = vjobs.submit("variant", SID, lambda r: None, label="2:1.0")
    vjobs.state[jid]["status"] = "running"
    with pytest.raises(P.UserError, match="稍後再試"), storage._library_frozen(lambda *a: None, 0.5):
        pytest.fail("不應該開始搬")
    assert not P.STORAGE["moving"] and not vjobs.frozen


def test_frozen_vjobs_do_not_start(queues):
    _, vjobs = queues
    jid = vjobs.submit("variant", SID, lambda r: None, label="2:1.0")
    vjobs.freeze(True)
    try:
        assert vjobs._next(False, 1)[0] is None
    finally:
        vjobs.freeze(False)
    assert vjobs._next(False, 1)[0] == jid


def test_background_lyrics_search_waits_for_move(song, moving, monkeypatch, tmp_path):
    """背景歌詞搜尋在搬移中等搬完，再用新的位置寫（等的期間 LIB 換成新的位置）。"""
    import pipeline as P

    seen = []
    done = threading.Event()
    monkeypatch.setattr(P, "find_and_store_candidates", lambda d, cfg, apply_first=False: (seen.append(d), done.set()))
    new_lib = tmp_path / "newlib"
    (new_lib / SID).mkdir(parents=True)
    P.search_lyrics_later(song, {})
    time.sleep(0.5)
    assert seen == []
    monkeypatch.setattr(P, "LIB", new_lib)   # 搬完了：歌曲庫在新的位置
    P.STORAGE["moving"] = False
    assert done.wait(5)
    assert seen == [new_lib / SID]
    P._LYRICS_POOL.submit(lambda: None).result(5)   # 等背景函式整個結束再還原 LIB


# ---------------------------------------------------------------- 排隊中被停止要恢復暫停


def test_cancel_queued_move_restores_pause(queues, app_module, tmp_path):
    """搬移排進佇列時先按了全部暫停；還沒開始就被停止，要恢復原本的狀態（原本一直停在全部暫停，還寫進設定）。"""
    import pipeline as P

    jobs, _ = queues
    before = P.load_config()
    try:
        jobs.set_paused(False)
        c = app_module.app.test_client()
        r = c.post("/api/storage/move", json={"target": str(tmp_path / "new"), "library": True})
        jid = r.get_json()["job"]
        assert jobs.paused
        assert c.post(f"/api/jobs/{jid}/cancel").status_code == 200
        assert not jobs.paused
        assert not P.load_config().get("queue_paused")
    finally:
        jobs.paused = False
        P.save_config(before)


def test_on_cancel_not_called_after_start(queues):
    """排程器取出工作（要開始了）之後才按停止：不能再恢復暫停（工作自己會收尾）。"""
    jobs, _ = queues
    calls = []
    jid = jobs.submit("storage", "", lambda r: None, label="move", on_cancel=lambda: calls.append(1))
    with jobs.cv:
        assert jobs._next(False, 1)[0] == jid
        jobs.state[jid]["status"] = "running"
    jobs.cancel(jid)
    assert calls == []


# ---------------------------------------------------------------- 搬移中只讀


def test_song_get_is_read_only_while_moving(song, moving, app_module):
    """搬移中開歌曲：不補寫 last_opened、響度等到舊位置（搬完會不見）。"""
    before = (song / "meta.json").read_bytes()
    assert app_module.app.test_client().get(f"/api/songs/{SID}").status_code == 200
    assert (song / "meta.json").read_bytes() == before


def test_setlist_count_skipped_while_moving(song, moving, app_module, tmp_path, monkeypatch):
    import pipeline as P
    from leihu.web import live

    monkeypatch.setattr(live, "SETLIST_PATH", tmp_path / "setlist.json")
    monkeypatch.setattr(live, "SETLIST_DIR", tmp_path / "setlists")
    r = app_module.app.test_client().post("/api/setlist/add", json={"id": SID, "title": "t"})
    assert r.status_code == 200
    assert "sung" not in P.read_meta(song)


# ---------------------------------------------------------------- 排隊中的工作開始時才取路徑


def test_queued_variant_resolves_path_when_it_starts(song, queues, monkeypatch, app_module, tmp_path):
    """KEY 轉換排隊時歌曲庫搬走了：開始時要用新的位置，不能讀寫舊位置。"""
    import pipeline as P

    meta = P.read_meta(song)
    meta["steps"]["separate"] = True
    P.write_meta(song, meta)
    _, vjobs = queues
    r = app_module.app.test_client().post(f"/api/songs/{SID}/variant?key=2&speed=1", json={})
    assert r.get_json()["ready"] is False
    (_, fn), = vjobs.pending
    new = tmp_path / "newlib" / SID
    seen = []
    monkeypatch.setattr(P, "song_dir", lambda sid: new)   # 搬移後 song_dir 回傳新的位置
    monkeypatch.setattr(P, "step_variant", lambda d, key, speed, report: seen.append(d))
    fn(lambda *a: None)
    assert seen == [new]


# ---------------------------------------------------------------- 接用、背景下載


def test_adopt_waits_for_running_variant(queues, monkeypatch, app_module):
    """接用現有歌曲庫原本只看主佇列：KEY 轉換正在寫的時候也不能切換。"""
    import pipeline as P
    from leihu.web import storage

    _, vjobs = queues
    jid = vjobs.submit("variant", SID, lambda r: None, label="2:1.0")
    vjobs.state[jid]["status"] = "running"
    called = []
    monkeypatch.setattr(storage, "ADOPT_WAIT", 0.5)
    monkeypatch.setattr(P, "adopt_storage", lambda path: called.append(path) or {})
    r = app_module.app.test_client().post("/api/storage/adopt", json={"path": "D:/x"})
    assert r.status_code == 400
    assert called == []
    assert not P.STORAGE["moving"]


def test_no_background_model_download_while_moving(moving, monkeypatch):
    """搬移中不開始背景下載模型：下載完的檔案會留在舊位置。"""
    import pipeline as P

    started = []
    monkeypatch.setattr(P, "NO_BACKGROUND", False)
    monkeypatch.setattr(P.threading, "Thread", lambda *a, **k: started.append(1) or pytest.fail("不應該開始下載"))
    P.start_hq_download()
    assert started == []


# ---------------------------------------------------------------- Codex 審查補的情境


def test_write_slot(moving):
    """順手補寫：搬移中拿不到（只讀），沒在搬移時登記成寫入者、離開後減回。"""
    P = moving
    with P.library_write_slot() as w:
        assert w is False
    P.STORAGE["moving"] = False
    start = P._LIB_WRITERS[0]
    with P.library_write_slot() as w:
        assert w is True and P._LIB_WRITERS[0] == start + 1
    assert P._LIB_WRITERS[0] == start


def test_adopt_freezes_main_queue(queues, monkeypatch, app_module):
    """接用現有歌曲庫期間，主佇列排隊中的評分、匯出不能開始（會抓住舊路徑）。"""
    import pipeline as P

    jobs, _ = queues
    jobs.submit("take", SID, lambda r: None, label="20990101-000000-abcd")
    seen = []

    def adopt(path):
        with jobs.cv:
            seen.append(jobs._next(False, 1)[0])
        return {}

    monkeypatch.setattr(P, "adopt_storage", adopt)
    assert app_module.app.test_client().post("/api/storage/adopt", json={"path": "D:/x"}).status_code == 200
    assert seen == [None]
    assert not jobs.frozen


def test_lyrics_get_is_read_only_while_moving(song, moving, app_module):
    """搬移中開歌詞：修正過的歌詞照樣回傳，但不存回舊位置。"""
    (song / "lyrics.lrc").write_text("[00:01.00]優優獨播劇場\n[00:02.00]第一句\n", "utf-8")
    before = (song / "lyrics.lrc").read_bytes()
    r = app_module.app.test_client().get(f"/api/songs/{SID}/lyrics")
    assert r.status_code == 200
    assert (song / "lyrics.lrc").read_bytes() == before


def test_cancel_youtube_while_moving_marks_after_move(song, moving, queues, app_module):
    """搬移中取消排隊的 YouTube 下載：搬完再標成已取消（寫到新的位置；不寫的話重開會自動重新下載）。"""
    import pipeline as P

    jobs, _ = queues
    meta = P.read_meta(song)
    meta["steps"]["import"] = False   # 還沒下載完的歌
    P.write_meta(song, dict(meta, dl_state="queued"))
    jid = jobs.submit("process", SID, lambda r: None, label="youtube")
    assert app_module.app.test_client().post(f"/api/jobs/{jid}/cancel").status_code == 200
    time.sleep(0.3)
    assert P.read_meta(song).get("dl_state") == "queued"   # 搬移中還沒寫
    P.STORAGE["moving"] = False
    for _ in range(50):
        if P.read_meta(song).get("dl_state") == "cancelled":
            break
        time.sleep(0.1)
    assert P.read_meta(song).get("dl_state") == "cancelled"


def test_startup_recheck_cleanup_waits_for_move(song, moving):
    """啟動時的背景檢查在搬移中等搬完，不改寫正在複製的舊歌曲庫。"""
    import pipeline as P

    P.update_meta(song, lyrics_recheck={"fit": 0.9, "before": 0.9})
    t = threading.Thread(target=P.clear_false_recheck, daemon=True)
    t.start()
    time.sleep(0.5)
    assert P.read_meta(song).get("lyrics_recheck")   # 還在等
    P.STORAGE["moving"] = False
    t.join(10)
    assert not t.is_alive()


def test_move_rechecks_model_download_when_it_starts(queues, monkeypatch, app_module, tmp_path):
    """搬移排隊期間開始了背景下載模型：真正開始搬時要再檢查，不能漏掉下載中的模型。"""
    import pipeline as P

    jobs, _ = queues
    before = P.load_config()
    called = []
    monkeypatch.setattr(P, "move_storage", lambda target, what, report: called.append(what) or {})
    try:
        r = app_module.app.test_client().post("/api/storage/move",
                                              json={"target": str(tmp_path / "new"), "models": True})
        (_, fn), = jobs.pending
        monkeypatch.setitem(P.HQ_DL, "state", "downloading")   # 排隊期間開始下載
        with pytest.raises(P.UserError, match="下載"):
            fn(lambda *a: None)
        assert called == []
        assert r.status_code == 200
    finally:
        jobs.paused = False
        P.save_config(before)
