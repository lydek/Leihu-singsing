"""狀態、設定、處理佇列的操作、遠端開歌、開啟網址與資料夾。"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request, send_from_directory

import claude_api as C
import pipeline as P

from .common import RELEASES_URL, VERSION, _jobs_for_ui, _locked, jobs, vjobs, yt_submit
from .update import UPDATE

bp = Blueprint("system", __name__)


@bp.get("/")
def index():
    return send_from_directory(current_app.static_folder, "index.html")


# ---------------------------------------------------------------- 狀態與設定

@bp.get("/api/status")
def status():
    cfg = P.load_config()
    mods = P.module_status()
    q = cfg.get("karaoke_quality", "auto")
    hq = P.hq_status() if (q == "high" or (q == "auto" and mods.get("cuda"))) else None
    vq = cfg.get("vocal_quality", "auto")
    hqv = P.hqv_status() if (vq == "high" or (vq == "auto" and mods.get("cuda"))) else None
    # fork：外站網頁可以用 <img> 一直打這個網址讓背景工作一直暫停，只認本機頁面
    # （瀏覽器會帶 Sec-Fetch-Site；本機的其他程式不帶，照常算）
    if request.args.get("live") == "1" and request.headers.get("Sec-Fetch-Site") != "cross-site":
        P.live_touch()
    live = P.live_status(cfg)
    live["holding"] = jobs.holding
    live["paused"] = jobs.paused
    return jsonify(version=VERSION, root=str(P.ROOT), modules=mods, cuda_note=P.CUDA_NOTE, hq=hq,
                   vram=(P.gpu_info(block=False) or {}).get("vram"), parallel_ok=P.parallel_ok(),
                   hq_model=P.HQ_KARAOKE, hqv=hqv, live=live, yt=bool(cfg.get("yt_enabled")),
                   claude_ready=bool(cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")),
                   session_cost=C.summary()["session"]["cost_usd"],
                   remote=dict(REMOTE), yt_pause=dict(P.YT_PAUSE), update=dict(UPDATE), **_jobs_for_ui())


# 遠端開歌：本機的其他程式（例如點歌系統）請練唱畫面打開某首歌並切到指定 KEY。
# 練唱畫面輪詢 /api/status 時看到 seq 變了就照做。
REMOTE = {"seq": 0, "id": None, "key": 0}


_remote_lock = threading.Lock()


@bp.post("/api/remote/open")
def remote_open():
    # 只給本機程式呼叫：瀏覽器裡的網頁送出的請求一定帶 Origin，一律拒絕，
    # 避免任何網站趁你開著練唱台時切換歌曲
    if request.headers.get("Origin"):
        abort(403)
    body = request.get_json(force=True) or {}
    sid = body.get("id") or ""
    P.song_dir(sid)
    try:
        key = int(body.get("key", 0))
    except (TypeError, ValueError):
        raise P.UserError("KEY 格式不正確")
    if not -6 <= key <= 6:
        raise P.UserError("KEY 範圍是 ±6")
    with _remote_lock:
        REMOTE.update(seq=REMOTE["seq"] + 1, id=sid, key=key)
        return jsonify(seq=REMOTE["seq"])


UI_PREFS = P.DATA / "ui_prefs.json"   # fork：上游寫在 ROOT，跟著 S28 放使用者資料夾
UI_PREF_KEYS = {"masterVol", "micGain", "lastVol"}


@bp.get("/api/ui-prefs")
def ui_prefs_get():
    """2.3.4：介面的音量設定（主音量、麥克風增益、各音軌音量），開程式時沿用上次關閉時的值。"""
    try:
        return jsonify(json.loads(UI_PREFS.read_text("utf-8")))
    except Exception:
        return jsonify({})


@bp.post("/api/ui-prefs")
def ui_prefs_set():
    b = request.get_json(force=True) or {}
    try:
        cur = json.loads(UI_PREFS.read_text("utf-8")) if UI_PREFS.exists() else {}
    except Exception:
        cur = {}
    for k, v in b.items():
        if k in UI_PREF_KEYS:
            cur[k] = v
    P.atomic_write_text(UI_PREFS, json.dumps(cur, ensure_ascii=False))
    return jsonify(ok=True)


@bp.get("/api/config")
def get_config():
    cfg = P.load_config()
    k = cfg.get("anthropic_api_key") or ""
    cfg["anthropic_api_key"] = (k[:7] + "…" + k[-4:]) if len(k) > 12 else ""
    cfg["has_key"] = bool(k or os.environ.get("ANTHROPIC_API_KEY"))
    return jsonify(cfg)


def _balance(v) -> float | None:
    """fork：設定畫面填的餘額（美元），空白是清掉。NaN／無限大寫進 config.json 後 GET /api/config 會輸出 NaN，
    設定畫面打不開；不是數字原本回 500。"""
    if v in (None, ""):
        return None
    try:
        x = round(float(v), 2)
    except (TypeError, ValueError, OverflowError):
        raise P.UserError("餘額格式不正確") from None
    if not math.isfinite(x):
        raise P.UserError("餘額格式不正確")
    return x


@bp.post("/api/config")
@_locked
def set_config():
    body = request.get_json(force=True) or {}
    cfg = P.load_config()
    for k in P.DEFAULT_CONFIG:
        if k not in body:
            continue
        if k == "balance_usd":
            v = _balance(body[k])
            if v != cfg.get("balance_usd"):
                cfg["balance_usd"], cfg["balance_set_at"] = v, (time.time() if v is not None else None)
            continue
        if k == "balance_set_at":
            continue
        if k == "anthropic_api_key":
            v = (body[k] or "").strip()
            if v == "" or "…" in v:
                if body.get("clear_key"):
                    cfg[k] = ""
                continue
            cfg[k] = v
        else:
            cfg[k] = body[k]
    P.save_config(cfg)
    return get_config()


@bp.post("/api/jobs/<jid>/move")
def job_move(jid):
    """調整排隊順序（往前／往後一個）。"""
    delta = int((request.get_json(force=True) or {}).get("delta") or 0)
    if not jobs.move(jid, delta):
        raise P.UserError("無法移動（可能已經開始處理）")
    return jsonify(ok=True)


@bp.post("/api/jobs/<jid>/top")
def job_top(jid):
    """移到佇列最前面。"""
    if not jobs.to_front(jid):
        raise P.UserError("無法移動（可能已經開始處理）")
    return jsonify(ok=True)


@bp.get("/api/debug/stacks")
def debug_stacks():
    """疑難排解用：列出每個執行緒目前停在哪一行。"""
    import traceback as tb
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for tid, frame in sys._current_frames().items():
        out.append(f"--- {names.get(tid, tid)}\n" + "".join(tb.format_stack(frame)[-12:]))
    return current_app.response_class("\n".join(out), mimetype="text/plain; charset=utf-8")


@bp.post("/api/jobs/<jid>/cancel")
def job_cancel(jid):
    """停止處理（例如歌詞辨識卡很久）。已經完成的步驟會保留。"""
    q = jobs if jobs.get(jid) else vjobs
    if not q.cancel(jid):
        raise P.UserError("這個工作已經結束了")
    j = q.get(jid) or {}
    if j.get("label") == "youtube":   # 還在排隊的 YouTube 下載被取消：歌名標成「已取消下載」，之後可以重新下載
        with P.library_write_slot() as w:
            if w:
                _mark_dl_cancelled(j["song"])
        if not w:   # fork（70）：搬移中，搬完再寫到新的位置（不讓這個請求等幾分鐘；不寫的話重開會自動重新下載）
            threading.Thread(target=_mark_dl_cancelled_after_move, args=(j["song"],), daemon=True).start()
    return jsonify(ok=True)


def _mark_dl_cancelled(sid: str) -> None:
    try:
        d = P.song_dir(sid)
        if not P.read_meta(d).get("steps", {}).get("import"):
            P.set_dl_state(d, "cancelled")
    except Exception:
        pass


def _mark_dl_cancelled_after_move(sid: str) -> None:
    with P.library_writer():
        _mark_dl_cancelled(sid)


@bp.post("/api/songs/<sid>/prioritize")
def prioritize(sid):
    """2.1.1「優先處理」：這首歌排到處理佇列最前面。
    已經在排隊 → 移到最前面；還沒排（沒下載完、沒分離完、旋律沒分析完、還沒升級高品質）→ 補排並放最前面。"""
    d = P.song_dir(sid)
    meta = P.read_meta(d)
    mine = [j for j in jobs.active() if j.get("song") == sid and j.get("kind") == "process"]
    if any(j["status"] == "running" for j in mine):
        raise P.UserError("這首正在處理中")
    for j in mine:
        if j["status"] == "queued" and jobs.to_front(j["id"]):
            return jsonify(job=j["id"], what="moved")
    st = meta.get("steps") or {}
    if not st.get("import"):
        url = meta.get("source_url")
        if not url:
            raise P.UserError("這首歌沒有音檔，請重新匯入")
        P.YT_PAUSE.update(on=False, reason="")
        P.set_dl_state(d, "queued")
        jid = yt_submit(sid, url)
        what = "download"
    elif not st.get("separate") or not st.get("pitch"):
        steps = [s for s in ("separate", "pitch") if not st.get(s)]
        if not st.get("separate"):
            steps = ["separate", "pitch"]
        jid = jobs.submit("process", sid, P.process_song(sid, steps), label=",".join(steps))
        what = "process"
    elif meta.get("separation") == "uvr" and meta.get("vocal_model") != "bs_roformer" \
            and P.hqv_ready() and P.vocal_model(P.load_config()) == P.HQ_VOCAL:
        jid = jobs.submit("process", sid, P.process_song(sid, ["separate", "pitch"]), label="upgrade")
        what = "upgrade"
    else:
        raise P.UserError("這首已經處理完成，不需要再處理")
    jobs.to_front(jid)
    return jsonify(job=jid, what=what)


@bp.get("/api/jobs/<jid>")
def job(jid):
    q = jobs if jobs.get(jid) else vjobs
    j = q.get(jid)
    if not j:
        abort(404)
    j["ahead"] = q.position(jid)
    return jsonify(j)


@bp.get("/api/usage")
def usage():
    return jsonify(C.summary())


EXTERNAL_URLS = ("https://www.twitch.tv/leihustore", "https://github.com/lydek/Leihu-singsing", RELEASES_URL,
                 # 分享錄音：各平台的發文／上傳頁
                 "https://www.facebook.com", "https://www.instagram.com", "https://x.com/compose/post",
                 "https://www.threads.com")


@bp.post("/api/yt-unlock")
def yt_unlock():
    """開啟 YouTube 下載與歌單批次匯入（存在設定檔裡，之後更新版本也維持開啟）。"""
    cfg = P.load_config()
    cfg["yt_enabled"] = True
    P.save_config(cfg)
    return jsonify(ok=True)


@bp.post("/api/jobs/pause-all")
def jobs_pause_all():
    """2.1.1「全部暫停」：處理歌曲的工作都先停（處理中的那首在下一個進度點停下、讓出顯示卡）。
    on 省略時切換。錄音評分、匯出、KEY 轉換照常。"""
    body = request.get_json(force=True, silent=True) or {}
    on = body.get("on")
    on = (not jobs.paused) if on is None else bool(on)
    jobs.set_paused(on)
    return jsonify(paused=on)


@bp.post("/api/live/override")
def live_override():
    """「直播中也照常處理」：接下來一小時不管直播偵測。"""
    P.live_override(3600)
    return jsonify(ok=True)


@bp.post("/api/open-url")
def open_url():
    """用系統預設瀏覽器開啟開發者頁面（只允許固定的幾個網址）。"""
    url = (request.get_json(force=True) or {}).get("url", "").rstrip("/")
    if url not in EXTERNAL_URLS:
        raise P.UserError("不允許開啟這個網址")
    import webbrowser
    if not P.spawned("browser", url):
        webbrowser.open(url)
    return jsonify(ok=True)


@bp.post("/api/reveal")
def reveal():
    """在檔案總管裡顯示匯出的檔案（只允許下載資料夾裡的練唱台資料夾）。"""
    path = Path((request.get_json(force=True) or {}).get("path", "")).resolve()
    base = (P.downloads_dir() / P.APP_NAME).resolve()
    if base not in path.parents or not path.exists():
        raise P.UserError("找不到這個檔案")
    import subprocess
    if P.spawned("reveal", str(path)):
        pass
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])
    return jsonify(ok=True)
