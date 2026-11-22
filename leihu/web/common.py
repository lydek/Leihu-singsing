"""路由共用的狀態與函式：版本、埠號、設定檔鎖、處理佇列、移調與下載排程、檔名。"""
from __future__ import annotations

import os
import threading
import time

from flask import request

import pipeline as P

VERSION = "2.3.3.1"   # fork：上游版本＋第 4 段 fork 序號


HOST, PORT = "127.0.0.1", int(os.environ.get("LIANTAI_PORT", "8765"))


_SETTINGS_LOCK = threading.Lock()   # 設定檔、分類清單：讀出→修改→存回 之間不讓別的請求插進來


def _locked(fn):
    import functools

    @functools.wraps(fn)
    def wrap(*a, **k):
        with _SETTINGS_LOCK:
            return fn(*a, **k)
    return wrap


jobs = P.Jobs(live_safe=True, pipeline=True)   # 分離、分析、辨識：直播中暫停，不搶顯示卡


vjobs = P.Jobs()   # 移調／變速另外排隊：不會被分軌、評分卡住，也不會卡住它們


UI_QUEUE_SHOWN = 30


def _jobs_for_ui() -> dict:
    """畫面每秒會問一次進度。排隊的歌有好幾百首時，只送前 30 首（其餘只給數量），
    不然每秒都要傳、畫面也要重畫幾百列，介面會變得很卡。"""
    out, queued = [], 0
    for j in jobs.active() + vjobs.active():
        if j["status"] == "queued" and j["kind"] == "process":
            queued += 1
            if queued > UI_QUEUE_SHOWN:
                continue
        out.append(j)
    return {"jobs": out, "queued_total": queued}


def prerender_key(sid: str) -> str | None:
    """這首歌有預設 KEY（例如表單的 -4）時，先把那個 KEY 的移調版本做好，打開就能直接唱。"""
    d = P.song_dir(sid)
    meta = P.read_meta(d)
    key = meta.get("key") or 0
    if not key or not meta.get("steps", {}).get("separate") or P.variant_ready(d, key, 1.0):
        return None
    label = f"{key}:1.0"
    return vjobs.find("variant", sid, label) or vjobs.submit(
        "variant", sid, lambda report: P.step_variant(d, key, 1.0, report), label=label)


def after_process(sid: str) -> None:
    """一首歌處理完：先做好預設 KEY 的移調版本；合音多的歌，依設定自動排一次精細處理（排在佇列最後）。"""
    meta = P.read_meta(P.song_dir(sid))
    if meta.get("fine_suggest") and not meta.get("fine_done") and P.load_config().get("auto_fine", True):
        queue_fine(sid)   # 精細處理完才做移調版本（重新分軌會清掉舊的移調版本）
    else:
        prerender_key(sid)


def queue_fine(sid: str) -> str:
    jid = jobs.find("process", sid, "fine")
    if jid:
        return jid
    P.update_meta(P.song_dir(sid), fine_queued=True)
    job = P.fine_job(sid, after=prerender_key)

    def run(report):
        try:
            job(report)
        except P.JobDeferred:
            raise   # 直播中先延後：還在佇列裡，標記保留
        except BaseException:
            P.update_meta(P.song_dir(sid), fine_queued=False)   # 失敗或取消：不要一直顯示「排隊中」
            raise
    return jobs.submit("process", sid, run, label="fine")


def yt_submit(sid: str, url: str) -> str:
    return jobs.submit("process", sid, P.youtube_job(sid, url, after=after_process), label="youtube")


def apply_sheet(sid: str, info: dict, batch_cat: str | None = None) -> None:
    """把表單的歌手、曲名、KEY、語言（＋分類）寫進歌曲資料。曲名有填就鎖住，下載完不會被 YouTube 標題蓋掉。"""
    d = P.song_dir(sid)
    meta = P.read_meta(d)
    ch: dict = {}
    if info.get("song"):
        ch.update(title=str(info["song"])[:120], title_locked=True)
    if info.get("artist"):
        ch["artist"] = str(info["artist"])[:80]
    if info.get("key") is not None:
        k = P.parse_key(info["key"])
        if k is not None and k != (meta.get("key") or 0):
            ch.update(key=k, key_set_at=time.time())
    if info.get("lang") in ("zh", "ja", "en", "ko", "yue"):
        ch["lyrics_language"] = info["lang"]
    cat = batch_cat or (info.get("category") if not meta.get("category") else None)
    if cat:
        with _SETTINGS_LOCK:
            cats = P.load_categories()
            if cat not in cats:
                P.save_categories(cats + [cat])
        ch["category"] = cat
    if ch:
        P.update_meta(d, **ch)
        if meta.get("dl_state"):   # 還沒下載完：歌名後面繼續標（排隊下載）等狀態
            P.set_dl_state(d, meta["dl_state"])


def _downloading(sid: str) -> bool:
    return any(j.get("song") == sid and j.get("status") in ("queued", "running") for j in jobs.active())


def queue_max(sid: str) -> str:
    """1.6.5：用最高品質重新分離人聲（高品質模型＋最精細設定），再重新分析旋律、重新辨識歌詞。
    合唱、合音多的歌，標準處理常把合音算進主唱，旋律線和辨識都會亂；這個選項最慢但最乾淨。
    目前使用的線上／匯入歌詞不會被覆蓋（辨識結果另外保存，可以在歌詞面板切換）。"""
    if not P.hq_ready():
        P.start_hq_download()
        raise P.UserError("最高品質的分離模型（約 900 MB）還在下載，進度顯示在歌曲庫上方，下載完成後再按一次")
    jid = jobs.find("process", sid, "max")
    if jid:
        return jid
    proc = P.process_song(sid, ["separate", "pitch", "lyrics"],
                          cfg_override={"karaoke_quality": "high", "karaoke_overlap": 4})

    def run(report):
        proc(report)
        P.update_meta(P.song_dir(sid), fine_suggest=False, fine_done=True, fine_queued=False)
        try:
            prerender_key(sid)   # 重新分軌會清掉舊的移調版本，重做預設 KEY 的版本
        except Exception as e:
            P.log_error("queue_max prerender", e)
    return jobs.submit("process", sid, run, label="max")


# ---------------------------------------------------------------- 音訊與移調

def _key_speed():
    try:
        key = int(request.args.get("key", request.json.get("key", 0) if request.is_json else 0))
        speed = float(request.args.get("speed", request.json.get("speed", 1) if request.is_json else 1))
    except (TypeError, ValueError):
        raise P.UserError("KEY 或速度格式不正確")
    if not -6 <= key <= 6 or not 0.5 <= speed <= 1.5:
        raise P.UserError("KEY 範圍是 ±6，速度範圍是 50%–150%")
    return key, round(speed, 2)


def _safe_name(name: str) -> str:
    import re
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name).strip(" .") or "歌詞"
    return name[:80]


RELEASES_URL = "https://github.com/lydek/Leihu-singsing/releases/latest"   # fork：更新與下載頁都指向 fork（授權要求的原作來源見 LICENSE.md）
