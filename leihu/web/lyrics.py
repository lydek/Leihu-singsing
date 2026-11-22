"""歌詞：讀寫、假名、Claude 對時、線上搜尋、匯出。"""
from __future__ import annotations

import json

from flask import Blueprint, jsonify, request

import claude_api as C
import pipeline as P

from .common import VERSION, _safe_name

bp = Blueprint("lyrics", __name__)


@bp.get("/api/songs/<sid>/lyrics")
def get_lyrics(sid):
    d = P.song_dir(sid)

    meta = P.read_meta(d)

    def rd(n):
        if not (d / n).exists():
            return ""
        t = (d / n).read_text("utf-8")
        if n == "lyrics_auto.lrc" or (n == "lyrics.lrc" and meta.get("lyrics_source") in (None, "whisper")):
            c = P.strip_hallucinations(t)   # 辨識結果裡「優優獨播劇場」之類的幻覺句子
            if c != t:
                P.atomic_write_text(d / n, c)
                t = c
        if P.is_japanese(t):   # 1.5 以前把日文歌詞轉成了台灣繁體（来→來）：改回日文字形並存回去
            j = P.tw_to_ja(t)
            if j != t:
                P.atomic_write_text(d / n, j)
                t = j
        return t
    meta = P.read_meta(d)
    return jsonify(lrc=rd("lyrics.lrc"), auto=rd("lyrics_auto.lrc"), online=rd("lyrics_online.lrc"),
                   source=meta.get("lyrics_source"), online_info=meta.get("lyrics_online"),
                   unreliable=not P.recognition_reliable(meta),
                   recognized=meta.get("lyrics_recognized") is not False or (d / "lyrics_auto.lrc").exists(),
                   reasons=(meta.get("lyrics_quality") or {}).get("reasons", []),
                   language=meta.get("language"), song_language=meta.get("lyrics_language"),
                   kana_fix=meta.get("kana_fix") or {})


@bp.post("/api/furigana")
def furigana():
    """日文歌詞的假名標音。字典還沒裝好時先在背景安裝，回傳安裝狀態。"""
    lines = [str(x)[:300] for x in ((request.get_json(force=True) or {}).get("lines") or [])][:500]
    if not P.kana_ready():
        P.start_kana_install()
        return jsonify(state=P.kana_status())
    out = {}
    for t in dict.fromkeys(lines):
        try:
            out[t] = P.furigana(t)
        except Exception as e:
            P.log_error("furigana", e)
            out[t] = None
    return jsonify(state={"state": "ready"}, ruby=out)


@bp.post("/api/songs/<sid>/kana")
def set_kana(sid):
    """修改這首歌某個漢字詞的讀音（每首歌各自記住）；讀音空白＝恢復自動判斷。"""
    b = request.get_json(force=True) or {}
    base, reading = str(b.get("base") or "").strip()[:40], str(b.get("reading") or "").strip()[:80]
    if not base:
        raise P.UserError("缺少要修改的字")
    d = P.song_dir(sid)
    with P._META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
        meta = P.read_meta(d)
        fix = meta.get("kana_fix") or {}
        if reading:
            fix[base] = reading
        else:
            fix.pop(base, None)
        meta["kana_fix"] = fix
        P.write_meta(d, meta)
    return jsonify(kana_fix=fix)


@bp.post("/api/songs/<sid>/lyrics/use")
def use_lyrics(sid):
    """切換目前使用的歌詞：辨識結果或線上歌詞（兩份各自保存，切換不會互相覆蓋）。"""
    d = P.song_dir(sid)
    which = (request.get_json(force=True) or {}).get("which")
    src = {"auto": "lyrics_auto.lrc", "online": "lyrics_online.lrc"}.get(which)
    if not src or not (d / src).exists():
        raise P.UserError("沒有這份歌詞")
    lrc = (d / src).read_text("utf-8")
    P.atomic_write_text(d / "lyrics.lrc", lrc)
    with P._META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
        meta = P.read_meta(d)
        meta["lyrics_source"] = "online" if which == "online" else "whisper"
        meta["lyrics_pending"] = False
        meta["steps"]["lyrics"] = True
        P.write_meta(d, meta)
    return jsonify(lrc=lrc, source=meta["lyrics_source"])


@bp.put("/api/songs/<sid>/lyrics")
def put_lyrics(sid):
    d = P.song_dir(sid)
    lrc = (request.get_json(force=True) or {}).get("lrc", "")
    P.atomic_write_text(d / "lyrics.lrc", lrc)
    with P._META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
        meta = P.read_meta(d)
        meta["steps"]["lyrics"] = bool(lrc.strip())
        if meta.get("lyrics_source") == "online" and lrc.strip():
            P.atomic_write_text(d / "lyrics_online.lrc", lrc)   # 調整線上歌詞的時間：存回線上歌詞那一份
        else:
            meta["lyrics_source"] = "manual"
        if lrc.strip():
            meta["notes"] = [x for x in meta.get("notes", []) if "歌詞辨識沒有完成" not in x]
        P.write_meta(d, meta)
    return jsonify(ok=True)


# ---------------------------------------------------------------- Claude（需要連線）

ALIGN_TOOL = {
    "name": "set_line_times",
    "description": "回傳每一行歌詞的開始秒數，順序和行數都必須與使用者提供的歌詞相同。",
    "input_schema": {
        "type": "object",
        "properties": {
            "times": {"type": "array", "items": {"type": "number"},
                      "description": "每行歌詞的開始秒數（遞增，保留兩位小數）"},
        },
        "required": ["times"],
    },
}


@bp.post("/api/songs/<sid>/claude/align")
def claude_align(sid):
    """把使用者貼的正確歌詞，對到辨識出的時間與旋律段落上。"""
    d = P.song_dir(sid)
    ref = ((request.get_json(force=True) or {}).get("text") or "").strip()
    ref_lines = [l.strip() for l in ref.splitlines() if l.strip()]
    if not ref_lines:
        raise P.UserError("先貼上正確的歌詞")
    meta = P.read_meta(d)
    auto = d / "lyrics_auto.lrc"
    heard = P.parse_lrc(auto.read_text("utf-8")) if auto.exists() else []
    phrases = []
    if (d / "pitch.json").exists():
        _, notes = P.load_pitch(d)
        for s, e, m in notes:
            if phrases and s - phrases[-1][1] < 0.35:
                phrases[-1][1] = e
                phrases[-1][2] += 1
            else:
                phrases.append([s, e, 1])
    n = len(ref_lines)
    tool = json.loads(json.dumps(ALIGN_TOOL))
    tool["input_schema"]["properties"]["times"]["minItems"] = n
    tool["input_schema"]["properties"]["times"]["maxItems"] = n
    prompt = (
        "你在幫歌曲練唱工具對齊歌詞時間。\n"
        f"歌曲長度 {meta.get('duration') or 0:.1f} 秒。\n"
        "資料一：語音辨識從人聲音軌聽到的句子與開始秒數（可能有錯字、斷句不準、漏句）。\n"
        "資料二：從人聲旋律偵測到的樂句（開始秒、結束秒、音符數），一個音符大約對應一個中文字。\n"
        "資料三：使用者提供的正確歌詞，每行一句，順序就是演唱順序。\n\n"
        "請為資料三的每一行決定開始秒數，依據是內容與辨識結果的相似度，以及字數與樂句音符數的對應。"
        "秒數必須遞增。重複的段落（副歌）要對到各自出現的位置。"
        f"用 set_line_times 工具回傳，times 必須剛好 {n} 個數字。\n\n"
        "資料一：\n" + "\n".join(f"{l['t']:.2f}\t{l['text']}" for l in heard if l["t"] is not None)
        + "\n\n資料二：\n" + "\n".join(f"{s:.2f}\t{e:.2f}\t{k}" for s, e, k in phrases)
        + f"\n\n資料三（共 {n} 行）：\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(ref_lines))
    )
    # 回傳的只是每行一個數字（約 6 token），上限依行數給，夠用又不會在出錯時白白燒掉很多 token
    data, usage = C.ask_tool(prompt, tool, min(16000, 2000 + 25 * n), "align", meta.get("title", ""))
    times = data.get("times") or []
    if len(times) != n:
        raise P.UserError(f"Claude 回傳了 {len(times)} 個時間，但歌詞有 {n} 行。再試一次。")
    dur = float(meta.get("duration") or 1e9)
    clean, prev = [], -1.0
    for t in times:   # 確保遞增、不超出歌曲長度
        t = min(max(float(t), prev + 0.3, 0.0), dur)
        clean.append(round(t, 2))
        prev = t
    lrc = P.to_lrc([{"t": t, "text": txt} for t, txt in zip(clean, ref_lines)])
    P.atomic_write_text(d / "lyrics.lrc", lrc)
    steps = dict(P.read_meta(d).get("steps") or {}, lyrics=True)
    P.update_meta(d, steps=steps, lyrics_source="claude")   # Claude 回應要等一陣子，不能整段鎖住
    return jsonify(lrc=lrc, usage=usage)


@bp.get("/api/songs/<sid>/lyrics/search")
def lyrics_search(sid):
    """手動搜尋：LRCLIB＋網易雲＋酷狗（原文與簡體各搜一次），依和辨識結果的相似度排序。"""
    d = P.song_dir(sid)
    q = (request.args.get("q") or "").strip()
    if not q and not P.lyric_queries(P.read_meta(d)):
        raise P.UserError("請輸入歌名或歌手再搜尋")
    tried, results = P.find_candidates(d, P.load_config(), q)
    return jsonify(query=q or (tried[0] if tried else ""), queries=tried, results=results)


@bp.get("/api/songs/<sid>/lyrics/candidates")
def lyrics_candidates(sid):
    d = P.song_dir(sid)
    p = d / "lyrics_candidates.json"
    meta = P.read_meta(d)
    if not p.exists() and meta.get("lyrics_source") in (None, "whisper") and (d / "lyrics_auto.lrc").exists():
        try:   # 1.1 之前匯入的歌：第一次開啟時補做線上歌詞比對
            P.find_and_store_candidates(d, P.load_config())
            meta = P.read_meta(d)
        except Exception:
            pass
    cands = json.loads(p.read_text("utf-8")) if p.exists() else []
    return jsonify(pending=bool(meta.get("lyrics_pending")), results=cands,
                   recognized=P.recognized_text(d)[:300])


@bp.post("/api/songs/<sid>/lyrics/autoshift")
def lyrics_autoshift(sid):
    """1.6.8：依主唱開口位置，算出目前歌詞整體要移多少秒（由畫面套用）。"""
    times = (request.get_json(force=True) or {}).get("times") or []
    sc, off = P.lyrics_shift(P.song_dir(sid), times[:2000])
    return jsonify(scale=sc, offset=off)


@bp.post("/api/songs/<sid>/lyrics/apply")
def lyrics_apply(sid):
    d = P.song_dir(sid)
    cand = (request.get_json(force=True) or {}).get("candidate") or {}
    if not (cand.get("synced") or cand.get("plain")):
        raise P.UserError("這筆歌詞沒有內容")
    lrc, how = P.apply_candidate(d, cand)
    return jsonify(lrc=lrc, how=how)


@bp.post("/api/songs/<sid>/lyrics/import")
def lyrics_import(sid):
    d = P.song_dir(sid)
    body = request.get_json(force=True) or {}
    lrc, how = P.import_lyrics(d, body.get("text", ""), P.load_config(), body.get("name", ""))
    return jsonify(lrc=lrc, how=how)


@bp.post("/api/songs/<sid>/lyrics/keep")
def lyrics_keep(sid):
    d = P.song_dir(sid)
    P.update_meta(d, lyrics_pending=False)
    return jsonify(ok=True)


# ---------------------------------------------------------------- 匯出歌詞

def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


@bp.post("/api/songs/<sid>/lyrics/export")
def export_lyrics(sid):
    d = P.song_dir(sid)
    body = request.get_json(force=True) or {}
    fmt = (body.get("fmt") or "lrc").lower()
    if fmt not in ("lrc", "txt", "srt"):
        raise P.UserError("格式只支援 LRC、TXT、SRT")
    text = body.get("lrc")
    if text is None:
        p = d / "lyrics.lrc"
        text = p.read_text("utf-8") if p.exists() else ""
    lines = P.parse_lrc(text)
    if not any(l["text"] for l in lines):
        raise P.UserError("這首歌還沒有歌詞可以匯出")
    meta = P.read_meta(d)
    if fmt == "lrc":
        head = f"[ti:{meta.get('title', '')}]\n[re:{P.APP_NAME} {VERSION}]\n"
        body = "\n".join(l for l in text.splitlines() if not __import__("re").match(r"^\[(ti|ar|al|re|by|offset|length):", l))
        out = head + body.strip() + "\n"   # 保留逐字時間標記
    elif fmt == "txt":
        out = "\n".join(l["text"] for l in lines) + "\n"
    else:
        timed = [l for l in lines if l["t"] is not None]
        if not timed:
            raise P.UserError("SRT 需要有時間的歌詞")
        dur = float(meta.get("duration") or 0)
        rows = []
        for i, l in enumerate(timed):
            end = timed[i + 1]["t"] if i + 1 < len(timed) else (min(l["t"] + 5, dur) if dur else l["t"] + 5)
            end = max(end - 0.05, l["t"] + 0.5)
            rows.append(f"{i + 1}\n{_srt_time(l['t'])} --> {_srt_time(end)}\n{l['text']}\n")
        out = "\n".join(rows)
    folder = P.downloads_dir() / P.APP_NAME
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_safe_name(meta.get('title', '歌詞'))}.{fmt}"
    path.write_text(out, encoding="utf-8-sig" if fmt in ("txt", "srt") else "utf-8")
    return jsonify(path=str(path))
