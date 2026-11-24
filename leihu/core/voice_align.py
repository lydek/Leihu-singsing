# 從 pipeline.py 搬出（tests/tools/split_moves.py，docs/pipeline-split-plan.md 方式一）。
# pipeline._load_part 把這個檔案載入 pipeline 的命名空間，等於這段程式寫在 pipeline.py 裡；不能單獨 import。


def vocal_energy_phrases(d: Path, gap: float = 0.35, min_len: float = 0.3) -> list[tuple[float, float]]:
    """2.3.2：還沒分析旋律時，用「主唱＋合音」的音量找有在唱的段落。
    主唱被分到合音的副歌也算得到，所以拿來對歌詞時間不會因為主唱分錯而對歪。"""
    try:
        y = read_audio(stem_path(d, "lead"), sr=PITCH_SR, mono=True, fallback=False)
        b = stem_path(d, "back")
        if b.exists():
            yb = read_audio(b, sr=PITCH_SR, mono=True, fallback=False)
            n = min(len(y), len(yb))
            y = y[:n] + yb[:n]
    except Exception:
        return []
    H = int(PITCH_SR * 0.02)
    n = len(y) // H * H
    if n < H * 50:
        return []
    r = np.sqrt(np.mean(y[:n].reshape(-1, H) ** 2, axis=1))
    r = np.convolve(r, np.ones(3) / 3, "same")
    ref = float(np.percentile(r, 95))
    if ref <= 1e-5:
        return []
    on = r > 0.3 * ref   # 實測 6 首：和旋律分析結果對歌詞時間的準度相當
    out, i = [], 0
    while i < len(on):
        if not on[i]:
            i += 1
            continue
        j = i
        while j < len(on) and on[j]:
            j += 1
        a, e = i * 0.02, j * 0.02
        if out and a - out[-1][1] < gap:
            out[-1] = (out[-1][0], e)
        else:
            out.append((a, e))
        i = j
    return [(a, e) for a, e in out if e - a >= min_len]


def voice_phrases(d, gap: float = 0.35, min_len: float = 0.3) -> list[tuple[float, float]]:
    """從旋律分析結果找出「有在唱」的段落 (開始, 結束)。還沒分析旋律時改用主唱＋合音的音量。"""
    if isinstance(d, _PhraseStub):
        return d.phrases
    try:
        pj = json.loads((d / "pitch.json").read_text("utf-8"))
    except Exception:
        return vocal_energy_phrases(d, gap, min_len) if isinstance(d, Path) else []
    hop = pj.get("hop", HOP_S)
    on = np.array([v is not None for v in pj["midi"]])
    out, i, n = [], 0, len(on)
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        while j < n and on[j]:
            j += 1
        a, b = i * hop, j * hop
        if out and a - out[-1][1] < gap:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
        i = j
    return [(a, b) for a, b in out if b - a >= min_len]


def align_synced_to_voice(lines: list[dict], d: Path, cand_dur: float | None, dur: float | None) -> tuple[list[dict], str]:
    """網站的時間軸和這首歌對不上（版本長度不同、前奏不同）時，找最能對上主唱開口位置的整體位移（與速度比例）。"""
    r = voice_fit([l["t"] for l in lines], d, cand_dur, dur) if lines else None
    if not r:
        return lines, ""
    sc, off = r
    out = [{**l, "t": round(max(0.0, l["t"] * sc + off), 2)} for l in lines]
    return out, f"依主唱的聲音位置調整了網站的時間軸（{off:+.1f} 秒{'、速度比例 %.3f' % sc if sc != 1.0 else ''}）"


def voice_fit(times: list[float], d: Path, cand_dur: float | None = None, dur: float | None = None):
    """回傳 (速度比例, 位移秒數)；和原本的時間差不多就回傳 None。"""
    ph = voice_phrases(d)
    if not ph or not times:
        return None
    onsets = np.array([a for a, _ in ph])
    ts = np.array(times, dtype=float)
    scales = [1.0]
    if cand_dur and dur and 0.8 < dur / cand_dur < 1.25:
        scales.append(dur / cand_dur)

    if len(onsets) < 2:
        return None

    def fit(t):
        k = np.clip(np.searchsorted(onsets, t), 1, len(onsets) - 1)
        dist = np.minimum(np.abs(t - onsets[k - 1]), np.abs(t - onsets[k]))
        return float(np.exp(-(dist ** 2) / (2 * 0.3 ** 2)).sum())
    base = fit(ts)
    best = (base, 1.0, 0.0)
    t0 = float(ts.min())
    # 1.6.8：MV 版前奏常常長很多，範圍放寬（2.3.5：往後最多 120 秒）；先每 0.25 秒粗找，再在最佳位置附近每 0.02 秒細找
    # 2.3.5：不能把任何一句移到 0 秒以前——以前會被擠在 0 秒、好幾句黏在一起（〈オーケストラ〉前 6 句）
    for sc in scales:
        lo = max(-60.0, -t0 * sc - 0.3)
        hi = 120.0   # THE FIRST TAKE、現場版開頭常有一分鐘以上的講話（〈オーケストラ〉約 66 秒）
        coarse = max(((fit(ts * sc + off), float(off)) for off in np.arange(lo, hi + 0.01, 0.25)))
        for off in np.arange(max(lo, coarse[1] - 0.5), coarse[1] + 0.501, 0.02):
            v = fit(ts * sc + off)
            if v > best[0] + 1e-9:
                best = (v, sc, round(float(off), 2))
    v, sc, off = best
    # 明顯比原本的時間軸更貼合主唱才調整，避免把本來就對的時間軸亂移
    if v < base + max(1.0, 0.08 * len(ts)) or (abs(off) < 0.15 and sc == 1.0):
        return None
    return sc, off


def lyrics_shift(d: Path, times: list[float]) -> tuple[float, float]:
    """目前的歌詞整體和歌聲差了好幾秒時，依主唱開口位置找出整體位移與速度比例（畫面套用，逐字時間一起移動）。
    不需要調整時回傳 (1, 0)。"""
    ts = [float(t) for t in times if t is not None]
    if len(ts) < 3:
        raise UserError("含時間的歌詞太少，沒辦法自動校正")
    if not (d / "pitch.json").exists():
        raise UserError("這首歌還沒分析旋律，沒辦法依歌聲校正")
    meta = read_meta(d)
    return voice_fit(ts, d, None, meta.get("duration")) or (1.0, 0.0)


def align_by_voice(lines: list[str], d: Path, bonus: float = 0.6) -> list[dict]:
    """只有文字的歌詞：依每句的字數，找出最符合「有唱歌的時間長度」的分句位置（動態規劃）。

    候選的句子開頭是主唱開口的位置（段落開頭、音符開頭）；每句預期唱的時間和字數成正比。
    和語言無關，但只是估計，通常還需要用「對準目前位置」和逐句調整微調。"""
    try:
        pj = json.loads((d / "pitch.json").read_text("utf-8"))
    except Exception:
        pj = None
    if not pj or not lines:
        return [{"t": round(3.5 * i, 2), "text": t} for i, t in enumerate(lines)]
    hop = pj.get("hop", HOP_S)
    on = np.array([v is not None for v in pj["midi"]], dtype=float)
    cumv = np.concatenate([[0.0], np.cumsum(on)]) * hop          # 累積「有唱歌」的秒數
    strong = {round(a, 2) for a, _ in voice_phrases(d)}
    cands = sorted(strong | {round(n[0], 2) for n in pj.get("notes", [])})
    if len(cands) < len(lines):
        return [{"t": round(3.5 * i, 2), "text": t} for i, t in enumerate(lines)]
    C = np.array(cands)
    is_strong = np.array([c in strong for c in cands])
    V = np.array([cumv[min(len(cumv) - 1, int(t / hop))] for t in C])
    end_v = cumv[-1]
    w = np.array([max(1, len(_norm(t))) for t in lines], float)
    e = w * (end_v / w.sum())                                     # 每句預期的有聲秒數
    N, M = len(lines), len(C)
    cost = np.full((N, M), 1e18)
    back = np.zeros((N, M), int)
    cost[0] = 0.02 * V - bonus * is_strong
    for i in range(1, N):
        prev = cost[i - 1]
        for k in range(i, M):
            js = np.arange(max(i - 1, k - 80), k)
            c = prev[js] + (V[k] - V[js] - e[i - 1]) ** 2 / (e[i - 1] + 0.5)
            j = int(np.argmin(c))
            cost[i, k] = c[j] - bonus * is_strong[k]
            back[i, k] = js[j]
    last = cost[N - 1] + 0.3 * (end_v - V - e[N - 1]).clip(min=0) ** 2 / (e[N - 1] + 0.5)
    k = int(np.argmin(last))
    idx = [k]
    for i in range(N - 1, 0, -1):
        k = back[i, k]
        idx.append(k)
    idx.reverse()
    return [{"t": round(max(0.0, float(C[k]) - 0.1), 2), "text": t} for k, t in zip(idx, lines)]


def import_lyrics(d: Path, text: str, cfg: dict, filename: str = "") -> tuple[str, str]:
    """匯入歌詞檔（LRC、SRT 或純文字）。純文字依辨識結果的時間自動對上每一句。回傳 (lrc, 說明)。"""
    text = (text or "").replace("\ufeff", "").strip()
    if not text:
        raise UserError("歌詞檔是空的")
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "srt" or re.search(r"\d+:\d+:\d+[,.]\d+\s*-->", text):
        lrc, how = srt_to_lrc(text), "已從 SRT 字幕轉成 LRC，使用字幕的時間"
    elif re.search(r"\[\d+:\d+(?:\.\d+)?\]", text):
        lrc, how = clean_lrc(text) + "\n", "使用歌詞檔裡的時間"
    else:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        auto = d / "lyrics_auto.lrc"
        if auto.exists() and recognition_reliable(read_meta(d)):
            lrc = to_lrc(align_to_recognized(lines, d))
            how = "歌詞檔沒有時間，已依辨識結果自動對上每一句"
        elif (d / "pitch.json").exists():
            lrc = to_lrc(align_by_voice(lines, d))
            how = "歌詞檔沒有時間，已依主唱有聲音的段落分配每一句，可以用「對準目前位置」和逐句調整微調"
        else:
            lrc = to_lrc(align_to_recognized(lines, d))
            how = "歌詞檔沒有時間，暫時每句間隔 3.5 秒，可以用「跟拍打點」或 Claude 對齊調整"
    if not lrc.strip():
        raise UserError("讀不到歌詞內容，請確認檔案格式（LRC、SRT 或 TXT）")
    if cfg.get("convert_to_taiwan", True) and re.search(r"[\u4e00-\u9fff]", lrc):
        lrc = to_taiwan(lrc)
    atomic_write_text(d / "lyrics.lrc", lrc)
    with _META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
        meta = read_meta(d)
        meta["steps"]["lyrics"] = True
        meta["lyrics_source"] = "import"
        meta["lyrics_pending"] = False
        meta["notes"] = [x for x in meta.get("notes", []) if "歌詞辨識沒有完成" not in x]
        write_meta(d, meta)
    return lrc, how


def apply_candidate(d: Path, cand: dict) -> tuple[str, str]:
    """套用一筆線上歌詞。長度相符的同步歌詞直接用；否則用辨識結果的時間對齊。回傳 (lrc, 說明)。"""
    meta = read_meta(d)
    dur = meta.get("duration")
    synced = cand.get("synced") or ""
    plain_lines = [l.strip() for l in (cand.get("plain") or "").splitlines() if l.strip()]
    diff = abs(cand["duration"] - dur) if (cand.get("duration") and dur) else None
    reliable = recognition_reliable(meta) and (d / "lyrics_auto.lrc").exists()
    if synced and (diff is None or diff <= 3):
        lines = [l for l in parse_lrc(synced) if l["t"] is not None and l["text"]]
        # 1.6.8：長度相同，前奏卻可能不同（MV 版、剪輯版），整體差好幾秒；依主唱開口位置檢查並校正
        lines, how = align_synced_to_voice(lines, d, cand.get("duration"), dur)
        how = how or "使用網站提供的時間軸"
    elif synced:
        # 網站有時間軸、但版本長度不同：不靠辨識結果，改依主唱的聲音位置整體平移
        lines = [l for l in parse_lrc(synced) if l["t"] is not None and l["text"]]
        lines, how = align_synced_to_voice(lines, d, cand.get("duration"), dur)
        how = how or "使用網站提供的時間軸（版本長度不同，可能需要用「對準目前位置」微調）"
    else:
        if reliable:
            lines = align_to_recognized(plain_lines, d)
            how = "依辨識結果的時間自動對齊"
        else:
            lines = align_by_voice(plain_lines, d)
            how = "依主唱有聲音的段落分配時間（建議用「對準目前位置」和逐句調整微調）"
    lrc = to_lrc(lines)
    atomic_write_text(d / "lyrics.lrc", lrc)
    atomic_write_text(d / "lyrics_online.lrc", lrc)   # 線上歌詞另外保存，和辨識結果互不影響
    with _META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
        meta = read_meta(d)
        meta["steps"]["lyrics"] = True
        meta["lyrics_source"] = "online"
        meta["lyrics_pending"] = False
        meta["lyrics_online"] = {"source": cand.get("source"), "track": cand.get("track"), "artist": cand.get("artist")}
        meta["notes"] = [x for x in meta.get("notes", []) if "歌詞辨識沒有完成" not in x]
        write_meta(d, meta)
    return lrc, how
