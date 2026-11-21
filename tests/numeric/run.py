"""用指定的程式碼處理數值比對素材，輸出各步驟的結果（docs/refactor-port-plan.md 3.8）。

要用裝好 torch、audio-separator、faster-whisper、torchcrepe 的 Python 執行（已安裝版本的內嵌 Python），
通常由 tests/numeric/baseline.py 呼叫：它把這支程式的輸出（含顯示卡子程式的訊息）存成 log、再比對結果。

上游（2.2.0 起）的分離、音高、語音辨識都在 gpu_worker.py 開的子程式裡跑，子程式以程式碼資料夾為 ROOT 重新 import pipeline。
所以這裡不改 pipeline 的路徑，而是照實際安裝的樣子排：
- 程式碼複製到暫存資料夾（ROOT），歌曲寫在 ROOT/library，主程式和子程式看到的是同一份；
- ROOT/models 是新的空資料夾，裡面的 uvr、whisper 用 junction 指到 <模型資料夾>（不複製 2 GB；
  顯示卡探測結果 gpu_probe.json 寫在暫存的 ROOT/models，不會沿用上一次的）。執行前後比對模型檔，有變動就記下來；
- 子程式的啟動命令前面先插入：把 ROOT 放到 sys.path 最前面（內嵌 Python 的 ._pth 會把安裝資料夾放進 sys.path，
  不插的話子程式會 import 到已安裝版本的程式）、擋掉對外連線、關掉 torchcrepe 的隨機抖動，並印出載入的 gpu_worker 路徑。

為了讓同一份程式連跑兩次結果相同，這裡的執行條件和平常不同（全部寫進 <輸出>/run.json 的 conditions，報告會列出）：
- OMP_NUM_THREADS、MKL_NUM_THREADS＝1（排除浮點加總順序的差異；所以這裡的處理時間不是效能基準）；
- torchcrepe 的隨機抖動（±20 cents）關掉；
- 分離固定用標準模型（vocal_quality、karaoke_quality＝standard），兩台電腦、有沒有顯示卡都用同一組模型；
- 直播軟體偵測固定回傳沒有、不跑 nvidia-smi、不搜尋線上歌詞、不下載模型。

用法：<python> tests/numeric/run.py <程式碼資料夾> <素材資料夾> <模型資料夾> <輸出資料夾>
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
NETBLOCK_DIR = HERE.parent / "contract"
code, inputs, models, out = (Path(a).resolve() for a in sys.argv[1:5])
tmp = Path(tempfile.mkdtemp(prefix="leihu-numeric-"))
ROOT = tmp / "app"
CONDITIONS: list[str] = []


def cond(msg: str) -> None:
    CONDITIONS.append(msg)
    print(f"[numeric] 條件：{msg}", flush=True)


def model_files() -> dict[str, list]:
    return {
        str(p.relative_to(models)): [p.stat().st_size, int(p.stat().st_mtime)]
        for p in sorted(models.rglob("*"))
        if p.is_file()
    }


# ---- 排出和安裝時一樣的資料夾
shutil.copytree(code, ROOT, ignore=shutil.ignore_patterns("library", "models", "logs", "tests", "__pycache__", ".git"))
(ROOT / "models").mkdir()
import _winapi  # noqa: E402  Windows 的目錄 junction（一般使用者就能建，不需要開發人員模式）

JUNCTIONS: list[Path] = []


@atexit.register  # 中途失敗也要清：先拿掉 junction（只刪連結），再刪暫存資料夾，不會動到模型資料夾
def _cleanup() -> None:
    G = sys.modules.get("gpu_worker")
    for pool in (getattr(G, "POOL", None), getattr(G, "CPU_POOL", None)):
        try:
            pool and pool.shutdown()
        except Exception as e:  # noqa: BLE001
            print(f"[numeric] 關閉子程式失敗：{e}", flush=True)
    for j in JUNCTIONS:
        os.rmdir(j)
    os.chdir(HERE)  # 這個行程的 cwd 在 ROOT：不先離開的話 ROOT 刪不掉
    for _ in range(20):  # 子程式被結束後要一點時間才放掉檔案
        shutil.rmtree(tmp, ignore_errors=True)
        if not tmp.exists():
            break
        time.sleep(0.5)
    else:
        print(f"[numeric] 暫存資料夾刪不掉：{tmp}", flush=True)


for sub in ("uvr", "whisper"):
    if (models / sub).is_dir():
        _winapi.CreateJunction(str(models / sub), str(ROOT / "models" / sub))
        JUNCTIONS.append(ROOT / "models" / sub)
MODELS_BEFORE = model_files()

# LIANTAI_DATA 只給 netblock 寫擋下的連線（tmp/net_blocked.log）；上游不認這個變數。子程式會繼承這些設定
os.environ.update(LIANTAI_DATA=str(tmp), HF_HUB_OFFLINE="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
cond("OMP_NUM_THREADS、MKL_NUM_THREADS＝1（主程式與子程式）；處理時間不是效能基準")
sys.path[:0] = [str(ROOT), str(NETBLOCK_DIR)]
os.chdir(ROOT)
import netblock  # noqa: E402, F401  主程式不連網

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torchcrepe.convert  # noqa: E402

torchcrepe.convert.dither = lambda cents: cents
cond("torchcrepe 隨機抖動關掉（主程式與子程式）")

import pipeline as P  # noqa: E402
import share as SH  # noqa: E402

assert Path(P.__file__).resolve().parent == ROOT, f"import 到別的 pipeline：{P.__file__}"

# ---- 顯示卡子程式：啟動命令前面先插入的程式
CHILD_PREFIX = (
    f"import sys; sys.path[:0] = [{str(ROOT)!r}, {str(NETBLOCK_DIR)!r}]; import netblock; "
    "import torchcrepe.convert as _c; _c.dither = lambda cents: cents; "
    "import gpu_worker as _g; print('[numeric] 子程式載入', _g.__file__, flush=True); "
)
SPAWNS = {"n": 0}
_Popen = subprocess.Popen


class _NumericPopen(_Popen):
    """只改 gpu_worker 開子程式的那個命令；ffmpeg 等其他命令照原樣。要用類別：asyncio 等會繼承 subprocess.Popen。"""

    def __init__(self, args, *a, **k):
        if isinstance(args, list) and len(args) == 3 and args[1] == "-c" and "gpu_worker.serve(" in args[2]:
            args = [args[0], "-c", CHILD_PREFIX + args[2]]
            SPAWNS["n"] += 1
        super().__init__(args, *a, **k)


subprocess.Popen = _NumericPopen


def stub(name: str, value, why: str) -> None:
    if not hasattr(P, name):
        cond(f"找不到 pipeline.{name}，沒有替換（{why}）")
        return
    setattr(P, name, value)
    cond(f"pipeline.{name}：{why}")


stub("search_lyrics_later", lambda *a, **k: None, "不搜尋線上歌詞（要連網，和推論無關）")
stub("_running_apps", lambda names: [], "直播軟體偵測固定回傳沒有")
stub("_sample_gpu_load", lambda: None, "不跑 nvidia-smi（顯示卡被其他程式佔用時結果會不同）")
for n in ("start_hq_download", "start_hqv_download", "start_kana_install", "_upgrade_ytdlp"):
    stub(n, lambda *a, **k: None, "不下載模型、不裝套件")
if hasattr(P, "init_runtime"):  # 重構版才有；上游 import 時就做完
    P.init_runtime()
CFG = {"auto_recognize_lyrics": True, "karaoke_quality": "standard", "vocal_quality": "standard", "lyrics_language": ""}
cond("分離固定用標準模型（vocal_quality、karaoke_quality＝standard）；歌詞用語音辨識（auto_recognize_lyrics）")
OPTS = {
    "vocal_db": 0.0,
    "back_db": 0.0,
    "inst_db": 0.0,
    "reverb": "light",
    "polish": True,
    "formats": ["wav"],
    "range": "full",
}


def report(step, p):
    print(f"  {p:5.2f} {step}", flush=True)


def song(sid: str, src: Path) -> Path:
    d = P.LIB / sid
    d.mkdir(parents=True)
    y, sr = sf.read(str(src), dtype="float32", always_2d=True)
    P.save_stem(d, "mix", y.T)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "id": sid,
                "title": sid,
                "created": 1577836800.0,
                "duration": len(y) / sr,
                "notes": [],
                "steps": {"import": True, "separate": False, "pitch": False, "lyrics": False},
            },
            ensure_ascii=False,
        ),
        "utf-8",
    )
    return d


def keep(d: Path, sid: str, timing: dict) -> None:
    o = out / sid
    o.mkdir(parents=True, exist_ok=True)
    for name in ("lead.flac", "back.flac", "inst.flac", "pitch.json", "lyrics_auto.lrc", "meta.json"):
        if (d / name).exists():
            shutil.copy2(d / name, o / name)
    for p in (d / "takes").glob("*.json"):
        shutil.copy2(p, o / f"take_{p.name}")
    (o / "timing.json").write_text(json.dumps(timing, indent=1), "utf-8")


def inputs_check() -> list[str]:
    """素材和 manifest.json 的 sha256 不一致（或沒有 manifest）就回傳說明。"""
    man = inputs / "manifest.json"
    if not man.exists():
        return [f"{man} 不存在，素材沒有核對"]
    want = json.loads(man.read_text("utf-8"))["sha256"]
    return [
        f"素材 {name} 的 sha256 和 manifest.json 不同"
        for name, h in want.items()
        if hashlib.sha256((inputs / name).read_bytes()).hexdigest() != h
    ]


def environment() -> dict:
    env = {"python": sys.version.split()[0], "executable": sys.executable}
    from importlib.metadata import version

    for pkg in ("torch", "onnxruntime", "onnxruntime-gpu", "audio-separator", "faster-whisper", "ctranslate2", "torchcrepe"):
        try:
            env[pkg] = version(pkg)
        except Exception:  # noqa: S112  沒裝的套件就不記
            continue
    try:
        import onnxruntime

        env["onnx_providers"] = onnxruntime.get_available_providers()
    except Exception as e:
        env["onnx_providers"] = f"讀不到：{e}"
    probe = ROOT / "models" / "gpu_probe.json"
    env["gpu_probe"] = json.loads(probe.read_text("utf-8")) if probe.exists() else None
    return env


def main() -> None:
    out.mkdir(parents=True, exist_ok=True)
    problems = inputs_check()
    for p in problems:
        print(f"[numeric] 異常：{p}", flush=True)
    for sid in ("songsing01", "songspeech01"):
        print(sid, flush=True)
        d = song(sid, inputs / ("sing.flac" if "sing" in sid else "speech.flac"))
        timing = {}
        t0 = time.time()
        P.process_song(sid, ["separate", "pitch", "lyrics"], CFG)(report)
        timing["process"] = round(time.time() - t0, 1)
        if sid == "songsing01":
            td = d / "takes"
            td.mkdir(exist_ok=True)
            shutil.copy2(inputs / "sing_take.wav", td / "take01.wav")
            (td / "take01.json").write_text(
                json.dumps({"id": "take01", "key": 0, "speed": 1.0, "start": 0.0, "status": "scoring"}), "utf-8"
            )
            t0 = time.time()
            P.score_take(d, "take01", report)
            timing["score"] = round(time.time() - t0, 1)
            y, sr, _ = SH.mix_take(d, "take01", OPTS)
            (out / sid).mkdir(parents=True, exist_ok=True)
            sf.write(
                str(out / sid / "export.wav"),
                np.asarray(y).T if np.asarray(y).ndim == 2 and np.asarray(y).shape[0] == 2 else y,
                sr,
                subtype="FLOAT",
            )
        keep(d, sid, timing)
    changed = sorted(k for k in MODELS_BEFORE.keys() | model_files().keys() if MODELS_BEFORE.get(k) != model_files().get(k))
    problems += [f"模型資料夾的檔案在執行中有變動：{k}" for k in changed]
    blocked = tmp / "net_blocked.log"
    if blocked.exists():
        shutil.copy2(blocked, out / "net_blocked.log")
        problems.append(f"有程式想對外連線（已擋下），見 {out / 'net_blocked.log'}")
    if SPAWNS["n"] == 0:
        problems.append("沒有開過顯示卡子程式：分離、音高、語音辨識可能都在主程式裡做")
    (out / "run.json").write_text(
        json.dumps(
            {
                "code": str(code),
                "conditions": CONDITIONS,
                "problems": problems,
                "worker_spawns": SPAWNS["n"],
                "environment": environment(),
                "inputs": json.loads((inputs / "manifest.json").read_text("utf-8"))
                if (inputs / "manifest.json").exists()
                else None,
            },
            ensure_ascii=False,
            indent=1,
        ),
        "utf-8",
    )
    print("完成：", out, flush=True)


main()
