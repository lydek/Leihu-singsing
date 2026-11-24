"""練唱台：音訊處理流程。

每首歌存在 library/<id>/：
  original.*        匯入的原始檔（轉成 mix 後刪除）
  mix.flac          44.1kHz 立體聲（舊版是 .wav，照樣讀得到）
  lead.flac         主唱
  back.flac         合音
  inst.flac         伴奏
  pitch.json        主唱音高曲線與音符
  lyrics.lrc        歌詞（含時間）
  v2_k+2_s100/      移調／變速後的分軌快取
  takes/            你的錄音與評分
"""
from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

# 不常改的純運算搬到 leihu/（E 組步驟 4）；這裡轉出同樣的名稱，P.<名稱> 和 gpu_worker 的用法不變
from leihu.lyrics_text import (  # noqa: F401
    _CREDIT,
    _NOISE,
    _norm,
    align_to_recognized,
    clean_lrc,
    fmt_lrc_time,
    lyric_queries,
    parse_lrc,
    similarity,
    srt_to_lrc,
    to_lrc,
    to_simplified,
)
from leihu.pitch import (  # noqa: F401
    HOP,
    HOP_S,
    PITCH_SR,
    bridge_gaps,
    clean_pitch,
    fix_octaves,
    octave_low_score,
    segment_notes,
    trim_glides,
)

APP_NAME = "雷虎練唱台"
ROOT = Path(__file__).resolve().parent
PARTS_DIR = ROOT / "leihu" / "core"   # fork（拆檔，docs/pipeline-split-plan.md 方式一）：分出去的檔案
_PARTS: list[str] = []                # 已載入的分出去的檔案（依載入順序）


def _load_part(name: str) -> None:
    """fork（拆檔，docs/pipeline-split-plan.md 方式一）：把 PARTS_DIR/<name>.py 載入這個模組的命名空間，等於那段程式
    寫在呼叫的位置：P.<名稱> 是同一個物件，測試的替換、global 重新指派照常有效。呼叫要放在那段程式原本的位置
    （模組層級有載入時就執行的敘述）。__future__ 的 annotations 明確指定、不靠繼承，型別註記和原本一樣不在載入時執行。"""
    import __future__

    p = PARTS_DIR / f"{name}.py"
    code = compile(p.read_bytes(), str(p), "exec",   # 給 bytes：和 import 一樣處理 BOM、編碼宣告
                   flags=__future__.annotations.compiler_flag, dont_inherit=True)
    exec(code, globals())   # noqa: S102  內容是 repo 裡的程式，和寫在這裡相同
    _PARTS.append(name)

# S28：使用者資料（歌曲庫、設定、用量、歌單、logs）的根目錄。測試用 LIANTAI_DATA 指到暫存資料夾；沒設時就是程式資料夾。
# 只給伺服器（app.py）的測試用：桌面版、安裝程式不支援。相對路徑先轉成絕對路徑寫回環境變數，
# gpu_worker 子程式（cwd 是 ROOT）繼承後才會指到同一個地方
for _k in ("LIANTAI_DATA", "LIANTAI_DOWNLOADS"):
    if os.environ.get(_k):
        os.environ[_k] = str(Path(os.environ[_k]).resolve())
DATA = Path(os.environ["LIANTAI_DATA"]) if os.environ.get("LIANTAI_DATA") else ROOT
if DATA != ROOT:
    DATA.mkdir(parents=True, exist_ok=True)  # 指到還不存在的資料夾也能啟動
LIB = DATA / "library"
MODELS = ROOT / "models"
CONFIG_PATH = DATA / "config.json"
# S28：測試與契約快照用，沒設時和原本相同。NO_BACKGROUND：不跑啟動時的背景工作、不在背景下載高品質模型、
# 不裝套件、不升級 yt-dlp、不在背景探測顯示卡（佇列工作裡的模型下載由 NO_JOBS 一起關掉）；
# NO_JOBS：佇列只排隊不執行；NO_SPAWN：不開瀏覽器、檔案總管、安裝檔，改記到 DATA/spawn.log
NO_BACKGROUND = os.environ.get("LIANTAI_NO_BACKGROUND") == "1"
NO_JOBS = os.environ.get("LIANTAI_NO_JOBS") == "1"
NO_SPAWN = os.environ.get("LIANTAI_NO_SPAWN") == "1"
DEFAULT_LIB, DEFAULT_MODELS = LIB, MODELS


def _apply_storage() -> None:
    """2.3.0：歌曲庫、模型可以搬到別的位置（設定裡的 library_dir／models_dir；空的＝程式資料夾裡）。"""
    global LIB, MODELS
    try:
        c = json.loads(CONFIG_PATH.read_text("utf-8"))
    except Exception:
        c = {}
    LIB = Path(c["library_dir"]) if c.get("library_dir") else DEFAULT_LIB
    MODELS = Path(c["models_dir"]) if c.get("models_dir") else DEFAULT_MODELS


_apply_storage()


def spawned(kind: str, target: str) -> bool:
    """LIANTAI_NO_SPAWN=1 時，把要開的外部程式記到 DATA/spawn.log 並回傳 True，呼叫端就不真的開。"""
    if not NO_SPAWN:
        return False
    with open(DATA / "spawn.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, "target": target}, ensure_ascii=False) + "\n")
    return True

SR = 44100
STEMS = ("lead", "back", "inst")

DEFAULT_CONFIG = {
    "separation_model": "UVR-MDX-NET-Inst_HQ_3.onnx",
    "vocal_quality": "auto",   # 人聲／伴奏分離：auto：有顯示卡用高品質（BS-Roformer，乾淨很多但慢一倍）；standard；high
    "pause_while_streaming": True,   # 偵測到直播軟體（OBS、VTube Studio…）或直播模式開著時，暫停背景的分離、辨識
    "parallel_separation": False,
    "yt_enabled": False,     # 2.3.1：YouTube 下載與歌單批次匯入預設隱藏；點左上標題五下開啟後一直保持開啟（更新也不會關）
    "queue_paused": False,   # 2.1.1：使用者按了「全部暫停」（重開程式後仍維持暫停）   # 2.1.1：沒在直播時兩首同時分離（顯示卡記憶體 10 GB 以上建議）
    "streaming_apps": ["obs64.exe", "obs32.exe", "VTube Studio.exe", "Streamlabs OBS.exe", "XSplit.Core.exe", "vMix64.exe"],
    "karaoke_model": "UVR_MDXNET_KARA_2.onnx",
    "karaoke_quality": "auto",   # auto：有顯示卡用高品質（Roformer）；standard；high
    "karaoke_overlap": 2,
    "auto_fine": True,   # 合音多的歌，處理完自動排一次最精細的主唱分離（排在佇列最後）   # 高品質主唱分離的重疊次數：2＝快（建議），4＝模型預設、約慢一倍
    "whisper_model": "large-v3-turbo",
    "lyrics_language": "",   # 空字串＝自動判斷
    "convert_to_taiwan": True,
    "auto_recognize_lyrics": False,
    "global_hotkeys": True,   # 1.7：Ctrl+Alt 全域快捷鍵（重開練唱台後生效）   # 匯入時自動辨識歌詞（關閉時直接套用線上搜尋到的第一個版本）
    "audio_out": "",   # 音樂輸出裝置名稱；空字串＝系統預設
    "monitor_out": "",   # 「原唱只給我聽」時，自己聽的裝置（耳機）名稱
    "font": "huninn",   # 介面與歌詞字體：huninn（內建 jf open 粉圓）／kai（楷書）／sans（黑體）
    "pitch_model": "auto",
    "claude_model": "claude-sonnet-5-5",
    "anthropic_api_key": "",
    "balance_usd": None,
    "balance_set_at": None,
}


if sys.platform == "win32":
    # DLL 載入失敗時不要跳出需要按「確定」的視窗（會讓背景處理卡住），改成一般錯誤；子程序也會沿用
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass


class UserError(Exception):
    """可直接顯示給使用者的錯誤。"""


# ---------------------------------------------------------------- 設定

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
        except Exception:
            pass
    return cfg


def keep_corrupt(path: Path, valid) -> None:
    """要覆寫 path 之前呼叫（防止資料遺失）：內容壞掉時改名成 <檔名>.<時間>.bad 保留；
    檔案在但讀不到（例如被其他程式鎖住）時丟 UserError，不覆寫。不存在就什麼都不做。"""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as e:
        raise UserError(f"{path.name} 讀不到，這次沒有存檔（{e}）") from e
    try:
        ok = valid(json.loads(raw.decode("utf-8")))   # UnicodeDecodeError、JSONDecodeError 都是 ValueError
    except ValueError:
        ok = False
    if not ok:
        bad = path.with_name(f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.bad")
        try:
            path.replace(bad)
        except OSError as e:
            raise UserError(f"{path.name} 內容壞掉、無法改名保留，這次沒有存檔（{e}）") from e
        # 不記內容：config.json 裡有 API 金鑰
        log_error(f"{path.name} 內容壞掉，已改名保留成 {bad.name}", ValueError("內容不是有效的 JSON 或格式不對"))


def save_config(cfg: dict) -> None:
    # 壞掉的 config.json 讀起來是預設值，直接存會蓋掉原本的金鑰與設定：先改名保留
    keep_corrupt(CONFIG_PATH, lambda d: isinstance(d, dict))
    atomic_write_text(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))


def module_status() -> dict:
    """哪些離線功能已安裝。"""
    import importlib.util as u
    has = lambda m: u.find_spec(m) is not None
    return {
        "uvr": has("audio_separator"),
        "crepe": has("torchcrepe"),
        "whisper": has("faster_whisper"),
        "pedalboard": has("pedalboard"),
        "opencc": has("opencc"),
        "anthropic": has("anthropic"),
        "kana": has("sudachipy") and has("sudachidict_small"),
        "cuda": cuda_ok() if IN_WORKER else (gpu_info(block=False) or {}).get("ok", False),
    }


_CUDA_OK = None
CUDA_NOTE = ""
IN_WORKER = False   # True：這裡是顯示卡子程式（gpu_worker.py），可以載入 PyTorch／CUDA
_GPU_INFO: dict | None = None
_GPU_INFO_LOCK = threading.Lock()
_GPU_PROBING = {"on": False}


def _torch_version() -> str:
    try:
        from importlib.metadata import version
        return version("torch")
    except Exception:
        return ""


def gpu_info(block: bool = True) -> dict | None:
    """顯示卡能不能用（主程式不載入 CUDA，所以由子程式檢查一次，結果存在 models/gpu_probe.json；
    換了 PyTorch 版本才重新檢查）。block=False：還不知道時回傳 None，並在背景開始檢查。"""
    global _GPU_INFO, CUDA_NOTE
    if _GPU_INFO is not None:
        return _GPU_INFO
    tv = _torch_version()
    if not tv:
        _GPU_INFO = {"ok": False, "note": "", "cc": None, "torch": ""}
        return _GPU_INFO
    cache = MODELS / "gpu_probe.json"
    try:
        c = json.loads(cache.read_text("utf-8"))
        if c.get("torch") == tv and (not c.get("ok") or "vram" in c):   # 舊的紀錄沒有記憶體大小：重新檢查一次
            _GPU_INFO = c
            CUDA_NOTE = c.get("note") or ""
            return c
    except Exception:
        pass
    if not block:
        with _GPU_INFO_LOCK:
            if not _GPU_PROBING["on"] and not NO_BACKGROUND:   # S28：測試時不開 gpu_worker 子程式探測顯示卡
                _GPU_PROBING["on"] = True
                threading.Thread(target=gpu_info, daemon=True, name="gpu-probe").start()
        return None
    with _GPU_INFO_LOCK:
        if _GPU_INFO is not None:
            return _GPU_INFO
        import gpu_worker as G
        try:
            info = G.POOL.call("probe")
        except Exception as e:
            log_error("gpu probe", e)
            info = {"ok": False, "note": "", "cc": None}
        info["torch"] = tv
        try:
            MODELS.mkdir(parents=True, exist_ok=True)
            atomic_write_text(cache, json.dumps(info, ensure_ascii=False))
        except Exception:
            pass
        CUDA_NOTE = info.get("note") or ""
        _GPU_INFO = info
        return info


def cuda_ok() -> bool:
    """顯示卡能不能真的跑 PyTorch。

    torch.cuda.is_available() 只代表「有卡、有驅動」；如果裝的 PyTorch 不支援這張卡的架構
    （例如 GTX 10 系列配上 CUDA 12.8 版），實際運算時才會出現「no kernel image is available」。
    所以實際跑一個小運算確認，失敗就改用 CPU。結果只算一次。"""
    global _CUDA_OK, CUDA_NOTE
    if not IN_WORKER:   # 主程式不載入 CUDA：問顯示卡子程式（結果會存起來）
        return bool((gpu_info() or {}).get("ok"))
    if _CUDA_OK is not None:
        return _CUDA_OK
    try:
        import torch
        if not torch.cuda.is_available():
            _CUDA_OK = False
            return False
        x = torch.ones(64, device="cuda:0")
        _CUDA_OK = float((x * 2).sum().item()) == 128.0
    except Exception as e:
        _CUDA_OK = False
        try:
            import torch
            name = torch.cuda.get_device_name(0)
        except Exception:
            name = "顯示卡"
        CUDA_NOTE = (f"{name} 和目前安裝的 PyTorch 版本不相容，已改用 CPU 處理（比較慢）。"
                     "執行開始選單的「雷虎練唱台 修復與更新」可改裝相容的版本。")
        print(f"[cuda] 顯示卡無法使用，改用 CPU：{type(e).__name__}: {str(e)[:300]}", flush=True)
    return _CUDA_OK


# ---------------------------------------------------------------- 檔案

def unique_path(p: Path, siblings: tuple[str, ...] = ()) -> Path:
    """2.3.3：下載資料夾裡已經有同名的檔案（或資料夾）時，改成「名稱 (2)」「名稱 (3)」…，不覆蓋、也不會因為
    舊檔正被播放器開著而匯出失敗。siblings：同一次匯出會一起產生的其他副檔名（例如 .mp3、.mp4），一起避開。"""
    def taken(q: Path) -> bool:
        return q.exists() or any(q.with_name(q.name + s).exists() if s.startswith("_") else q.with_suffix(s).exists()
                                 for s in siblings)
    if not taken(p):
        return p
    stem, suf = (p.stem, p.suffix) if p.suffix and not p.is_dir() else (p.name, "")
    n = 2
    while True:
        q = p.with_name(f"{stem} ({n}){suf}")
        if not taken(q):
            return q
        n += 1


def downloads_dir() -> Path:
    """使用者的「下載」資料夾（Windows 會讀系統設定，處理被搬到其他磁碟的情況）。"""
    if os.environ.get("LIANTAI_DOWNLOADS"):
        return Path(os.environ["LIANTAI_DOWNLOADS"])
    if sys.platform == "win32":
        try:
            import ctypes
            guid = ctypes.c_byte * 16
            # FOLDERID_Downloads = {374DE290-123F-4565-9164-39C4925E467B}
            import uuid as _uuid
            raw = _uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le
            fid = guid(*raw)
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out)) == 0:
                path = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                return path
        except Exception:
            pass
    d = Path.home() / "Downloads"
    return d if d.exists() else Path.home()

def song_dir(song_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]{6,16}", song_id or ""):
        raise UserError("歌曲編號不正確")
    d = LIB / song_id
    if not d.exists():
        raise UserError("找不到這首歌")
    return d


# meta.json 會被好幾個執行緒同時讀寫（處理佇列、預先下載、背景歌詞搜尋、網頁請求）。
# 同時寫入同一個暫存檔會把兩份內容疊在一起，留下壞掉的 JSON，整個歌曲庫就讀不出來；
# 所以讀寫都排隊進行，每次寫入也用自己的暫存檔。
_META_LOCK = threading.RLock()


def _replace_retry(src: Path, dst: Path) -> None:
    """Windows 上目標檔剛好被別的程式（防毒、雲端同步、其他讀取）開著時，取代會失敗；稍等重試幾次。"""
    for i in range(6):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if i == 5:
                raise
            time.sleep(0.05 * (i + 1))


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """先寫到自己專用的暫存檔，再一次取代正式檔：寫到一半當機或同時寫入，都不會留下壞掉的檔案。"""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding)
        _replace_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_meta(d: Path) -> dict:
    p = d / "meta.json"
    with _META_LOCK:
        if not p.exists():
            return {}
        text = p.read_text("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            meta = _repair_meta(d, text, e)
            write_meta(d, meta)   # 修好後存回去，下次就正常了
            return meta


def _repair_meta(d: Path, text: str, err: Exception) -> dict:
    """1.6.1 以前同時寫入可能留下壞掉的 meta.json（兩份內容疊在一起）。
    先試著取出第一份完整的內容；完全讀不出來時，依資料夾裡的檔案重建基本資料，歌曲和錄音都不會不見。"""
    log_error(f"meta.json 壞掉，自動修復：{d.name}", err)
    try:
        meta, _ = json.JSONDecoder().raw_decode(text.lstrip())
        if isinstance(meta, dict) and meta.get("id"):
            return meta
    except Exception:
        pass
    m = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    title = json.loads(f'"{m.group(1)}"') if m else f"（資料損壞的歌曲 {d.name}）"
    has = lambda n: any((d / f"{n}{e}").exists() for e in (".flac", ".wav"))
    meta = {"id": d.name, "title": title[:120], "created": d.stat().st_mtime, "duration": None,
            "steps": {"import": has("mix"), "separate": has("lead") and has("back") and has("inst"),
                      "pitch": (d / "pitch.json").exists(), "lyrics": (d / "lyrics.lrc").exists()},
            "separation": "uvr" if has("lead") else None, "lyrics_source": None,
            "notes": ["這首歌的資料檔曾經損壞，已自動修復；如果歌名或設定不對，可以用「編輯資訊」修改。"]}
    for key in ("source_url", "artist"):   # 能找到的欄位盡量救回來（還沒下載的歌靠 source_url 才能接著下載）
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m:
            meta[key] = json.loads(f'"{m.group(1)}"')
    m = re.search(r'"key"\s*:\s*(-?\d+)', text)
    if m and -6 <= int(m.group(1)) <= 6:
        meta["key"] = int(m.group(1))
    if meta.get("source_url") and not meta["steps"]["import"]:
        meta["dl_state"] = "failed"   # 還沒下載的歌：標成下載失敗，按「重新下載」就能接著下載
        meta["title"] = _DL_RE.sub("", meta["title"]) + "（下載失敗）"
    return meta


def write_meta(d: Path, meta: dict) -> None:
    with _META_LOCK:
        atomic_write_text(d / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))


def update_meta(d: Path, **kw) -> dict:
    with _META_LOCK:   # 讀出、修改、寫回之間不讓別的執行緒插進來，免得蓋掉對方的修改
        meta = read_meta(d)
        meta.update(kw)
        write_meta(d, meta)
        return meta


_SONG_CACHE: dict = {}   # 歌曲編號 → (檔案時間, 整理好的資料)


def _song_entry(d: Path) -> dict | None:
    """一首歌在歌曲庫清單裡的資料。資料檔、歌詞、錄音都沒變時直接用上次整理好的（幾百首歌時差很多）。"""
    def mt(p: Path) -> int:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0
    stamp = (mt(d / "meta.json"), mt(d / "lyrics.lrc"), mt(d / "takes"))
    hit = _SONG_CACHE.get(d.name)
    if hit and hit[0] == stamp:
        return dict(hit[1])
    try:
        m = read_meta(d)
    except Exception as e:   # 一首歌的資料讀不出來，不要讓整個歌曲庫打不開
        log_error(f"list_songs {d.name}", e)
        return None
    rc = m.get("lyrics_recheck")
    if isinstance(rc, dict):
        try:
            if (d / "lyrics.lrc").stat().st_mtime_ns != rc.get("mtime"):
                m["lyrics_recheck"] = None   # 歌詞改過了
        except OSError:
            m["lyrics_recheck"] = None
    takes = list_takes(d)
    m["takes"] = len(takes)
    scored = [t["acc"] for t in takes if t.get("acc") is not None]
    m["best"] = max(scored) if scored else None
    lp = d / "lyrics.lrc"
    if lp.exists():
        try:
            m["lyrics_text"] = " ".join(l["text"] for l in parse_lrc(lp.read_text("utf-8")))[:3000]
        except Exception:
            pass
    _SONG_CACHE[d.name] = (stamp, m)
    return dict(m)


def usage_score(m: dict, setlist_ids: set | None = None) -> float:
    """這首歌常不常唱（2.1.1）：大量重新處理時，常唱的排前面。
    看最近開過／直播唱過的時間、直播唱過幾次、錄音幾次、有沒有自己整理過歌詞或分類。"""
    now = time.time()
    score = 0.0
    t = max(m.get("last_opened") or 0, m.get("last_sung") or 0)
    if t:
        score += 1000 + max(0.0, 180 - (now - t) / 86400)   # 最近半年內開過，越近越前面
    if setlist_ids and m.get("id") in setlist_ids:
        score += 500
    score += 20 * (m.get("sung") or 0) + 10 * min(20, m.get("takes") or 0)
    if m.get("key"):
        score += 30   # 設過自己的 KEY：一定是會唱的歌
    if m.get("lyrics_source") in ("online", "import", "claude"):
        score += 5
    if m.get("category"):
        score += 2
    return score


def list_songs() -> list[dict]:
    LIB.mkdir(exist_ok=True)
    out = []
    for d in LIB.iterdir():
        if d.is_dir() and (d / "meta.json").exists():
            m = _song_entry(d)
            if m is not None:
                out.append(m)
    out.sort(key=lambda m: m.get("created", 0), reverse=True)
    return out


# ---------------------------------------------------------------- 歌曲庫搜尋（繁簡互通＋相近歌名）

_T2S = None
_SKEY_CACHE: dict = {}


def search_key(text: str) -> str:
    """搜尋用的正規化：全形半形統一、轉小寫、繁體一律轉成簡體（繁簡多對一，兩邊都會對到同一個字）。"""
    global _T2S
    import unicodedata
    t = unicodedata.normalize("NFKC", text or "").lower()
    if re.search(r"[\u3400-\u9fff]", t):
        if _T2S is None:
            try:
                from opencc import OpenCC
                _T2S = OpenCC("t2s")
            except Exception:
                _T2S = False
        if _T2S:
            t = _T2S.convert(t)
    return t


def _song_search_key(m: dict) -> str:
    hay = " ".join(str(x) for x in (m.get("title"), m.get("artist"), m.get("yt_title"), m.get("yt_artist"), m.get("yt_track"), m.get("yt_uploader"),
                                    m.get("category"), m.get("lyrics_text"),
                                    (m.get("lyrics_online") or {}).get("artist") if isinstance(m.get("lyrics_online"), dict) else None)
                   if x)
    k = _SKEY_CACHE.get(hay)
    if k is None:
        k = _SKEY_CACHE[hay] = search_key(hay)
        if len(_SKEY_CACHE) > 5000:
            _SKEY_CACHE.clear()
    return k


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def _skey(*parts) -> str:
    hay = " ".join(str(x) for x in parts if x)
    k = _SKEY_CACHE.get(hay)
    if k is None:
        k = _SKEY_CACHE[hay] = search_key(hay)
        if len(_SKEY_CACHE) > 5000:
            _SKEY_CACHE.clear()
    return k


def _lyric_snip(text: str, words: list[str]) -> str:
    """歌詞裡符合的那一句（給搜尋結果顯示）。"""
    if not text:
        return ""
    norm = search_key(text)
    pos = norm.find(words[0]) if words else -1
    if pos < 0:
        return ""
    src = text if len(text) == len(norm) else norm   # 繁簡轉換長度不同時，退回顯示轉換後的文字
    a = src.rfind(" ", 0, pos) + 1
    b = src.find(" ", pos + len(words[0]))
    b = len(src) if b < 0 else b
    a, b = max(a, pos - 14), min(b, pos + len(words[0]) + 14)
    return ("…" if a > 0 else "") + src[a:b].strip() + ("…" if b < len(src) else "")


def search_songs(q: str, scope: str = "all") -> dict:
    """回傳符合的歌曲（每個關鍵字都要出現，繁簡互通），依符合的地方排序：
    歌名 → 歌手 → 歌名＋歌手 → 其他資訊 → 只有歌詞裡有（另外附上符合的那句歌詞），以及歌名相近的推薦。
    scope：name＝只找歌名、歌手；lyrics＝只找歌詞；all＝都找（歌詞符合的排最後）。"""
    from difflib import SequenceMatcher
    nq = search_key(q).strip()
    if not nq:
        return {"match": [], "similar": [], "kind": {}, "snip": {}}
    words = nq.split()
    squash = lambda s: re.sub(r"[\W_]+", "", s)
    qq = squash(nq)

    def has(key: str) -> bool:
        # 也比對去掉空白、標點後的版本（「牛仔很忙cowboy」找得到「牛仔很忙 Cowboy on the Run」）
        return all(w in key for w in words) or (len(qq) >= 2 and qq in squash(key))
    ranked, sim, kind, snip = [], [], {}, {}
    for n, m in enumerate(list_songs()):
        lo = m.get("lyrics_online") if isinstance(m.get("lyrics_online"), dict) else {}
        tkey = _skey(m.get("title"), m.get("yt_track"))
        akey = _skey(m.get("artist"), m.get("yt_artist"), lo.get("artist"), m.get("yt_uploader"))
        r = None
        if scope == "lyrics":
            lyr = _skey(m.get("lyrics_text"))
            if has(lyr):
                ranked.append((5, n, m["id"]))
                kind[m["id"]] = "lyrics"
                snip[m["id"]] = _lyric_snip(m.get("lyrics_text") or "", words)
            continue
        if has(tkey):
            r = 0 if squash(tkey) == qq else 1
        elif has(akey):
            r = 2
        elif has(tkey + " " + akey):
            r = 3
        elif has(_skey(m.get("title"), m.get("yt_track"), m.get("artist"), m.get("yt_artist"), lo.get("artist"),
                       m.get("yt_uploader"), m.get("yt_title"), m.get("category"))):
            r = 4
        elif scope != "name" and has(_song_search_key(m)):
            r = 5
            kind[m["id"]] = "lyrics"
            snip[m["id"]] = _lyric_snip(m.get("lyrics_text") or "", words)
        if r is not None:
            ranked.append((r, n, m["id"]))
            continue
        if len(qq) < 2:
            continue
        best = 0.0
        for name in (m.get("title"), m.get("yt_track")):
            tk = squash(search_key(name or ""))
            if not tk:
                continue
            ratio = SequenceMatcher(None, qq, tk).ratio()
            cover = len(_bigrams(qq) & _bigrams(tk)) / len(_bigrams(qq))   # 輸入的字有多少出現在歌名裡
            best = max(best, ratio, cover * 0.9)
        if best >= 0.45:
            sim.append((best, m["id"]))
    ranked.sort()
    sim.sort(reverse=True)
    return {"match": [i for _, _, i in ranked], "similar": [i for _, i in sim[:6]], "kind": kind, "snip": snip}


def _blank_song(title: str, **extra) -> str:
    LIB.mkdir(exist_ok=True)
    sid = uuid.uuid4().hex[:10]
    d = LIB / sid
    d.mkdir()
    write_meta(d, {
        "id": sid, "title": title, "created": time.time(), "duration": None,
        "steps": {"import": False, "separate": False, "pitch": False, "lyrics": False},
        "separation": None, "lyrics_source": None, "notes": [], **extra,
    })
    return sid


def new_song(filename: str, data_path: Path) -> str:
    sid = _blank_song(Path(filename).stem)
    ext = Path(filename).suffix.lower() or ".wav"
    shutil.move(str(data_path), LIB / sid / f"original{ext}")
    return sid


# ---------------------------------------------------------------- ffmpeg 與 YouTube

_load_part("batch_import")


# ---------------------------------------------------------------- 曲名／歌手／預設 KEY

LANG_FROM_SHEET = [   # 表單「語言」欄 → 歌詞辨識語言、分類名稱
    (("國語", "華語", "中文", "普通話", "mandarin", "chinese"), "zh", "中文歌"),
    (("台語", "閩南語", "台灣話", "taiwanese", "hokkien"), "zh", "台語歌"),
    (("粵語", "廣東話", "cantonese"), "yue", "粵語歌"),
    (("日語", "日文", "日本", "japanese"), "ja", "日文歌"),
    (("英語", "英文", "english"), "en", "英文歌"),
    (("韓語", "韓文", "韓國", "korean"), "ko", "韓文歌"),
]


def sheet_language(text: str) -> tuple[str, str] | None:
    t = (text or "").strip().lower()
    for words, code, cat in LANG_FROM_SHEET:
        if t and any(w in t for w in words):
            return code, cat
    return None


def parse_key(text) -> int | None:
    """表單裡的 KEY：-4、+2、4、＋３、=4（＝升 4）這類純數字才算，超過 ±6 的不採用。"""
    import unicodedata
    t = unicodedata.normalize("NFKC", str(text or "")).strip().replace("−", "-")
    if t.startswith("="):
        t = t[1:].strip()
    if not re.fullmatch(r"[+-]?\d{1,2}(?:\.0+)?", t):
        return None
    k = int(float(t))
    return k if -6 <= k <= 6 else None


_NOISE_RE = re.compile(r"\s*[\(（\[【「]?\s*(?:official\s*(?:music\s*)?(?:video|mv|audio|lyric(?:s)?\s*video)|"
                       r"music\s*video|lyric(?:s)?\s*video|官方\s*(?:完整版\s*)?(?:mv|音樂錄影帶|版)?|完整版|"
                       r"動態歌詞(?:版)?|歌詞版|高音質|hd|4k|mv|audio)\s*[\)）\]】」]?\s*", re.I)


def split_artist_title(yt_title: str, track: str = "", artist: str = "") -> tuple[str, str]:
    """從 YouTube 的資訊拆出（歌手, 曲名）。YouTube 有提供曲名就用；沒有就從標題常見寫法拆，拆不出來歌手留空。"""
    if track:
        return (artist or "").split(",")[0].strip(), track.strip()
    t = (yt_title or "").strip()
    m = re.match(r"^(.+?)\s*[【《「『〈]([^】》」』〉]+)[】》」』〉](.*)$", t)
    if m and not _NOISE_RE.fullmatch(m.group(2)):
        return m.group(1).strip(" -｜|/"), m.group(2).strip()
    clean = _NOISE_RE.sub(" ", t).strip()
    m = re.match(r"^(.+?)\s+[-–—｜|]\s+(.+)$", clean)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", re.sub(r"\s+", " ", clean).strip() or t


# YouTube 下載的狀態：排隊中／失敗／取消。寫在 meta 的 dl_state，歌名後面也標出來；下載成功就拿掉
_load_part("youtube")


def read_audio(path: Path, sr: int = SR, mono: bool = False, fallback: bool = True) -> np.ndarray:
    """回傳 (ch, n) 或 mono 時 (n,) 的 float32。
    fallback=False（fork）：讀自己的分軌時，soundfile 讀不了就報錯，不退回 audioread（可能讀出殘缺的音訊又不報錯）。
    傳 SoundFile 物件給 librosa 走的是同一段讀法，數值和預設完全相同。"""
    import librosa
    if path.suffix.lower() not in (".wav", ".flac", ".ogg"):
        ffmpeg_path()  # m4a、mp3 等格式需要 ffmpeg
    y, _ = librosa.load(str(path) if fallback else sf.SoundFile(str(path)), sr=sr, mono=mono)
    y = np.asarray(y, dtype=np.float32)
    if mono:
        return y
    if y.ndim == 1:
        y = np.stack([y, y])
    return y[:2]


def write_wav(path: Path, y: np.ndarray, sr: int = SR) -> None:
    """寫成 16 位元音訊；副檔名是 .flac 就存成 FLAC（無損壓縮，大小約 WAV 的一半，音質完全相同）。"""
    data = y.T if y.ndim == 2 else y
    sf.write(str(path), np.clip(data, -1.0, 1.0), sr, subtype="PCM_16")


# 分軌（mix／lead／back／inst，含移調版本）存成 FLAC；舊的 WAV 照樣讀得到，可以在設定裡一次轉換。
STEM_EXT = ".flac"


def stem_path(base: Path, name: str) -> Path:
    """某個分軌的檔案：有 FLAC 用 FLAC，沒有再找舊的 WAV；都沒有時回傳要寫入的 FLAC 路徑。"""
    for ext in (".flac", ".wav"):
        p = base / f"{name}{ext}"
        if p.exists():
            return p
    return base / f"{name}{STEM_EXT}"


def storage_report() -> dict:
    """歌曲庫佔用的空間，以及轉成 FLAC、刪掉原始檔後大約可以省多少。"""
    total = convertible = removable = 0
    songs = 0
    for d in LIB.iterdir() if LIB.exists() else []:
        if not d.is_dir() or d.name.startswith("."):   # fork（複審 P3-1）：.trash 不是歌
            continue
        songs += 1
        for f in d.rglob("*"):
            if not f.is_file():
                continue
            sz = f.stat().st_size
            total += sz
            if f.suffix == ".wav" and f.stem in ("mix", *STEMS) and f.parent.name != "takes":
                convertible += sz
            elif f.name.startswith("original.") and (stem_path(d, "mix").exists()):
                removable += sz
            elif f.parent.name.startswith("v_k"):
                removable += sz
    # FLAC 通常是 WAV 的 40～60%，用 50% 估
    return {"songs": songs, "bytes": total, "convertible": convertible, "removable": removable,
            "saving": int(convertible * 0.5) + removable}


def compact_song(d: Path, busy=lambda: False) -> int:
    """把一首歌的舊 WAV 分軌（含移調版本）轉成 FLAC、刪掉已經不需要的原始檔。回傳省下的位元組。
    轉換是逐位元相同的無損壓縮，先寫暫存檔、確認長度一樣才取代原檔。"""
    saved = 0
    meta = read_meta(d)
    bases = [d] + [v for v in d.iterdir() if v.is_dir() and v.name.startswith("v2_")]
    for base in bases:
        if any(base.glob("*.tmp.*")) or busy():   # 正在產生的移調版本：這次先跳過
            continue
        for name in ("mix", *STEMS):
            w = base / f"{name}.wav"
            if not w.exists():
                continue
            if (base / f"{name}.flac").exists():   # fork：已經有 FLAC（新的分軌，舊 WAV 當時刪不掉）：以 FLAC 為準，不能拿舊 WAV 蓋掉
                before = w.stat().st_size
                w.unlink()
                saved += before
                continue
            info = sf.info(str(w))
            y, sr = sf.read(str(w), dtype="int16" if info.subtype == "PCM_16" else "float32", always_2d=True)
            tmp = base / f"{name}.tmp.flac"
            sf.write(str(tmp), y, sr, subtype="PCM_16")
            if sf.info(str(tmp)).frames != info.frames:
                tmp.unlink(missing_ok=True)
                raise IOError(f"{w.name} 轉換後長度不符，保留原檔")
            before = w.stat().st_size
            tmp.replace(base / f"{name}.flac")
            w.unlink()
            saved += before - (base / f"{name}.flac").stat().st_size
    for v in [v for v in d.iterdir() if v.is_dir() and v.name.startswith("v_k")]:   # 1.3 以前的移調版本，已經用不到
        saved += sum(f.stat().st_size for f in v.rglob("*") if f.is_file())
        shutil.rmtree(v, ignore_errors=True)
    if (meta.get("steps") or {}).get("import") and stem_path(d, "mix").exists():
        for f in d.glob("original.*"):
            saved += f.stat().st_size
            f.unlink(missing_ok=True)
    return saved


def compact_library(report, busy_song=lambda sid: False) -> None:
    dirs = [d for d in LIB.iterdir() if d.is_dir() and (d / "meta.json").exists()]
    saved, errors = 0, 0
    for i, d in enumerate(dirs):
        report(f"轉成 FLAC 節省空間 {i + 1}/{len(dirs)}（已省 {saved / 1e9:.1f} GB）", i / max(1, len(dirs)))
        try:
            saved += compact_song(d, lambda: busy_song(d.name))
        except Exception as e:
            errors += 1
            log_error(f"compact {d.name}", e)
    report(f"完成：省下 {saved / 1e9:.1f} GB" + (f"，{errors} 首沒有轉換（詳見紀錄）" if errors else ""), 1.0)


def save_stem(base: Path, name: str, y: np.ndarray, sr: int = SR) -> Path:
    """寫入分軌（先寫暫存檔再改名，中途中斷不會留下壞檔），並刪掉同名的舊 WAV。"""
    out = base / f"{name}{STEM_EXT}"
    tmp = base / f"{name}.tmp{STEM_EXT}"
    write_wav(tmp, y, sr)
    _replace_retry(tmp, out)
    if STEM_EXT != ".wav":
        (base / f"{name}.wav").unlink(missing_ok=True)
    return out


def _replace_stems(base: Path, stems: dict, sr: int = SR) -> None:
    """fork（70）：幾軌一起換（主唱補回同時改主唱、合音）。全部先寫好暫存檔才換；換之前複製一份原檔，
    換到一半失敗就換回原本的，不會留下一新一舊。用複製不用改名：程式在任何時間點被關掉，分軌檔都還在。
    shortcut: 兩次改名之間被強制關閉（或換回也失敗）仍可能一新一舊，沒有開機復原；先標記 KEY 版本失效、
    旋律待重做只會讓它們重做，修不好一新一舊的分軌（要重新分離；換不回的原檔在 .bak）。分軌都能從 mix
    重新分離，所以先接受；要完全避免時改成寫交易紀錄、開機時依紀錄復原。"""
    tmps: dict[str, Path] = {}
    try:
        for name, y in stems.items():
            tmps[name] = base / f"{name}.tmp{STEM_EXT}"
            write_wav(tmps[name], y, sr)
        _swap_stems(base, tmps)
    finally:
        for t in tmps.values():   # 換成功的已經不在了
            t.unlink(missing_ok=True)
    if STEM_EXT != ".wav":
        for name in stems:
            try:   # 新的 FLAC 已經生效（stem_path 先找 FLAC）；舊 WAV 刪不掉只是佔空間，不能讓呼叫端以為沒換成
                (base / f"{name}.wav").unlink(missing_ok=True)
            except OSError as e:
                log_error(f"replace_stems {name}.wav", e)


def _swap_stems(base: Path, tmps: dict) -> None:
    """暫存檔換成正式檔；換到一半失敗就把已經換掉的換回原本的。"""
    baks: dict[str, Path] = {}
    done: list[str] = []
    try:
        for name, t in tmps.items():
            out = base / f"{name}{STEM_EXT}"
            if out.exists():   # 原本是舊的 WAV 時新檔寫到 FLAC，不會蓋到它，不用備份
                baks[name] = out.with_name(out.name + ".bak")
                shutil.copy2(out, baks[name])
            _replace_retry(t, out)
            done.append(name)
    except BaseException:
        for name in done:   # 每一軌各自盡力換回；換不回的備份檔留在磁碟上（不在 baks 裡，下面不會刪）
            out = base / f"{name}{STEM_EXT}"
            try:
                if name in baks:
                    _replace_retry(baks.pop(name), out)
                else:
                    out.unlink(missing_ok=True)   # 舊的 WAV 還在，stem_path 會再用它
            except OSError as e:
                log_error(f"replace_stems 換回 {name}", e)
        raise
    finally:
        for p in baks.values():
            try:   # 已經換好（或換回）了，備份刪不掉只是佔空間，不能改變結果
                p.unlink(missing_ok=True)
            except OSError as e:
                log_error(f"replace_stems 刪備份 {p.name}", e)


def fit_length(y: np.ndarray, n: int) -> np.ndarray:
    if y.ndim == 1:
        y = np.stack([y, y])
    if y.shape[1] >= n:
        return y[:, :n]
    return np.pad(y, ((0, 0), (0, n - y.shape[1])))


# ---------------------------------------------------------------- 匯入

# ---------------------------------------------------------------- 音量標準化
# 每首歌的響度差很多（YouTube 抓下來的歌常常一首很大聲、一首很小聲）。
# 匯入時用 ITU-R BS.1770（和 YouTube、Spotify 相同的響度標準）量出整首歌的響度，
# 播放時把主唱／合音／伴奏一起調到目標響度。不改動原始檔，關掉「音量標準化」就是原本的音量。
TARGET_LUFS = -16.0


def _biquad(b0, b1, b2, a0, a1, a2):
    return np.array([b0, b1, b2]) / a0, np.array([a0, a1, a2]) / a0


def _k_weight(sr: int):
    """BS.1770 的 K 加權濾波器（高架＋高通），依取樣率計算係數。"""
    import math
    # 第一段：高架濾波器（模擬頭部對高頻的影響）
    G, Q, fc = 3.999843853973347, 0.7071752369554196, 1681.974450955533
    A = 10 ** (G / 40)
    w0 = 2 * math.pi * fc / sr
    alpha = math.sin(w0) / (2 * Q)
    sh = _biquad(A * ((A + 1) + (A - 1) * math.cos(w0) + 2 * math.sqrt(A) * alpha),
                 -2 * A * ((A - 1) + (A + 1) * math.cos(w0)),
                 A * ((A + 1) + (A - 1) * math.cos(w0) - 2 * math.sqrt(A) * alpha),
                 (A + 1) - (A - 1) * math.cos(w0) + 2 * math.sqrt(A) * alpha,
                 2 * ((A - 1) - (A + 1) * math.cos(w0)),
                 (A + 1) - (A - 1) * math.cos(w0) - 2 * math.sqrt(A) * alpha)
    # 第二段：高通濾波器（去掉極低頻）
    Q, fc = 0.5003270373238773, 38.13547087602444
    w0 = 2 * math.pi * fc / sr
    alpha = math.sin(w0) / (2 * Q)
    hp = _biquad((1 + math.cos(w0)) / 2, -(1 + math.cos(w0)), (1 + math.cos(w0)) / 2,
                 1 + alpha, -2 * math.cos(w0), 1 - alpha)
    return sh, hp


def _block_power(y: np.ndarray, sr: int) -> np.ndarray | None:
    """K 加權後每個 400 ms 區塊（重疊 75%）的功率，左右聲道相加。"""
    from scipy.signal import lfilter
    x = np.atleast_2d(np.asarray(y, dtype=np.float64))
    (b1, a1), (b2, a2) = _k_weight(sr)
    x = lfilter(b2, a2, lfilter(b1, a1, x, axis=1), axis=1)
    block, step = int(0.4 * sr), int(0.1 * sr)
    if x.shape[1] < block:
        return None
    n = 1 + (x.shape[1] - block) // step
    p2 = np.cumsum(np.concatenate([np.zeros((x.shape[0], 1)), x ** 2], axis=1), axis=1)
    idx = np.arange(n) * step
    z = (p2[:, idx + block] - p2[:, idx]) / block          # 每個區塊、每個聲道的均方值
    return z.sum(axis=0)                                    # 左右聲道權重都是 1


def _gate(zsum: np.ndarray) -> np.ndarray | None:
    """BS.1770 的兩段門檻，回傳要計入的區塊。"""
    lk = -0.691 + 10 * np.log10(np.maximum(zsum, 1e-12))
    keep = lk > -70
    if not keep.any():
        return None
    rel = -0.691 + 10 * np.log10(zsum[keep].mean()) - 10
    keep &= lk > rel
    return keep if keep.any() else None


def integrated_loudness(y: np.ndarray, sr: int) -> float | None:
    """整首歌的響度（LUFS）。y 為 (聲道, 取樣) 或單聲道。太安靜量不到時回傳 None。"""
    zsum = _block_power(y, sr)
    if zsum is None:
        return None
    lk = -0.691 + 10 * np.log10(np.maximum(zsum, 1e-12))
    keep = lk > -70                                         # 絕對門檻：去掉靜音
    if not keep.any():
        return None
    rel = -0.691 + 10 * np.log10(zsum[keep].mean()) - 10    # 相對門檻：去掉比平均小 10 LU 的段落
    keep &= lk > rel
    if not keep.any():
        return None
    return round(float(-0.691 + 10 * np.log10(zsum[keep].mean())), 2)


def measure_loudness(d: Path) -> dict:
    """量 mix.wav 的響度與峰值，記在 meta（只算一次）。"""
    meta = read_meta(d)
    if meta.get("lufs") is not None or not stem_path(d, "mix").exists():
        return meta
    try:
        y = read_audio(stem_path(d, "mix"))
        lufs = integrated_loudness(y, SR)
        peak = float(np.max(np.abs(y))) if y.size else 0.0
        return update_meta(d, lufs=lufs, peak_db=round(20 * np.log10(max(peak, 1e-6)), 2))
    except Exception:
        traceback.print_exc()
        return meta


NORM_MIN_DB, NORM_MAX_DB = -15.0, 12.0


def _stems_sig(d: Path) -> str:
    try:
        return "|".join(str(stem_path(d, k).stat().st_mtime_ns) for k in ("mix", *STEMS))
    except OSError:
        return ""


def measure_stem_loudness(d: Path) -> dict:
    """1.6.5：量「主唱／合音／伴奏」每一種開關組合的響度（共 7 種）。
    只用整首歌的響度來調音量時，關掉原唱（卡拉OK）後的音量會差很多：
    人聲很突出的歌，拿掉人聲後伴奏會小聲很多。分軌之間有相關性，不能把各軌響度直接相加，
    所以每種組合實際混起來量。分軌重新處理過（例如精細處理）會自動重算。"""
    meta = read_meta(d)
    sig = _stems_sig(d)
    if not sig or (meta.get("combo_lufs") and meta.get("combo_lufs_sig") == sig):
        return meta
    try:
        y = {k: read_audio(stem_path(d, k)) for k in STEMS}
        n = min(v.shape[1] for v in y.values())
        out = {}
        for mask in range(1, 8):
            key = "".join("1" if mask >> (2 - i) & 1 else "0" for i in range(3))   # 順序：主唱、合音、伴奏
            mix = sum(y[k][:, :n] for k, b in zip(STEMS, key) if b == "1")
            v = integrated_loudness(mix, SR)
            out[key] = None if v is None else round(float(v), 2)
        return update_meta(d, combo_lufs=out, combo_lufs_sig=sig)
    except Exception:
        traceback.print_exc()
        return meta


def norm_gain_db(meta: dict, gains: dict | None = None) -> float:
    """播放時要加減幾 dB 才會到目標響度。小聲的歌最多加 12 dB（超過的部分由播放端的限幅器處理）。
    gains 是各音軌的音量：有組合響度時，用開著的音軌（音量 ≥ 0.5）那一種組合的響度計算，
    所以卡拉OK（關掉原唱）時伴奏也會調到一致的大小；拉桿的細部調整仍然照你設的比例。"""
    combo = meta.get("combo_lufs") or {}
    if gains is not None and combo:
        key = "".join("1" if (gains.get(k, 1) or 0) >= 0.5 else "0" for k in STEMS)
        if combo.get(key) is not None:
            return round(float(np.clip(TARGET_LUFS - combo[key], NORM_MIN_DB, NORM_MAX_DB)), 2)
    lufs = meta.get("lufs")
    if lufs is None:
        return 0.0
    return round(float(np.clip(TARGET_LUFS - lufs, NORM_MIN_DB, NORM_MAX_DB)), 2)


def step_import(d: Path, report) -> None:
    report("讀取音訊", 0.02)
    src = next(d.glob("original.*"))
    try:
        y = read_audio(src)
    except Exception as e:
        raise UserError(
            f"無法讀取這個音訊檔（{src.suffix}）。wav、mp3、flac、ogg 可直接讀；"
            "m4a/aac 需要先安裝 ffmpeg。") from e
    save_stem(d, "mix", y)
    for f in d.glob("original.*"):   # mix 已經是同一首歌（統一成 44.1 kHz），原始檔不用再留
        f.unlink(missing_ok=True)
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    lufs = integrated_loudness(y, SR)
    with _META_LOCK:   # fork：讀出 steps 到寫回之間不讓別的執行緒改，免得蓋掉別人剛記下的步驟
        steps = read_meta(d).get("steps", {})
        steps["import"] = True
        update_meta(d, duration=round(y.shape[1] / SR, 3), steps=steps,
                    lufs=lufs, peak_db=round(20 * np.log10(max(peak, 1e-6)), 2))


# ---------------------------------------------------------------- 分離

_load_part("separation")


# ---------------------------------------------------------------- 音高

def track_pitch(y16: np.ndarray, cfg: dict | None = None, strict: bool = False) -> np.ndarray:
    """16 kHz 單聲道 → 每 10 ms 一個 MIDI 音高（無聲為 NaN）。
    2.1.1：用 torchcrepe（顯示卡）時在子程式裡跑，主程式不載入 CUDA。"""
    cfg = cfg or load_config()
    if not IN_WORKER and module_status()["crepe"]:
        import gpu_worker as G
        # 直播中（含錄音評分）：在只用 CPU 的處理程式裡分析，不跟直播軟體、VTube Studio 搶顯示卡
        cpu = bool(cfg.get("_cpu")) or bool(live_status(cfg)["hold"])
        if strict and not cpu and not parallel_ok() and G._GPU_GATE.locked():
            cpu = True   # 錄音評分不要等顯示卡上的分離做完：先用 CPU 分析
        return G.call("pitch", np.ascontiguousarray(y16, dtype=np.float32), cfg, strict, cpu=cpu)
    return _track_pitch_local(y16, cfg, strict)


def _track_pitch_local(y16: np.ndarray, cfg: dict | None = None, strict: bool = False) -> np.ndarray:
    """16 kHz 單聲道 → 每 10 ms 一個 MIDI 音高（無聲為 NaN）。

    strict=True 用於麥克風錄音：門檻較嚴、依背景噪音決定靜音門檻，並去掉每個音頭尾的滑落／滑入
    （聲音變弱時音高分析會往下掉，畫出來像垂直往下的線，也會拉低評分）。"""
    import librosa
    cfg = cfg or load_config()
    n_frames = 1 + len(y16) // HOP
    f0 = per = None
    if module_status()["crepe"]:
        import torch
        import torchcrepe
        dev = "cuda:0" if cuda_ok() else "cpu"
        model = cfg.get("pitch_model", "auto")
        if model not in ("full", "tiny"):
            model = "full" if dev != "cpu" else "tiny"   # CPU 跑 full 太慢
        x = torch.from_numpy(np.ascontiguousarray(y16)).unsqueeze(0)
        with torch.no_grad():
            f, p = torchcrepe.predict(
                x, PITCH_SR, HOP, 65.0, 1100.0, model,
                decoder=torchcrepe.decode.viterbi, return_periodicity=True,
                batch_size=512, device=dev)
            p = torchcrepe.filter.median(p, 3)
        f0 = f[0].cpu().numpy()
        per = p[0].cpu().numpy()
        voiced = per > (0.6 if strict else 0.45)
        if not strict:
            # 原唱：把「確定有聲音」的段落往前後延伸到中等把握的影格（氣音、咬字、尾音），減少旋律斷斷續續
            weak = per > 0.22
            for _ in range(25):   # 最多延伸 0.25 秒
                grow = weak & ~voiced & (np.roll(voiced, 1) | np.roll(voiced, -1))
                if not grow.any():
                    break
                voiced = voiced | grow
    else:
        f0, voiced, _ = librosa.pyin(y16, fmin=65, fmax=1100, sr=PITCH_SR,
                                     frame_length=1024, hop_length=HOP)
    rms = librosa.feature.rms(y=y16, frame_length=1024, hop_length=HOP)[0]
    m = min(len(f0), len(rms), n_frames)
    f0, voiced, rms = f0[:m], voiced[:m], rms[:m]
    gate = rms < max(float(rms.max()) * 0.03, 1e-4)
    if strict and m:
        floor = float(np.percentile(rms, 10))   # 背景噪音
        gate |= rms < max(floor * 4, float(np.percentile(rms, 95)) * 0.06)
    midi = np.full(m, np.nan, dtype=np.float32)
    ok = voiced & ~gate & np.isfinite(f0) & (f0 > 0)
    midi[ok] = 69 + 12 * np.log2(f0[ok] / 440.0)
    if strict:
        midi[midi < 36] = np.nan   # 低於 C2 不可能是歌聲
        midi = trim_glides(midi)
        return clean_pitch(midi)
    return fix_octaves(bridge_gaps(clean_pitch(midi)), rms, y16)


PITCH_ALGO = 2   # 1.4.2：旋律分析改良（補斷點、修八度）；舊的歌可以重新分析旋律


def analysis_vocal(d: Path) -> np.ndarray:
    """旋律分析與歌詞辨識用的人聲（16 kHz 單聲道）。
    1.6.5：多人輪流唱的歌（例如 S.H.E），分離模型常把某位成員唱的整句歸到「合音」，主唱軌就斷斷續續；
    這種歌設定成「合唱模式」後，改用主唱＋合音一起分析，音高追蹤會跟著當下最大聲（通常就是旋律）的那個人。"""
    lead = read_audio(stem_path(d, "lead"), sr=PITCH_SR, mono=True, fallback=False)
    if read_meta(d).get("vocal_mode") == "all" and stem_path(d, "back").exists():
        back = read_audio(stem_path(d, "back"), sr=PITCH_SR, mono=True, fallback=False)
        n = min(len(lead), len(back))
        lead = lead[:n] + back[:n]
    return lead


_PITCH_LOCK = threading.Lock()


def step_pitch(d: Path, cfg: dict, report) -> None:
    report("分析主唱旋律" if read_meta(d).get("vocal_mode") != "all" else "分析旋律（合唱模式：主唱＋合音）", 0.55)
    y16 = analysis_vocal(d)
    with _PITCH_LOCK:   # 重疊處理時一次只分析一首的旋律（模型共用、也比較省顯示卡記憶體）
        midi = track_pitch(y16, cfg)
    notes = segment_notes(midi)
    data = {
        "hop": HOP_S,
        "midi": [None if np.isnan(v) else round(float(v), 2) for v in midi],
        "notes": notes,
    }
    atomic_write_text(d / "pitch.json", json.dumps(data, separators=(",", ":")))
    free_gpu()
    with _META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
        meta = read_meta(d)
        meta["steps"]["pitch"] = True
        meta["pitch_algo"] = PITCH_ALGO
        meta.pop("pitch_stale", None)   # fork（69、70）：分軌改過之後的旋律已經重新分析
        write_meta(d, meta)


def load_pitch(d: Path) -> tuple[np.ndarray, list]:
    data = json.loads((d / "pitch.json").read_text("utf-8"))
    midi = np.array([np.nan if v is None else v for v in data["midi"]], dtype=np.float32)
    return midi, data["notes"]


# ---------------------------------------------------------------- 歌詞

# Whisper 在間奏、沒有人聲的地方常會「聽到」訓練資料裡影片字幕的片頭片尾字樣，這些句子直接丟掉
HALLUCINATION = re.compile(
    r"字幕|訂閱|订阅|點贊|点赞|感謝觀看|感谢观看|Amara|請不吝|请不吝|明鏡|明镜"
    r"|優優獨播|优优独播|獨播劇場|独播剧场|YoYo\s*Television|Television\s*Series|Series\s*Exclusive"
    r"|ご視聴|チャンネル登録|시청해\s*주셔서|구독"
    r"|thanks?\s+(you\s+)?for\s+watching|please\s+subscribe|subtitles?\s+by", re.I)


def strip_hallucinations(lrc: str) -> str:
    """把辨識結果裡的幻覺句子拿掉（1.6.5 以前辨識的歌，開啟時順便清理）。"""
    out = [l for l in lrc.splitlines() if not HALLUCINATION.search(re.sub(r"^(\[[^\]]*\])+", "", l))]
    return "\n".join(out) + ("\n" if lrc.endswith("\n") and out else "")
PUNCT_END = "，。！？、,.!?;；：:…"


def is_japanese(text: str) -> bool:
    """文字裡有一定比例的平假名／片假名 → 日文（日文的漢字不能轉成台灣繁體，例如「来」會被改成「來」）。"""
    kana = len(re.findall(r"[\u3040-\u30ff]", text or ""))
    han = len(re.findall(r"[\u3400-\u9fff]", text or ""))
    return kana >= 3 and kana >= 0.15 * (kana + han)


def to_taiwan(text: str) -> str:
    if is_japanese(text):
        return text
    try:
        from opencc import OpenCC
    except ImportError:
        return text
    # 只做簡體→繁體的字形轉換（s2tw）。以前用的 s2twp 還會把詞換成台灣慣用語
    # （例如「信息→資訊」「出租車→計程車」），會改到歌詞原本的用字，所以不用
    for conf in ("s2tw", "s2tw.json"):
        try:
            return OpenCC(conf).convert(text)
        except Exception:
            continue
    return text


def _whisper_model(cfg, raise_oom: bool = False):
    """raise_oom（fork，Codex r2-3）：顯示卡記憶體不夠時直接丟出去，不改用 CPU（重疊處理時交給佇列）。"""
    try:
        import torch  # noqa: F401  Whisper 需要的 cublas64_12.dll 由 PyTorch 提供
    except Exception:
        pass
    from faster_whisper import WhisperModel
    root = str(MODELS / "whisper")
    name = cfg.get("whisper_model") or "large-v3-turbo"
    try:
        model = WhisperModel(name, device="auto", compute_type="default", download_root=root)
        if model.model.device != "cuda":
            return model
        try:
            # 有 NVIDIA 顯示卡時 device="auto" 一定選顯示卡，但裝的是 CPU 版 PyTorch 時沒有 cublas64_12.dll。
            # 這時第一次運算會出錯，之後的 transcribe 卻會一直卡住、按停止也沒用，所以先試算一小段確認
            model.detect_language(np.zeros(16000, dtype=np.float32), vad_filter=False)
            return model
        except RuntimeError as e:
            if raise_oom and "out of memory" in str(e).lower():
                raise
            print(f"[lyrics] 顯示卡無法執行語音辨識，改用 CPU（比較慢）：{e}", flush=True)
            del model
    except Exception as e:
        if raise_oom and "out of memory" in str(e).lower():
            raise
        traceback.print_exc()
    try:
        return WhisperModel(name, device="cpu", compute_type="int8", download_root=root)
    except Exception as e:
        raise UserError(f"載入語音辨識模型 {name} 失敗（第一次使用需要連網下載）：{e}") from e


def _collect_words(segs, dur: float, report, stats: list | None = None) -> list:
    """逐段取出辨識結果，同時更新進度；使用者按「停止」時在這裡停下。stats 會收到每段的可信度資料。"""
    words = []
    for s in segs:
        done = min(1.0, (s.end or 0) / max(dur, 1))
        report(f"辨識歌詞 {int(s.end // 60)}:{int(s.end % 60):02d} / {int(dur // 60)}:{int(dur % 60):02d}",
               0.75 + 0.19 * done)
        words.extend(s.words or [])
        if stats is not None:
            stats.append({"start": s.start, "end": s.end, "lp": getattr(s, "avg_logprob", 0.0),
                          "cr": getattr(s, "compression_ratio", 1.0), "ns": getattr(s, "no_speech_prob", 0.0),
                          "text": s.text or ""})
    return words


LANG_NAMES = {"zh": "中文", "ja": "日文", "en": "英文", "ko": "韓文", "yue": "粵語"}


def _sung_windows(y16: np.ndarray, n: int = 3, win: float = 30.0) -> np.ndarray:
    """主唱聲音最多的幾段（每段 30 秒）接起來，用來判斷語言。
    以前先對整首歌跑一次語音偵測（VAD）再判斷，比較慢；主唱軌本來就只有人聲，直接挑最大聲的段落即可。"""
    sr = PITCH_SR
    W = int(win * sr)
    if len(y16) <= W:
        return y16.astype(np.float32)
    hop = sr // 2
    e = np.array([float(np.sqrt(np.mean(y16[i:i + hop] ** 2))) for i in range(0, len(y16) - hop + 1, hop)])
    k = W // hop
    sums = np.convolve((e > max(1e-4, np.median(e) * 0.5)).astype(np.float32) * e, np.ones(k), "valid")
    picks: list[int] = []
    for i in np.argsort(-sums):
        if all(abs(int(i) - j) >= k for j in picks):
            picks.append(int(i))
        if len(picks) >= n:
            break
    return np.concatenate([y16[j * hop:j * hop + W] for j in sorted(picks)]).astype(np.float32)


def pick_language(model, y16: np.ndarray, want: str, per_song: bool) -> tuple[str | None, str]:
    """決定辨識語言。回傳 (語言, 說明)。

    每首歌個別指定的語言一定照用。全域設定是某個語言（例如中文）時，如果偵測到明顯是別的語言
    （例如日文歌），改用偵測到的語言，避免把日文硬辨識成中文。設定是自動時直接用偵測結果。"""
    if per_song and want:
        return want, ""
    t0 = time.time()
    try:
        det, prob, _ = model.detect_language(_sung_windows(y16), vad_filter=False, language_detection_segments=3)
    except Exception:
        traceback.print_exc()
        return (want or None), ""
    print(f"[lyrics] 偵測語言 {det} {prob:.2f}（設定 {want or '自動'}，{time.time() - t0:.1f} 秒）", flush=True)
    if want and det != want and prob >= 0.7:
        return det, f"偵測到這首是{LANG_NAMES.get(det, det)}歌，改用{LANG_NAMES.get(det, det)}辨識"
    if want:
        return want, ""
    return (det if prob >= 0.5 else None), ""


def lyric_quality(stats: list, words: list, d: Path, lines: list) -> dict:
    """判斷辨識結果可不可靠（Whisper 的可信度指標＋和主唱有聲音的時間對不對得上）。"""
    total = sum(max(0.0, s["end"] - s["start"]) for s in stats) or 0.0
    bad = sum(max(0.0, s["end"] - s["start"]) for s in stats if s["lp"] < -1.0 or s["cr"] > 2.4)
    lp = (sum(s["lp"] * max(0.0, s["end"] - s["start"]) for s in stats) / total) if total else -9.0
    chars = sum(len(re.sub(r"\s", "", l["text"])) for l in lines)
    texts = [_norm(l["text"]) for l in lines if _norm(l["text"])]
    loop, run = 0, 1   # 同一句「連續」重複很多次：Whisper 卡住的典型症狀（副歌隔一段重複不算）
    for a, b in zip(texts, texts[1:]):
        run = run + 1 if a == b else 1
        loop = max(loop, run)
    sung = 0.0
    try:   # 主唱有聲音（有旋律）的總時間
        pj = json.loads((d / "pitch.json").read_text("utf-8"))
        sung = sum(1 for v in pj["midi"] if v is not None) * pj.get("hop", HOP_S)
    except Exception:
        pass
    spoken = sum(max(0.0, (w.end or 0) - (w.start or 0)) for w in words)
    cover = min(1.0, spoken / sung) if sung > 5 else None
    q = {"lp": round(lp, 3), "bad": round(bad / total, 3) if total else 1.0, "chars": chars,
         "loop": loop, "cover": None if cover is None else round(cover, 3)}
    reasons = []
    if chars < 15:
        reasons.append("幾乎沒有辨識出文字")
    if q["bad"] > 0.5:
        reasons.append("大部分段落的辨識把握很低")
    if lp < -1.1:
        reasons.append("整體辨識把握很低")
    if loop >= 5:
        reasons.append("同一句連續重複太多次")
    if cover is not None and cover < 0.25:
        reasons.append("有唱歌的地方大多沒有辨識出字")
    q["ok"] = not reasons
    q["reasons"] = reasons
    return q


def step_lyrics(d: Path, cfg: dict, report) -> None:
    if not module_status()["whisper"]:
        raise UserError("沒有安裝 faster-whisper，無法辨識歌詞")
    from types import SimpleNamespace
    import gpu_worker as G
    report("載入語音辨識模型（第一次會下載）", 0.7)
    y16 = analysis_vocal(d)
    meta0 = read_meta(d)
    song_lang = meta0.get("lyrics_language")          # 這首歌個別指定的語言（None＝依設定）
    want = song_lang if song_lang is not None else (cfg.get("lyrics_language") or "")
    # 2.1.1：載入模型、判斷語言、辨識都在顯示卡子程式裡跑（CTranslate2 的顯示卡記憶體才放得乾淨）
    overlapping = getattr(G.CURRENT, "overlapping", None)   # fork（36、Codex r2-3）：重疊處理時 OOM 交給佇列
    r = G.call("whisper", np.ascontiguousarray(y16, dtype=np.float32), cfg, want, song_lang is not None,
               raise_oom=bool(overlapping and overlapping()), progress=report)
    words = [SimpleNamespace(**w) for w in r["words"]]
    stats = r["stats"]
    lang, lang_note = r["lang"], r["lang_note"]
    info = SimpleNamespace(language=r["language"])

    lines, cur = [], []

    def flush():
        if not cur:
            return
        text = "".join(w.word for w in cur).strip()
        text = re.sub(f"[{re.escape(PUNCT_END)}]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text and not HALLUCINATION.search(text):
            lines.append({"t": round(cur[0].start, 2), "end": round(cur[-1].end, 2), "text": text})
        cur.clear()

    for w in words:
        if not w.word.strip():
            continue
        if cur:
            gap = w.start - cur[-1].end
            length = len("".join(x.word for x in cur).strip())
            if gap > 0.5 or length >= 18:
                flush()
        cur.append(w)
        if w.word.strip()[-1] in PUNCT_END:
            flush()
    flush()

    language = getattr(info, "language", lang) or ""
    if cfg.get("convert_to_taiwan", True) and language.startswith("zh"):
        for l in lines:
            l["text"] = to_taiwan(l["text"])

    lrc = to_lrc(lines)
    q = lyric_quality(stats, words, d, lines)
    print(f"[lyrics] 辨識可信度 {q}", flush=True)
    atomic_write_text(d / "lyrics_auto.lrc", lrc)      # 辨識結果一律保留，之後可以叫出來看
    with _META_LOCK:   # 讀出到寫回之間不讓別的執行緒改這首歌的資料
        meta = read_meta(d)
        keep_user = meta.get("lyrics_source") in ("online", "import", "claude") and (d / "lyrics.lrc").exists() \
            and (d / "lyrics.lrc").read_text("utf-8").strip()
        notes = [x for x in meta.get("notes", []) if "歌詞" not in x and "辨識" not in x]
        if lang_note:
            notes.append(lang_note + "。")
        if q["ok"]:
            if not keep_user:   # 使用者已經換成線上／匯入的歌詞時，重新辨識不覆蓋
                atomic_write_text(d / "lyrics.lrc", lrc)
                meta["lyrics_source"] = "whisper"
            meta["lyrics_unreliable"] = False
        else:
            # 辨識不可靠：不要把錯的歌詞放上去，留白並建議找線上歌詞
            if not keep_user:
                atomic_write_text(d / "lyrics.lrc", "")
                meta["lyrics_source"] = None
            meta["lyrics_unreliable"] = True
            notes.append("自動辨識的歌詞不夠準確（" + "、".join(q["reasons"]) + "），已先留白。建議用練唱畫面的「搜尋線上歌詞」或匯入歌詞檔。")
        meta["notes"] = notes
        meta["steps"]["lyrics"] = True
        meta["language"] = language
        meta["lyrics_quality"] = q
        write_meta(d, meta)


# ---------------------------------------------------------------- 移調／變速

def variant_dir(d: Path, key: int, speed: float) -> Path:
    if key == 0 and abs(speed - 1) < 1e-3:
        return d
    # v2：1.3 起分段處理（時間不漂移）；舊版的 v_k… 資料夾會在產生新版本時清掉
    return d / f"v2_k{key:+d}_s{int(round(speed * 100))}"


VARIANT_STALE = ".stale"   # fork（70）：原調分軌改過、這個 KEY 版本還沒重做（資料夾刪不掉時留下的記號）


def variant_stale(vd: Path) -> bool:
    return (vd / VARIANT_STALE).exists()


def invalidate_variants(d: Path) -> None:
    """fork（70）：原調分軌要改了，KEY 版本都要重做。Windows 上檔案正在被讀（例如畫面正在播放）時資料夾刪不掉，
    就在裡面留記號：variant_ready、音訊路由不再把它當成有效版本，step_variant 會整份重做。
    刪不掉、記號也寫不進去時丟出 OSError：呼叫端要在改分軌之前呼叫，這時就停下、不改分軌。"""
    for old in [*d.glob("v_*"), *d.glob("v2_*")]:
        shutil.rmtree(old, ignore_errors=True)
        if old.exists():
            (old / VARIANT_STALE).write_text("1", "utf-8")


def shift_audio(y: np.ndarray, sr: int, semis: int, speed: float) -> np.ndarray:
    try:
        from pedalboard import time_stretch
        return time_stretch(np.ascontiguousarray(y, dtype=np.float32), sr,
                            stretch_factor=float(speed),
                            pitch_shift_in_semitones=float(semis))
    except ImportError:
        import librosa
        out = []
        for ch in y:
            z = ch
            if abs(speed - 1) > 1e-3:
                z = librosa.effects.time_stretch(z, rate=speed)
            if semis:
                z = librosa.effects.pitch_shift(z, sr=sr, n_steps=semis)
            out.append(z)
        return np.stack(out)


def shift_parts(n: int, sr: int, cpus: int = 0) -> list[tuple[int, int]]:
    """把一軌切成約 6 秒一段。

    好處有兩個：所有 CPU 核心可以一起算；而且移調演算法處理長音檔時時間會慢慢漂移
    （實測 40 秒累積約 45 毫秒），切短後每段重新對齊，伴奏和旋律線、歌詞不會越來越不同步。"""
    parts = max(1, -(-n // (6 * sr)))
    step = -(-n // parts)
    return [(a, min(n, a + step)) for a in range(0, n, step)]


def shift_piece(y: np.ndarray, sr: int, semis: int, speed: float, a: int, b: int) -> np.ndarray:
    """處理 [a, b) 這一段，前後多帶 1 秒讓接縫處的聲音和整段處理時一樣。"""
    pad = sr
    sa, sb = max(0, a - pad), min(y.shape[1], b + pad)
    return shift_audio(y[:, sa:sb], sr, semis, speed), sa


def join_pieces(pieces: list, bounds: list[tuple[int, int]], n: int, sr: int, speed: float) -> np.ndarray:
    """把各段接回去：交界處用 40 ms 交叉淡化，聽不出接縫。"""
    total = int(round(n / speed))
    ch = pieces[0][0].shape[0]
    out = np.zeros((ch, total), dtype=np.float32)
    xf = int(0.04 * sr)
    for i, ((seg, sa), (a, b)) in enumerate(zip(pieces, bounds)):
        g0 = int(round(a / speed)) - (xf // 2 if i else 0)
        g1 = int(round(b / speed)) + (xf - xf // 2 if i < len(bounds) - 1 else 0)
        g1 = min(g1, total)
        off = int(round(sa / speed))
        l0, l1 = g0 - off, g1 - off
        l1 = min(l1, seg.shape[1])
        g1 = g0 + (l1 - l0)
        w = np.ones(g1 - g0, dtype=np.float32)
        if i:
            w[:xf] = np.linspace(0, 1, xf, dtype=np.float32)
        if i < len(bounds) - 1:
            w[-xf:] = np.linspace(1, 0, xf, dtype=np.float32)
        out[:, g0:g1] += seg[:, l0:l1] * w
    return out


def step_variant(d: Path, key: int, speed: float, report) -> None:
    """產生移調／變速版本。三軌各切成多段，用所有 CPU 核心同時處理。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    vd = variant_dir(d, key, speed)
    if vd == d:
        return
    vd.mkdir(exist_ok=True)
    for old in d.glob("v_k*"):   # 1.3 以前產生的版本（時間會漂移），用不到了
        shutil.rmtree(old, ignore_errors=True)
    stale = variant_stale(vd)   # fork（70）：原調分軌改過，已經在的檔案也是舊的，整份重做
    todo = [s for s in STEMS if stale or not stem_path(vd, s).exists()]
    if not todo:
        return
    src = _stems_sig(d)   # fork：算到一半原調分軌被換掉的話，做出來的是舊的，結束時不能當成有效
    label = f"移調 {key:+d}、速度 {int(speed * 100)}%"
    cpus = max(1, os.cpu_count() or 1)
    try:
        if live_status()["hold"]:   # 2.1.1 直播中：只用一半的 CPU 核心，留給直播軟體和 VTube Studio
            cpus = max(1, cpus // 2)
    except Exception:
        pass
    report(f"{label}：讀取音軌", 0.02)
    data = {}
    for stem in todo:
        y, sr = sf.read(str(stem_path(d, stem)), dtype="float32", always_2d=True)
        y = np.ascontiguousarray(y.T)
        data[stem] = (y, sr, shift_parts(y.shape[1], sr, cpus))
    total = sum(len(v[2]) for v in data.values())
    report(f"{label}：用 {min(cpus, total)} 個 CPU 核心同時處理", 0.05)
    results = {stem: [None] * len(v[2]) for stem, v in data.items()}
    done = 0
    ex = ThreadPoolExecutor(max_workers=cpus)
    try:
        futs = {ex.submit(shift_piece, y, sr, key, speed, a, b): (stem, i)
                for stem, (y, sr, parts) in data.items() for i, (a, b) in enumerate(parts)}
        for f in as_completed(futs):
            stem, i = futs[f]
            results[stem][i] = f.result()
            done += 1
            report(f"{label}：{done}/{total} 段（{min(cpus, total)} 核心同時處理）", 0.05 + 0.9 * done / total)
    finally:
        ex.shutdown(wait=True, cancel_futures=True)   # 取消時，還沒開始的段落直接丟掉
    for stem, (y, sr, parts) in data.items():
        out = join_pieces(results[stem], parts, y.shape[1], sr, speed)
        save_stem(vd, stem, out, sr)
    if _stems_sig(d) == src:
        (vd / VARIANT_STALE).unlink(missing_ok=True)
    else:   # 期間原調分軌換過（重新分離、主唱補回）：留記號，下次整份重做
        (vd / VARIANT_STALE).write_text("1", "utf-8")
    report(f"{label}：完成", 1.0)


def variant_ready(d: Path, key: int, speed: float) -> bool:
    vd = variant_dir(d, key, speed)
    return all(stem_path(vd, s).exists() for s in STEMS) and (vd == d or not variant_stale(vd))


# ---------------------------------------------------------------- 錄音評分

def list_takes(d: Path) -> list[dict]:
    td = d / "takes"
    if not td.exists():
        return []
    out = []
    for p in td.glob("*.json"):
        try:
            t = json.loads(p.read_text("utf-8"))
            t.pop("pitch", None)
            # 評分中途程式被關掉：超過 5 分鐘還在「評分中」就標成失敗，讓使用者可以重新評分或刪除
            if t.get("status") == "scoring" and time.time() - t.get("created", 0) > 300 and not t.get("_job"):
                t["status"], t["error"] = "error", "評分沒有完成（可能是程式被關掉），可以按「重新評分」"
            out.append(t)
        except Exception:
            pass
    out.sort(key=lambda t: t.get("created", 0), reverse=True)
    return out


# ---------------------------------------------------------------- 日文假名標音（ふりがな）

# 用 SudachiPy（Apache-2.0）的小型字典判斷漢字讀音。第一次用到時才在背景安裝（約 45 MB），安裝檔也會先裝好。
KANA_PKGS = ["sudachipy==0.7.0", "sudachidict_small==20261015"]
KANA = {"state": "idle", "error": ""}
_kana_lock = threading.Lock()
_KANA_TK = None
_KANJI = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff々〆ヶ"
_KANJI_RE = re.compile(f"[{_KANJI}]")
_PARTS_RE = re.compile(f"[{_KANJI}]+|[^{_KANJI}]+")


def kana_ready() -> bool:
    import importlib.util as u
    return u.find_spec("sudachipy") is not None and u.find_spec("sudachidict_small") is not None


def kana_status() -> dict:
    return {"state": "ready"} if kana_ready() else dict(KANA)


def start_kana_install() -> None:
    """在背景用 pip 安裝日文讀音字典（舊版升級上來的使用者沒有，不用重跑安裝）。"""
    if NO_BACKGROUND:
        return
    with _kana_lock:
        if KANA["state"] == "installing" or kana_ready():
            return
        KANA.update(state="installing", error="")

    def run():
        import importlib
        import subprocess
        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
            exe = exe.parent / "python.exe"
        try:
            r = subprocess.run([str(exe), "-m", "pip", "install", "--disable-pip-version-check", *KANA_PKGS],
                               capture_output=True, text=True, timeout=1800,
                               creationflags=0x08000000 if sys.platform == "win32" else 0)
            importlib.invalidate_caches()
            if r.returncode != 0 or not kana_ready():
                raise RuntimeError((r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["安裝失敗"])
            KANA.update(state="done")
        except Exception as e:
            log_error("kana install", e)
            KANA.update(state="error", error=f"{type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True).start()


def _kata(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def _hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _ruby_word(surface: str, reading: str) -> list[list[str]]:
    """一個詞拆成「漢字＋讀音」與「假名」：沈む／シズム → [["沈","しず"],["む",""]]"""
    if not _KANJI_RE.search(surface) or not reading or reading == "*" or _KANJI_RE.search(reading):
        return [[surface, ""]]   # 字典查不到讀音（讀音還是漢字）：不標
    parts = _PARTS_RE.findall(surface)
    pat = "".join("(.+?)" if _KANJI_RE.match(p) else re.escape(_kata(p)) for p in parts)
    m = re.fullmatch(pat, _kata(reading))
    if not m:
        return [[surface, _hira(reading)]]
    out, g = [], iter(m.groups())
    for p in parts:
        out.append([p, _hira(next(g))] if _KANJI_RE.match(p) else [p, ""])
    return out


_JA_RE = re.compile(f"[\u3040-\u30ff{_KANJI}]")


_TW2JA: dict | None = None


def tw_to_ja(text: str) -> str:
    """把被轉成台灣繁體的日文字改回日文字形（1.5 以前的線上歌詞／匯入歌詞會把「来」改成「來」、「会」改成「會」）。
    對照表：日本常用漢字（JIS 第一水準）經過簡轉繁後變成的字 → 原本的日文字；只換逐字對應、不影響日文本來就用的字。"""
    global _TW2JA
    if _TW2JA is None:
        table: dict = {}
        try:
            from opencc import OpenCC
            cc = OpenCC("s2twp")
            level1 = []
            for hi in range(0x88, 0x99):
                for lo in list(range(0x40, 0x7F)) + list(range(0x80, 0xFD)):
                    if (hi, lo) < (0x88, 0x9F) or (hi, lo) > (0x98, 0x72):
                        continue
                    try:
                        level1.append(bytes([hi, lo]).decode("shift_jis"))
                    except UnicodeDecodeError:
                        pass
            jp = set(level1)
            for j in level1:
                c = cc.convert(j)
                if len(c) == 1 and c != j and c not in jp:
                    table.setdefault(c, j)
        except Exception as e:
            log_error("tw_to_ja", e)
        _TW2JA = table
    return "".join(_TW2JA.get(c, c) for c in text) if _TW2JA else text


def furigana(text: str) -> list[dict] | None:
    """一句日文歌詞拆成詞：[{"p": [[字, 讀音], …], "pt": 是否助詞}, …]。
    讀音只標在漢字上（空字串＝不用標）；羅馬拼音由前端依每個詞的讀音換算（助詞は／へ／を 念 wa／e／o）。
    沒有假名也沒有漢字的句子回傳 None。"""
    global _KANA_TK
    if not text or not _JA_RE.search(text):
        return None
    with _kana_lock:
        if _KANA_TK is None:
            from sudachipy import dictionary
            _KANA_TK = dictionary.Dictionary(dict="small").create()
        from sudachipy import tokenizer
        norm = tw_to_ja(text)
        toks = _KANA_TK.tokenize(norm, tokenizer.Tokenizer.SplitMode.C)
    out: list[dict] = []
    for t in toks:
        parts: list[list[str]] = []
        pos = t.begin()
        for b, r in _ruby_word(t.surface(), t.reading_form()):
            b, pos = text[pos:pos + len(b)], pos + len(b)   # 顯示原本的字
            if not r and parts and not parts[-1][1]:
                parts[-1][0] += b
            else:
                parts.append([b, r])
        tok = {"p": parts}
        if t.part_of_speech()[0] == "助詞":
            tok["pt"] = 1
        out.append(tok)
    if "".join(b for t in out for b, _ in t["p"]) != text:
        return None
    return out


def lyric_lines(d: Path) -> list[dict]:
    p = d / "lyrics.lrc"
    if not p.exists():
        return []
    return [l for l in parse_lrc(p.read_text("utf-8")) if l["t"] is not None]


def suggest_key(dd: np.ndarray, key: int) -> dict | None:
    """依你和原唱的音高差（已去掉八度差，-6～+6 半音）找出最常見的偏移，推算適合你的 KEY。

    大多數音都固定差同樣幾個半音，代表你習慣的音域和目前的 KEY 不合，而不是單純唱不準。"""
    v = dd[~np.isnan(dd)]
    if v.size < 150:
        return None
    b = np.round(v).astype(int)
    b[b == -6] = 6   # -6 和 +6 是同一個音
    vals, counts = np.unique(b, return_counts=True)
    i = int(np.argmax(counts))
    shift, share = int(vals[i]), float(counts[i] / b.size)
    # 只差 0～1 個半音多半是音準問題；差 2 個以上而且有一定比例的音都這樣，才建議換 KEY
    if abs(shift) < 2 or share < 0.3:
        return {"shift": 0, "share": round(share, 2), "suggest": key}
    return {"shift": shift, "share": round(share, 2), "suggest": (key + shift + 6) % 12 - 6}   # 保持在 ±6


TAKE_ALGO = 5   # 錄音分析方式的版本；舊版分析的錄音會自動重新評分（5：用開口瞬間對齊時間）


def _onset_env(y: np.ndarray, H: int) -> np.ndarray:
    """每 10 ms 的「開口力道」：音量（對數）往上跳多少。每個字開口的瞬間會是一個尖峰。"""
    n = len(y) // H * H
    if n < H * 10:
        return np.zeros(0, dtype=np.float32)
    e = np.log(np.sqrt(np.mean(y[:n].reshape(-1, H) ** 2, axis=1)) + 1e-4)
    return np.maximum(0, np.diff(e, prepend=e[0])).astype(np.float32)


def onset_offset(d: Path, y16: np.ndarray, start: float, speed: float,
                 lo: float = -0.5, hi: float = 1.0) -> float | None:
    """2.3.3：用「每個字開口的瞬間」對齊錄音和原唱，回傳 offset（歌曲時間 = start + offset + τ×speed）。
    以前只用音高對齊：長音只要音高對，早晚 0.1 秒都一樣吻合，實測會把人聲放晚約 0.1 秒，聽起來拖拍。
    開口瞬間對時間很敏感。找不到明顯的最佳位置（例如幾乎沒唱）時回傳 None，改用音高對齊。"""
    lp = stem_path(d, "lead")
    if not lp.exists():
        return None
    try:
        ref = _onset_env(read_audio(lp, sr=PITCH_SR, mono=True), PITCH_SR // 100)
    except Exception:
        return None
    mine = _onset_env(y16, PITCH_SR // 100)
    if len(mine) < 500 or len(ref) < 500:
        return None
    tau = np.arange(len(mine)) * 0.01
    mine = mine - mine.mean()
    offs = np.arange(lo, hi + 1e-9, 0.01)
    score = np.full(len(offs), np.nan)
    for i, off in enumerate(offs):
        idx = np.round((start + off + tau * speed) / 0.01).astype(int)
        ok = (idx >= 0) & (idx < len(ref))
        if ok.sum() < 500:
            continue
        r = ref[idx[ok]]
        score[i] = float(np.dot(mine[ok], r - r.mean()) / ok.sum())
    if np.all(np.isnan(score)):
        return None
    k = int(np.nanargmax(score))
    s = score[~np.isnan(score)]
    if s.std() <= 0 or (score[k] - np.median(s)) / s.std() < 3:   # 最佳位置不夠突出：不可靠
        return None
    return float(offs[k])
TOL_FRAMES = 10   # 評分的時間容錯：±0.1 秒


# fork（M7）：錄音的寫入和刪除共用一把鎖。評分、Claude 建議、試聽處理期間錄音被刪掉時，
# 寫回會留下孤兒檔（錄音清單裡出現沒有錄音檔的紀錄）。運算在鎖外做完，鎖內只確認錄音還在、寫檔。
_TAKE_LOCK = threading.Lock()


def write_take_info(td: Path, take_id: str, info: dict) -> bool:
    """寫回錄音資料；錄音已經被刪掉就不寫，回傳 False。"""
    with _TAKE_LOCK:
        if not (td / f"{take_id}.wav").exists():
            return False
        atomic_write_text(td / f"{take_id}.json", json.dumps(info, ensure_ascii=False))
        return True


def write_take_audio(td: Path, take_id: str, path: Path, y, sr: int) -> bool:
    """寫試聽用的暫存人聲或混音；錄音已經被刪掉就不寫，回傳 False。"""
    with _TAKE_LOCK:
        if not (td / f"{take_id}.wav").exists():
            return False
        write_wav(path, y, sr)
        return True


def delete_take_files(td: Path, take_id: str) -> None:
    """刪除一次錄音的資料、錄音檔和試聽用的暫存人聲、混音。"""
    with _TAKE_LOCK:
        for f in [td / f"{take_id}.json", td / f"{take_id}.wav", *td.glob(f"{take_id}.*.wav")]:
            f.unlink(missing_ok=True)


def score_take(d: Path, take_id: str, report) -> None:
    td = d / "takes"
    info = json.loads((td / f"{take_id}.json").read_text("utf-8"))
    report("分析你的錄音", 0.2)
    y16 = read_audio(td / f"{take_id}.wav", sr=PITCH_SR, mono=True)
    mine = track_pitch(y16, strict=True)
    ref, _ = load_pitch(d)
    key = int(info.get("key", 0))
    speed = float(info.get("speed", 1.0))
    start = float(info.get("start", 0.0))
    ref_shifted = ref + key

    # 每一個錄音影格對應的歌曲時間 = start + τ*speed + offset
    tau = np.arange(len(mine)) * HOP_S
    voiced = ~np.isnan(mine)

    def diffs(offset):
        ts = start + tau * speed + offset
        idx = np.round(ts / HOP_S).astype(int)
        ok = voiced & (idx >= 0) & (idx < len(ref_shifted))
        r = np.full(len(mine), np.nan, dtype=np.float32)
        r[ok] = ref_shifted[idx[ok]]
        dd = mine - r
        dd = dd - 12 * np.round(dd / 12)
        return ts, dd, r

    report("對齊時間", 0.7)
    best, best_off = -1, 0.0
    for off in np.arange(-0.5, 1.0001, 0.01):
        _, dd, _ = diffs(off)
        m = int(np.sum(np.abs(dd[~np.isnan(dd)]) < 0.5))
        if m > best:
            best, best_off = m, float(off)
    # 錄音放回伴奏上的位置（播放、合成成品）：用開口瞬間對齊，找不到時才用音高對齊。
    # 評分仍用音高對齊（換音早晚 0.1 秒內本來就算對），分數不會因為這次修正改變
    on_off = onset_offset(d, y16, start, speed)
    place_off = on_off if on_off is not None else best_off
    # 建議 KEY 用另一種對齊：找「最多音都差同樣幾個半音」的位置（整首唱在別的 KEY 時，
    # 上面「最準」的對齊會找錯位置）
    kbest, koff = -1, best_off
    for off in np.arange(-0.5, 1.0001, 0.02):
        _, dk, _ = diffs(off)
        b = np.round(dk[~np.isnan(dk)]).astype(int) % 12
        c = int(np.bincount(b, minlength=12).max()) if b.size else 0
        if c > kbest:
            kbest, koff = c, float(off)
    key_hint = suggest_key(diffs(koff)[1], key)
    ts, dd, r = diffs(best_off)
    # 時間容錯：每一格取前後 0.1 秒內最接近的原唱音高（換音稍早或稍晚不算走音）
    for k in list(range(-TOL_FRAMES, 0)) + list(range(1, TOL_FRAMES + 1)):
        _, d2, _ = diffs(best_off + k * HOP_S)
        better = np.isnan(dd) & ~np.isnan(d2) | (~np.isnan(d2) & (np.abs(d2) < np.abs(dd)))
        dd = np.where(better, d2, dd)
    valid = ~np.isnan(dd)
    cents = dd[valid] * 100
    octave = 0
    raw = (mine - r)[~np.isnan(mine - r)]
    if raw.size:
        octave = int(np.median(np.round(raw / 12)))

    per_line = []
    lines = lyric_lines(d)
    for i, l in enumerate(lines):
        end = lines[i + 1]["t"] if i + 1 < len(lines) else l["t"] + 8
        sel = valid & (ts >= l["t"]) & (ts < end)
        c = dd[sel] * 100
        if c.size < 10:
            continue
        per_line.append({
            "t": l["t"], "text": l["text"],
            "acc": round(float(np.mean(np.abs(c) <= 50)), 3),
            "mean": round(float(np.mean(c)), 1),
            "n": int(c.size),
        })

    if not cents.size:
        # 沒有偵測到歌聲：通常是麥克風沒收到音（音量太小、選錯裝置）或這段沒有唱
        peak = float(np.max(np.abs(y16))) if y16.size else 0.0
        info.update({"status": "empty", "acc": None, "mean": None, "frames": 0, "algo": TAKE_ALGO,
                     "error": "沒有偵測到歌聲" + ("：錄音幾乎沒有聲音，請確認麥克風選對、音量表有動" if peak < 0.02
                                                else "：這段時間原唱沒有旋律，或沒有唱到")})
        info.pop("pitch", None)
        write_take_info(td, take_id, info)
        return
    info.update({
        "offset": round(place_off, 2),
        "score_offset": round(best_off, 2),
        "acc": round(float(np.mean(np.abs(cents) <= 50)), 3) if cents.size else None,
        "mean": round(float(np.mean(cents)), 1) if cents.size else None,
        "spread": round(float(np.mean(np.abs(cents))), 1) if cents.size else None,
        "frames": int(cents.size),
        "octave": octave,
        "lines": per_line,
        "pitch": {
            "t0": round(start + place_off, 3), "hop": round(HOP_S * speed, 5),
            "midi": [None if np.isnan(v) else round(float(v), 2) for v in mine],
        },
        "status": "done",
        "algo": TAKE_ALGO,
        "key_hint": key_hint,
    })
    if not write_take_info(td, take_id, info):
        return   # 評分期間錄音被刪掉了
    # 對齊位置可能變了：先前做好的人聲暫存（試聽、成品用）要重做
    for old in list(td.glob(f"{take_id}.vocal_*.wav")) + list(td.glob(f"{take_id}.preview.wav")):
        old.unlink(missing_ok=True)


# ---------------------------------------------------------------- 背景工作

_load_part("jobs")


_load_part("process")


# 啟動時就把 ffmpeg 放進 PATH：AI 分離（audio-separator）與 m4a/mp3 讀取都需要
try:
    ffmpeg_path()
except Exception:
    traceback.print_exc()


# ---------------------------------------------------------------- 線上歌詞搜尋（LRCLIB）

LRCLIB = "https://lrclib.net/api/search"




USER_AGENT = "LeihuLiantai/2.3.5.1 (+https://github.com/lydek/Leihu-singsing)"   # HTTP 標頭只能用英文字


def _http_json(url: str, headers: dict | None = None, timeout: int = 8):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def log_error(where: str, err: Exception) -> None:
    """把連線等錯誤寫進 logs/lyrics.log，方便之後查原因。"""
    try:
        (DATA / "logs").mkdir(exist_ok=True)
        with open(DATA / "logs" / "lyrics.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {where}: {type(err).__name__}: {err}\n")
    except Exception:
        pass


_load_part("lyrics_search")


# ---------------------------------------------------------------- 依主唱聲音對時間（不需要辨識結果，任何語言都可以）

_load_part("voice_align")


# ---------------------------------------------------------------- 分類

def _cats_path() -> Path:
    return LIB / "categories.json"   # 歌曲庫可以搬家：每次用目前的位置


def load_categories() -> list[str]:
    try:
        return [c for c in json.loads(_cats_path().read_text("utf-8")) if isinstance(c, str)]
    except Exception:
        return []


def save_categories(cats: list[str]) -> None:
    LIB.mkdir(exist_ok=True)
    atomic_write_text(_cats_path(), json.dumps(cats, ensure_ascii=False))


# ---------------------------------------------------------------- 歌曲庫／模型搬移（2.3.0）
STORAGE = {"moving": False}   # 搬移中：擋住會寫進歌曲庫的操作

# fork（69）：寫進歌曲庫的人（HTTP 寫入請求、背景歌詞搜尋、預先下載、補量響度、主唱補回檢查）先登記。
# 搬移開始後不再有新的寫入者，搬移等目前的寫完才複製；不然複製途中寫進舊位置的東西，搬完就不見了
_LIB_CV = threading.Condition()
_LIB_WRITERS = [0]


@contextlib.contextmanager
def library_writer(wait: bool = True):
    """寫進歌曲庫前用 with 包起來。搬移中：wait=True 等搬完（之後要用 LIB 重新取路徑），wait=False 丟 UserError。"""
    with _LIB_CV:
        while STORAGE["moving"]:
            if not wait:
                raise UserError("正在搬移歌曲庫，搬完才能操作歌曲（大約幾分鐘，進度在處理清單）")
            _LIB_CV.wait(1)
        _LIB_WRITERS[0] += 1
    try:
        yield
    finally:
        with _LIB_CV:
            _LIB_WRITERS[0] -= 1
            _LIB_CV.notify_all()


@contextlib.contextmanager
def library_write_slot():
    """順手補寫用（開歌曲時補量響度、記演唱次數）：搬移中回傳 False（這次只讀，下次再補），
    否則登記成寫入者、回傳 True。檢查和登記在同一把鎖內，不會看到「沒在搬移」之後搬移才開始。"""
    cm = library_writer(wait=False)
    try:
        cm.__enter__()
    except UserError:
        yield False
        return
    try:
        yield True
    finally:
        cm.__exit__(None, None, None)


def set_moving(on: bool) -> None:
    with _LIB_CV:
        STORAGE["moving"] = on
        _LIB_CV.notify_all()


def wait_library_idle(timeout: float) -> bool:
    """等目前的寫入者都寫完；timeout 秒內沒等到回傳 False。"""
    with _LIB_CV:
        return _LIB_CV.wait_for(lambda: _LIB_WRITERS[0] == 0, timeout)


def dir_size(p: Path) -> tuple[int, int]:
    """資料夾大小（位元組、檔案數）。"""
    total = n = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                n += 1
            except OSError:
                pass
    return total, n


def drives() -> list[dict]:
    """可以搬過去的磁碟（Windows：每個磁碟機代號的剩餘空間）。"""
    out = []
    if sys.platform == "win32":
        import string
        for L in string.ascii_uppercase:
            r = f"{L}:\\"
            if os.path.exists(r):
                try:
                    u = shutil.disk_usage(r)
                    out.append({"path": r, "free": u.free, "total": u.total})
                except OSError:
                    pass
    else:
        u = shutil.disk_usage("/")
        out.append({"path": "/", "free": u.free, "total": u.total})
    return out


def storage_info() -> dict:
    cfg = load_config()
    info = {"library": str(LIB), "models": str(MODELS),
            "library_default": LIB == DEFAULT_LIB, "models_default": MODELS == DEFAULT_MODELS,
            "library_missing": not LIB.exists(), "drives": drives(),
            "old": [p for p in cfg.get("storage_old", []) if Path(p).exists()],
            "moving": STORAGE["moving"]}
    for k, p in (("library", LIB), ("models", MODELS)):
        info[k + "_size"] = dir_size(p)[0] if p.exists() else 0
        try:
            info[k + "_free"] = shutil.disk_usage(p if p.exists() else p.anchor).free
        except OSError:
            info[k + "_free"] = None
    return info


def _inside(a: Path, b: Path) -> bool:
    """a 是不是 b 本身或在 b 裡面。"""
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except ValueError:
        return False


def move_storage(target: str, what: list[str], report) -> dict:
    """把歌曲庫（library）和／或模型（models）複製到 target 底下，逐檔核對大小後才改用新位置。
    舊的那份不刪，記在設定的 storage_old，使用者確認新位置正常後再刪。中途停止：設定不變，照常用舊的。"""
    global LIB, MODELS
    tgt = Path(target.strip().strip('"'))
    if not tgt.is_absolute():
        raise UserError("請輸入完整的資料夾位置，例如 D:\\雷虎練唱台資料")
    if not Path(tgt.anchor).exists():
        raise UserError(f"找不到磁碟 {tgt.anchor}")
    srcs = [(k, LIB if k == "library" else MODELS) for k in ("library", "models") if k in what]
    if not srcs:
        raise UserError("請至少選一項要搬移的東西")
    plan = []
    for k, src in srcs:
        dst = tgt / k
        if _inside(dst, src) or _inside(src, dst):
            raise UserError("新位置不能在原本的資料夾裡面（或包含原本的資料夾）")
        if dst.exists() and any(dst.iterdir()):
            raise UserError(f"{dst} 已經有檔案，請換一個空的資料夾")
        if not src.exists():
            raise UserError(f"找不到目前的{'歌曲庫' if k == 'library' else '模型'}資料夾：{src}")
        plan.append((k, src, dst))
    report("計算大小", 0.01)
    need = 0
    files: list[tuple[Path, Path, int]] = []
    for k, src, dst in plan:
        for root, _dirs, fs in os.walk(src):
            for f in fs:
                s = Path(root) / f
                if f.endswith((".part", ".tmp")) or ".part" in f:
                    continue   # 下載到一半的暫存檔不搬
                try:
                    size = s.stat().st_size
                except OSError:
                    continue
                files.append((s, dst / s.relative_to(src), size))
                need += size
    free = shutil.disk_usage(tgt.anchor).free
    if need > free - (1 << 30):   # 至少多留 1 GB
        raise UserError(f"空間不夠：需要 {need / 1e9:.1f} GB，{tgt.anchor} 只剩 {free / 1e9:.1f} GB")
    done, t0, last = 0, time.time(), 0.0
    try:
        for s, d, size in files:
            d.parent.mkdir(parents=True, exist_ok=True)
            with open(s, "rb") as fi, open(d, "wb") as fo:
                while True:
                    b = fi.read(8 << 20)
                    if not b:
                        break
                    fo.write(b)
                    done += len(b)
                    if time.time() - last > 0.5:
                        last = time.time()
                        sp = done / max(1e-3, last - t0)
                        left = (need - done) / sp if sp > 0 else 0
                        eta = f"（約剩 {left / 60:.0f} 分鐘）" if left >= 90 else ""
                        report(f"搬移中 {done / 1e9:.1f}／{need / 1e9:.1f} GB{eta}", 0.02 + 0.93 * done / max(1, need))
            shutil.copystat(s, d)
            if d.stat().st_size != size:
                raise UserError(f"複製後大小不符：{s.name}，已停止搬移（仍使用原本的位置）")
    except BaseException:
        for _k, _src, dst in plan:   # 沒搬完：清掉新位置的半成品，繼續用舊的
            shutil.rmtree(dst, ignore_errors=True)
        raise
    report("核對檔案", 0.96)
    for k, src, dst in plan:
        a, b = dir_size(dst)
        exp = sum(sz for s, d, sz in files if _inside(d, dst))
        if a != exp:
            for _k, _src, dst2 in plan:   # fork：先複製好的那一項也要清，不然重試同一個位置會被擋（已經有檔案）
                shutil.rmtree(dst2, ignore_errors=True)
            raise UserError("核對失敗：新位置的檔案總大小不符，已停止搬移（仍使用原本的位置）")
    cfg = load_config()
    old = list(cfg.get("storage_old", []))
    for k, src, dst in plan:
        cfg[k + "_dir"] = str(dst)
        if str(src) not in old:
            old.append(str(src))
    cfg["storage_old"] = old
    save_config(cfg)
    _apply_storage()
    try:
        _SONG_CACHE.clear()
    except Exception:
        pass
    report("完成", 1.0)
    return {k: str(dst) for k, _src, dst in plan}


def delete_old_storage(path: str) -> None:
    """刪除搬移前的舊位置（只能刪設定裡記下來的舊位置，而且不能是目前正在用的）。"""
    cfg = load_config()
    old = cfg.get("storage_old", [])
    if path not in old:
        raise UserError("只能刪除搬移前的舊位置")
    p = Path(path)
    if _inside(LIB, p) or _inside(MODELS, p) or _inside(p, LIB) or _inside(p, MODELS):
        raise UserError("這是目前正在使用的位置，不能刪除")
    shutil.rmtree(p, ignore_errors=True)
    cfg["storage_old"] = [x for x in old if x != path]
    save_config(cfg)


def adopt_storage(path: str) -> dict:
    """換電腦、外接硬碟：直接改用一個已經有歌曲庫的資料夾（例如從舊電腦整個複製過來的），不複製檔案。
    path 可以是含有 library／models 的資料夾，或 library 資料夾本身。"""
    global LIB, MODELS
    p = Path(path.strip().strip('"'))
    if not p.is_absolute() or not p.exists():
        raise UserError("找不到這個資料夾")
    lib = p / "library" if (p / "library").is_dir() else p
    n = sum(1 for d in lib.iterdir() if (d / "meta.json").exists()) if lib.is_dir() else 0
    if not n:
        raise UserError("這個資料夾裡沒有練唱台的歌曲庫（找不到任何歌曲）")
    cfg = load_config()
    cfg["library_dir"] = str(lib)
    out = {"library": str(lib), "songs": n}
    models = lib.parent / "models"
    if lib.name == "library" and (models / "uvr").is_dir():
        cfg["models_dir"] = str(models)
        out["models"] = str(models)
    save_config(cfg)
    _apply_storage()
    try:
        _SONG_CACHE.clear()
    except Exception:
        pass
    return out
