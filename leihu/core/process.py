# 從 pipeline.py 搬出（tests/tools/split_moves.py，docs/pipeline-split-plan.md 方式一）。
# pipeline._load_part 把這個檔案載入 pipeline 的命名空間，等於這段程式寫在 pipeline.py 裡；不能單獨 import。


def process_song(song_id: str, steps: list[str], cfg_override: dict | None = None):
    def run(report):
        d = song_dir(song_id)
        cfg = dict(load_config(), **(cfg_override or {}))
        meta = read_meta(d)
        recheck = "import" not in steps and ("separate" in steps or "pitch" in steps) and (d / "lyrics.lrc").exists()
        fit_before = lyrics_timing_fit(d) if recheck else None   # 重新分離前，歌詞和舊的歌聲位置吻合度
        if "import" in steps or not meta["steps"].get("import"):
            step_import(d, report)
        # 2.3.2：匯入時（不自動辨識）先上網抓歌詞，和分離同時進行；分離完先套用歌詞，
        # 再用歌詞逐句檢查主唱有沒有被分到合音、搬回來，最後才分析旋律——一次做好，不用事後再補一輪。
        early = None
        if ("lyrics" in steps and "import" in steps and "separate" in steps
                and not cfg.get("auto_recognize_lyrics", False)
                and not (d / "lyrics.lrc").exists() and not meta.get("lyrics_source")):
            early = _early_lyrics(read_meta(d))
        if "separate" in steps:
            # 2.3.2：直播中不管是匯入、優先處理或重新處理，都改用 CPU＋標準模型（不碰顯示卡），下播後自動補做
            light = live_light_cfg(cfg)
            if light:   # 直播中匯入：先用輕量模型，直播結束後自動補做高品質
                cfg.update(light)
                update_meta(d, live_light=True)
                report("直播中：改用輕量模型分離（下播後自動補做高品質）", 0.1)
            used_light = step_separate(d, cfg, report)
            # fork（69）：依實際的分離方式。補做時顯示卡又卡住、中途改用 CPU＋標準模型，還要再補做高品質
            if not light and not used_light and meta.get("live_light"):
                update_meta(d, live_light=False)   # 高品質補做完成
            recognize = "lyrics" in steps and not ("import" in steps and not cfg.get("auto_recognize_lyrics", False))
            if not recognize:   # 接下來只剩分析旋律、存檔：下一首可以先開始分離
                getattr(report, "gpu_done", lambda: None)()
            measure_stem_loudness(d)   # 各音軌響度，播放時依開著的音軌調整音量
        if early is not None:
            early = _apply_early_lyrics(d, cfg, early, report)
        if "pitch" in steps:
            step_pitch(d, cfg, report)
        if recheck:
            try:
                check_lyrics_after_reseparate(d, fit_before)
            except Exception as e:
                log_error("check_lyrics_after_reseparate", e)
        if early:
            pass   # 歌詞已經在分離後套用（或確定找不到），不用再排背景搜尋
        elif "lyrics" in steps and "import" in steps and not cfg.get("auto_recognize_lyrics", False):
            # 1.6.5：匯入時預設不自動辨識（準度不穩、又佔顯示卡），直接套用線上搜尋到的第一個版本
            report("排入線上歌詞搜尋（背景進行）", 0.97)
            with _META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
                meta = read_meta(d)
                meta["steps"]["lyrics"] = True
                meta["lyrics_recognized"] = False
                meta["notes"] = [x for x in meta.get("notes", []) if "歌詞" not in x and "辨識" not in x] + [
                    "正在背景搜尋線上歌詞，找到後會自動套用。"]
                write_meta(d, meta)
            search_lyrics_later(d, cfg, apply_first=True)
        elif "lyrics" in steps:
            try:
                step_lyrics(d, cfg, report)   # 歌詞相關的提示由 step_lyrics 自己更新
            except JobDeferred:
                raise
            except JobCancelled:
                with _META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
                    meta = read_meta(d)
                    meta["notes"] = [x for x in meta.get("notes", []) if "歌詞" not in x] + [
                        "已停止辨識歌詞。可以在練唱畫面貼上或搜尋歌詞，或用「重新處理 → 只重新辨識歌詞」再試一次。"]
                    write_meta(d, meta)
                raise
            except Exception as e:
                if _overlap_oom(e):
                    raise   # fork（36）：重疊處理時記憶體不夠，交給佇列一次一首重做，不算辨識失敗
                traceback.print_exc()
                with _META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
                    meta = read_meta(d)
                    meta["notes"] = [x for x in meta.get("notes", []) if "歌詞" not in x] + [
                        f"歌詞辨識沒有完成：{e}。可以在練唱畫面貼上歌詞。"]
                    write_meta(d, meta)
            # 線上歌詞搜尋要等網站回應（網站忙的時候可能要幾十秒），改在背景做，不卡住下一首歌的處理
            report("排入線上歌詞搜尋（背景進行）", 0.97)
            search_lyrics_later(d, cfg)
    return run


_LYRICS_POOL = None
_EARLY_POOL = None


def _early_lyrics(meta: dict):
    """在背景先上網搜尋歌詞（只抓資料，不需要分離結果）。回傳 Future。"""
    global _EARLY_POOL
    from concurrent.futures import ThreadPoolExecutor
    if _EARLY_POOL is None:
        _EARLY_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lyrics-early")
    return _EARLY_POOL.submit(fetch_candidates, meta, "", None, 45)


def _apply_early_lyrics(d: Path, cfg: dict, fut, report) -> bool:
    """分離完：等先前的歌詞搜尋（最多再等 45 秒），套用最相符的版本，再逐句檢查主唱。
    回傳 True＝這一步處理完了（套用、或確定沒有相符的）；False＝搜尋失敗，交回原本的背景搜尋。"""
    report("套用線上歌詞、檢查主唱有沒有被分到合音", 0.62)
    import gpu_worker as G
    check = getattr(G.CURRENT, "check", None)
    end = time.time() + 45
    while not fut.done() and time.time() < end:
        if check:
            check()
        time.sleep(0.3)
    if not fut.done():
        return False
    try:
        pre = fut.result()
        find_and_store_candidates(d, cfg, apply_first=True, pre=pre)
    except Exception as e:
        log_error("early lyrics", e)
        return False
    with _META_LOCK:
        meta = read_meta(d)
        meta["steps"]["lyrics"] = True
        meta.setdefault("lyrics_recognized", False)
        write_meta(d, meta)
    if (d / "lyrics.lrc").exists():
        try:
            fix_lead_gaps(d)   # 有歌詞了：逐句檢查一次（分離時只看得到整段的）
        except Exception as e:
            log_error("fix_lead_gaps(early)", e)
    return True


def search_lyrics_later(d: Path, cfg: dict, apply_first: bool = False) -> None:
    """在背景依序搜尋線上歌詞（一次一首），結果出來後歌曲庫會標「有線上歌詞可選」。
    apply_first：沒有自動辨識的歌，直接套用最符合的第一個版本。"""
    global _LYRICS_POOL
    from concurrent.futures import ThreadPoolExecutor
    if _LYRICS_POOL is None:
        _LYRICS_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lyrics-search")

    def run():
        with library_writer():   # fork（69）：搬移中等搬完，再用新的位置
            dd = LIB / d.name
            try:
                if dd.exists():
                    find_and_store_candidates(dd, cfg, apply_first=apply_first)
            except Exception as e:
                traceback.print_exc()   # 沒有網路或網站不通時，維持辨識結果
                if apply_first and dd.exists():
                    _lyrics_note(dd, f"線上歌詞搜尋失敗（{e}）。可以在練唱畫面按「搜尋線上歌詞」再試一次，或用「重新處理 → 只重新辨識歌詞」自動辨識。")
    _LYRICS_POOL.submit(run)
