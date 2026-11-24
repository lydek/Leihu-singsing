# 從 pipeline.py 搬出（tests/tools/split_moves.py，docs/pipeline-split-plan.md 方式一）。
# pipeline._load_part 把這個檔案載入 pipeline 的命名空間，等於這段程式寫在 pipeline.py 裡；不能單獨 import。


DL_SUFFIX = {"queued": "（排隊下載）", "failed": "（下載失敗）", "cancelled": "（已取消下載）", "paused": "（暫停下載）"}
_DL_RE = re.compile("（(?:排隊下載|下載失敗|已取消下載|暫停下載)）$")

# YouTube 暫時限制這個 IP（429、要求登入確認不是機器人）時，暫停後面所有的下載，不再繼續送請求；
# 使用者確認後按「繼續下載」才會接著下載。只記在記憶體：程式重開時暫停的歌會留著「重新下載」按鈕。
YT_PAUSE: dict = {"on": False, "at": 0.0, "reason": ""}
_RATE_LIMIT_RE = re.compile(r"HTTP Error 429|Too Many Requests|Sign in to confirm you(?:'|’)re not a bot|"
                            r"confirm you(?:'|’)re not a bot|rate[- ]limit|try again later", re.I)


class YouTubeRateLimited(UserError):
    pass


def set_dl_state(d: Path, state: str | None) -> None:
    with _META_LOCK:
        meta = read_meta(d)
        base = _DL_RE.sub("", meta.get("title") or "").strip()
        if base.startswith("YouTube 下載"):
            base = ""
        if state == "cancelled":
            meta["dl_cancelled"] = True   # 預先下載剛好在進行中時，下載完也要維持「已取消」
        elif state == "queued":
            meta.pop("dl_cancelled", None)
        if state:
            meta["dl_state"] = state
            meta["title"] = (base or "YouTube 影片") + DL_SUFFIX[state]
        else:
            meta.pop("dl_state", None)
            meta["title"] = base or "YouTube 歌曲"
        write_meta(d, meta)


def pending_downloads() -> list[dict]:
    """還沒下載完成的 YouTube 歌曲（程式關掉時還在排隊的、失敗的、取消的）。
    舊版沒有記錄狀態：歌名結尾是（排隊下載）的就當作排隊中，有錯誤訊息的當作失敗。"""
    out = []
    for m in list_songs():
        if not m.get("source_url") or (m.get("steps") or {}).get("import"):
            continue
        st = m.get("dl_state")
        if not st:
            title = m.get("title") or ""
            if not (title.endswith("（排隊下載）") or title.startswith("YouTube 下載")):
                continue
            st = "failed" if m.get("notes") else "queued"
            set_dl_state(song_dir(m["id"]), st)
        out.append({**m, "dl_state": st})
    return out


def new_youtube_song(url: str, title: str = "") -> str:
    return _blank_song((title or "YouTube 影片") + DL_SUFFIX["queued"], source_url=url, dl_state="queued")


def _upgrade_ytdlp() -> None:
    if NO_BACKGROUND:
        return
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp", "--disable-pip-version-check"],
                   capture_output=True, timeout=300)


YT_TIMEOUT = 1800   # fork（S8）：yt-dlp 整體逾時（秒），網路卡住時不會一直占著下載佇列


def _kill_tree(p) -> None:
    """fork（S8）：結束子程序和它開的子程序（yt-dlp 轉檔時會再開 ffmpeg），並等它結束。"""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=0x08000000)
    else:
        p.kill()
    try:
        p.wait(10)
    except Exception:
        p.kill()


def step_youtube(d: Path, url: str, report) -> None:
    import importlib.util
    import subprocess
    if importlib.util.find_spec("yt_dlp") is None:
        raise UserError("沒有安裝 yt-dlp，無法從 YouTube 下載")
    ff = ffmpeg_path()
    if not ff:
        raise UserError("找不到 ffmpeg，無法轉換 YouTube 音訊")

    def attempt() -> tuple[int, str, str]:
        args = [sys.executable, "-m", "yt_dlp", url, "--no-playlist", "-f", "bestaudio/best",
                "-x", "--audio-format", "wav", "--ffmpeg-location", str(Path(ff).parent),
                "-o", str(d / "original.%(ext)s"), "--force-overwrites", "--no-simulate",
                "--newline", "--progress", "--progress-template", "download:PROG %(progress._percent_str)s",
                "--print", "before_dl:TITLE %(title)s",
                "--print", "before_dl:META %(track)s\t%(artist)s\t%(uploader)s", "--no-warnings"]
        flags = 0x08000000 if sys.platform == "win32" else 0   # 不要跳出黑色視窗
        # fork（S8）：stderr 併進 stdout。分開讀時 stderr 寫滿管線緩衝區，會和讀 stdout 的迴圈互相卡住
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", creationflags=flags,
                             env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        timed_out = []
        timer = threading.Timer(YT_TIMEOUT, lambda: (timed_out.append(True), _kill_tree(p)))
        timer.daemon = True
        timer.start()
        title, other = "", []
        # fork（Codex r5-6）：讀輸出放在另一個執行緒，這裡每秒檢查一次停止／暫停（report 會丟例外）。
        # 原本只在收到 PROG 時呼叫 report：yt-dlp 卡在取得資料、或 ffmpeg 轉檔沒有輸出時按停止沒反應，要等逾時
        lines: queue.Queue = queue.Queue()

        def pump():
            for ln in p.stdout:
                lines.put(ln)
            lines.put(None)
        threading.Thread(target=pump, daemon=True, name="yt-dlp-out").start()
        last = ("取得影片資訊", 0.01)   # 和開始前的進度訊息相同，沒有輸出時照樣顯示這句
        try:
            while True:
                try:
                    line = lines.get(timeout=1.0)
                except queue.Empty:
                    report(*last)
                    continue
                if line is None:
                    break
                line = line.strip()
                if line.startswith("TITLE "):
                    title = line[6:]
                    if not read_meta(d).get("title_locked"):
                        update_meta(d, title=title[:120])
                elif line.startswith("META "):
                    track, artist, uploader = (line[5:].split("\t") + ["", "", ""])[:3]
                    clean = lambda v: "" if v in ("NA", "None") else v.strip()
                    update_meta(d, yt_track=clean(track), yt_artist=clean(artist), yt_uploader=clean(uploader))
                elif line.startswith("PROG "):
                    m = re.search(r"([\d.]+)%", line)
                    if m:
                        frac = float(m.group(1)) / 100
                        last = (f"從 YouTube 下載音訊 {frac * 100:.0f}%", 0.01 + 0.05 * frac)
                        report(*last)
                elif line:
                    other.append(line)   # 錯誤訊息（原本的 stderr）和其他輸出，判斷失敗原因用
            p.wait()
        finally:
            timer.cancel()
            if p.poll() is None:   # 按了停止（report 丟例外）或出錯：yt-dlp 不能留在背景繼續下載
                _kill_tree(p)
        if timed_out:
            raise UserError(f"YouTube 下載超過 {YT_TIMEOUT // 60} 分鐘沒有完成，已停止。可以稍後再試")
        return p.returncode, title, "\n".join(other[-50:])

    if YT_PAUSE["on"]:
        raise YouTubeRateLimited("YouTube 下載暫停中")
    report("取得影片資訊", 0.01)
    code, title, err = attempt()
    if code and _RATE_LIMIT_RE.search(err):   # 被限制時不要再更新 yt-dlp 重試，避免送更多請求
        raise YouTubeRateLimited(err.strip()[-200:])
    if code:
        if re.search(r"Private video|Video unavailable|This video is unavailable|members-only|"
                     r"confirm your age|age-restricted|age restricted", err):
            raise UserError(f"這部影片無法下載（可能是私人、會員限定或需要登入）：{err.strip()[-200:]}")
        report("下載失敗，更新 yt-dlp 後重試", 0.02)
        _upgrade_ytdlp()
        code, title, err = attempt()
        if code and _RATE_LIMIT_RE.search(err):
            raise YouTubeRateLimited(err.strip()[-200:])
        if code:
            raise UserError(f"YouTube 下載失敗：{err.strip()[-240:]}")
    for f in d.glob("original.*"):
        if f.suffix.lower() != ".wav":
            f.unlink(missing_ok=True)
    if not (d / "original.wav").exists():
        raise UserError("YouTube 下載完成，但找不到轉換後的音訊檔")
    meta = read_meta(d)
    yt = (title or "YouTube 歌曲")[:120]
    if meta.get("title_locked"):   # 從表單匯入：曲名、歌手用表單的寫法
        update_meta(d, yt_title=yt, source_url=url)
    else:
        artist, song = split_artist_title(yt, meta.get("yt_track", ""), meta.get("yt_artist", ""))
        update_meta(d, title=(song or yt)[:120], artist=(meta.get("artist") or artist)[:80], yt_title=yt,
                    source_url=url)
    set_dl_state(d, None)


# 預先下載：顯示卡在處理這首歌的時候，背景先把佇列裡接下來的 YouTube 歌下載好（網路和顯示卡同時工作）。
# 每首歌一把鎖，同一首不會同時被預先下載和正式處理。
_DL_LOCKS: dict = {}
_DL_LOCKS_GUARD = threading.Lock()


def _dl_lock(song_id: str) -> threading.Lock:
    with _DL_LOCKS_GUARD:
        return _DL_LOCKS.setdefault(song_id, threading.Lock())


def prefetch_youtube(song_id: str) -> bool:
    """先下載一首還在排隊的 YouTube 歌（只下載，不處理）。回傳有沒有真的下載。"""
    lock = _dl_lock(song_id)
    if not lock.acquire(blocking=False):
        return False
    try:
        d = song_dir(song_id)
        meta = read_meta(d)
        if YT_PAUSE["on"] or meta.get("steps", {}).get("import") or meta.get("prefetched") \
                or meta.get("prefetch_failed") or meta.get("dl_state") != "queued" or not meta.get("source_url"):
            return False
        try:
            step_youtube(d, meta["source_url"], lambda *a: None)
        except YouTubeRateLimited as e:
            if not YT_PAUSE["on"]:
                YT_PAUSE.update(on=True, at=time.time(), reason=str(e))
                log_error("youtube rate limited (prefetch)", e)
            set_dl_state(d, "queued")
            return False
        except Exception as e:   # 預先下載失敗沒關係，輪到這首時會再正式下載一次（失敗才標示）
            log_error(f"prefetch {song_id}", e)
            update_meta(d, prefetch_failed=True)
            set_dl_state(d, "queued")
            return False
        update_meta(d, prefetched=True)
        # 還沒處理，維持「排隊」；程式重開也會接著處理。下載途中被取消的，維持「已取消」
        set_dl_state(d, "cancelled" if read_meta(d).get("dl_cancelled") else "queued")
        return True
    finally:
        lock.release()


def _cancel_requested() -> bool:
    """fork：這個工作的使用者按了停止（佇列放在 CURRENT 的檢查函式會丟 JobCancelled；只是暫停的話丟 JobDeferred）。"""
    import gpu_worker as G
    check = getattr(G.CURRENT, "check", None)
    try:
        if check:
            check()
    except JobDeferred:   # 子類別要先接：全部暫停不是停止
        return False
    except JobCancelled:
        return True
    return False


def youtube_job(song_id: str, url: str, after=None):
    """after(song_id)：整首處理完後要做的事（例如先把預設 KEY 的移調版本做好）。"""
    process = process_song(song_id, ["import", "separate", "pitch", "lyrics"])

    def run(report):
        d = song_dir(song_id)
        update_meta(d, notes=[])
        try:
            with _dl_lock(song_id):   # 正在預先下載這首時，等它下載完
                meta = read_meta(d)
                if meta.get("prefetched") and any(d.glob("original.*")):
                    report("已預先下載完成", 0.06)
                    update_meta(d, prefetched=False)
                    set_dl_state(d, None)
                else:
                    step_youtube(d, url, report)
        except JobDeferred:
            if not _cancel_requested():   # fork：讓位途中按了停止，停止的路由已經標成已取消，不能再改回排隊
                set_dl_state(d, "queued")   # 暫停：之後會重新下載，不算取消
            raise
        except JobCancelled:
            set_dl_state(d, "cancelled")
            raise
        except YouTubeRateLimited as e:
            if not YT_PAUSE["on"]:
                YT_PAUSE.update(on=True, at=time.time(), reason=str(e))
                log_error("youtube rate limited", e)
            set_dl_state(d, "paused")
            update_meta(d, notes=["YouTube 暫時限制下載，已暫停。過幾小時後按「繼續下載」或「重新下載」。"])
            return   # 不算錯誤：後面排隊的歌也會照樣停下來，不會再連到 YouTube
        except Exception as e:
            set_dl_state(d, "failed")
            update_meta(d, notes=[f"{e}　可以按「重新下載」再試一次，或刪除這一項。"])
            raise
        process(report)
        if after:
            try:
                after(song_id)
            except Exception as e:
                log_error("youtube_job after", e)
    return run
