"""歌詞文字處理：LRC／SRT 轉換、清理、搜尋關鍵字、繁簡轉換、相似度、對到辨識結果（從 pipeline.py 原封搬出；pipeline 轉出同樣的名稱）。"""
from __future__ import annotations

import re
from pathlib import Path


def fmt_lrc_time(t: float) -> str:
    t = max(0.0, t)
    m = int(t // 60)
    return f"{m:02d}:{t - m * 60:05.2f}"


def parse_lrc(text: str) -> list[dict]:
    lines = []
    for raw in text.splitlines():
        tags = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", raw)
        body = re.sub(r"\[[^\]]*\]", "", raw)
        body = re.sub(r"<\d+:\d+(?:\.\d+)?>", "", body).strip()   # 逐字時間標記（Enhanced LRC）
        if not tags:
            if body:
                lines.append({"t": None, "text": body})
            continue
        for mm, ss in tags:
            lines.append({"t": int(mm) * 60 + float(ss), "text": body})
    timed = sorted([l for l in lines if l["t"] is not None], key=lambda l: l["t"])
    return timed + [l for l in lines if l["t"] is None]


def to_lrc(lines: list[dict]) -> str:
    out = []
    for l in lines:
        out.append((f"[{fmt_lrc_time(l['t'])}]" if l.get("t") is not None else "") + l["text"])
    return "\n".join(out) + "\n"


_NOISE = re.compile(r"(official|music\s*video|lyric[s]?|audio|mv|m/v|4k|hd|hq|live|cover|翻唱|清唱|純人聲|人聲|"
                    r"a\s*cappella|acapella|vocal[s]?\s*only|伴奏|動態歌詞|歌詞版?|完整版|高音質|官方|字幕)", re.I)


def lyric_queries(meta: dict) -> list[str]:
    """從歌名與 YouTube 資訊猜幾個搜尋字串。"""
    out = []
    track, artist = meta.get("yt_track") or "", meta.get("yt_artist") or ""
    if track:
        out.append(f"{track} {artist}".strip())
    title = meta.get("title") or ""
    inner = re.findall(r"[【「《『\[]([^】」》』\]]+)[】」》』\]]", title)
    for x in inner:
        if not _NOISE.search(x):
            out.append(x.strip())
    t = re.sub(r"[【「《『\[(（][^】」》』\])）]*[】」》』\])）]", " ", title)
    t = _NOISE.sub(" ", t)
    t = re.sub(r"[|｜/\\_~\-–—:：#]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    good_inner = [x.strip() for x in inner if not _NOISE.search(x)]
    if good_inner and t:
        out.insert(len(out) - len(good_inner), f"{good_inner[0]} {t}")   # 歌名＋歌手 優先
    if t:
        out.append(t)
    seen, uniq = set(), []
    for q in out:
        k = q.lower()
        if q and k not in seen:
            seen.add(k)
            uniq.append(q)
    return uniq[:3]


def to_simplified(text: str) -> str:
    try:
        from opencc import OpenCC
    except ImportError:
        return text
    for conf in ("t2s", "t2s.json"):
        try:
            return OpenCC(conf).convert(text)
        except Exception:
            continue
    return text


_CREDIT = re.compile(r"^\s*(作词|作曲|编曲|作詞|編曲|制作人|製作人|制作|製作|监制|監製|演唱|原唱|词|詞|曲|混音|录音|錄音|和声|和聲|"
                     r"吉他|贝斯|貝斯|鼓|弦乐|弦樂|出品|发行|發行|OP|SP|Lyrics|Composer|Arranger|Producer)\s*[:：]", re.I)


def clean_lrc(text: str, track: str = "", artist: str = "") -> str:
    """去掉作詞作曲等資訊行、「歌手 - 歌名」標題行與空白行。"""
    out = []
    title_lines = {f"{artist} - {track}", f"{track} - {artist}"} if track and artist else set()

    def n(s: str) -> str:
        s = re.sub(r"\(.*?\)|（.*?）", "", s or "")
        return "".join(ch for ch in s.lower() if ch.isalnum())
    T, A = n(track), n(artist)

    def title_line(body: str) -> bool:
        # 2.3.5：「巴黎草莓 - 許茹芸 (Valen Hsu)」這種標題行，歌手名後面多了英文名也要認得
        sides = re.split(r"\s+[-–—]\s+", body)
        if len(sides) != 2:
            return False
        return any(q and ((T and (q in T or T in q)) or (A and (q in A or A in q))) for q in map(n, sides))
    for line in (text or "").splitlines():
        body = re.sub(r"\[[^\]]*\]", "", line).strip()
        if not body or _CREDIT.match(body) or body in title_lines or title_line(body) or \
                re.match(r"^\[(ti|ar|al|by|offset|length|re|id|hash|sign|qq|total):", line, re.I):
            continue
        out.append(line.strip())
    return "\n".join(out)


def _norm(text: str) -> str:
    """比對用：轉簡體、只留文字。"""
    text = re.sub(r"\[[^\]]*\]|<[^>]*>", "", text or "")
    return "".join(ch for ch in to_simplified(text).lower() if ch.isalnum())


def similarity(recognized: str, candidate: str) -> float | None:
    """辨識結果中，有多少比例的字依序出現在候選歌詞裡（0–1）。"""
    import difflib
    a, b = _norm(recognized), _norm(candidate)
    if len(a) < 6 or not b:
        return None
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return round(sum(x.size for x in m.get_matching_blocks()) / len(a), 3)


def srt_to_lrc(text: str) -> str:
    """SRT 字幕 → LRC（每段字幕取開始時間，多行合成一句）。"""
    out = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->", block)
        if not m:
            continue
        t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)[:3].ljust(3, "0")) / 1000
        body = " ".join(l.strip() for l in block.splitlines()[block.splitlines().index(
            next(l for l in block.splitlines() if "-->" in l)) + 1:] if l.strip())
        body = re.sub(r"<[^>]+>|\{[^}]*\}", "", body).strip()
        if body:
            out.append({"t": t, "text": body})
    return to_lrc(out)


def align_to_recognized(lines: list[str], d: Path) -> list[dict]:
    """把只有文字的歌詞，依辨識結果的時間對上：逐字比對後，每句取第一個對到的字的時間。"""
    import difflib
    auto = d / "lyrics_auto.lrc"
    rec = [l for l in parse_lrc(auto.read_text("utf-8")) if l["t"] is not None] if auto.exists() else []
    stream, times = [], []
    for i, l in enumerate(rec):
        end = rec[i + 1]["t"] if i + 1 < len(rec) else l["t"] + 4
        chars = _norm(l["text"])
        for k, ch in enumerate(chars):
            stream.append(ch)
            times.append(l["t"] + (end - l["t"]) * 0.85 * k / max(1, len(chars)))
    cand, owner = [], []
    for i, l in enumerate(lines):
        for ch in _norm(l):
            cand.append(ch)
            owner.append(i)
    starts = [None] * len(lines)
    if stream and cand:
        m = difflib.SequenceMatcher(None, "".join(cand), "".join(stream), autojunk=False)
        for blk in m.get_matching_blocks():
            for k in range(blk.size):
                li = owner[blk.a + k]
                if starts[li] is None:
                    starts[li] = times[blk.b + k]   # 該句第一個對到的字
    # 沒對到的句子：前後內插
    known = [(i, t) for i, t in enumerate(starts) if t is not None]
    for i in range(len(lines)):
        if starts[i] is not None:
            continue
        prev = max((k for k in known if k[0] < i), default=None, key=lambda k: k[0])
        nxt = min((k for k in known if k[0] > i), default=None, key=lambda k: k[0])
        if prev and nxt:
            starts[i] = prev[1] + (nxt[1] - prev[1]) * (i - prev[0]) / (nxt[0] - prev[0])
        elif prev:
            starts[i] = prev[1] + 3.5 * (i - prev[0])
        elif nxt:
            starts[i] = max(0.0, nxt[1] - 3.5 * (nxt[0] - i))
        else:
            starts[i] = 3.5 * i
    out, last = [], -1.0
    for t, text in zip(starts, lines):   # 保持遞增
        t = max(t, last + 0.3)
        out.append({"t": round(t, 2), "text": text})
        last = t
    return out
