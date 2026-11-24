"""直播與練唱畫面周邊：歌詞疊加、全域快捷鍵、本場歌單、app 視窗、逐句調整視窗。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_from_directory

import exporter as EX  # 2.3.0：匯出歌單、直播歌單回顧
import hotkeys as HK  # 2.0：自訂全域快捷鍵
import pipeline as P

from .common import HOST, PORT, _locked, _safe_name

bp = Blueprint("live", __name__)


# 1.4.3：直播歌詞（去背）。練唱畫面把目前狀態送來，/overlay 頁面再來讀
OVERLAY = {"sid": None, "title": "", "t": 0.0, "playing": False, "rate": 1.0, "color": True,
           "lines": [], "ver": 0, "at": 0.0}


_ov_lock = threading.Lock()


@bp.get("/overlay")
def overlay_page():
    return send_from_directory(current_app.static_folder, "overlay.html", max_age=0)


@bp.post("/api/overlay")
def overlay_set():
    d = request.get_json(force=True) or {}
    with _ov_lock:
        if d.get("sid") != OVERLAY["sid"] and "lines" not in d:
            OVERLAY["lines"] = []
        if "lines" in d:
            OVERLAY["lines"] = d["lines"] if isinstance(d["lines"], list) else []
        if "lines" in d or d.get("sid") != OVERLAY["sid"]:
            OVERLAY["ver"] += 1
        OVERLAY.update(sid=d.get("sid"), title=str(d.get("title") or ""), artist=str(d.get("artist") or ""),
                       key=int(d.get("key") or 0), t=float(d.get("t") or 0),
                       playing=bool(d.get("playing")), rate=float(d.get("rate") or 1),
                       color=bool(d.get("color", True)), at=time.time())
    return jsonify(ok=True)


@bp.get("/api/overlay")
def overlay_get():
    with _ov_lock:
        o = dict(OVERLAY)
    t = o["t"] + ((time.time() - o["at"]) * o["rate"] if o["playing"] else 0)
    out = {"sid": o["sid"], "title": o["title"], "artist": o.get("artist", ""), "key": o.get("key", 0),
           "t": t, "playing": o["playing"], "rate": o["rate"],
           "color": o["color"], "ver": o["ver"]}
    if request.args.get("ver") != str(o["ver"]):
        out["lines"] = o["lines"]
    return jsonify(out)


# ---------------------------------------------------------------- 全域快捷鍵設定（2.0）
@bp.get("/api/hotkeys")
def hotkeys_get():
    cfg = P.load_config()
    try:
        status = json.loads((P.DATA / "hotkeys_status.json").read_text("utf-8"))
    except (OSError, ValueError):
        status = None   # 不是用桌面版開的（瀏覽器），或還沒註冊過
    return jsonify(actions=[{"id": a, "label": l, "default": d} for a, l, d in HK.ACTIONS],
                   current=HK.current(cfg), enabled=cfg.get("global_hotkeys") is not False, status=status)


@bp.post("/api/hotkeys")
@_locked
def hotkeys_set():
    combos = (request.get_json(force=True) or {}).get("combos") or {}
    cfg = P.load_config()
    cur = HK.current(cfg)
    for a, v in combos.items():
        if a not in HK.ACTION_IDS:
            continue
        v = str(v or "").strip()
        if v and not HK.parse(v):
            raise P.UserError(f"「{v}」不能當快捷鍵：要加 Ctrl、Alt、Shift 或 Win（F13～F24 可以單獨使用）")
        cur[a] = v
    used = [v for v in cur.values() if v]
    if len(used) != len(set(used)):
        dup = next(v for v in used if used.count(v) > 1)
        raise P.UserError(f"「{dup}」設定給了兩個功能，請換一個")
    cfg["hotkeys"] = cur
    P.save_config(cfg)
    return jsonify(current=cur)


@bp.post("/api/hotkeys/pause")
def hotkeys_pause():
    """設定畫面錄新按鍵時，請桌面程式暫時放開全部快捷鍵（不然按下去會直接觸發，畫面收不到）。"""
    f = P.DATA / ".hotkeys_pause"
    if (request.get_json(force=True) or {}).get("on"):
        f.write_text("1", "utf-8")
    else:
        f.unlink(missing_ok=True)
    return jsonify(ok=True)


# ---------------------------------------------------------------- 本場歌單（1.7）
SETLIST_PATH = P.DATA / "setlist.json"


SETLIST_DIR = P.DATA / "setlists"   # 2.3.0：過去場次的歌單（開始新的一場時把上一場存起來）


_SETLIST_LOCK = threading.Lock()


SETLIST_GAP = 6 * 3600   # 距離上一首超過 6 小時就當成新的一場


def _setlist_read() -> dict:
    try:
        d = json.loads(SETLIST_PATH.read_text("utf-8"))
        if isinstance(d.get("items"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"started": None, "items": []}


def _setlist_save_current(d: dict) -> None:
    # 壞掉的 setlist.json 讀起來是空歌單，直接寫會蓋掉原本的場次：先改名保留（防止資料遺失）
    P.keep_corrupt(SETLIST_PATH, lambda x: isinstance(x, dict) and isinstance(x.get("items"), list))
    P.atomic_write_text(SETLIST_PATH, json.dumps(d, ensure_ascii=False))


def _setlist_archive(d: dict) -> None:
    """把一場歌單存進 setlists\（檔名是開始時間），之後還能回顧、匯出。"""
    if not d.get("items"):
        return
    SETLIST_DIR.mkdir(exist_ok=True)
    t = d.get("started") or d["items"][0]["t"]
    P.atomic_write_text(SETLIST_DIR / (time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + ".json"),
                        json.dumps(d, ensure_ascii=False))


def _setlist_load(name: str | None) -> dict:
    if not name:
        return _setlist_read()
    import re
    if not re.fullmatch(r"\d{8}-\d{6}\.json", name or ""):
        raise P.UserError("找不到這一場")
    try:
        return json.loads((SETLIST_DIR / name).read_text("utf-8"))
    except (OSError, ValueError):
        raise P.UserError("找不到這一場")


@bp.get("/api/setlist")
def setlist_get():
    with _SETLIST_LOCK:
        return jsonify(_setlist_load(request.args.get("file")))


def _setlist_write(name: str | None, d: dict) -> None:
    if name:
        _setlist_load(name)   # 檢查檔名
        P.atomic_write_text(SETLIST_DIR / name, json.dumps(d, ensure_ascii=False))
    else:
        _setlist_save_current(d)


@bp.post("/api/setlist/remove")
def setlist_remove():
    """刪除一場裡勾選的歌（idx：第幾首，從 0 開始）。刪光了就連這一場一起刪。"""
    b = request.get_json(force=True) or {}
    name = b.get("file") or None
    idx = {int(i) for i in (b.get("idx") or []) if str(i).lstrip("-").isdigit()}
    if not idx:
        raise P.UserError("請先勾選要刪除的歌")
    with _SETLIST_LOCK:
        d = _setlist_load(name)
        d["items"] = [it for i, it in enumerate(d["items"]) if i not in idx]
        if name and not d["items"]:
            (SETLIST_DIR / name).unlink(missing_ok=True)
        else:
            if not d["items"]:
                d["started"] = None
            _setlist_write(name, d)
    return jsonify(left=len(d["items"]))


@bp.post("/api/setlist/delete")
def setlist_delete():
    """刪除整場（本場：清空、不保存；過去的場次：刪掉那個檔案）。"""
    b = request.get_json(force=True) or {}
    name = b.get("file") or None
    with _SETLIST_LOCK:
        if name:
            _setlist_load(name)
            (SETLIST_DIR / name).unlink(missing_ok=True)
        else:
            _setlist_save_current({"started": None, "items": []})
    return jsonify(ok=True)


@bp.get("/api/setlist/history")
def setlist_history():
    """本場＋過去的場次（新的在前），最多 60 場。"""
    out = []
    with _SETLIST_LOCK:
        cur = _setlist_read()
        if cur["items"]:
            out.append({"file": "", "started": cur.get("started") or cur["items"][0]["t"], "count": len(cur["items"]), "current": True})
        if SETLIST_DIR.exists():
            for p in sorted(SETLIST_DIR.glob("*.json"), reverse=True)[:60]:
                try:
                    d = json.loads(p.read_text("utf-8"))
                    out.append({"file": p.name, "started": d.get("started") or d["items"][0]["t"], "count": len(d["items"])})
                except (OSError, ValueError, KeyError, IndexError):
                    continue
    return jsonify(out)


@bp.post("/api/export/setlist")
def export_setlist():
    """直播歌單回顧：存成文字檔（YouTube 章節或歌單文字，內容由畫面產生）或 Excel。"""
    b = request.get_json(force=True) or {}
    with _SETLIST_LOCK:
        d = _setlist_load(b.get("file") or None)
    if not d.get("items"):
        raise P.UserError("這一場沒有唱過的歌")
    day = time.strftime("%Y%m%d", time.localtime(d.get("started") or d["items"][0]["t"]))
    if b.get("format") == "xlsx":
        start = float(b["start"]) if b.get("start") else None
        p = EX.save_download(f"歌單回顧_{day}.xlsx", EX.setlist_xlsx(d["items"], start))
    else:
        text = str(b.get("text") or "")[:200000]
        if not text.strip():
            raise P.UserError("沒有內容可以存")
        p = EX.save_download(f"{'YouTube章節' if b.get('kind') == 'chapters' else '歌單'}_{day}.txt", text)
    _reveal(p)
    return jsonify(path=str(p))


@bp.post("/api/export/songlist")
def export_songlist():
    """匯出歌單：網頁（給觀眾看）、Excel，或可以用「批次加入」再匯回的 CSV。存到「下載\雷虎練唱台」並在檔案總管裡顯示。"""
    b = request.get_json(force=True) or {}
    fmt = b.get("format", "html")
    cat = b.get("category")
    scope = None if not cat or cat == "__all__" else ("" if cat == "__none__" else str(cat))
    rows = EX.song_rows(scope, ready_only=bool(b.get("ready_only", True)))
    if not rows:
        raise P.UserError("沒有符合條件的歌可以匯出")
    title = str(b.get("title") or "點歌清單").strip()[:60] or "點歌清單"
    day = time.strftime("%Y%m%d")
    # fork：標題當檔名前先清掉 \ / : 等字元（原本 / 會 500、../ 寫到下載資料夾外、: 在 NTFS 寫成替代資料流）
    if fmt == "xlsx":
        p = EX.save_download(_safe_name(f"{title}_{day}.xlsx"), EX.songlist_xlsx(rows, bool(b.get("show_key", True))))
    elif fmt == "csv":
        p = EX.save_download(f"練唱台歌單_{day}.csv", EX.songlist_csv(rows))
    else:
        p = EX.save_download(_safe_name(f"{title}_{day}.html"),
                             EX.songlist_html(rows, title, bool(b.get("show_key", True))))
    _reveal(p)
    return jsonify(path=str(p), count=len(rows))


def _reveal(p: Path) -> None:
    if sys.platform == "win32" and not P.spawned("reveal", str(p)):
        import subprocess
        subprocess.Popen(["explorer", "/select,", str(p)])


@bp.post("/api/setlist/add")
def setlist_add():
    b = request.get_json(force=True) or {}
    now = time.time()
    item = {"t": now, "id": str(b.get("id", ""))[:20], "title": str(b.get("title", ""))[:200],
            "artist": str(b.get("artist", ""))[:120], "key": int(b.get("key") or 0)}
    try:   # 2.3.0：這首開始唱的時間（畫面送來的是累積播放了幾秒），給 YouTube 章節用
        item["s"] = now - max(0.0, min(600.0, float(b.get("played") or 30)))
    except (TypeError, ValueError):
        item["s"] = now - 30
    with _SETLIST_LOCK:
        d = _setlist_read()
        if not d["items"] or now - d["items"][-1]["t"] > SETLIST_GAP:
            _setlist_archive(d)   # 上一場存起來
            d = {"started": item["s"], "items": []}
        last = d["items"][-1] if d["items"] else None
        if not (last and last["id"] == item["id"] and now - last["t"] < 600):   # 同一首十分鐘內不重複記
            d["items"].append(item)
            try:
                with P.library_write_slot() as w:   # fork（70）：搬移中只記在歌單裡，次數寫到舊位置搬完會不見
                    if w:
                        sd = P.song_dir(item["id"])
                        with P._META_LOCK:
                            m = P.read_meta(sd)
                            m["sung"] = (m.get("sung") or 0) + 1
                            m["last_sung"] = now
                            P.write_meta(sd, m)
            except Exception:
                pass   # 歌曲已刪除或編號不對：只記在歌單裡
        _setlist_save_current(d)
    return jsonify(ok=True)


@bp.post("/api/setlist/clear")
def setlist_clear():
    with _SETLIST_LOCK:
        _setlist_archive(_setlist_read())   # 清空前先存起來，之後還能回顧
        _setlist_save_current({"started": None, "items": []})
    return jsonify(ok=True)


@bp.post("/api/open-overlay")
def open_overlay():
    """用預設瀏覽器開啟直播歌詞預覽（只允許本機的 /overlay）。"""
    q = (request.get_json(force=True) or {}).get("query", "")
    import re
    import webbrowser
    if not re.fullmatch(r"[A-Za-z0-9=&%._-]*", q):
        raise P.UserError("網址格式不正確")
    url = f"http://{HOST}:{PORT}/overlay?{q}"
    if not P.spawned("browser", url):
        webbrowser.open(url)
    return jsonify(ok=True)


def _app_browser() -> str | None:
    """找 Edge 或 Chrome（Windows 內建 Edge，通常一定找得到）。"""
    if sys.platform != "win32":
        return None
    for env, rel in (("ProgramFiles(x86)", r"Microsoft\Edge\Application\msedge.exe"),
                     ("ProgramFiles", r"Microsoft\Edge\Application\msedge.exe"),
                     ("ProgramFiles", r"Google\Chrome\Application\chrome.exe"),
                     ("ProgramFiles(x86)", r"Google\Chrome\Application\chrome.exe"),
                     ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe")):
        base = os.environ.get(env)
        if base and (Path(base) / rel).is_file():
            return str(Path(base) / rel)
    return None


@bp.post("/api/open-app")
def open_app():
    """開一個獨立的練唱台視窗（Edge 應用程式模式，沒有網址列）。
    桌面版視窗的聲音由 WebView2 的背景程式播出，Discord「分享應用程式」抓不到；
    這個視窗是一般瀏覽器程式，分享它時畫面和音樂會一起送出。
    用獨立的設定資料夾，所以是自己一個程式，不會混到其他分頁的聲音。"""
    url = f"http://{HOST}:{PORT}/"
    exe = _app_browser()
    if exe:
        prof = P.DATA / ".appwin"
        prof.mkdir(exist_ok=True)
        if not P.spawned("app-window", url):
            subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={prof}", "--window-size=1380,920",
                              "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                              "--disable-sync", "--autoplay-policy=no-user-gesture-required"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import webbrowser
        if not P.spawned("browser", url):
            webbrowser.open(url)
    return jsonify(ok=True, app=bool(exe))


# ---------------------------------------------------------------- 逐句調整的獨立視窗（1.6.5）
# 獨立視窗（/timing）和練唱畫面可能在不同的程式裡（桌面版視窗、Edge 視窗），所以透過伺服器轉送：
# 練唱畫面把歌詞與播放時間送上來，獨立視窗用長輪詢讀；獨立視窗按的按鈕變成指令，練唱畫面用長輪詢收。
# owner 是練唱畫面開視窗時產生的代號，指令只送給開它的那個畫面。
TIMING: dict = {"state": {}, "sseq": 0, "cmds": [], "cseq": 0, "seen": {}}


_TCOND = threading.Condition()


@bp.post("/api/timing/state")
def timing_state_put():
    body = request.get_json(force=True) or {}
    owner = str(body.pop("owner", ""))[:20]
    if not owner:
        raise P.UserError("缺少 owner")
    with _TCOND:
        for o in [o for o, t in TIMING["seen"].items() if time.time() - t > 3600 and o != owner]:   # 很久沒開的調整視窗
            TIMING["seen"].pop(o, None); TIMING["state"].pop(o, None)
        st = TIMING["state"].setdefault(owner, {})
        st.update(body)
        TIMING["sseq"] += 1
        st["seq"] = TIMING["sseq"]
        _TCOND.notify_all()
    return jsonify(ok=True)


@bp.get("/api/timing/state")
def timing_state_get():
    owner = request.args.get("owner", "")[:20]
    after = int(request.args.get("after", 0) or 0)
    with _TCOND:
        TIMING["seen"][owner] = time.time()
        _TCOND.wait_for(lambda: TIMING["state"].get(owner, {}).get("seq", 0) > after, timeout=15)
        st = dict(TIMING["state"].get(owner, {}))
    return jsonify(st)


@bp.post("/api/timing/cmd")
def timing_cmd_put():
    body = request.get_json(force=True) or {}
    owner = str(body.get("owner", ""))[:20]
    with _TCOND:
        TIMING["seen"][owner] = time.time()
        TIMING["cseq"] += 1
        TIMING["cmds"] = TIMING["cmds"][-200:] + [{"seq": TIMING["cseq"], "owner": owner, "cmd": body.get("cmd") or {}}]
        _TCOND.notify_all()
    return jsonify(ok=True)


@bp.get("/api/timing/cmds")
def timing_cmds_get():
    owner = request.args.get("owner", "")[:20]
    after = int(request.args.get("after", -1) or 0)

    def mine():
        return [c for c in TIMING["cmds"] if c["owner"] == owner and c["seq"] > after]
    with _TCOND:
        if after < 0:   # 第一次：從現在開始收
            return jsonify(seq=TIMING["cseq"], cmds=[], alive=False)
        _TCOND.wait_for(lambda: bool(mine()), timeout=20)
        out = mine()
        alive = time.time() - TIMING["seen"].get(owner, 0) < 40
        return jsonify(seq=max([after] + [c["seq"] for c in out]), cmds=[c["cmd"] for c in out], alive=alive)


@bp.get("/timing")
def timing_page():
    return send_from_directory(P.ROOT / "static", "timing.html", max_age=0)
