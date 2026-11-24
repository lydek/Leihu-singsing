"""錄音：評分、匯出與分享、Claude 建議。"""
from __future__ import annotations

import contextlib
import json
import math
import time
import uuid

from flask import Blueprint, abort, jsonify, request, send_file

import claude_api as C
import pipeline as P
import share as SH

from .common import _safe_name, jobs, vjobs

bp = Blueprint("takes", __name__)


# ---------------------------------------------------------------- 錄音

@bp.post("/api/songs/<sid>/takes")
def add_take(sid):
    d = P.song_dir(sid)
    f = request.files.get("file")
    if not f:
        raise P.UserError("沒有收到錄音")
    # fork（D3）：存錄音檔之前先驗證。不是數字原本回 500，超出範圍照存（之後評分可能除以零）
    try:
        start, duration = float(request.form.get("start", 0)), float(request.form.get("duration", 0))
        key, speed = int(request.form.get("key", 0)), float(request.form.get("speed", 1))
    except (TypeError, ValueError):
        raise P.UserError("錄音的開始時間、長度、KEY 或速度格式不正確") from None
    if not (math.isfinite(start) and math.isfinite(duration) and start >= 0 and duration >= 0):
        raise P.UserError("錄音的開始時間或長度不正確")
    if not -6 <= key <= 6 or not 0.5 <= speed <= 1.5:
        raise P.UserError("KEY 範圍是 ±6，速度範圍是 50%–150%")
    td = d / "takes"
    td.mkdir(exist_ok=True)
    tid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    f.save(td / f"{tid}.wav")
    info = {
        "id": tid, "created": time.time(), "status": "scoring",
        "start": start, "key": key, "speed": speed, "duration": duration,
    }
    P.atomic_write_text(td / f"{tid}.json", json.dumps(info, ensure_ascii=False))
    jid = jobs.submit("take", sid, lambda report: _score(P.song_dir(sid), tid, report), label=tid)  # 開始時才取路徑
    return jsonify(id=tid, job=jid)


def _score(d, tid, report):
    """評分；失敗時把原因寫回錄音資料，畫面才不會一直停在「評分中」。"""
    try:
        P.score_take(d, tid, report)
    except Exception as e:
        j = d / "takes" / f"{tid}.json"
        try:
            info = json.loads(j.read_text("utf-8"))
            info.update(status="error", error=f"評分失敗：{e}")
            P.write_take_info(j.parent, tid, info)   # fork（M7）：錄音已刪就不寫
        except Exception:
            pass
        raise


def _take_paths(sid, tid):
    d = P.song_dir(sid)
    if not all(c.isalnum() or c == "-" for c in tid):
        abort(404)
    return d, d / "takes" / f"{tid}.json", d / "takes" / f"{tid}.wav"


@contextlib.contextmanager
def _deleted_take_404(j, w):
    """fork（9、Codex r4-6）：讀錄音、算人聲的途中錄音被刪掉回 404，不是 500。讀 JSON 丟 FileNotFoundError，
    soundfile 讀 WAV 丟 LibsndfileError（RuntimeError，複審 P2-2）。錄音還在、是別的錯誤照原本的錯誤處理。"""
    try:
        yield
    except (OSError, RuntimeError):
        if j.exists() and w.exists():
            raise
        abort(404)


@bp.get("/api/songs/<sid>/takes/<tid>")
def get_take(sid, tid):
    _, j, _ = _take_paths(sid, tid)
    if not j.exists():
        abort(404)
    return send_file(j, mimetype="application/json", max_age=0)


@bp.post("/api/songs/<sid>/takes/<tid>/rescore")
def rescore_take(sid, tid):
    _, j, w = _take_paths(sid, tid)
    if not (j.exists() and w.exists()):
        abort(404)
    jid = jobs.find("take", sid, tid) or jobs.submit("take", sid, lambda report: _score(P.song_dir(sid), tid, report), label=tid)
    return jsonify(job=jid)


# ---------------------------------------------------------------- 匯出／分享錄音（1.4）

def _share_opts():
    b = request.get_json(force=True) or {}
    def num(k, lo, hi):
        try:
            return max(lo, min(hi, float(b.get(k, 0))))
        except (TypeError, ValueError):
            return 0.0
    return {"vocal_db": num("vocal_db", -40, 12), "back_db": num("back_db", -40, 6), "inst_db": num("inst_db", -40, 6),
            "reverb": b.get("reverb") if b.get("reverb") in SH.REVERB else "light", "polish": bool(b.get("polish", True)),
            "formats": [f for f in (b.get("formats") or ["mp3"]) if f in ("mp3", "wav", "mp4", "png")] or ["mp3"],
            "range": "take" if b.get("range") == "take" else "full"}


def _need_variant(d, sid, tid):
    """匯出要用錄音當時的 KEY／速度版本；還沒有就先排隊產生。"""
    info = SH.load_take(d, tid)
    key, speed = SH.variant_needed(d, info)
    if P.variant_ready(d, key, speed):
        return None
    label = f"{key}:{speed}"
    return vjobs.find("variant", sid, label) or vjobs.submit(
        "variant", sid, lambda report: P.step_variant(P.song_dir(sid), key, speed, report), label=label)  # 開始時才取路徑


@bp.post("/api/songs/<sid>/takes/<tid>/mixprep")
def take_mixprep(sid, tid):
    """即時試聽用：回傳處理好的人聲（和伴奏一樣長）、合音／伴奏的網址，以及自動平衡的建議音量。"""
    d, j, w = _take_paths(sid, tid)
    opts = _share_opts()
    with _deleted_take_404(j, w):
        need = _need_variant(d, sid, tid)
        if need:
            return jsonify(need_job=need)
        info = SH.load_take(d, tid)
        key, speed = SH.variant_needed(d, info)
        back = P.stem_path(P.variant_dir(d, key, speed), "back")
        sr, n = sf_info(back)
        tag = f"v2_{int(opts['polish'])}_{opts['reverb']}"   # v2：1.4.9 改了人聲處理，舊的暫存要重做
        vp = d / "takes" / f"{tid}.vocal_{tag}.wav"
        if not vp.exists():
            for old in (d / "takes").glob(f"{tid}.vocal_*.wav"):
                old.unlink(missing_ok=True)
            if not P.write_take_audio(d / "takes", tid, vp, SH.vocal_track(d, tid, opts, sr, n), sr):
                abort(404)   # fork（9）：處理期間錄音被刪掉了
        mtime = int(vp.stat().st_mtime)
        balance = SH.suggest_balance(d, tid, opts) if request.args.get("balance") else None
    q = f"key={key}&speed={speed}"
    start = (float(info.get("start", 0)) + float(info.get("offset", 0))) / speed
    return jsonify(vocal=f"/api/songs/{sid}/takes/{tid}/vocal/{tag}?t={mtime}",
                   back=f"/api/songs/{sid}/audio/back?{q}", inst=f"/api/songs/{sid}/audio/inst?{q}",
                   start=round(start, 3), key=key, speed=speed, master_db=SH.master_db(d, key, speed),
                   balance=balance)


def sf_info(path):
    import soundfile as sf
    i = sf.info(str(path))
    return i.samplerate, i.frames


@bp.get("/api/songs/<sid>/takes/<tid>/vocal/<tag>")
def take_vocal(sid, tid, tag):
    d, _, _ = _take_paths(sid, tid)
    p = d / "takes" / f"{tid}.vocal_{tag}.wav"
    if not all(c.isalnum() or c == "_" for c in tag) or not p.exists():
        abort(404)
    return send_file(p, mimetype="audio/wav", max_age=0)


@bp.post("/api/songs/<sid>/takes/delete")
def delete_takes(sid):
    """批次刪除錄音。"""
    ids = (request.get_json(force=True) or {}).get("ids") or []
    n = 0
    for tid in ids:
        _, _, w = _take_paths(sid, str(tid))
        P.delete_take_files(w.parent, str(tid))
        n += 1
    return jsonify(deleted=n)


@bp.post("/api/songs/<sid>/takes/<tid>/preview")
def take_preview(sid, tid):
    d, j, w = _take_paths(sid, tid)
    with _deleted_take_404(j, w):
        need = _need_variant(d, sid, tid)
        if need:
            return jsonify(need_job=need)
        y, sr, _ = SH.mix_take(d, tid, _share_opts())
    if not P.write_take_audio(d / "takes", tid, d / "takes" / f"{tid}.preview.wav", y, sr):
        abort(404)   # fork（9）：處理期間錄音被刪掉了
    return jsonify(url=f"/api/songs/{sid}/takes/{tid}/preview.wav?t={int(time.time() * 1000)}")


@bp.get("/api/songs/<sid>/takes/<tid>/preview.wav")
def take_preview_file(sid, tid):
    d, _, _ = _take_paths(sid, tid)
    p = d / "takes" / f"{tid}.preview.wav"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="audio/wav", max_age=0)


@bp.post("/api/songs/<sid>/takes/<tid>/export")
def take_export(sid, tid):
    d, j, w = _take_paths(sid, tid)
    opts = _share_opts()
    with _deleted_take_404(j, w):   # 複審 P2-2：錄音剛被刪掉回 404
        if opts["formats"] != ["png"]:   # 只匯出成績卡不需要伴奏
            need = _need_variant(d, sid, tid)
            if need:
                return jsonify(need_job=need)
        SH.load_take(d, tid)
    title = P.read_meta(d).get("title", "我的演唱")
    base = P.downloads_dir() / P.APP_NAME / f"{_safe_name(title)[:50]}_我的演唱_{tid[:13]}"
    # 2.3.3：同一次錄音匯出過，這次的檔名加上編號、不覆蓋上次的。
    # fork：檢查和產生檔名都用 SH.out_path（M4：歌名有「.」時 with_suffix 會截斷檔名）
    sib = tuple("." + f for f in ("wav", "mp3", "mp4") if f in opts["formats"]) + (("_成績卡.png",) if "png" in opts["formats"] else ())
    if any(SH.out_path(base, s).exists() for s in sib):
        n = 2
        while any(SH.out_path(base.with_name(f"{base.name} ({n})"), s).exists() for s in sib):
            n += 1
        base = base.with_name(f"{base.name} ({n})")
    files = ([str(SH.out_path(base, "_成績卡.png"))] if "png" in opts["formats"] else []) + \
        [str(SH.out_path(base, "." + f)) for f in ("wav", "mp3", "mp4") if f in opts["formats"]]
    jid = jobs.submit("export", sid, lambda report: SH.export_take(P.song_dir(sid), tid, opts, base, report), label=tid)  # 開始時才取路徑
    return jsonify(job=jid, files=files)


@bp.get("/api/songs/<sid>/takes/<tid>/audio")
def take_audio(sid, tid):
    _, _, w = _take_paths(sid, tid)
    if not w.exists():
        abort(404)
    return send_file(w, mimetype="audio/wav", conditional=True, max_age=0)


@bp.delete("/api/songs/<sid>/takes/<tid>")
def delete_take(sid, tid):
    _, _, w = _take_paths(sid, tid)
    P.delete_take_files(w.parent, tid)   # 含試聽用的暫存人聲
    return jsonify(ok=True)


@bp.post("/api/songs/<sid>/claude/coach")
def claude_coach(sid):
    d = P.song_dir(sid)
    tid = (request.get_json(force=True) or {}).get("take")
    _, j, _ = _take_paths(sid, tid or "")
    if not j.exists():
        raise P.UserError("找不到這次錄音")
    t = json.loads(j.read_text("utf-8"))
    if t.get("acc") is None:
        raise P.UserError("這次錄音還沒評分完成")
    title = P.read_meta(d).get("title", "")
    rows = "\n".join(f"{l['t']:.1f}s\t{l['text']}\t命中 {l['acc'] * 100:.0f}%\t平均偏差 {l['mean']:+.0f} 音分"
                     for l in t.get("lines", []))
    prompt = (
        f"你是聲樂老師。學生用練唱工具唱了〈{title}〉，以下是音準分析結果。\n"
        f"移調 {t['key']:+d} 半音，速度 {t['speed'] * 100:.0f}%，"
        f"{'比原唱低一個八度' if t.get('octave', 0) < 0 else '比原唱高一個八度' if t.get('octave', 0) > 0 else '與原唱同八度'}。\n"
        f"整體：音準命中率 {t['acc'] * 100:.0f}%（±50 音分內），平均偏差 {t['mean']:+.0f} 音分（正值偏高、負值偏低），"
        f"平均絕對偏差 {t['spread']:.0f} 音分。\n"
        "逐句：\n" + (rows or "（沒有歌詞時間，無逐句資料）") + "\n\n"
        "請用台灣繁體中文，寫 200 字以內的練習建議：先講整體傾向，再挑最需要練的 2–3 句，"
        "說明可能原因（例如起音偏低、高音撐不住、尾音下墜）和具體練法。不要用條列以外的格式，不要客套。"
    )
    text, usage = C.ask_text(prompt, 3000, "coach", title)
    t["coach"] = text
    P.write_take_info(j.parent, j.stem, t)   # fork（M7）：等 Claude 回覆期間錄音可能被刪掉，照樣回傳建議
    return jsonify(text=text, usage=usage)
