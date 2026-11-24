# 從 pipeline.py 搬出（tests/tools/split_moves.py，docs/pipeline-split-plan.md 方式一）。
# pipeline._load_part 把這個檔案載入 pipeline 的命名空間，等於這段程式寫在 pipeline.py 裡；不能單獨 import。


def _stem_kind(name: str) -> str | None:
    marks = re.findall(r"\((vocals|instrumental|other|no vocals)\)", name.lower())
    return marks[-1] if marks else None


def _outputs(result, outdir: Path) -> list[Path]:
    paths = []
    for r in result or []:
        p = Path(r)
        if not p.is_absolute():
            p = outdir / p
        paths.append(p)
    if not paths:
        paths = list(outdir.glob("*.wav"))
    return paths


def _pick(paths: list[Path], kind: str) -> Path:
    for p in paths:
        if _stem_kind(p.name) == kind:
            return p
    alt = {"instrumental": ("other", "no vocals")}.get(kind, ())
    for p in paths:
        if _stem_kind(p.name) in alt:
            return p
    raise RuntimeError(f"分離結果裡找不到 {kind}：{[p.name for p in paths]}")


# 主唱／合音分離的高品質模型（Mel-Band Roformer，aufr33／viperx）。
# 合音多、或有和聲吟唱的歌，標準模型常把合音留在主唱軌，旋律會在兩個聲部之間亂跳；這個模型分得乾淨很多。
# 用 PyTorch 運算：有顯示卡約幾十秒，只有 CPU 會非常慢（一首歌可能要十幾分鐘），所以「自動」只在顯示卡可用時才用。
HQ_KARAOKE = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"
# 2.1.0：高品質的人聲／伴奏分離模型（BS-Roformer，viperx）。MVSEP 排行榜人聲 10.87／伴奏 17.17 dB，
# 比原本的 MDX-NET Inst HQ 3（9.38／15.68）乾淨很多；伴奏殘留的人聲和主唱軌殘留的鼓都明顯減少
HQ_VOCAL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
_HQ_VOCAL_YAML = "model_bs_roformer_ep_317_sdr_12.9755.yaml"


# 模型下載來源：Hugging Face 的鏡像在台灣比 GitHub 快很多，放第一個；失敗或太慢再換 GitHub。
# 每個檔案都核對大小與 SHA-256（與 GitHub 原檔一致），鏡像就算被換掉也不會用到錯的檔案。
_HF_UVR = "https://huggingface.co/Politrees/UVR_resources/resolve/main/models/"
_RAW_CFG = "https://raw.githubusercontent.com/TRvlvr/application_data/main/mdx_model_data/mdx_c_configs/"
_GH_UVR = "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/"
_GH_AS = "https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/"
_HQ_YAML = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956_config.yaml"
MODEL_FILES = {   # 檔名 → (大小, SHA-256, 下載網址…)
    "UVR-MDX-NET-Inst_HQ_3.onnx": (66759214, "317554b07fe1ea5279a77f2b1520a41ea4b93432560c4ffd08792c30fddf9adc", [
        _HF_UVR + "MDXNet/UVR-MDX-NET-Inst_HQ_3.onnx", _GH_UVR + "UVR-MDX-NET-Inst_HQ_3.onnx"]),
    "UVR_MDXNET_KARA_2.onnx": (52786726, "bf32e15105a09c0f7dddd2b67346146334d6f3ecb399ed7638eba2ab07cbf5f4", [
        _HF_UVR + "MDXNet/UVR_MDXNET_KARA_2.onnx", _GH_UVR + "UVR_MDXNET_KARA_2.onnx"]),
    HQ_KARAOKE: (913096801, "1de20d459332fe8869aeb01327a31df0032262706e1365114e852dc271779813", [
        _HF_UVR + "Roformer/MelBand/" + HQ_KARAOKE, _GH_UVR + HQ_KARAOKE, _GH_AS + HQ_KARAOKE]),
    # O12：釘住大小與 SHA-256（2026-11-21 下載核對過）
    _HQ_YAML: (1726, "b35077d94861f068097cce1a5e54633c055e7dcc2613eade4e4dc7c7c9c3f48b", [_GH_AS + _HQ_YAML]),
    # 2.1.1：Hugging Face 的資料夾是 BandSplit（2.1.0 寫成 BSRoformer → 404）；再多一個 GitHub 來源
    HQ_VOCAL: (639331213, "5b84f37e8d444c8cb30c79d77f613a41c05868ff9c9ac6c7049c00aefae115aa", [
        _HF_UVR + "Roformer/BandSplit/" + HQ_VOCAL, _GH_UVR + HQ_VOCAL, _GH_AS + HQ_VOCAL]),
    _HQ_VOCAL_YAML: (2273, "2bfdd16c656bd9519aba757cc4f8834b7ede675eb1e00ec4772d74ae1c41af7f", [
        _GH_AS + _HQ_VOCAL_YAML, _RAW_CFG + _HQ_VOCAL_YAML]),
}
# audio-separator 第一次載入模型時要的設定檔；raw.githubusercontent 不通時改走 jsDelivr
# 釘住 commit 並核對檢查碼（O12）：抓 main 分支的話內容會變，檢查碼就對不上。
# 2026-11-21 核對過：main 上的三個檔和這個 commit 的大小、SHA-256 相同
_DATA_COMMIT = "3826b05b570dbd4fbedbc807758803b35348ba1b"
_RAW = f"https://raw.githubusercontent.com/TRvlvr/application_data/{_DATA_COMMIT}/"
_JSD = f"https://cdn.jsdelivr.net/gh/TRvlvr/application_data@{_DATA_COMMIT}/"
MODEL_DATA = {   # 檔名 → (大小, SHA-256, 下載網址…)
    "download_checks.json": (28267, "d3622e1fa19c161d3cf704927711b453d593a3f1eb0f2e0838c3136907935151",
                             [_RAW + "filelists/download_checks.json", _JSD + "filelists/download_checks.json"]),
    "vr_model_data.json": (4380, "6a2021b9aa355c10f845b68c73944d47bd9e1fdf37b8a584b14ce8f88b09bb2f",
                           [_RAW + "vr_model_data/model_data_new.json", _JSD + "vr_model_data/model_data_new.json"]),
    "mdx_model_data.json": (15358, "1aca8f9bcc57233bc714029663a9ec2345d9c7721f91e5e08f46392a879c6a9a",
                            [_RAW + "mdx_model_data/model_data_new.json", _JSD + "mdx_model_data/model_data_new.json"]),
}
HQ_FILES = {k: MODEL_FILES[k] for k in (HQ_KARAOKE, _HQ_YAML)}
HQ_DL = {"state": "idle", "got": 0, "total": HQ_FILES[HQ_KARAOKE][0], "error": ""}
_hq_lock = threading.Lock()
HQV_FILES = {k: MODEL_FILES[k] for k in (HQ_VOCAL, _HQ_VOCAL_YAML)}
HQV_DL = {"state": "idle", "got": 0, "total": HQV_FILES[HQ_VOCAL][0], "error": ""}


STD_VOCAL = "UVR-MDX-NET-Inst_HQ_3.onnx"
_HQ_FAIL = {"vocal": 0, "kara": 0}   # 這次執行中高品質模型失敗幾次；連續失敗 2 次就先不用（重開練唱台再試）


def hq_disabled(cfg: dict) -> bool:
    """fork（Codex r5-3）：設定要用的高品質模型，有一個這次執行已經停用（連續失敗 2 次），補做高品質也只會用標準模型。
    只看目前設定要用的（Codex r6-1：人聲失敗兩次後改成標準，主唱的高品質補做照做）。"""
    return ((_uses_hq(cfg, "vocal_quality") and _HQ_FAIL["vocal"] >= 2)
            or (_uses_hq(cfg, "karaoke_quality") and _HQ_FAIL["kara"] >= 2))


def _hq_fail_note(d: Path, what: str, e: Exception) -> None:
    msg = f"高品質{what}分離模型無法使用，這首改用標準模型：{str(e)[:160]}"
    with _META_LOCK:
        meta = read_meta(d)
        meta["notes"] = [x for x in meta.get("notes", []) if not x.startswith(f"高品質{what}分離模型無法使用")] + [msg]
        write_meta(d, meta)
_MODEL_CHECKED: set = set()


def _check_model_file(name: str) -> None:
    """模型載入失敗時核對檔案（大小、SHA-256），不對就刪掉讓下次重新下載。每個模型一次執行只核對一次。"""
    spec = MODEL_FILES.get(name)
    p = MODELS / "uvr" / name
    if not spec or not p.exists() or name in _MODEL_CHECKED:
        return
    _MODEL_CHECKED.add(name)
    size, sha, _ = spec
    try:
        bad = (size and p.stat().st_size != size) or (sha and _sha256(p) != sha)
    except OSError:
        bad = False
    if bad:
        print(f"[separate] 模型檔 {name} 核對不正確，已刪除，下次會重新下載", flush=True)
        p.unlink(missing_ok=True)
        # fork（Codex r5-1）：只重設這個模型自己的下載狀態。原本兩組都設成 idle：另一組還在下載時被當成沒在下載，
        # 搬移歌曲庫的檢查就會放行，下載繼續寫舊位置
        tr = {HQ_VOCAL: HQV_DL, HQ_KARAOKE: HQ_DL}.get(name)
        if tr is not None and tr.get("state") != "downloading":
            tr.update(state="idle")
    # 設定檔（yaml）也一起看：下載到錯誤頁面時會是 HTML
    for cfgname, (csize, csha, _) in MODEL_FILES.items():
        if cfgname.endswith(".yaml") and name.rsplit(".", 1)[0] in cfgname:
            cp = MODELS / "uvr" / cfgname
            try:
                if cp.exists() and ((csha and _sha256(cp) != csha) or cp.read_bytes()[:20].lstrip().startswith(b"<")):
                    print(f"[separate] 設定檔 {cfgname} 不正確，已刪除", flush=True)
                    cp.unlink(missing_ok=True)
            except OSError:
                pass


def hqv_ready() -> bool:
    return _files_ready(HQV_FILES)


def hq_ready() -> bool:
    """高品質模型已經完整下載（大小完全正確才算，避免下載到一半的檔案被當成可用）。"""
    return _files_ready(HQ_FILES)


def _files_ready(files: dict) -> bool:
    uvr = MODELS / "uvr"
    for name, (size, sha, _) in files.items():
        p = uvr / name
        try:
            n = p.stat().st_size   # 只 stat 一次：ensure_model 可能剛好在刪壞掉的 yaml，消失就當成還沒好
        except OSError:
            return False
        if (size and n != size) or n == 0:
            return False
        if not _small_file_ok(p, size, sha):
            return False   # 唯讀：狀態輪詢會呼叫這裡，刪檔重抓交給 ensure_model（在模型鎖裡）
    return True


def _small_file_ok(p: Path, size: int | None, sha: str | None) -> bool:
    """小於 1 MB 的設定檔（yaml）也核對 SHA-256（O12：舊版沒核對就下載過）；大檔只看大小。讀不到當成不符。"""
    if not (sha and size and size < (1 << 20)):
        return True
    try:
        return _sha256(p) == sha
    except OSError:
        return False


class _SlowSource(Exception):
    pass


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _open(url: str, start: int = 0, end: int | None = None, timeout: int = 30):
    import urllib.request
    hdr = {"User-Agent": USER_AGENT}
    if start or end is not None:
        hdr["Range"] = f"bytes={start}-{'' if end is None else end - 1}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=timeout)


_LINGER: dict[str, list] = {}   # fork（38）：取消時 5 秒內沒結束的下載執行緒（卡在 read），下次下載同一個檔案前先等它們
_LINGER_LOCK = threading.Lock()


def fetch_file(urls: list[str], dst: Path, size: int | None = None, sha256: str | None = None,
               progress=None, conns: int = 4) -> None:
    """下載檔案：大檔分成幾段同時下載（單一連線被限速時快很多），每段都能續傳；
    一個來源連不上或中斷就換下一個來源。完成後核對大小與 SHA-256 才改成正式檔名。
    progress(已下載, 總大小, 速度) 每半秒呼叫一次；它丟出例外（例如按了停止）就中止下載。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    legacy = dst.with_name(dst.name + ".part")
    if dst.exists():
        if not size or dst.stat().st_size == size:
            return
        if not legacy.exists():
            dst.replace(legacy)            # 舊版直接寫正式檔名、下載到一半的檔案：接著續傳
        else:
            dst.unlink()

    if not size or size < (16 << 20):      # 小檔：一次下載
        last = None
        for url in urls:
            try:
                with _open(url) as r, open(legacy, "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                if size and legacy.stat().st_size != size:
                    raise IOError(f"檔案大小不對（{legacy.stat().st_size} / {size}）")
                if sha256 and _sha256(legacy) != sha256:
                    raise IOError("檢查碼不符")
                legacy.replace(dst)
                return
            except Exception as e:
                last = e
                legacy.unlink(missing_ok=True)
                log_error(f"download {dst.name} {url}", e)
        raise last or IOError("下載失敗")

    with _LINGER_LOCK:
        old = _LINGER.pop(str(dst), [])
    for t in old:   # 它們的 stop 已經設了，讀完那段（最多 30 秒逾時）就離開，不會再寫入；還開著 .partK 時接不起來
        t.join()
    seg = -(-size // conns)
    ranges = [(k * seg, min(size, (k + 1) * seg)) for k in range(conns)]
    parts = [dst.with_name(f"{dst.name}.part{k}") for k in range(conns)]
    if legacy.exists():                    # 1.4.5～1.4.9 的單一續傳檔：當作第一段的開頭
        if not parts[0].exists():
            with open(legacy, "r+b") as f:
                f.truncate(min(legacy.stat().st_size, seg))
            legacy.replace(parts[0])
        else:
            legacy.unlink()
    stop = threading.Event()
    errors: list[Exception] = []
    pref = [0]                             # 目前最好用的來源，其他段落也跟著用
    dead: set[str] = set()                 # 確定沒有這個檔案的來源（404 等）：之後所有段落都不再用
    lock = threading.Lock()

    def have(k: int) -> int:
        return parts[k].stat().st_size if parts[k].exists() else 0

    def alive() -> list[int]:
        with lock:
            return [i for i, u in enumerate(urls) if u not in dead]

    def next_src(ui: int) -> int:
        """從 ui 的下一個開始，找一個還能用的來源。"""
        ok = alive()
        if not ok:
            return ui
        for step in range(1, len(urls) + 1):
            if (ui + step) % len(urls) in ok:
                return (ui + step) % len(urls)
        return ui

    def worker(k: int) -> None:
        a, b = ranges[k]
        fails, ui = 0, pref[0]
        while not stop.is_set():
            got = have(k)
            if got >= b - a:
                if got > b - a:
                    with open(parts[k], "r+b") as f:
                        f.truncate(b - a)
                return
            if urls[ui % len(urls)] in dead:
                ui = next_src(ui)
            url = urls[ui % len(urls)]
            wrote = 0
            try:
                with _open(url, a + got, b) as r:
                    if r.status != 206:
                        raise IOError(f"來源不支援分段下載（HTTP {r.status}）")
                    with open(parts[k], "ab") as f:
                        w0, n0 = time.time(), 0
                        while not stop.is_set():
                            chunk = r.read(256 << 10)
                            if not chunk or stop.is_set():   # fork（38）：讀這段的期間按了停止，不寫
                                break
                            f.write(chunk)
                            n0 += len(chunk)
                            wrote += len(chunk)
                            if time.time() - w0 > 30:   # 這個來源慢到不行（每段 < 30 KB/s）：有別的來源就換
                                if n0 < 30e3 * 30 and len(alive()) > 1:
                                    raise _SlowSource()
                                w0, n0 = time.time(), 0
                    if not wrote and not stop.is_set():
                        # fork：回 206 後一個位元組都沒給，算一次失敗、等一下再試（原本不停頓地重連，每條執行緒佔滿一個核心）
                        raise OSError("來源回應後沒有資料")
                fails = 0
            except _SlowSource:
                ui = next_src(ui)          # 太慢：換來源，不算失敗
                pref[0] = ui
            except Exception as e:
                code = getattr(e, "code", None)
                if code in (403, 404, 410):   # 這個來源沒有這個檔案：永久剔除，不要來回切換
                    with lock:
                        dead.add(url)
                    log_error(f"download {dst.name}：來源沒有這個檔案（HTTP {code}），不再使用 {url}", e)
                    if not alive():
                        errors.append(IOError(f"所有下載來源都找不到 {dst.name}"))
                        stop.set()
                        return
                    ui = next_src(ui)
                    pref[0] = ui
                    continue
                fails = 0 if wrote else fails + 1   # 有下載到東西（例如中途斷線）不算連續失敗
                ui = next_src(ui)          # 換下一個來源
                pref[0] = ui
                log_error(f"download {dst.name}[{k}] {url}", e)
                if fails >= 3 * max(1, len(alive())):
                    errors.append(e)
                    stop.set()
                    return
                stop.wait(2)

    threads = [threading.Thread(target=worker, args=(k,), daemon=True) for k in range(conns)]
    for t in threads:
        t.start()
    start_got, t0 = sum(have(k) for k in range(conns)), time.time()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5 / conns)
            if progress:
                got = sum(have(k) for k in range(conns))
                progress(got, size, (got - start_got) / max(0.5, time.time() - t0))
    except BaseException:
        stop.set()
        for t in threads:
            t.join(5)
        stuck = [t for t in threads if t.is_alive()]
        if stuck:   # 還卡在 read：記下來，下次下載同一個檔案前先等它們（fork，38）
            with _LINGER_LOCK:
                _LINGER.setdefault(str(dst), []).extend(stuck)
        raise
    if errors:
        raise errors[0]
    with open(parts[0], "ab") as f:        # 接起來
        for p in parts[1:]:
            with open(p, "rb") as g:
                shutil.copyfileobj(g, f, 1 << 22)
            p.unlink()
    if parts[0].stat().st_size != size or (sha256 and _sha256(parts[0]) != sha256):
        parts[0].unlink(missing_ok=True)
        raise IOError(f"{dst.name} 下載不完整（檢查碼不符），下次會重新下載")
    parts[0].replace(dst)


_MODEL_DATA_LOCK = threading.Lock()


def ensure_model_data() -> None:
    """audio-separator 需要的模型設定檔；壞掉（例如下載到一半）就重新下載。"""
    with _MODEL_DATA_LOCK:   # fork：背景下載高品質模型和處理歌曲會同時呼叫，小檔的暫存檔名固定，同時下載會互相蓋掉
        _ensure_model_data()


def _ensure_model_data() -> None:
    uvr = MODELS / "uvr"
    for name, (size, sha, urls) in MODEL_DATA.items():
        p = uvr / name
        if p.exists() and _sha256(p) == sha:
            continue
        p.unlink(missing_ok=True)   # 舊版從 main 抓的、下載到一半的、內容不符的：都改抓固定版本（O12）
        try:
            fetch_file(urls, p, size, sha)
        except Exception as e:
            log_error(f"model data {name}", e)
            # 不交給 audio-separator 自己下載：它從 main 抓、不核對檢查碼（O12）
            raise UserError(f"下載分離模型的設定檔失敗（{name}），請確認網路連線後再試") from e


_model_locks: dict = {}


def ensure_model(name: str, progress=None) -> None:
    """確認分離模型已經完整下載；沒有或不完整就從最快的來源下載。"""
    ensure_model_data()
    spec = MODEL_FILES.get(name)
    if not spec:
        return                             # 不認得的模型：交給 audio-separator 自己下載
    size, sha, urls = spec
    p = MODELS / "uvr" / name
    with _model_locks.setdefault(name, threading.Lock()):   # 兩首同時處理時不要重複下載同一個模型
        if p.exists() and (not size or p.stat().st_size == size):
            if _small_file_ok(p, size, sha):
                return
            p.unlink()   # O12：小設定檔大小對但內容不符（舊版沒核對就下載過）：刪掉重抓（fetch_file 只看大小）
        fetch_file(urls, p, size, sha, progress)


def start_hq_download() -> None:
    """在背景下載高品質主唱分離模型（約 900 MB），不佔用處理歌曲的佇列。"""
    _start_download(HQ_FILES, HQ_DL, hq_ready)


def start_hqv_download() -> None:
    """在背景下載高品質人聲／伴奏分離模型（約 640 MB）。"""
    _start_download(HQV_FILES, HQV_DL, hqv_ready)


def _start_download(files: dict, tracker: dict, ready) -> None:
    if NO_BACKGROUND:
        return
    with _hq_lock:
        if tracker["state"] == "downloading" or ready():
            return
        with _LIB_CV:   # fork（70）：搬移中不開始（下載完的檔案會留在舊位置）；和 set_moving 同一把鎖，搬移開始後一定看得到這次的「下載中」
            if STORAGE["moving"]:
                return
            tracker.update(state="downloading", error="")

    def run():
        try:
            for name in files:
                ensure_model(name, lambda got, total, spd: tracker.update(got=got))
            tracker.update(state="done", got=tracker["total"])
        except Exception as e:
            tracker.update(state="error", error=f"{type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True).start()


def hq_status() -> dict:
    if HQ_DL["state"] in ("idle", "done") and hq_ready():
        return {"state": "ready"}
    return dict(HQ_DL)


def hqv_status() -> dict:
    if HQV_DL["state"] in ("idle", "done") and hqv_ready():
        return {"state": "ready"}
    return dict(HQV_DL)


HQ_MIN_VRAM = 6e9   # fork（22）：auto 模式下顯示卡記憶體不到這個就用標準模型（BS-Roformer 放不下）；暫定，未實測


def hq_vram_ok() -> bool:
    """顯示卡記憶體夠不夠用高品質模型。還不知道記憶體大小時照舊（不擋）；不會為了問而開顯示卡處理程式。"""
    vram = (gpu_info(block=False) or {}).get("vram")
    return not vram or vram >= HQ_MIN_VRAM


def _uses_hq(cfg: dict, key: str) -> bool:
    """fork（22、Codex r6-1）：這個設定會不會用高品質模型。vocal_model、karaoke_model、wants_hq、hq_models_ready、
    hq_disabled、separate_uvr 共用，判斷一致。"""
    q = cfg.get(key, "auto")
    return q == "high" or (q == "auto" and cuda_ok() and hq_vram_ok())


def vocal_model(cfg: dict) -> str:
    """要用的人聲／伴奏分離模型。高品質模型還沒下載好時，這首先用標準模型，模型在背景下載。"""
    if _uses_hq(cfg, "vocal_quality") and _HQ_FAIL["vocal"] < 2:   # fork（22）：6 GB 門檻；2.3.5：連續失敗 2 次先不用
        if hqv_ready():
            return HQ_VOCAL
        start_hqv_download()
    return cfg["separation_model"]


def karaoke_model(cfg: dict) -> str:
    """要用的主唱／合音分離模型。高品質模型還沒下載好時，這首先用標準模型，模型在背景下載。"""
    if _uses_hq(cfg, "karaoke_quality") and _HQ_FAIL["kara"] < 2:
        if hq_ready():
            return HQ_KARAOKE
        start_hq_download()
    return cfg["karaoke_model"]


def free_gpu() -> None:
    """把 PyTorch 暫存的顯示卡記憶體還回去。不還的話，後面的歌詞辨識（CTranslate2）可能記憶體不夠，
    Windows 會改用很慢的共用記憶體，看起來就像卡在「判斷歌詞語言」。"""
    import gc
    gc.collect()
    if "torch" in sys.modules:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
        except Exception:
            pass


_AUTOCAST = {"bad": False}


FINE_HARMONY = 0.5   # 合音能量達到主唱的一半以上，建議用最精細的方式再分一次


def harmony_ratio(d: Path) -> float | None:
    """主唱有聲音的段落裡，合音和主唱的音量比（0＝幾乎沒有合音，1＝和主唱一樣大聲）。"""
    lead = read_audio(stem_path(d, "lead"), sr=PITCH_SR, mono=True, fallback=False)
    back = read_audio(stem_path(d, "back"), sr=PITCH_SR, mono=True, fallback=False)
    n = min(len(lead), len(back)) // 1600 * 1600
    if n < 1600 * 10:
        return None
    lr = np.sqrt(np.mean(lead[:n].reshape(-1, 1600) ** 2, axis=1))
    br = np.sqrt(np.mean(back[:n].reshape(-1, 1600) ** 2, axis=1))
    voiced = lr > max(1e-4, 0.15 * float(np.percentile(lr, 95)))
    if voiced.sum() < 10:
        return None
    return round(float(np.sqrt(np.sum(br[voiced] ** 2) / np.sum(lr[voiced] ** 2))), 3)


LEAD_FIX_V = 2   # 主唱補回的判斷方式版本：改了判斷方式就加一，舊歌會重新檢查一次
SPLIT_NOTE = "主唱和合音可能沒分好"


def _lyric_spans(d: Path) -> list[tuple[float, float]]:
    """有時間的歌詞句子（開始、結束秒數）；整句括號的（和聲、旁白）不算。每句最多算 8 秒。"""
    p = d / "lyrics.lrc"
    if not p.exists():
        return []
    try:
        rows = [r for r in parse_lrc(p.read_text(encoding="utf-8", errors="ignore")) if r["t"] is not None]
    except Exception:
        return []
    out = []
    for i, r in enumerate(rows):
        tx = r["text"].strip()
        if not tx or re.fullmatch(r"[（(].*[)）]", tx) or ":" in tx[:8] or "：" in tx[:8]:
            continue   # 空白、整句括號、「作詞：」這類資訊行
        end = rows[i + 1]["t"] if i + 1 < len(rows) else r["t"] + 5
        end = min(end, r["t"] + 8)
        if end - r["t"] >= 1.0:
            out.append((r["t"], end))
    return out


def _lead_gap_gain(lead_m: np.ndarray, back_m: np.ndarray, sr: int,
                   spans: list[tuple[float, float]] | None = None) -> tuple[np.ndarray | None, float, dict]:
    """找出主唱被分到合音的地方，回傳（每個樣本要搬回主唱的比例 0～1、搬了幾秒、統計）。
    1. 「主唱幾乎沒聲音、合音卻很大聲」持續 8 秒以上（整段疊錄的副歌，例如〈牛仔很忙〉）。
    2. 有時間歌詞時，再逐句看：明明在唱歌詞，主唱卻幾乎整句沒聲音、合音很大聲 → 那句搬回主唱
       （對唱、多人輪流唱的歌，常常一兩句被分到合音）。
    短短幾秒、又不在歌詞句子裡的，通常是真的合音（間奏和聲、前奏哼唱），不動。
    統計裡 weak 是「主唱有聲音、但比合音小很多」的句數：這種沒辦法整句搬，只提醒。"""
    stats = {"lines": 0, "moved_lines": 0, "weak": 0}
    H = sr // 10
    n = min(len(lead_m), len(back_m)) // H * H
    if n < H * 50:
        return None, 0.0, stats
    lr = np.sqrt(np.mean(lead_m[:n].reshape(-1, H) ** 2, axis=1))
    br = np.sqrt(np.mean(back_m[:n].reshape(-1, H) ** 2, axis=1))
    k = np.ones(15) / 15   # 1.5 秒平滑：一個字、一口氣的空檔不算
    ls = np.sqrt(np.convolve(lr ** 2, k, "same"))
    bs = np.sqrt(np.convolve(br ** 2, k, "same"))
    ref = float(np.percentile(np.maximum(ls, bs), 95))
    if ref <= 1e-5:
        return None, 0.0, stats
    blk = np.zeros(len(ls), dtype=np.float32)
    cand = (bs > 0.12 * ref) & (ls < 0.1 * bs)
    i = 0
    while i < len(cand):
        if not cand[i]:
            i += 1
            continue
        j = i
        while j < len(cand) and cand[j]:
            j += 1
        if j - i >= 80:
            blk[max(0, i - 3):min(len(cand), j + 3)] = 1.0   # 前後各多 0.3 秒，接縫不會吃字
        i = j
    if spans:
        k2 = np.ones(5) / 5   # 逐句用 0.5 秒平滑
        ls2 = np.sqrt(np.convolve(lr ** 2, k2, "same"))
        bs2 = np.sqrt(np.convolve(br ** 2, k2, "same"))
        for st, en in spans:
            a, z = int(st * 10), int(en * 10)
            if z > len(ls2) or z - a < 10:
                continue
            act = np.maximum(ls2[a:z], bs2[a:z]) > 0.1 * ref
            if act.sum() < 10:   # 這句幾乎沒唱到（歌詞時間不準或間奏）
                continue
            stats["lines"] += 1
            miss = float(((ls2[a:z] < 0.1 * bs2[a:z]) & act).sum() / act.sum())
            weak = float(((ls2[a:z] < 0.5 * bs2[a:z]) & act).sum() / act.sum())
            if miss >= 0.8:
                stats["moved_lines"] += 1
                blk[max(0, a - 2):min(len(blk), z + 2)] = 1.0
            elif weak >= 0.6:
                stats["weak"] += 1
    sec = float(blk.sum()) / 10
    if sec < 1.0:
        return None, 0.0, stats
    fade = np.convolve(blk, np.ones(3) / 3, "same")   # 0.3 秒淡入淡出
    g = np.interp(np.arange(len(lead_m)), np.arange(len(fade)) * H + H / 2, fade).astype(np.float32)
    return g, sec, stats


def _split_note(d: Path, stats: dict) -> None:
    """很多句「主唱比合音小很多」：沒辦法自動整句搬，提醒使用者可以怎麼處理。"""
    with _META_LOCK:
        meta = read_meta(d)
        notes = [x for x in meta.get("notes", []) if SPLIT_NOTE not in x]
        if stats["lines"] >= 6 and stats["weak"] >= max(3, 0.25 * stats["lines"]):
            notes.append(f"{SPLIT_NOTE}：有 {stats['weak']} 句主唱聲音很小、合音很大（疊錄、對唱或多人合唱的歌常見）。"
                         "可以試「用最高品質人聲分離重新辨識」；多人輪流唱的歌可以開「合唱模式」")
        meta["notes"] = notes
        meta["split_suspect"] = stats["weak"]
        write_meta(d, meta)


def fix_lead_gaps(d: Path) -> float:
    """主唱被誤分到合音的段落、句子搬回主唱（合音那段同時拿掉）。回傳搬了幾秒；沒有就不改檔。"""
    lp, bp = stem_path(d, "lead"), stem_path(d, "back")
    if not lp.exists() or not bp.exists():
        return 0.0
    lead, back = read_audio(lp, fallback=False), read_audio(bp, fallback=False)
    n = min(lead.shape[-1], back.shape[-1])
    lead, back = lead[..., :n], back[..., :n]
    g, sec, stats = _lead_gap_gain(lead.mean(axis=0), back.mean(axis=0), SR, _lyric_spans(d))
    if g is not None:
        # fork（69、70）：改分軌之前先標記：舊的 KEY 版本是用修正前的分軌做的、旋律要重新分析（step_pitch 做完才清）。
        # 換分軌途中失敗或被關掉，最多多做一次 KEY 版本和旋律，不會把舊的當成有效
        invalidate_variants(d)
        update_meta(d, pitch_stale=True)
        _replace_stems(d, {"lead": lead + back * g, "back": back * (1 - g)})   # 兩軌一起換
        invalidate_variants(d)   # 換的期間做好的 KEY 版本（移調在換之前就開始了）也是舊的
        print(f"[separate] 主唱有 {sec:.0f} 秒（{stats['moved_lines']} 句歌詞）被分到合音，已搬回主唱", flush=True)
    _split_note(d, stats)
    update_meta(d, lead_fix=LEAD_FIX_V, lead_fix_sec=round(sec, 1), lead_fix_lrc=_lrc_stamp(d))
    return sec


def lead_fix_job(song_id: str, after=None):
    """舊歌：主唱被分到合音的段落搬回主唱，再重新分析旋律（不用重新分離）。"""
    def run(report):
        d = song_dir(song_id)
        report("檢查主唱有沒有被分到合音", 0.05)
        # fork（69）：上次分軌改了、旋律還沒分析完就停止的，這次就算沒有要搬的也重新分析旋律
        if fix_lead_gaps(d) > 0 or read_meta(d).get("pitch_stale"):
            step_pitch(d, load_config(), report)
            if after:
                after(song_id)
    return run


def _lrc_stamp(d: Path) -> float:
    p = d / "lyrics.lrc"
    return round(p.stat().st_mtime, 1) if p.exists() else 0.0


def lead_fix_needed(meta: dict) -> bool:
    """還沒用目前的判斷方式檢查過，或檢查之後歌詞換過（逐句檢查要用新的歌詞時間）。"""
    stp = meta.get("steps") or {}
    if not (stp.get("separate") and meta.get("separation") == "uvr" and meta.get("id")):
        return False
    if meta.get("lead_fix") != LEAD_FIX_V:
        return True
    return meta.get("lead_fix_lrc", 0.0) != _lrc_stamp(song_dir(meta["id"]))


def fine_job(song_id: str, after=None):
    """精細處理：用最精細的設定（重疊 4 次）重新分主唱與合音，再重新分析旋律。歌詞、錄音不變。"""
    def run(report):
        d = song_dir(song_id)
        cfg = dict(load_config())
        cfg["karaoke_overlap"] = 4
        if cfg.get("karaoke_quality") == "standard":
            cfg["karaoke_quality"] = "auto"
        full = hq_models_ready(cfg)   # 高品質模型都在：這次就等於直播後的補做
        lrc = (d / "lyrics.lrc").exists()
        fit_before = lyrics_timing_fit(d) if lrc else None
        used_light = step_separate(d, cfg, report)
        getattr(report, "gpu_done", lambda: None)()
        step_pitch(d, cfg, report)
        # fork（70）：依實際的分離方式。顯示卡卡住、中途改用 CPU＋標準模型時不算補做完成
        update_meta(d, fine_suggest=False, fine_done=True, fine_queued=False,
                    **({"live_light": False} if full and not used_light else {}))
        if lrc:
            try:
                check_lyrics_after_reseparate(d, fit_before)
            except Exception as e:
                log_error("check_lyrics_after_reseparate", e)
        if after:
            try:
                after(song_id)
            except Exception as e:
                log_error("fine_job after", e)
    return run


def _autocast_ok() -> bool:
    """顯示卡支援快速的半精度運算（RTX 20 系列以後）時，高品質分離用混合精度，速度約快一倍、結果幾乎相同。
    GTX 10 系列以前的顯示卡半精度反而比較慢，維持原本的方式。"""
    if _AUTOCAST["bad"] or not cuda_ok():
        return False
    if not IN_WORKER:
        return ((gpu_info() or {}).get("cc") or 0) >= 7
    try:
        import torch
        return torch.cuda.get_device_capability(0)[0] >= 7
    except Exception:
        return False


_PROG_TLS = threading.local()   # 每個執行緒目前的分離進度（2.1.1：兩首同時分離時各回報各的）
_PROG_PATCHED = {"done": False}
_prog_lock = threading.Lock()


def _patch_tqdm() -> None:
    """把 audio-separator 內部的 tqdm 換成會依執行緒回報進度的版本（只換一次，之後不再還原）。"""
    with _prog_lock:
        if _PROG_PATCHED["done"]:
            return
        import importlib

        def tq(it=None, *a, **k):
            cur = getattr(_PROG_TLS, "cur", None)
            if cur is None or it is None:
                return it if it is not None else iter(())
            return cur.wrap(it)
        for name in _sep_progress.MODS:
            try:
                m = importlib.import_module("audio_separator.separator.architectures." + name)
                m.tqdm = tq
            except Exception:
                pass
        _PROG_PATCHED["done"] = True


class _sep_progress:
    """audio-separator 內部用 tqdm 跑每一段，換成會回報進度的版本，畫面上就看得到分離跑到哪裡；
    按停止時 report 會丟出例外，也能在分離到一半時停下來。"""
    MODS = ("mdx_separator", "mdxc_separator", "vr_separator")

    def __init__(self, label: str, report, start: float, span: float):
        self.best, self.t0 = 0.0, time.time()
        self.cb = lambda p, eta: report(f"{label} {p * 100:.0f}%{eta}", start + span * p)

    def eta(self, p: float) -> str:
        el = time.time() - self.t0
        if p < 0.05 or el < 5:
            return ""
        left = el * (1 - p) / p
        return f"（約剩 {left / 60:.0f} 分鐘）" if left >= 90 else f"（約剩 {max(5, round(left / 5) * 5):.0f} 秒）"

    def wrap(self, it):
        try:
            n = len(it)
        except Exception:
            n = 0

        def gen():
            for i, x in enumerate(it):
                if n:
                    p = i / n
                    if p >= self.best:
                        self.best = p
                        self.cb(p, self.eta(p))
                yield x
        return gen()

    def __enter__(self):
        _patch_tqdm()
        self.prev = getattr(_PROG_TLS, "cur", None)
        _PROG_TLS.cur = self
        return self

    def __exit__(self, *exc):
        _PROG_TLS.cur = self.prev
        return False


def _dl_report(report, label: str, frac: float):
    last = [0.0]

    def cb(got, total, spd):
        if time.time() - last[0] >= 0.5:
            last[0] = time.time()
            report(f"下載{label} {got / 1e6:.0f}／{total / 1e6:.0f} MB（{spd / 1e6:.1f} MB/s）", frac)
    return cb


def _separate(model: str, inp: Path, tmp: Path, overlap: int, autocast: bool,
              label: str, report, start: float, span: float, cpu: bool = False) -> list[Path]:
    """用一個分離模型分一次（2.1.1：在顯示卡子程式裡跑，主程式不載入 CUDA）。"""
    import gpu_worker as G
    outs = G.call("separate", model, str(inp), str(tmp), overlap, autocast, label, start, span, progress=report, cpu=cpu)
    return [Path(p) for p in outs]


def separate_uvr(d: Path, cfg: dict, report) -> bool:
    """回傳這次是不是沒用到高品質：途中高品質失敗改用標準模型，或高品質這次執行已經停用（fork，Codex r5-3）。"""
    tmp = d / "_sep"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    (MODELS / "uvr").mkdir(parents=True, exist_ok=True)
    overlap = 4 if cfg.get("karaoke_overlap") == 4 else 2
    cpu = bool(cfg.get("_cpu"))   # 直播中：只用 CPU
    autocast = False if cpu else _autocast_ok()

    report("準備人聲分離模型", 0.07)
    vmodel = vocal_model(cfg)
    vhq = vmodel == HQ_VOCAL
    missed = not vhq and _uses_hq(cfg, "vocal_quality") and _HQ_FAIL["vocal"] >= 2   # 要高品質、但這次執行已停用
    ensure_model(vmodel, _dl_report(report, "人聲分離模型", 0.07))
    vlabel = "分離人聲與伴奏" + ("（直播中：CPU 輕量處理，下播後自動補做高品質）" if cpu else "") + ("（高品質）" if vhq else "（高品質模型下載中，這首先用標準模型）" if HQV_DL["state"] == "downloading" else "")
    report(vlabel, 0.08)
    try:
        try:
            outs = _separate(vmodel, stem_path(d, "mix"), tmp, overlap, autocast, vlabel, report, 0.08, 0.26, cpu)
        except JobCancelled:
            raise
        except Exception as e:
            if not (vhq and autocast) or "out of memory" in str(e).lower() or "GpuStalled" in type(e).__name__ or _no_worker(e):
                raise
            log_error("autocast vocal separation", e)
            outs = _separate(vmodel, stem_path(d, "mix"), tmp, overlap, False, vlabel, report, 0.08, 0.26, cpu)
            # fork（22）：一般精度重試成功才算混合精度的問題（兩種都失敗交給下面退回標準模型）；
            # 之後（含這首的第二段）都改用一般精度
            _AUTOCAST["bad"] = True
            autocast = False
    except JobCancelled:
        raise
    except Exception as e:
        # fork（36）：和別首重疊處理時記憶體不夠，交給佇列一次一首重做，不在這裡退回標準模型
        if not vhq or type(e).__name__ == "GpuStalled" or _overlap_oom(e) or _no_worker(e):
            raise
        # 2.3.5：高品質人聲模型載入或執行失敗（模型檔損壞、顯示卡不支援…）：改用標準模型，不要整首退回聲像分離。
        # 模型檔核對不過就刪掉，下次會重新下載
        print(f"[separate] 高品質人聲分離失敗，改用標準模型：{type(e).__name__}: {e}", flush=True)
        log_error("hq vocal separation", e)
        _check_model_file(vmodel)
        _HQ_FAIL["vocal"] += 1
        _hq_fail_note(d, "人聲", e)
        vmodel, vhq, missed = cfg.get("separation_model") or STD_VOCAL, False, True
        vlabel = "分離人聲與伴奏（高品質模型失敗，這首先用標準模型）"
        report(vlabel, 0.08)
        ensure_model(vmodel, _dl_report(report, "人聲分離模型", 0.07))
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        outs = _separate(vmodel, stem_path(d, "mix"), tmp, overlap, autocast, vlabel, report, 0.08, 0.26, cpu)
    update_meta(d, vocal_model="bs_roformer" if vhq else "mdx")   # 之後可以找出還沒用新模型處理的歌
    voc = tmp / "vocals_only.wav"
    _pick(outs, "vocals").replace(voc)
    inst_p = _pick(outs, "instrumental")

    kara = karaoke_model(cfg)
    hq = kara == HQ_KARAOKE
    missed = missed or (not hq and _uses_hq(cfg, "karaoke_quality") and _HQ_FAIL["kara"] >= 2)
    waiting = not hq and HQ_DL["state"] == "downloading"
    label = "把人聲分成主唱與合音" + ("（高品質）" if hq else "（高品質模型下載中，這首先用標準模型）" if waiting else "")
    if not hq:
        ensure_model(kara, _dl_report(report, "主唱分離模型", 0.35))
    report(label, 0.35)

    def run_kara(kara, label, ac):
        outs = _separate(kara, voc, tmp, overlap, ac, label, report, 0.35, 0.18, cpu)
        return [p for p in outs if p != voc and p != inst_p]
    try:
        try:
            outs2 = run_kara(kara, label, autocast)
        except JobCancelled:
            raise
        except Exception as e:
            if _stalled(e) or not (hq and autocast) or "out of memory" in str(e).lower() or _no_worker(e):
                raise
            log_error("autocast separation", e)
            outs2 = run_kara(kara, label, False)
            # 混合精度在這張顯示卡上出問題：之後都改用一般精度（fork：一般精度重試成功才算，兩種都失敗不是精度的問題）
            _AUTOCAST["bad"] = True
    except JobCancelled:
        raise
    except Exception as e:
        # fork：顯示卡卡住交給 step_separate 改用 CPU（原本會在顯示卡上改用標準模型、不設 live_light）；
        # 36：重疊處理時記憶體不夠交給佇列重做
        if not hq or _stalled(e) or _overlap_oom(e) or _no_worker(e):
            raise
        # 高品質模型下載失敗或顯示卡記憶體不夠：改用標準模型，不要整首失敗
        print(f"[separate] 高品質主唱分離失敗，改用標準模型：{type(e).__name__}: {e}", flush=True)
        if "out of memory" not in str(e).lower():
            _check_model_file(HQ_KARAOKE)
            _HQ_FAIL["kara"] += 1
            _hq_fail_note(d, "主唱", e)
        kara, missed = cfg["karaoke_model"], True
        report("把人聲分成主唱與合音（標準）", 0.35)
        ensure_model(kara, _dl_report(report, "主唱分離模型", 0.35))
        outs2 = run_kara(kara, "把人聲分成主唱與合音（標準）", autocast)
    with _META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
        meta = read_meta(d)
        meta["karaoke"] = kara
        write_meta(d, meta)
    lead_p = _pick(outs2, "vocals")
    back_p = _pick(outs2, "instrumental")

    n = sf.info(str(stem_path(d, "mix"))).frames
    for name, p in (("lead", lead_p), ("back", back_p), ("inst", inst_p)):
        save_stem(d, name, fit_length(read_audio(p), n))
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        fix_lead_gaps(d)   # 2.3.2：疊錄的副歌被整段分到合音 → 搬回主唱
    except Exception as e:
        log_error("fix_lead_gaps", e)
    # 合音比重：合音越多，主唱和合音越難分乾淨，更精細的分離越有幫助
    try:
        h = harmony_ratio(d)
        used_hq = kara == HQ_KARAOKE
        update_meta(d, harmony=h, sep_overlap=overlap if used_hq else None,
                    fine_suggest=bool(used_hq and overlap < 4 and h is not None and h >= FINE_HARMONY))
    except Exception as e:
        log_error("harmony ratio", e)
    return missed


def separate_dsp(d: Path, report) -> None:
    """沒有 AI 模型時的備援：依聲像位置分離（中央=主唱）。"""
    import librosa
    from scipy.ndimage import uniform_filter1d

    report("以聲像位置分離（備援模式）", 0.1)
    mix = read_audio(stem_path(d, "mix"))
    n = mix.shape[1]
    N, H = 2048, 512
    L = librosa.stft(mix[0], n_fft=N, hop_length=H)
    R = librosa.stft(mix[1], n_fft=N, hop_length=H)
    P = np.abs(L) ** 2 + np.abs(R) ** 2 + 1e-12
    cross = L * np.conj(R)
    center = 2 * cross.real / P
    sim = 2 * np.abs(cross) / P
    f = librosa.fft_frequencies(sr=SR, n_fft=N)[:, None]
    band = np.where(f < 110, 0, np.where(f < 220, (f - 110) / 110,
                    np.where(f > 9000, np.maximum(0.25, 1 - (f - 9000) / 6000), 1)))
    x = np.clip((center - 0.67) / 0.25, 0, 1)
    x = x * x * (3 - 2 * x)
    ml = uniform_filter1d((x * band).astype(np.float32), 3, axis=1)
    vb = ((f > 180) & (f < 7000)).astype(np.float32)
    yy = np.clip((sim - 0.45) / 0.3, 0, 1)
    mb = uniform_filter1d(((1 - x) * yy * vb * (center > 0)).astype(np.float32), 3, axis=1)
    report("合成分軌", 0.3)
    lead = librosa.istft(ml * (L + R) / 2, hop_length=H, length=n)
    bl = librosa.istft(mb * L, hop_length=H, length=n)
    br = librosa.istft(mb * R, hop_length=H, length=n)
    back = np.stack([bl, br])
    inst = mix - lead[None, :] - back
    save_stem(d, "lead", np.stack([lead, lead]))
    save_stem(d, "back", back)
    save_stem(d, "inst", inst)


def is_acapella(d: Path) -> bool:
    """伴奏軌幾乎沒有聲音 → 原曲本來就是清唱／純人聲。"""
    try:
        def rms(name):
            y, _ = sf.read(str(stem_path(d, name)), dtype="float32", always_2d=True)
            return float(np.sqrt(np.mean(y ** 2)) + 1e-9)
        lead, inst = rms("lead"), rms("inst")
        return inst / lead < 0.12
    except Exception:
        return False


def step_separate(d: Path, cfg: dict, report) -> bool:
    """fork（69）：回傳這次實際上是不是沒用到要求的品質：用輕量設定（CPU＋標準模型）分離（顯示卡卡住時會中途改用），
    或要求高品質、卻因為高品質模型失敗改用了標準模型（Codex r5-3）。呼叫端依這個決定 live_light 能不能清掉。"""
    notes = []
    method = "dsp"
    light = bool(cfg.get("_cpu"))
    # fork（70）：分離會寫分軌，先讓 KEY 版本失效（刪不掉時留記號）、標記旋律待重做（step_pitch 做完才清），
    # 中途失敗或被關掉也不會把舊的當成有效；中途停止時背景會補做旋律
    invalidate_variants(d)
    with _META_LOCK:
        meta = read_meta(d)
        meta["pitch_stale"] = True
        # fork（Codex r2-4、r5-2）：上一次分離留下的說明在開始時清掉；做完時不再清，
        # 這次分離途中寫的（2.3.5 的「高品質…分離模型無法使用」）才留得住、畫面看得到
        meta["notes"] = [x for x in meta.get("notes", []) if "分離" not in x]
        write_meta(d, meta)
    if module_status()["uvr"]:
        try:
            try:
                missed = separate_uvr(d, cfg, report)
            except Exception as e:
                if type(e).__name__ != "GpuStalled" or cfg.get("_cpu"):
                    raise
                # 2.3.2：顯示卡被其他程式佔滿、分離沒有進度 → 改用 CPU＋標準模型重做，之後自動補做高品質
                print("[gpu] 顯示卡太忙，這首改用 CPU＋標準模型分離（之後自動補做高品質）", flush=True)
                cfg = dict(cfg, **LIGHT_CFG)
                light = True
                update_meta(d, live_light=True)
                report("顯示卡太忙：改用 CPU 輕量處理（之後自動補做高品質）", 0.08)
                missed = separate_uvr(d, cfg, report)
            method = "uvr"
            # fork（Codex r5-3）：這次途中高品質失敗改用標準模型、或高品質這次執行已停用，不算補做完成。
            # 看實際發生的事，不重算設定（重算會和 vocal_model 的判斷不一致）
            light = light or bool(missed)
        except JobCancelled:
            raise   # fork（22）：停止、讓位不是分離失敗，不改跑聲像分離
        except Exception as e:
            if _overlap_oom(e) or _no_worker(e):
                raise   # fork（36）：交給佇列一次一首重做；第 7 批：處理程式開不起來直接失敗
            traceback.print_exc()
            notes.append(f"AI 分離失敗，改用聲像分離：{e}")
            separate_dsp(d, report)
    else:
        notes.append("沒有安裝 audio-separator，使用聲像分離（品質較差）")
        separate_dsp(d, report)
    invalidate_variants(d)   # fork：分離期間做好的 KEY 版本（移調在分離之前就開始了）也是舊的
    acap = is_acapella(d)   # 要讀音訊，先在鎖外面算好
    with _META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
        meta = read_meta(d)
        meta["steps"]["separate"] = True
        meta["separation"] = method
        meta["acapella"] = acap
        meta["notes"] = meta.get("notes", []) + notes
        write_meta(d, meta)
    return light
