# 從 pipeline.py 搬出（tests/tools/split_moves.py，docs/pipeline-split-plan.md 方式一）。
# pipeline._load_part 把這個檔案載入 pipeline 的命名空間，等於這段程式寫在 pipeline.py 裡；不能單獨 import。


def _overlap_oom(e: Exception) -> bool:
    """fork（36）：和別首重疊處理時顯示卡記憶體不夠。不要自己退回標準模型或聲像分離，交給佇列排回最前面、
    一次一首重做（Jobs._work 的記憶體不夠重試）；原本兩首都變成標準品質，或一首變成聲像分離。"""
    if getattr(e, "overlap_oom", False):   # 下面那層已經判定過：往上丟的途中另一首剛好做完，也照同一個判定
        return True
    import gpu_worker as G
    overlapping = getattr(G.CURRENT, "overlapping", None)
    if "out of memory" in str(e).lower() and overlapping and overlapping():
        with contextlib.suppress(AttributeError):   # 少數內建例外不能加屬性：就只靠這一次的判斷
            e.overlap_oom = True
        return True
    return False


def _stalled(e: Exception) -> bool:
    """顯示卡處理程式沒有進度（gpu_worker.GpuStalled）：交給 step_separate 改用 CPU＋標準模型。"""
    return type(e).__name__ == "GpuStalled"


def _no_worker(e: Exception) -> bool:
    """fork（第 7 批）：處理程式開不起來（gpu_worker 標的 worker_unavailable）。不是模型或精度的問題：不重試、
    不退回標準模型或聲像分離（Agy 審查 1：原本會誤停用高品質模型、用聲像分離蓋掉分軌），這首直接失敗、顯示原因。"""
    return bool(getattr(e, "worker_unavailable", False))


class JobCancelled(Exception):
    """工作被新的要求取代（例如又換了 KEY），中途停止。"""


# ---------------------------------------------------------------- 直播中暫停背景處理（2.1.0）
# 分離、辨識會把顯示卡吃滿，直播畫面和 VTube Studio 的模型追蹤會掉格。
# 偵測到直播軟體在跑、或練唱台的直播模式開著時，背景的重工作（升級、細修、最高品質、壓縮）先停住；結束後自動繼續。
# 直播中自己匯入的歌照常處理，但改用輕量模型（標準分離），處理快、佔顯示卡時間短，
# 直播結束後再自動用高品質模型補做一次（live_light 標記）。KEY 轉換、錄音評分照常。
LIVE_HOLD = {"apps": [], "live_seen": 0.0, "override_until": 0.0, "checked": 0.0}
_live_lock = threading.Lock()


def _process_names() -> set[str] | None:
    """目前所有程式的執行檔名稱（小寫）。2.3.2：直接問 Windows（不到 0.1 秒），
    不再用 tasklist：電腦很忙時 tasklist 會超過 10 秒逾時，結果「沒偵測到直播」。"""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return None
        try:
            e = PE()
            e.dwSize = ctypes.sizeof(PE)
            out = set()
            ok = k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out.add(e.szExeFile.lower())
                ok = k32.Process32NextW(snap, ctypes.byref(e))
            return out
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return None


def _running_apps(names: list[str]) -> list[str] | None:
    """有在跑的直播軟體；查不到（不是「沒有」）時回傳 None，沿用上一次的結果。"""
    if sys.platform != "win32" or not names:
        return []
    procs = _process_names()
    how = "Windows API" if procs is not None else "tasklist"
    if _LIVE_LAST.get("how") != how:
        _LIVE_LAST["how"] = how
        print(f"[live] 直播軟體偵測方式：{how}", flush=True)
    if procs is None:
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                 timeout=20, creationflags=flags, encoding="utf-8", errors="ignore").stdout
        except Exception:
            return None
        low = out.lower()
        return [n for n in names if f'"{n.lower()}"' in low]
    return [n for n in names if n.lower() in procs]


# 2.3.2：顯示卡被其他程式（直播軟體、VTube Studio、遊戲、錄影）佔滿時，也不要再把重工作丟給顯示卡：
# 顯示卡記憶體不夠時 Windows 會把它搬到系統記憶體，分離慢幾十倍、整台電腦跟著卡。
GPU_LOAD = {"checked": 0.0, "free": None, "total": None, "util": None, "hot": 0, "stalled_until": 0.0,
            "calm": 0, "busy": "", "ok_at": 0.0, "err": ""}
GPU_MIN_FREE = 3.5e9    # 剩下的顯示卡記憶體少於這個就不用顯示卡分離
GPU_BUSY_UTIL = 85      # 其他程式已經把顯示卡用到這麼滿（連續兩次）也不用
GPU_CALM_SAMPLES = 3    # fork：判定忙碌後要連續這麼多次取樣（約 1 分鐘）都低於門檻才恢復，不會每 20 秒來回切換
GPU_STALE = 120         # fork：這麼久沒有成功取樣，就不再沿用舊的忙碌結果（不然 nvidia-smi 壞掉後會一直暫停）


def _gpu_min_free(total: float | None) -> float:
    """fork：剩下的顯示卡記憶體少於這個算被其他程式佔用。小於 5 GB 的卡光是桌面就剩不到 3.5 GB，
    改成其他程式用掉超過 1.5 GB 才算；6 GB 以上的卡和原本一樣是 3.5 GB。"""
    return min(GPU_MIN_FREE, total - 1.5e9) if total else GPU_MIN_FREE


def gpu_stalled_mark(seconds: float = 300) -> None:
    """顯示卡處理程式剛剛卡住：接下來一段時間先當作顯示卡很忙。"""
    GPU_LOAD["stalled_until"] = time.time() + seconds


def _sample_gpu_load() -> None:
    """用 nvidia-smi 看整張顯示卡（含其他程式）的用量。練唱台自己的顯示卡處理程式開著時不看（會算到自己）。"""
    try:
        import gpu_worker as G
        if G.POOL.count():
            GPU_LOAD["ok_at"] = time.time()   # fork：刻意不取樣，不算取樣失敗（上一次的結果照用）
            return
    except Exception:
        pass
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=8, creationflags=flags).stdout.strip().splitlines()
        used, total, util = (float(x) for x in out[0].split(","))
    except Exception as e:
        if not GPU_LOAD["err"]:   # fork：只在開始失敗時記一次（沒有 NVIDIA 顯示卡的電腦每 20 秒都會失敗）
            print(f"[live] 讀不到顯示卡用量（nvidia-smi：{type(e).__name__}: {e}），不依顯示卡用量暫停", flush=True)
        GPU_LOAD["err"] = f"{type(e).__name__}: {e}"
        return
    if GPU_LOAD["err"]:
        print("[live] 又讀得到顯示卡用量了", flush=True)
    free = (total - used) * 2**20
    GPU_LOAD.update(err="", ok_at=time.time(), free=free, total=total * 2**20, util=util,
                    hot=GPU_LOAD["hot"] + 1 if util >= GPU_BUSY_UTIL else 0,
                    calm=GPU_LOAD["calm"] + 1 if util < GPU_BUSY_UTIL and free >= _gpu_min_free(total * 2**20) else 0)


def gpu_busy_reason() -> str:
    now = time.time()
    if now < GPU_LOAD["stalled_until"]:
        return "顯示卡剛剛處理沒有進度（可能被其他程式佔滿）"
    if now - GPU_LOAD["checked"] > 20:
        GPU_LOAD["checked"] = now
        _sample_gpu_load()
    if GPU_LOAD["free"] is not None and now - GPU_LOAD["ok_at"] > GPU_STALE:
        # fork：取樣一直失敗，舊的數字不能再用：忘掉它們（log 已經記了讀不到的原因）
        GPU_LOAD.update(free=None, util=None, hot=0, calm=0, busy="")
    free = GPU_LOAD["free"]
    if free is not None and free < _gpu_min_free(GPU_LOAD["total"]):
        GPU_LOAD["busy"] = f"顯示卡記憶體只剩 {free / 1e9:.1f} GB（被其他程式佔用）"
    elif GPU_LOAD["hot"] >= 2:
        GPU_LOAD["busy"] = f"顯示卡已被其他程式用到 {GPU_LOAD['util']:.0f}%"
    elif GPU_LOAD["calm"] >= GPU_CALM_SAMPLES:
        GPU_LOAD["busy"] = ""
    return GPU_LOAD["busy"]


_LIVE_LAST = {"reason": None}


def _live_log(st: dict) -> dict:
    # fork（複審 P3-5、Codex r6-3）：每一種狀態都帶，偵測到直播軟體時少了這欄，畫面會當成恢復、之後重複提示
    # 沒有可用顯示卡的電腦本來就讀不到，不提示；不用 cuda_ok()：主程式裡會等顯示卡子程式探測完（/api/status 每秒經過這裡）
    if GPU_LOAD["err"] and (gpu_info(block=False) or {}).get("ok"):
        st["gpu_err"] = GPU_LOAD["err"]
    r = st.get("reason") or ""
    key = ("gpu" if st.get("gpu") else r)   # 顯示卡用量的數字每次都不一樣，只在狀態改變時記
    if key != _LIVE_LAST["reason"]:
        _LIVE_LAST["reason"] = key
        print(f"[live] {r}：新的分離、音高分析改用 CPU，背景工作暫停" if r else "[live] 沒有偵測到直播或顯示卡忙碌：恢復正常處理", flush=True)
    return st


def live_touch() -> None:
    """練唱台畫面在直播模式時每隔幾秒呼叫一次。"""
    LIVE_HOLD["live_seen"] = time.time()


def live_override(seconds: float = 3600) -> None:
    """使用者按「直播中也照常處理」：暫時不管偵測結果。"""
    LIVE_HOLD["override_until"] = time.time() + seconds


def live_status(cfg: dict | None = None) -> dict:
    """現在該不該暫停重工作，以及原因（給狀態列顯示）。每 15 秒才真的去查一次程式清單。
    2.3.2：顯示卡被其他程式佔滿時也算（不管有沒有開「直播中暫停背景處理」）。"""
    cfg = cfg or load_config()
    now = time.time()
    if now < LIVE_HOLD["override_until"]:
        return _live_log({"hold": False, "reason": "", "apps": [], "override": True})
    if cfg.get("pause_while_streaming", True):
        with _live_lock:
            if now - LIVE_HOLD["checked"] > 15:
                LIVE_HOLD["checked"] = now
                got = _running_apps(cfg.get("streaming_apps") or [])
                if got is not None:
                    LIVE_HOLD["apps"] = got
        apps = list(LIVE_HOLD["apps"])
        if apps:
            return _live_log({"hold": True, "reason": "偵測到 " + "、".join(a.rsplit(".", 1)[0] for a in apps), "apps": apps})
        if now - LIVE_HOLD["live_seen"] < 20:
            return _live_log({"hold": True, "reason": "直播模式開著", "apps": []})
    busy = gpu_busy_reason()
    if busy:
        return _live_log({"hold": True, "reason": busy, "apps": [], "gpu": True})
    return _live_log({"hold": False, "reason": "", "apps": []})


# 直播中要先停住的背景工作（使用者沒有在等結果的）
LIVE_BACKGROUND = {"upgrade", "fine", "compact", "max", "leadfix",
                   "unfinished"}   # fork（Codex r5-5）：2.3.5「重做全部歌曲」批次補做沒處理完的歌
OVERLAP_GRACE = 60.0   # fork（36）：別首做完後這麼久內記憶體不夠，仍當成重疊處理造成的（子程式清理模型、回報錯誤要時間）


class JobDeferred(JobCancelled):
    """背景工作做到一半開播：放回佇列，直播結束後從頭再做（不佔著佇列擋住直播中的匯入）。"""


def wants_hq(cfg: dict) -> bool:
    """這台電腦平常會不會用高品質（較慢、較吃顯示卡）的分離模型。"""
    return _uses_hq(cfg, "vocal_quality") or _uses_hq(cfg, "karaoke_quality")


def hq_models_ready(cfg: dict) -> bool:
    """平常要用的高品質模型都下載好了（直播後補做才有意義）。"""
    return ((not _uses_hq(cfg, "vocal_quality") or hqv_ready())
            and (not _uses_hq(cfg, "karaoke_quality") or hq_ready()))


LIGHT_CFG = {"vocal_quality": "standard", "karaoke_quality": "standard", "auto_recognize_lyrics": False, "_cpu": True}


def live_light_cfg(cfg: dict) -> dict | None:
    """直播中（或顯示卡被其他程式佔滿）：回傳要改用的輕量設定（CPU＋標準模型）；否則 None。"""
    if not live_status(cfg)["hold"]:
        return None
    return dict(LIGHT_CFG)


# 2.1.1：可以和下一首重疊處理的工作（分離完、還在分析旋律時，下一首就先開始分離）
PIPELINE_LABELS = {"all", "youtube", "upgrade", "fine", "max"}


PARALLEL_MIN_VRAM = 10e9   # 2.3.1：顯示卡記憶體 10 GB 以上才同時分離兩首


def parallel_ok() -> bool:
    """這張顯示卡適不適合兩首同時分離。8 GB 的顯示卡實測：兩首互搶運算，每首反而慢一倍以上，
    所以記憶體不到 10 GB 就不啟用（設定勾了也一樣）。還不知道顯示卡資訊時先當作不適合。"""
    info = gpu_info(block=False) or {}
    return bool(info.get("ok")) and (info.get("vram") or 0) >= PARALLEL_MIN_VRAM


def _cpu_fallback_note(song_id: str | None, why: str) -> None:
    """fork（第 7 批）：顯示卡處理程式開不起來、這首改用 CPU 處理時，歌曲的說明寫原因（同一種只留最新一則，
    和 2.3.5 的 _hq_fail_note 相同做法）。寫不了只記 log，不影響這首的結果。"""
    if not song_id:
        return
    try:
        d = song_dir(song_id)
        if not (d / "meta.json").exists():
            return
        msg = f"顯示卡處理程式無法啟動，這首改用 CPU 處理：{str(why)[:160]}"
        with _META_LOCK:
            meta = read_meta(d)
            meta["notes"] = [x for x in meta.get("notes", []) if not x.startswith("顯示卡處理程式無法啟動")] + [msg]
            write_meta(d, meta)
    except Exception as e:
        log_error("cpu fallback note", e)


class Jobs:
    """重工作的佇列。預設一次分離一首；分離完的那首在分析旋律、存檔時，下一首可以先開始分離（pipeline）。
    設定開啟「閒置時兩首同時分離」且沒在直播時，最多兩首同時分離。"""

    def __init__(self, live_safe: bool = False, pipeline: bool = False, wait_for=None):
        self.pending: list[tuple[str, object]] = []   # 排隊中的工作（可以調整順序）
        self.state: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.live_safe = live_safe   # 直播中要不要暫停（只有重工作的佇列開）
        self.pipeline = pipeline     # 可不可以重疊處理
        self.holding = ""            # 目前因為直播暫停中的原因（空字串＝沒有暫停）
        self.running: dict[str, str] = {}   # 執行中的工作 → "gpu"（分離中）／"post"（分析、存檔）
        self.oom_until = 0.0                # 顯示卡記憶體不夠過：這個時間之前只同時分離一首
        self.paused = False                 # 「全部暫停」：處理歌曲的工作都先停（錄音評分、匯出照常）
        # fork（D6）：wait_for(狀態) 為真的工作先跳過、做後面的。會拿另一條佇列的鎖：
        # 兩條佇列要同時拿鎖時，一律先拿有 wait_for 的這條，不會互等
        self.wait_for = wait_for
        self.frozen = False   # fork（69）：搬移歌曲庫中不開始新的工作（不寫進設定，和全部暫停不同）
        self._on_cancel: dict = {}   # fork（69）：排隊中被取消時要呼叫的收尾
        if live_safe:
            try:
                self.paused = bool(load_config().get("queue_paused"))
            except Exception:
                pass
        if not NO_JOBS:   # 測試時只排隊不執行：工作會連網、要模型，結果也每次不同
            threading.Thread(target=self._run, daemon=True).start()

    def _held(self, jid: str) -> bool:
        st = self.state.get(jid) or {}
        return self.live_safe and st.get("label") in LIVE_BACKGROUND

    def _pausable(self, st: dict) -> bool:
        return st.get("kind") == "process"

    def set_paused(self, on: bool) -> None:
        """全部暫停／繼續處理。暫停時處理中的歌在下一個進度點停下、讓出顯示卡，繼續後從頭重做這首。"""
        with self.cv:
            self.paused = on
            for j in self.running:
                if on and self._pausable(self.state.get(j) or {}):
                    self.state[j]["pause"] = True
                elif not on:   # fork：走到下一個檢查點前就按了繼續，不用讓位重做
                    (self.state.get(j) or {}).pop("pause", None)
            self.cv.notify_all()
        try:
            cfg = load_config()
            cfg["queue_paused"] = on
            save_config(cfg)
        except Exception as e:
            log_error("save queue_paused", e)

    def _slots(self, hold: bool) -> int:
        """同時可以分離幾首。"""
        if not self.pipeline or hold or time.time() < self.oom_until:
            return 1
        try:
            return 2 if load_config().get("parallel_separation") and parallel_ok() else 1
        except Exception:
            return 1

    def _can_start(self, st: dict, slots: int) -> bool:
        if not self.running:
            return True
        if st.get("oom_retry"):
            return False   # fork（Codex r2-2）：重疊處理記憶體不夠的重做要等別的工作（含上一首的旋律分析）都做完
        if not self.pipeline or st.get("kind") != "process" or st.get("label") not in PIPELINE_LABELS:
            return False   # 其他工作（錄音評分、匯出、壓縮）等前面的都做完才開始
        others = [self.state.get(j) or {} for j in self.running]
        if any(o.get("kind") != "process" or o.get("label") not in PIPELINE_LABELS for o in others):
            return False
        if st.get("song") and any(o.get("song") == st.get("song") for o in others):
            return False   # 同一首歌不同時處理
        gpu = sum(1 for v in self.running.values() if v == "gpu")
        return gpu < slots and len(self.running) < slots + 1

    def _next(self, hold: bool, slots: int):
        """挑下一個要做的工作。直播中跳過背景工作，先做使用者在等的（例如直播中匯入的歌）。
        回傳 (jid, fn, 只剩直播中暫停的工作)。"""
        if self.frozen:   # fork（69）：搬移歌曲庫中
            return None, None, False
        held = False   # fork（Codex r4-8）：真的有工作因為直播被跳過，才回報「只剩直播中暫停的工作」
        for n, (jid, fn) in enumerate(self.pending):
            st = self.state.get(jid) or {}
            if st.get("status") == "cancelled":
                self.pending.pop(n)
                return jid, None, False
            if hold and st.get("label") in LIVE_BACKGROUND:
                held = True
                continue
            if self.paused and self._pausable(st):
                continue
            if self.wait_for and self.wait_for(st):
                st["step"] = "等這首歌的分軌處理完"
                continue
            if not self._can_start(st, slots):
                return None, None, False   # 依順序等前面的，不插隊
            self.pending.pop(n)
            self._on_cancel.pop(jid, None)   # fork（69）：取出來就要開始了，之後的停止由工作自己收尾
            return jid, fn, False
        return None, None, held and not self.paused

    def submit(self, kind: str, song_id: str, fn, label: str = "", on_cancel=None) -> str:
        """on_cancel（fork）：還沒開始就被使用者停止時呼叫（例如搬移歌曲庫排隊時先暫停了佇列，要恢復）。"""
        jid = uuid.uuid4().hex[:10]
        with self.cv:
            if on_cancel:
                self._on_cancel[jid] = on_cancel
            old = time.time() - 3600   # 結束超過一小時的工作紀錄不用再留（大量匯入時會累積上千筆）
            queued = {j for j, _ in self.pending}   # fork（Codex r4-1）：還在佇列裡的不能清
            for k in [k for k, j in self.state.items() if j.get("finished", old + 1) < old and k not in queued]:
                del self.state[k]
            self.state[jid] = {"id": jid, "kind": kind, "song": song_id, "label": label,
                               "status": "queued", "step": "排隊中", "progress": 0.0,
                               "error": None, "created": time.time()}
            self.pending.append((jid, fn))
            self.cv.notify_all()
        return jid

    def move(self, jid: str, delta: int) -> bool:
        """調整排隊順序：delta = -1 往前、+1 往後（只在還沒開始的工作之間移動）。"""
        with self.lock:
            ids = [j for j, _ in self.pending if self.state[j]["status"] == "queued"]
            if jid not in ids:
                return False
            i = ids.index(jid)
            k = i + (1 if delta > 0 else -1)
            if not 0 <= k < len(ids):
                return False
            a = next(n for n, (j, _) in enumerate(self.pending) if j == jid)
            b = next(n for n, (j, _) in enumerate(self.pending) if j == ids[k])
            self.pending[a], self.pending[b] = self.pending[b], self.pending[a]
            return True

    def to_front(self, jid: str) -> bool:
        """移到佇列最前面（下一個開始處理）。"""
        with self.cv:
            for n, (j, fn) in enumerate(self.pending):
                if j == jid and self.state[j]["status"] == "queued":
                    self.pending.insert(0, self.pending.pop(n))
                    self.state[j]["priority"] = True
                    self.cv.notify_all()
                    return True
        return False

    def upcoming(self, label: str, n: int) -> list[str]:
        """排在最前面、還沒開始的 n 個工作的歌曲編號（只看指定種類）。"""
        with self.lock:
            out = []
            for j, _ in self.pending:
                st = self.state[j]
                if st["status"] == "queued" and st["label"] == label and not st.get("cancel"):
                    out.append(st["song"])
                    if len(out) >= n:
                        break
            return out

    def order(self) -> dict[str, int]:
        with self.lock:
            ids = [j for j, _ in self.pending if self.state[j]["status"] == "queued"]
        return {j: n for n, j in enumerate(ids)}

    def update(self, jid: str, **kw) -> None:
        with self.lock:
            j = self.state.get(jid)
            if j is None:
                return
            if kw.get("status") in ("done", "error", "cancelled"):
                kw.setdefault("finished", time.time())
            j.update(kw)

    def get(self, jid: str) -> dict | None:
        with self.lock:
            j = self.state.get(jid)
            return dict(j) if j else None

    def active(self) -> list[dict]:
        cutoff = time.time() - 600
        order = self.order()
        with self.lock:
            out = [dict(j, order=order.get(j["id"])) for j in self.state.values() if j["status"] != "cancelled"
                   and (j["status"] in ("queued", "running") or j.get("finished", j["created"]) > cutoff)]
        # 執行中的在前，排隊中的依目前順序
        return sorted(out, key=lambda j: (j["status"] != "running", j["order"] if j["order"] is not None else -1,
                                          j["created"]))

    def cancel_queued(self, kind: str, song_id: str, keep_label: str) -> int:
        """取消同一首歌還在排隊、但已經不需要的工作（例如連按好幾次 KEY 時中間的那些）。"""
        n = 0
        with self.lock:
            for j in self.state.values():
                if j["kind"] == kind and j["song"] == song_id and j["label"] != keep_label:
                    if j["status"] == "queued":
                        j.update(status="cancelled", step="已取消")
                        n += 1
                    elif j["status"] == "running":
                        j["cancel"] = True   # 執行中的工作會在下一段處理完時停下
                        n += 1
        return n

    def _overlapping(self, jid: str) -> bool:
        """和別首同時在跑，或別首剛做完不久（Codex r2-1：OOM 從子程式傳回來要先清理模型，這段時間另一首可能剛好做完）。
        這首已經因為記憶體不夠重做過就不算（重做時照原本的方式退回標準模型）。"""
        with self.lock:
            st = self.state.get(jid) or {}
            if st.get("oom_retry"):
                return False
            return len(self.running) > 1 or time.time() - st.get("overlap_end", 0.0) < OVERLAP_GRACE

    def stems_busy(self, song_id: str) -> bool:
        """fork（D6、68）：這首歌有沒有會重寫分軌的工作在執行（匯入、分離、主唱補回；只重做旋律、歌詞的不算）。"""
        with self.lock:
            return any(j["song"] == song_id and j["status"] == "running" and j["kind"] == "process"
                       and not set(j["label"].split(",")) <= {"pitch", "lyrics"} for j in self.state.values())

    def song_running(self, song_id: str) -> bool:
        """fork（D2）：這首歌有沒有工作在執行。呼叫端要拿著 self.lock。"""
        return any(j["song"] == song_id and j["status"] == "running" for j in self.state.values())

    def cancel_song(self, song_id: str) -> None:
        """fork（D4）：取消這首歌所有排隊中的工作（刪除歌曲時）。呼叫端要拿著 self.lock。"""
        for j in self.state.values():
            if j["song"] == song_id and j["status"] == "queued":
                j.update(status="cancelled", step="已取消", finished=time.time())
        # Codex r4-1：同時從佇列拿掉。留在 pending 的話，一小時後 submit 清掉它的 state，
        # order()、move()、upcoming() 用 self.state[j] 就會丟 KeyError
        self.pending = [(jid, fn) for jid, fn in self.pending
                        if not (self.state[jid]["song"] == song_id and self.state[jid]["status"] == "cancelled")]

    def cancel(self, jid: str, queued_only: bool = False) -> bool:
        """使用者按「停止」：排隊中的直接取消；執行中的在下一個檢查點停下。
        queued_only（fork，複審 P3-4）：已經開始的不動、回 False。"""
        with self.lock:
            j = self.state.get(jid)
            if not j:
                return False
            cb = None
            if j["status"] == "queued":
                j.update(status="cancelled", step="已取消")
                cb = self._on_cancel.pop(jid, None)
            elif j["status"] == "running" and not queued_only:
                j["cancel"] = True
                j["step"] = "停止中…"
            else:
                return False
        if cb:   # 鎖外呼叫：收尾可能要再操作佇列（例如恢復暫停狀態）
            try:
                cb()
            except Exception as e:
                log_error("on_cancel", e)
        return True

    def freeze(self, on: bool) -> None:
        """fork（69）：搬移歌曲庫時不開始新的工作（已經在跑的照常做完）。"""
        with self.cv:
            self.frozen = on
            self.cv.notify_all()

    def position(self, jid: str) -> int:
        """前面還有幾個工作。"""
        with self.lock:
            me = self.state.get(jid)
            if not me or me["status"] != "queued":
                return 0
            ahead = 0
            for j, _ in self.pending:
                if j == jid:
                    break
                if self.state[j]["status"] == "queued":
                    ahead += 1
            return ahead + sum(1 for j in self.state.values() if j["status"] == "running")

    def find(self, kind: str, song_id: str, label: str = "") -> str | None:
        with self.lock:
            for j in self.state.values():
                if (j["kind"], j["song"], j["label"]) == (kind, song_id, label) \
                        and j["status"] in ("queued", "running") and not j.get("cancel"):
                    return j["id"]
        return None

    def _run(self):
        while True:
            st = live_status() if self.live_safe else {"hold": False}   # 查程式清單可能要幾秒，不要鎖著佇列查
            slots = self._slots(st["hold"])
            with self.cv:
                jid, fn, held = self._next(st["hold"], slots)
                if jid is None:
                    if held and not self.running:   # 只剩直播中暫停的背景工作
                        reason = st.get("reason") or "直播中"
                        self.holding = reason
                        for j, _ in self.pending:
                            js = self.state.get(j)
                            # fork（Codex r4-8）：只改真的因為直播暫停的；等分軌、全部暫停的說明照留
                            if js and js["status"] == "queued" and js.get("label") in LIVE_BACKGROUND:
                                js["step"] = f"直播中暫停（{reason}），結束後自動繼續"
                    elif not held:
                        self.holding = ""
                    if self.paused:
                        for j, _ in self.pending:
                            js = self.state.get(j)
                            if js and js["status"] == "queued" and self._pausable(js):
                                js["step"] = "已全部暫停"
                    self.cv.wait(5 if (self.pending or self.running) else None)
                    continue
                self.holding = ""
                if fn is None or self.state[jid]["status"] == "cancelled":
                    continue
                self.running[jid] = "gpu"
                self.state[jid].update(status="running", step="開始")
            threading.Thread(target=self._work, args=(jid, fn), daemon=True, name=f"job-{jid}").start()

    def _work(self, jid: str, fn) -> None:
        def report(step, p, jid=jid):
            if (self.state.get(jid) or {}).get("cancel"):
                raise JobCancelled()
            self.update(jid, step=step, progress=round(p, 3))
            if (self.state.get(jid) or {}).get("pause"):
                raise JobDeferred("paused")   # 按了全部暫停：讓出顯示卡，繼續後重做這首
            if self._held(jid) and live_status()["hold"]:
                raise JobDeferred()   # 做到一半開播：讓出顯示卡，直播後重做

        def gpu_done():
            """分離做完了（接下來是分析旋律、存檔）：讓下一首先開始分離。"""
            with self.cv:
                if jid in self.running:
                    self.running[jid] = "post"
                    self.cv.notify_all()
        report.gpu_done = gpu_done

        def check(jid=jid):
            """顯示卡子程式在跑、沒有進度回報時，也要能馬上停止／暫停。"""
            st = self.state.get(jid) or {}
            if st.get("cancel"):
                raise JobCancelled()
            if st.get("pause"):
                raise JobDeferred("paused")
        import gpu_worker as G
        G.CURRENT.check = check
        # fork（36）：和別首同時在跑、而且這首還沒因為記憶體不夠重做過（重做時照原本的方式退回標準模型）
        G.CURRENT.overlapping = lambda: self._overlapping(jid)
        G.CURRENT.cpu_fallback = None   # fork（第 7 批）：gpu_worker 改用 CPU 處理程式時記下原因
        try:
            fn(report)
            self.update(jid, status="done", step="完成", progress=1.0)
        except JobDeferred as e:
            paused = bool(e.args) and e.args[0] == "paused"
            with self.cv:
                self.state[jid].pop("pause", None)
                if self.state[jid].get("cancel"):   # fork：讓位的同時按了停止，照停止處理，不排回佇列
                    self.state[jid].update(status="cancelled", step="已取消", finished=time.time())
                    free_gpu()
                    return
                self.state[jid].update(status="queued", progress=0.0,
                                       step="已全部暫停，繼續後重新開始這首" if paused else "直播中暫停，結束後重新開始")
                if paused:
                    self.pending.insert(0, (jid, fn))   # 繼續處理時先做這首
                else:
                    self.pending.append((jid, fn))
            free_gpu()
        except JobCancelled:
            self.update(jid, status="cancelled", step="已取消")
        except UserError as e:
            self.update(jid, status="error", error=str(e))
        except Exception as e:
            traceback.print_exc()
            if ("out of memory" in str(e).lower() and not self.state[jid].get("oom_retry")
                    and (self._overlapping(jid) or getattr(e, "overlap_oom", False))):
                # fork（36）：判定時是重疊處理；複審 P3-8：別首剛做完（OVERLAP_GRACE 內）也算，旋律分析這類
                # 沒經過 _overlap_oom 的 OOM 才不會因為另一首剛好先做完就直接變成錯誤
                # 兩首同時處理時顯示卡記憶體不夠：接下來十分鐘只處理一首，這首排回最前面重做
                self.oom_until = time.time() + 600
                free_gpu()
                with self.cv:
                    if self.state[jid].get("cancel"):   # fork：同時按了停止，不排回佇列重做
                        self.state[jid].update(status="cancelled", step="已取消", finished=time.time())
                    else:
                        self.state[jid].update(status="queued", step="顯示卡記憶體不夠，稍後重新處理", progress=0.0,
                                               oom_retry=True)
                        self.pending.insert(0, (jid, fn))
                log_error("parallel separation OOM", e)
            else:
                self.update(jid, status="error", error=f"{type(e).__name__}: {e}")
        finally:
            why = getattr(G.CURRENT, "cpu_fallback", None)
            G.CURRENT.check = G.CURRENT.overlapping = G.CURRENT.cpu_fallback = None
            if why:
                _cpu_fallback_note((self.state.get(jid) or {}).get("song"), why)
            with self.cv:
                self.running.pop(jid, None)
                (self.state.get(jid) or {}).pop("pause", None)
                for other in self.running:   # fork（Codex r2-1）：還在跑的那幾首記下「剛剛還有別首一起跑」
                    (self.state.get(other) or {})["overlap_end"] = time.time()
                self.cv.notify_all()
