"""歌曲庫：加入歌曲、YouTube 與批次匯入、歌曲資訊、處理、移調、音訊、分類。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_file

import pipeline as P
import score_sheet as SS  # 1.7：旋律譜匯出
import share as SH

from .common import (
    _downloading,
    _key_speed,
    _locked,
    _safe_name,
    after_process,
    apply_sheet,
    jobs,
    prerender_key,
    queue_fine,
    queue_max,
    vjobs,
    yt_submit,
)
from .live import _SETLIST_LOCK, _setlist_read

bp = Blueprint("library", __name__)


@bp.post("/api/youtube/resume")
def youtube_resume():
    """YouTube 限制解除後，繼續下載所有被暫停的歌。"""
    P.YT_PAUSE.update(on=False, reason="")
    n = 0
    for m in sorted(P.pending_downloads(), key=lambda m: m.get("created") or 0):
        if m["dl_state"] == "paused" and not _downloading(m["id"]):
            P.set_dl_state(P.song_dir(m["id"]), "queued")
            P.update_meta(P.song_dir(m["id"]), notes=[])
            yt_submit(m["id"], m["source_url"])
            n += 1
    return jsonify(resumed=n)


# ---------------------------------------------------------------- 歌曲庫

@bp.get("/api/songs")
def songs():
    out = P.list_songs()
    for m in out:   # 2.3.3：「精細處理排隊中」看實際的佇列，不只看歌曲資料裡的標記
        if m.get("fine_queued"):
            m["fine_queued"] = bool(jobs.find("process", m["id"], "fine"))
    return jsonify(out)


@bp.get("/api/songs/search")
def songs_search():
    """歌曲庫搜尋：繁體、簡體互相找得到，另外列出歌名相近的歌。"""
    scope = request.args.get("scope", "all")
    return jsonify(P.search_songs(request.args.get("q", "")[:200], scope if scope in ("name", "lyrics", "all") else "all"))


@bp.post("/api/songs")
def add_song():
    f = request.files.get("file")
    if not f or not f.filename:
        raise P.UserError("沒有收到檔案")
    P.LIB.mkdir(exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=Path(f.filename).suffix, dir=P.LIB)
    os.close(fd)  # Windows 上檔案開著就無法搬移
    tmp = Path(name)
    f.save(tmp)
    sid = P.new_song(f.filename, tmp)
    cat = request.form.get("category") or None
    if cat in P.load_categories():
        P.update_meta(P.song_dir(sid), category=cat)
    steps = ["import", "separate", "pitch", "lyrics"]
    proc = P.process_song(sid, steps)

    def run(report, sid=sid):
        proc(report)
        try:
            after_process(sid)
        except Exception as e:   # 移調預跑等後續工作失敗，不影響這首歌已經處理好
            P.log_error("after_process", e)
    jid = jobs.submit("process", sid, run, label="all")
    return jsonify(id=sid, job=jid)


@bp.post("/api/youtube")
def add_youtube():
    url = P.clean_youtube_url((request.get_json(force=True) or {}).get("url", ""))
    for m in P.list_songs():
        if m.get("source_url") != url:
            continue
        if m.get("steps", {}).get("import"):
            raise P.UserError(f"這首歌已經在歌曲庫裡：{m.get('title')}")
        if _downloading(m["id"]) or m.get("dl_state") == "queued":
            raise P.UserError(f"這首歌已經在下載佇列裡：{m.get('title')}")
        # 之前下載失敗或取消的同一首：直接用原本那一項重新下載，不另外多一項
        P.set_dl_state(P.song_dir(m["id"]), "queued")
        jid = yt_submit(m["id"], url)
        return jsonify(id=m["id"], job=jid, retried=True)
    sid = P.new_youtube_song(url)
    cat = (request.get_json(force=True) or {}).get("category") or None
    if cat in P.load_categories():
        P.update_meta(P.song_dir(sid), category=cat)
    jid = yt_submit(sid, url)
    return jsonify(id=sid, job=jid)


@bp.post("/api/sheet-template")
def sheet_template():
    """批次加入的「下載範本」：把建議格式的歌單 .csv 存到「下載\雷虎練唱台」並在檔案總管裡顯示
    （視窗程式裡的網頁不能直接下載檔案）。Excel 開啟不會亂碼。"""
    out = P.downloads_dir() / P.APP_NAME / "歌單範本.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(P.ROOT / "static" / "song-list-template.csv", out)
    if sys.platform == "win32" and not P.spawned("reveal", str(out)):
        import subprocess
        subprocess.Popen(["explorer", "/select,", str(out)])
    return jsonify(path=str(out))


@bp.post("/api/youtube/resolve")
def youtube_resolve():
    """批次匯入：把多行輸入（影片網址、歌單網址、歌手＋歌名）解析成影片清單，讓使用者確認。"""
    body = request.get_json(force=True) or {}
    lines = body.get("lines") or []
    if not isinstance(lines, list):
        raise P.UserError("格式不正確")
    if isinstance(body.get("sheet"), list):   # 匯入的表單：每一列直接對應，不經過文字框
        return jsonify(items=P.resolve_lines([], body["sheet"]))
    return jsonify(items=P.resolve_lines([str(x) for x in lines]))


@bp.post("/api/youtube/batch")
def youtube_batch():
    """把確認好的影片依順序加入下載佇列。"""
    body = request.get_json(force=True) or {}
    cat = body.get("category") or None
    if cat not in P.load_categories():
        cat = None
    songs = [m for m in P.list_songs() if m.get("source_url")]
    existing = {m["source_url"] for m in songs
                if m.get("steps", {}).get("import") or m.get("dl_state") == "queued" or _downloading(m["id"])}
    retry = {m["source_url"]: m["id"] for m in songs if m["source_url"] not in existing}   # 失敗／取消過的
    added, skipped = [], 0
    updated = 0
    for it in (body.get("items") or [])[:P.BATCH_MAX]:
        sheet = it.get("sheet") if isinstance(it.get("sheet"), dict) else None
        if it.get("update") and sheet:   # 歌曲庫已經有的歌：只更新資訊，不重新下載
            try:
                apply_sheet(str(it["update"]), sheet, cat)
                prerender_key(str(it["update"]))
                updated += 1
            except Exception as e:
                P.log_error("batch update", e)
            continue
        try:
            url = P.clean_youtube_url(it.get("url", ""))
        except P.UserError:
            skipped += 1
            continue
        if url in existing:
            skipped += 1
            continue
        existing.add(url)
        if url in retry:   # 之前下載失敗或取消的同一首：用原本那一項重新下載
            sid = retry[url]
            if sheet:
                apply_sheet(sid, sheet, cat)
            P.set_dl_state(P.song_dir(sid), "queued")
            yt_submit(sid, url)
            added.append(sid)
            continue
        sid = P.new_youtube_song(url, str((sheet or {}).get("song") or it.get("title") or "")[:100])
        if sheet:
            apply_sheet(sid, sheet, cat)
        elif cat:
            P.update_meta(P.song_dir(sid), category=cat)
        yt_submit(sid, url)
        added.append(sid)
    return jsonify(added=len(added), skipped=skipped, updated=updated)


@bp.post("/api/youtube/sheet")
def youtube_sheet():
    f = request.files.get("file")
    if not f:
        raise P.UserError("沒有收到檔案")
    data = f.read()
    if len(data) > 10_000_000:
        raise P.UserError("檔案太大")
    rows = P.read_sheet(data, f.filename or "")
    return jsonify(lines=[r["line"] for r in rows if r.get("line")], rows=rows)


@bp.post("/api/songs/<sid>/mixdown")
def mixdown(sid):
    """隱藏功能：依目前的 KEY、速度、各軌音量（含音量標準化）匯出混音，例如去掉主唱的伴奏。"""
    d = P.song_dir(sid)
    body = request.get_json(force=True) or {}
    key, speed = _key_speed()
    if not P.variant_ready(d, key, speed):
        raise P.UserError("這個 KEY／速度的版本還沒準備好，請先播放一次再匯出")
    import numpy as np
    import soundfile as sf
    vol = body.get("vol") or {}
    gains = {k: min(2.0, max(0.0, float(vol.get(k, 1.0) or 0))) for k in P.STEMS}
    if not any(gains.values()):
        raise P.UserError("所有音軌都是靜音，沒有東西可以匯出")
    fmt = "wav" if body.get("fmt") == "wav" else "mp3"
    meta = P.read_meta(d)
    norm = P.norm_gain_db(meta, gains) if body.get("norm") else 0.0
    kname = f"KEY{key:+d}" if key else "原調"
    sname = f"_{int(round(speed * 100))}%" if abs(speed - 1) > 1e-3 else ""
    vname = "伴奏" if gains["lead"] == 0 else "混音"
    out = P.unique_path(P.downloads_dir() / P.APP_NAME / f"{_safe_name(meta.get('title', '歌曲'))[:50]}_{vname}_{kname}{sname}.{fmt}")

    def run(report):
        report("混音中", 0.2)
        vd = P.variant_dir(d, key, speed)
        parts, sr = [], P.SR
        for k, g in gains.items():
            if g <= 0:
                continue
            y, sr = sf.read(str(P.stem_path(vd, k)), dtype="float32", always_2d=True)
            y = y.T if y.shape[1] == 2 else np.repeat(y.T, 2, 0)
            parts.append(y * g)
        n = min(p.shape[1] for p in parts)
        mix = sum(p[:, :n] for p in parts) * (10 ** (norm / 20))
        mix = SH.peak_limit(mix, sr)
        report("輸出檔案", 0.7)
        out.parent.mkdir(parents=True, exist_ok=True)
        SH.export_audio(mix.astype(np.float32), sr, out)
    jid = jobs.submit("export", sid, run, label="mixdown")
    return jsonify(job=jid, file=str(out))


@bp.get("/api/songs/<sid>")
def song(sid):
    d = P.song_dir(sid)
    meta = P.read_meta(d)
    if time.time() - (meta.get("last_opened") or 0) > 3600:   # 記下最近開過的時間（大量重新處理時常唱的先做）
        meta = P.update_meta(d, last_opened=time.time())
    if "acapella" not in meta and P.stem_path(d, "inst").exists() and P.stem_path(d, "lead").exists():
        meta = P.update_meta(d, acapella=P.is_acapella(d))   # 舊歌曲補上清唱判斷
    if meta.get("lufs") is None and meta.get("steps", {}).get("import"):
        meta = P.measure_loudness(d)   # 舊歌曲第一次開啟時補量響度（約 1 秒）
    if meta.get("steps", {}).get("separate"):
        meta = P.measure_stem_loudness(d)   # 各音軌組合的響度（只算一次，分軌重新處理後重算）
    meta["norm_gain_db"] = P.norm_gain_db(meta)
    meta["norm_target"] = P.TARGET_LUFS
    meta["norm_range"] = [P.NORM_MIN_DB, P.NORM_MAX_DB]
    meta["takes"] = P.list_takes(d)
    return jsonify(meta)


@bp.patch("/api/songs/<sid>")
def rename(sid):
    d = P.song_dir(sid)
    body = request.get_json(force=True) or {}
    changes = {}
    if "title" in body:
        title = (body.get("title") or "").strip()
        if not title:
            raise P.UserError("歌名不能是空的")
        changes["title"] = title[:120]
    if "category" in body:
        cat = body.get("category") or None
        if cat and cat not in P.load_categories():
            raise P.UserError("找不到這個分類")
        changes["category"] = cat
    if "artist" in body:
        changes["artist"] = (body.get("artist") or "").strip()[:80]
    if "title" in body:
        changes["title_locked"] = True
    if "key" in body:   # 預設 KEY：打開這首歌時自動切到這個 KEY，並先做好移調版本
        k = P.parse_key(body.get("key"))
        changes.update(key=k or 0, key_set_at=time.time())
    if "lyrics_flag" in body:   # 歌詞待修標記（直播中發現不準先標起來，下播再處理）
        changes["lyrics_flag"] = bool(body.get("lyrics_flag"))
    if "lyrics_language" in body:   # 這首歌的辨識語言；null＝依設定
        v = body.get("lyrics_language")
        changes["lyrics_language"] = v if v in ("", "zh", "ja", "en", "ko", "yue") else None
    meta = P.update_meta(d, **changes)
    if "key" in changes:
        prerender_key(sid)
    return jsonify(meta)


@bp.delete("/api/songs/<sid>")
def delete_song(sid):
    d = P.song_dir(sid)
    dl = P._dl_lock(sid)
    if not dl.acquire(blocking=False):   # fork（28 A2）：預先下載中刪掉，下載完會再寫出殘缺的歌
        raise P.UserError("這首歌正在下載，請稍後再刪除")
    try:
        with vjobs.lock, jobs.lock:   # 順序和 vjobs 挑工作時查 jobs 一樣（Jobs.wait_for）
            # fork（D2）：執行中的工作接著會寫出殘缺的 meta.json；拒絕時排隊中的工作照舊（28 A5）
            if jobs.song_running(sid) or vjobs.song_running(sid):
                raise P.UserError("這首歌正在處理，請先停止工作再刪除")
            jobs.cancel_song(sid)    # fork（D4）：排隊中的改成已取消，不再顯示
            vjobs.cancel_song(sid)
        shutil.rmtree(d)
    finally:
        dl.release()
    return jsonify(ok=True)


@bp.post("/api/songs/<sid>/process")
def reprocess(sid):
    P.song_dir(sid)
    steps = (request.get_json(force=True) or {}).get("steps") or ["separate", "pitch", "lyrics"]
    steps = [s for s in steps if s in ("import", "separate", "pitch", "lyrics")]
    body = request.get_json(force=True) or {}
    if "vocal_mode" in body:   # 合唱模式：旋律與辨識改用主唱＋合音（多人輪流唱的歌）
        P.update_meta(P.song_dir(sid), vocal_mode="all" if body["vocal_mode"] == "all" else None)
    if body.get("max_quality"):
        return jsonify(job=queue_max(sid))
    label = ",".join(steps)
    jid = jobs.find("process", sid, label) or jobs.submit("process", sid, P.process_song(sid, steps), label=label)
    return jsonify(job=jid)


@bp.post("/api/songs/<sid>/sheet")
def export_sheet(sid):
    """1.7：把旋律＋歌詞畫成一頁頁的圖片（旋律譜），存到下載資料夾。"""
    d = P.song_dir(sid)
    body = request.get_json(force=True) or {}
    meta = P.read_meta(d)
    body.setdefault("title", meta.get("title", ""))
    layout = "A4" if body.get("layout") == "a4" else "手機"
    folder = P.unique_path(P.downloads_dir() / P.APP_NAME / f"{SS.safe_name(meta.get('title', '旋律譜'))} 旋律譜（{layout}）")
    files, tonic = SS.render(d, body, folder)
    return jsonify(files=[str(f) for f in files], folder=str(folder), tonic=tonic)


@bp.get("/api/songs/<sid>/pitch")
def pitch(sid):
    d = P.song_dir(sid)
    p = d / "pitch.json"
    if not p.exists():
        raise P.UserError("這首歌還沒分析旋律")
    return send_file(p, mimetype="application/json", max_age=0)


@bp.post("/api/songs/<sid>/variant")
def variant(sid):
    d = P.song_dir(sid)
    key, speed = _key_speed()
    if P.variant_ready(d, key, speed):
        return jsonify(ready=True)
    label = f"{key}:{speed}"
    vjobs.cancel_queued("variant", sid, label)   # 連按時，中間不要的 KEY 不用再算
    jid = vjobs.find("variant", sid, label) or vjobs.submit(
        "variant", sid, lambda report: P.step_variant(d, key, speed, report), label=label)
    return jsonify(ready=False, job=jid)


@bp.post("/api/songs/<sid>/fine")
def song_fine(sid):
    """手動排一次精細處理（最精細的主唱分離＋重新分析旋律）。"""
    P.song_dir(sid)
    return jsonify(job=queue_fine(sid))


@bp.get("/api/songs/<sid>/audio/<stem>")
def audio(sid, stem):
    d = P.song_dir(sid)
    if stem not in P.STEMS + ("mix",):
        abort(404)
    key, speed = _key_speed()
    base = d if stem == "mix" else P.variant_dir(d, key, speed)
    p = P.stem_path(base, stem)
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="audio/flac" if p.suffix == ".flac" else "audio/wav", conditional=True, max_age=0)


@bp.post("/api/songs/<sid>/redownload")
def redownload(sid):
    """下載失敗或取消的 YouTube 歌曲，重新排隊下載。"""
    d = P.song_dir(sid)
    meta = P.read_meta(d)
    url = meta.get("source_url")
    if not url or meta.get("steps", {}).get("import"):
        raise P.UserError("這首歌不需要重新下載")
    P.YT_PAUSE.update(on=False, reason="")   # 使用者手動重試：解除暫停
    if any(j.get("song") == sid and j.get("status") in ("queued", "running") for j in jobs.active()):
        raise P.UserError("這首歌已經在下載佇列裡")
    P.set_dl_state(d, "queued")
    jid = yt_submit(sid, url)
    return jsonify(job=jid)


# ---------------------------------------------------------------- 分類

@bp.get("/api/categories")
def categories():
    return jsonify(P.load_categories())


@bp.post("/api/categories")
@_locked
def edit_categories():
    body = request.get_json(force=True) or {}
    act, name, new = body.get("action"), (body.get("name") or "").strip(), (body.get("new") or "").strip()
    cats = P.load_categories()
    if act == "add":
        if not name:
            raise P.UserError("分類名稱不能是空的")
        if name in cats:
            raise P.UserError("已經有這個分類")
        cats.append(name[:30])
    elif act == "rename":
        if name not in cats or not new:
            raise P.UserError("找不到這個分類")
        if new in cats:
            raise P.UserError("已經有這個分類")
        cats[cats.index(name)] = new[:30]
        for m in P.list_songs():
            if m.get("category") == name:
                P.update_meta(P.song_dir(m["id"]), category=new[:30])
    elif act == "delete":
        if name in cats:
            cats.remove(name)
        for m in P.list_songs():   # 歌曲不會刪掉，只是變成未分類
            if m.get("category") == name:
                P.update_meta(P.song_dir(m["id"]), category=None)
    elif act == "order":
        order = [c for c in body.get("order") or [] if c in cats]
        cats = order + [c for c in cats if c not in order]
    else:
        raise P.UserError("不支援的操作")
    P.save_categories(cats)
    return jsonify(cats)


@bp.post("/api/upgrade-separation")
def upgrade_separation():
    """2.1.0：把還沒用新的人聲模型（BS-Roformer）處理過的歌，排進佇列重新分離＋分析旋律（歌詞、錄音不動）。
    排在佇列最後、直播中會暫停，所以可以放著慢慢跑。"""
    if not P.hqv_ready():
        P.start_hqv_download()
        raise P.UserError("高品質人聲模型（約 640 MB）還在下載，下載完成後再按一次")
    if P.vocal_model(P.load_config()) != P.HQ_VOCAL:
        raise P.UserError("目前「人聲分離品質」設定不會用高品質模型（標準，或沒有顯示卡），升級不會有差別")
    n = 0
    with _SETLIST_LOCK:
        sl = {i.get("id") for i in _setlist_read()["items"]}
    songs = sorted(P.list_songs(), key=lambda m: (-P.usage_score(m, sl), -(m.get("created") or 0)))   # 常唱的先做
    for s in songs:
        stp = s.get("steps") or {}
        if not stp.get("separate") or (s.get("vocal_model") == "bs_roformer" and stp.get("pitch")) or s.get("separation") != "uvr":
            continue
        if jobs.find("process", s["id"], "upgrade") or jobs.find("process", s["id"], "max"):
            continue
        jobs.submit("process", s["id"], P.process_song(s["id"], ["separate", "pitch"]), label="upgrade")
        n += 1
    return jsonify(queued=n)
