# 從 pipeline.py 搬出（tests/tools/split_moves.py，docs/pipeline-split-plan.md 方式一）。
# pipeline._load_part 把這個檔案載入 pipeline 的命名空間，等於這段程式寫在 pipeline.py 裡；不能單獨 import。


def search_lrclib(query: str) -> list[dict]:
    import urllib.parse
    data = _http_json("https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": query}), timeout=12)
    out = []
    for it in data if isinstance(data, list) else []:
        if it.get("instrumental") or not (it.get("plainLyrics") or it.get("syncedLyrics")):
            continue
        out.append({"source": "LRCLIB", "id": f"lrclib:{it.get('id')}",
                    "track": it.get("trackName") or it.get("name") or "", "artist": it.get("artistName") or "",
                    "album": it.get("albumName") or "", "duration": it.get("duration"),
                    "synced": clean_lrc(it.get("syncedLyrics") or ""), "plain": it.get("plainLyrics") or ""})
    return out


def search_netease(query: str, limit: int = 6) -> list[dict]:
    """網易雲音樂（中國最大的歌詞來源之一）：先搜尋歌曲，再取每首的同步歌詞。"""
    import urllib.parse
    h = {"Referer": "https://music.163.com/"}
    data = _http_json("https://music.163.com/api/search/get/web?" +
                      urllib.parse.urlencode({"s": query, "type": 1, "offset": 0, "limit": limit}), h)
    out = []
    for song in ((data or {}).get("result") or {}).get("songs") or []:
        try:
            lyr = _http_json(f"https://music.163.com/api/song/lyric?id={song['id']}&lv=1&kv=1&tv=-1", h)
        except Exception:
            continue
        synced = clean_lrc(((lyr or {}).get("lrc") or {}).get("lyric") or "")
        if not synced or "纯音乐" in synced:
            continue
        has_time = bool(re.search(r"\[\d+:\d+", synced))
        plain = "\n".join(re.sub(r"\[[^\]]*\]", "", l).strip() for l in synced.splitlines()).strip()
        out.append({"source": "網易雲音樂", "id": f"netease:{song['id']}", "track": song.get("name") or "",
                    "artist": " / ".join(a.get("name", "") for a in song.get("artists") or []),
                    "album": (song.get("album") or {}).get("name") or "",
                    "duration": round((song.get("duration") or 0) / 1000, 1) or None,
                    "synced": synced if has_time else "", "plain": plain})
    return out


def search_kugou(query: str, limit: int = 5) -> list[dict]:
    """酷狗音樂：先搜尋歌曲，再用歌曲 hash 找歌詞並下載 LRC。"""
    import base64
    import urllib.parse
    data = _http_json("http://mobilecdn.kugou.com/api/v3/search/song?" +
                      urllib.parse.urlencode({"format": "json", "keyword": query, "page": 1, "pagesize": limit}))
    out = []
    for song in (((data or {}).get("data") or {}).get("info") or [])[:limit]:
        try:
            dur = int(song.get("duration") or 0)
            cands = _http_json("http://krcs.kugou.com/search?" + urllib.parse.urlencode(
                {"ver": 1, "man": "yes", "client": "mobi", "keyword": "", "duration": dur * 1000,
                 "hash": song.get("hash", "")})).get("candidates") or []
            if not cands:
                continue
            c = cands[0]
            lyr = _http_json("http://lyrics.kugou.com/download?" + urllib.parse.urlencode(
                {"ver": 1, "client": "pc", "id": c["id"], "accesskey": c["accesskey"], "fmt": "lrc", "charset": "utf8"}))
            raw = base64.b64decode(lyr.get("content") or "").decode("utf-8", "replace")
        except Exception as e:
            log_error("kugou lyric", e)
            continue
        track, artist = song.get("songname") or "", song.get("singername") or ""
        synced = clean_lrc(raw, track, artist)
        if not synced:
            continue
        plain = "\n".join(re.sub(r"\[[^\]]*\]", "", l).strip() for l in synced.splitlines()).strip()
        out.append({"source": "酷狗音樂", "id": f"kugou:{song.get('hash')}", "track": track, "artist": artist,
                    "album": song.get("album_name") or "", "duration": dur or None,
                    "synced": synced if re.search(r"\[\d+:\d+", synced) else "", "plain": plain})
    return out


def recognized_text(d: Path) -> str:
    p = d / "lyrics_auto.lrc"
    return "\n".join(l["text"] for l in parse_lrc(p.read_text("utf-8"))) if p.exists() else ""


def find_candidates(d: Path, cfg: dict, query: str = "", check=None,
                    budget: float = 45, pre: tuple | None = None) -> tuple[list[str], list[dict]]:
    """用歌名（原文與簡體）到 LRCLIB、網易雲和酷狗搜尋，與辨識結果比對後排序。
    pre：先前已經抓好的（搜尋字串, 原始結果）——2.3.2 匯入時在分離的同時先上網抓，分離完再排序、套用。"""
    tried, found = pre if pre else fetch_candidates(read_meta(d), query, check, budget)
    return rank_candidates(d, cfg, tried, found)


def fetch_candidates(meta: dict, query: str = "", check=None, budget: float = 45) -> tuple[list[str], dict]:
    """只做上網搜尋（不需要分離結果）。"""
    queries = [query] if query else lyric_queries(meta)
    from concurrent.futures import ThreadPoolExecutor, wait as fwait
    tried, found, errors = [], {}, []
    sites = [search_lrclib, search_netease, search_kugou]
    dead = set()                      # 連不上的網站，後面的搜尋就跳過，不要每次都等逾時
    deadline = time.time() + budget   # 整體時間上限：網站很慢時也不會一直卡在這一步
    pool = ThreadPoolExecutor(max_workers=3)
    try:
        for q in queries:
            for qq in dict.fromkeys([q, to_simplified(q)]):
                if time.time() >= deadline:
                    break
                tried.append(qq)
                futs = {pool.submit(fn, qq): fn for fn in sites if fn not in dead}
                pending = set(futs)
                while pending and time.time() < deadline:
                    if check:
                        check()       # 使用者按「停止」時在這裡停下
                    _, pending = fwait(pending, timeout=0.5)
                for f, fn in futs.items():
                    if f in pending:
                        errors.append(f"{fn.__name__}: 逾時")
                        dead.add(fn)
                        continue
                    try:
                        for c in f.result():
                            found.setdefault(c["id"], c)
                    except Exception as e:
                        errors.append(f"{fn.__name__}: {type(e).__name__}")
                        log_error(f"{fn.__name__}({qq})", e)
                        dead.add(fn)
            if len(found) >= 8 or time.time() >= deadline or len(dead) == len(sites):
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if not found and errors and len(dead) == len(sites):
        raise UserError("無法連到歌詞網站，請確認網路後再試。")
    return tried, found


def rank_candidates(d: Path, cfg: dict, tried: list[str], found: dict) -> tuple[list[str], list[dict]]:
    meta = read_meta(d)
    found = {k: dict(v) for k, v in found.items()}   # 不改到呼叫的人手上的原始結果
    reliable = recognition_reliable(meta)
    rec = recognized_text(d) if reliable else ""
    dur = meta.get("duration")
    to_tw = bool(cfg.get("convert_to_taiwan", True))
    out = []
    for c in found.values():
        c["similarity"] = similarity(rec, c["plain"] or c["synced"])
        c["diff"] = round(abs(c["duration"] - dur), 1) if (c.get("duration") and dur) else None
        if to_tw and re.search(r"[\u4e00-\u9fff]", c["plain"] + c["synced"] + c["track"] + c["artist"]) \
                and not is_japanese(c["plain"] + c["synced"]):
            for k in ("plain", "synced", "track", "artist", "album"):
                c[k] = to_taiwan(c[k])
        lines = [l for l in c["plain"].splitlines() if l.strip()]
        c["preview"] = "\n".join(lines[:3])
        c["lines"] = len(lines)
        out.append(c)

    title = _norm(" ".join(filter(None, [meta.get("title"), meta.get("yt_track"), meta.get("yt_artist")])))
    # 2.1.0：有時間軸的候選，拿它的時間軸和這首歌主唱開口的位置比對，算出「吻合度」0～1。
    # 歌名和長度都一樣、但其實是翻唱版、不同剪輯版的，吻合度會明顯比較低，自動套用時就不會挑到
    phrases = voice_phrases(d)
    for c in out:
        c["voice_fit"] = timeline_fit(c, phrases, dur) if (c["synced"] and phrases) else None

    def score(c):
        # 歌名吻合度：候選的歌名（與歌手）有多少字出現在這首歌的標題裡；和辨識結果無關
        tr = _norm(c.get("track") or "")
        c["title_match"] = round(sum(1 for ch in tr if ch in title) / len(tr), 2) if tr and title else 0.0
        pen = min((c["diff"] or 0) / 30, 0.3) if c["diff"] is not None else 0.1
        base = 0.5 * c["title_match"] - pen + (0.08 if c["synced"] else 0)
        if reliable and c["similarity"] is not None:   # 辨識可靠時，相似度才拿來參考
            base += c["similarity"]
        if c.get("voice_fit") is not None:
            base += 0.6 * (c["voice_fit"] - 0.5)   # 吻合的加分、不吻合的扣分（±0.3）
        return -base
    out.sort(key=score)
    return list(dict.fromkeys(tried)), out[:12]


def timeline_fit(c: dict, phrases: list[tuple[float, float]], dur: float | None) -> float | None:
    """候選歌詞的時間軸和這首歌主唱開口位置的吻合度（0～1）。
    依網站的時間軸（長度不同時先依比例與位移對齊，和套用時的做法相同）算每一句開頭離最近一次開口多遠：
    0.3 秒內算吻合，愈遠分數愈低。句子太少時不算。"""
    times = [l["t"] for l in parse_lrc(c["synced"]) if l["t"] is not None and l["text"].strip()]
    if len(times) < 4 or len(phrases) < 2:
        return None
    ts = np.array(times, dtype=float)
    r = None
    try:
        r = voice_fit(times, _PhraseStub(phrases), c.get("duration"), dur)   # 需要整體對齊時先對齊
    except Exception:
        r = None
    if r:
        ts = ts * r[0] + r[1]
    onsets = np.array([a for a, _ in phrases])
    k = np.clip(np.searchsorted(onsets, ts), 1, len(onsets) - 1)
    dist = np.minimum(np.abs(ts - onsets[k - 1]), np.abs(ts - onsets[k]))
    return round(float(np.mean(np.exp(-(dist ** 2) / (2 * 0.35 ** 2)))), 2)


def lyrics_timing_fit(d: Path) -> float | None:
    """目前歌詞的時間軸（不做整體對齊，就是現在顯示的位置）和主唱開口位置的吻合度（0～1）。"""
    try:
        times = [l["t"] for l in lyric_lines(d) if l["text"].strip()]
    except Exception:
        return None
    phrases = voice_phrases(d)
    if len(times) < 4 or len(phrases) < 2:
        return None
    ts = np.array(times, dtype=float)
    onsets = np.array([a for a, _ in phrases])
    k = np.clip(np.searchsorted(onsets, ts), 1, len(onsets) - 1)
    dist = np.minimum(np.abs(ts - onsets[k - 1]), np.abs(ts - onsets[k]))
    return round(float(np.mean(np.exp(-(dist ** 2) / (2 * 0.35 ** 2)))), 2)


def recheck_bad(after: float | None, before: float | None) -> bool:
    """2.1.1：只看「重新分離前後」差多少。歌詞句子常從一段歌聲中間開始，吻合度本來就不高，
    單看分數會把大部分歌都標起來；分離前後明顯變差（掉 0.15 以上）才代表歌聲位置真的變了。"""
    return after is not None and before is not None and before - after >= 0.15


def clear_false_recheck() -> int:
    """2.1.0 的判斷太嚴，把很多歌詞沒問題的歌也標成「建議重新匯入歌詞」：依新的標準重新判斷一次。"""
    n = 0
    for d in LIB.iterdir() if LIB.exists() else []:
        with library_writer():   # fork（69）：搬移中等搬完，再用新的位置
            d = LIB / d.name
            mp = d / "meta.json"
            if not mp.exists():
                continue
            try:
                m = read_meta(d)
            except Exception:
                continue
            rc = m.get("lyrics_recheck")
            if isinstance(rc, dict) and not recheck_bad(rc.get("fit"), rc.get("before")):
                update_meta(d, lyrics_recheck=None)
                n += 1
    return n


def check_lyrics_after_reseparate(d: Path, before: float | None) -> None:
    """2.1.0：重新分離／分析旋律後，目前的歌詞和新的歌聲位置對不太上 → 標「建議重新匯入歌詞」。
    標記記下當時歌詞檔的時間，之後歌詞有改過（重新匯入、套用線上歌詞、手動調整）就自動消失。"""
    after = lyrics_timing_fit(d)
    bad = recheck_bad(after, before)
    rec = None
    if bad:
        try:
            mt = (d / "lyrics.lrc").stat().st_mtime_ns
        except OSError:
            mt = 0
        rec = {"fit": after, "before": before, "mtime": mt}
    update_meta(d, lyrics_recheck=rec)


class _PhraseStub:
    """讓 voice_fit 直接用算好的開口段落（不用每個候選都重讀 pitch.json）。"""
    def __init__(self, phrases):
        self.phrases = phrases


def recognition_reliable(meta: dict) -> bool:
    """自動辨識的歌詞可不可靠。1.4.7 之前辨識的歌沒有這項資料，視為可靠（維持原本的行為）。"""
    if meta.get("lyrics_recognized") is False and not meta.get("lyrics_quality"):
        return False   # 匯入時沒有自動辨識
    q = meta.get("lyrics_quality")
    return bool(q.get("ok", True)) if isinstance(q, dict) else not meta.get("lyrics_unreliable")


def guess_language(text: str) -> str:
    """從歌詞文字猜語言（沒有自動辨識時用）。"""
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    return "en" if re.search(r"[A-Za-z]", text) else ""


def _lyrics_note(d: Path, note: str | None) -> None:
    with _META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
        meta = read_meta(d)
        meta["notes"] = [x for x in meta.get("notes", []) if "線上歌詞" not in x] + ([note] if note else [])
        write_meta(d, meta)


def find_and_store_candidates(d: Path, cfg: dict, check=None, apply_first: bool = False, pre=None) -> None:
    _, cands = find_candidates(d, cfg, check=check, pre=pre)
    meta = read_meta(d)
    # 候選一律保留給使用者看，不再用辨識結果過濾。是否主動提示「有線上歌詞可選」：
    # 辨識可靠時看和辨識結果像不像；辨識不可靠（或沒有辨識）時看歌名和長度。
    if recognition_reliable(meta) and (d / "lyrics_auto.lrc").exists():
        likely = [c for c in cands if c["similarity"] is None or c["similarity"] >= 0.25]
    else:
        likely = [c for c in cands if c.get("title_match", 0) >= 0.5 or (c["diff"] is not None and c["diff"] <= 5)]
    # 和歌聲對不上的（吻合度低於 0.5：一半以上的句子離開口超過 0.5 秒）不自動套用，留給使用者自己挑
    likely = [c for c in likely if c.get("voice_fit") is None or c["voice_fit"] >= 0.5]
    atomic_write_text(d / "lyrics_candidates.json", json.dumps(cands, ensure_ascii=False))
    if meta.get("lyrics_source") in (None, "whisper"):
        update_meta(d, lyrics_pending=bool(likely))
    if not apply_first:
        return
    lrc = d / "lyrics.lrc"
    if meta.get("lyrics_source") or (lrc.exists() and lrc.read_text("utf-8").strip()):
        _lyrics_note(d, None)   # 等搜尋的時候使用者已經自己選好歌詞，不覆蓋
        return
    if not likely:
        _lyrics_note(d, "沒有找到相符的線上歌詞。" + ("可以在練唱畫面的「搜尋線上歌詞」看看其他候選、" if cands else "可以在練唱畫面")
                     + "貼上歌詞，或用「重新處理 → 只重新辨識歌詞」自動辨識。")
        return
    c = likely[0]
    _, how = apply_candidate(d, c)
    lang = guess_language((c.get("plain") or "") + (c.get("synced") or ""))
    if lang and not read_meta(d).get("language"):
        update_meta(d, language=lang)
    _lyrics_note(d, f"已自動套用線上歌詞（{c.get('track') or ''}／{c.get('artist') or ''}，{how}）。"
                    "不對的話可以在練唱畫面的「搜尋線上歌詞」換其他版本。")
