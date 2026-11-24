"""antigravity 審查（2026-11-24）對 origin/refactor 仍成立的項目：匯出歌單檔名、全部暫停時的預先下載、
4 GB 顯示卡的忙碌門檻、Claude 對時寫回 steps、usage.json 寫入失敗、搬移核對失敗的清理、ui_prefs.json 非 dict。"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

SID = "reviewfix001"


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


@pytest.fixture
def song(app_module):
    import shutil

    import pipeline as P

    d = P.LIB / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": SID, "title": "t", "duration": 60, "steps": {}}), "utf-8")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 匯出歌單：標題直接當檔名


@pytest.mark.parametrize("title", ["2026/11 歌單", "KTV: 歌單", "../../escape", "///"])
def test_songlist_title_is_a_safe_file_name(client, monkeypatch, title):
    """標題有 / : 時 Windows 存檔丟 OSError（500），../ 會寫到「下載\\雷虎練唱台」外面。"""
    import exporter as EX
    import pipeline as P
    from leihu.web import live

    monkeypatch.setattr(EX, "song_rows", lambda scope, ready_only=True: [{}])
    monkeypatch.setattr(EX, "songlist_html", lambda rows, t, show_key: f"<h1>{t}</h1>")
    monkeypatch.setattr(live, "_reveal", lambda p: None)
    r = client.post("/api/export/songlist", json={"format": "html", "title": title})
    assert r.status_code == 200, r.get_data(as_text=True)
    p = pathlib.Path(r.get_json()["path"])
    try:
        assert p.parent == P.downloads_dir() / P.APP_NAME
        assert not set(p.name) & set('\\/:*?"<>|')   # 冒號在 NTFS 會寫成替代資料流，使用者只看到空檔
        assert p.read_text("utf-8-sig") == f"<h1>{title}</h1>"   # 網頁裡的標題照使用者打的
    finally:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- 全部暫停時不預先下載


def test_prefetcher_idle_while_queue_paused(app_module, monkeypatch):
    """YouTube 下載是 process 工作，全部暫停時不開始；預先下載原本只看 YT_PAUSE，照樣下載、寫硬碟。"""
    import pipeline as P
    from leihu.web import background
    from leihu.web.common import jobs

    class Stop(BaseException):
        pass

    slept = []

    def sleep(s):   # 跑完一輪就停
        slept.append(s)
        if len(slept) > 1:
            raise Stop

    monkeypatch.setattr(background.time, "sleep", sleep)
    got = []
    monkeypatch.setattr(jobs, "upcoming", lambda label, n: got.append(label) or ["x"])
    monkeypatch.setattr(P, "prefetch_youtube", lambda sid: got.append(sid))
    monkeypatch.setitem(P.YT_PAUSE, "on", False)
    monkeypatch.setattr(jobs, "paused", True)
    with pytest.raises(Stop):
        background.prefetcher()
    assert got == []


# ---------------------------------------------------------------- 4 GB 顯示卡的忙碌門檻


def _drive_total(monkeypatch, total_mb, samples):
    """依序餵 nvidia-smi 的結果（已用 MB, 使用率），顯示卡總共 total_mb，回傳每次是否判定忙碌。"""
    import gpu_worker as G
    import pipeline as P

    monkeypatch.setattr(G.POOL, "count", lambda: 0)
    it = iter(samples)
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kw: SimpleNamespace(stdout="{0}, {2}, {1}\n".format(*next(it), total_mb)))
    for k, v in {"free": None, "total": None, "util": None, "hot": 0, "calm": 0, "busy": "",
                 "stalled_until": 0.0, "ok_at": 0.0, "err": ""}.items():
        monkeypatch.setitem(P.GPU_LOAD, k, v)
    got = []
    for _ in samples:
        P.GPU_LOAD["checked"] = 0.0
        got.append(bool(P.gpu_busy_reason()))
    return got


def test_4gb_card_idle_desktop_is_not_busy(monkeypatch):
    """4 GB 卡光是桌面、WebView2 就佔 0.6–1 GB，剩下的永遠不到 3.5 GB：原本永遠判定忙碌，分離全部改用 CPU。"""
    assert _drive_total(monkeypatch, 4096, [(900, 10)] * 3) == [False] * 3


def test_4gb_card_busy_when_others_use_much_memory(monkeypatch):
    """其他程式佔超過 1.5 GB（剩不到總量減 1.5 GB）照樣判定忙碌。"""
    assert _drive_total(monkeypatch, 4096, [(2700, 10)]) == [True]


def test_large_card_threshold_unchanged(monkeypatch):
    """6 GB 以上的卡維持剩不到 3.5 GB 才算忙（6144-3000 MB ≈ 3.3 GB 忙；6144-2000 MB ≈ 4.3 GB 不忙）。"""
    assert _drive_total(monkeypatch, 6144, [(3000, 10)]) == [True]
    assert _drive_total(monkeypatch, 6144, [(2000, 10)]) == [False]


# ---------------------------------------------------------------- Claude 對時寫回 steps


def test_claude_align_keeps_steps_written_meanwhile(client, song, monkeypatch):
    """claude_align 在鎖外讀出 steps、再整包寫回：中間背景分離記下的步驟被蓋掉（step_import 第 2 批修過同樣的問題）。"""
    import claude_api as C
    import pipeline as P

    monkeypatch.setattr(C, "ask_tool", lambda *a, **k: ({"times": [1.0, 5.0]}, {}))

    def other_writer():
        with P._META_LOCK:
            steps = P.read_meta(song).get("steps", {})
            steps["separate"] = True
            P.update_meta(song, steps=steps)

    real_read = P.read_meta
    other = threading.Thread(target=other_writer)
    seen = []

    def read_meta(path):
        meta = real_read(path)
        if sys._getframe(1).f_code.co_name == "claude_align":
            seen.append(1)
            if len(seen) == 2 and other.ident is None:   # 第二次讀：Claude 回覆之後、寫回之前
                other.start()
                other.join(0.5)
        return meta

    monkeypatch.setattr(P, "read_meta", read_meta)
    r = client.post(f"/api/songs/{SID}/claude/align", json={"text": "第一句\n第二句"})
    other.join(5)
    assert r.status_code == 200, r.get_data(as_text=True)
    meta = real_read(song)
    assert meta["steps"] == {"lyrics": True, "separate": True}
    assert meta["lyrics_source"] == "claude"


# ---------------------------------------------------------------- usage.json 寫不進去


def _fail_replace(monkeypatch, target, times):
    real = pathlib.Path.replace
    left = [times]

    def replace(self, dst):
        if pathlib.Path(dst) == target and left[0]:
            left[0] -= 1
            raise PermissionError(13, "檔案被其他程式開著")
        return real(self, dst)

    monkeypatch.setattr(pathlib.Path, "replace", replace)


def test_usage_record_retries_when_file_is_busy(tmp_path, monkeypatch):
    """/api/status 每秒讀 usage.json：Windows 上剛好撞到時取代失敗。原本沒重試，直接丟出（已計費卻回 500）。"""
    import claude_api as C

    monkeypatch.setattr(C, "USAGE_PATH", tmp_path / "usage.json")
    _fail_replace(monkeypatch, tmp_path / "usage.json", 2)
    usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    entry = C.record("claude-sonnet-4-5", usage, "align", "t")
    assert json.loads((tmp_path / "usage.json").read_text("utf-8"))["calls"] == [entry]


def test_usage_record_write_failure_still_returns_entry(tmp_path, monkeypatch):
    """一直寫不進去：記在 log，回覆照常交給使用者。"""
    import claude_api as C
    import pipeline as P

    monkeypatch.setattr(C, "USAGE_PATH", tmp_path / "usage.json")
    _fail_replace(monkeypatch, tmp_path / "usage.json", 99)
    logged = []
    monkeypatch.setattr(P, "log_error", lambda where, e: logged.append(type(e).__name__))
    entry = C.record("claude-sonnet-4-5", SimpleNamespace(input_tokens=1, output_tokens=1), "align", "t")
    assert entry["input_tokens"] == 1
    assert logged == ["PermissionError"]
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------- 搬移核對失敗


def test_move_verify_failure_removes_every_copy(tmp_path, monkeypatch, app_module):
    """核對總大小失敗時原本只刪當下那一項：先複製好的 library 留在新位置，重試同一個位置被擋（已經有檔案）。"""
    import pipeline as P

    lib, models, tgt = tmp_path / "lib", tmp_path / "models", tmp_path / "new"
    for d in (lib, models):
        (d / "x").mkdir(parents=True)
        (d / "x" / "f.bin").write_bytes(b"abc")
    tgt.mkdir()
    monkeypatch.setattr(P, "LIB", lib)
    monkeypatch.setattr(P, "MODELS", models)
    real = P.dir_size
    monkeypatch.setattr(P, "dir_size", lambda p: (0, 0) if p == tgt / "models" else real(p))
    with pytest.raises(P.UserError, match="核對失敗"):
        P.move_storage(str(tgt), ["library", "models"], lambda *a: None)
    assert not (tgt / "library").exists()
    assert not (tgt / "models").exists()


# ---------------------------------------------------------------- ui_prefs.json 不是 dict


@pytest.mark.parametrize("bad", ["[]", "null", "3"])
def test_ui_prefs_set_with_non_dict_file(client, monkeypatch, tmp_path, bad):
    """檔案內容是 JSON 但不是物件時，原本 cur[k] = v 丟 TypeError（500），音量設定再也存不了。"""
    from leihu.web import system

    p = tmp_path / "ui_prefs.json"
    p.write_text(bad, "utf-8")
    monkeypatch.setattr(system, "UI_PREFS", p)
    key = sorted(system.UI_PREF_KEYS)[0]
    r = client.post("/api/ui-prefs", json={key: 0.5})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert json.loads(p.read_text("utf-8")) == {key: 0.5}
