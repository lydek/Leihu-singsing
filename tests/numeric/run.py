"""用指定的程式碼處理數值比對素材，輸出各步驟的結果（docs/refactor-port-plan.md 3.8）。

要用裝好 torch、audio-separator、faster-whisper、torchcrepe 的 Python 執行（已安裝版本的內嵌 Python），
通常由 tests/numeric/baseline.py 呼叫：它把這支程式的輸出（含顯示卡子程式的訊息）存成 log、再比對結果。
執行環境（暫存 ROOT、模型 junction、子程式命令、替換掉的背景功能）見 sandbox.py，這裡用 deterministic=True。

為了讓同一份程式連跑兩次結果相同，這裡的執行條件和平常不同（全部寫進 <輸出>/run.json 的 conditions，報告會列出）：
- OMP_NUM_THREADS、MKL_NUM_THREADS＝1（排除浮點加總順序的差異；所以這裡的處理時間不是效能基準）；
- torchcrepe 的隨機抖動（±20 cents）關掉；
- 分離固定用標準模型（vocal_quality、karaoke_quality＝standard），兩台電腦、有沒有顯示卡都用同一組模型；
- 直播軟體偵測固定回傳沒有、不跑 nvidia-smi、不搜尋線上歌詞、不下載模型。

相似度基準（similarity.py）另外用：
- --normal：執行緒數、torchcrepe 隨機抖動都照平常（deterministic=False），量「上游自己跑多次」的自然差異；
- --quality=auto：分離品質照平常的 auto（顯示卡上會用高品質模型）；預設 standard；
- --songs=<資料夾>：另外處理裡面的音檔（真實歌曲，走匯入→分離→音高→歌詞），輸出在 song01、song02…（依檔名排序）。

用法：<python> tests/numeric/run.py <程式碼資料夾> <素材資料夾> <模型資料夾> <輸出資料夾> [--normal] [--quality=auto] [--songs=<資料夾>]
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import Sandbox  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
opt = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in sys.argv[1:] if a.startswith("--"))
code, inputs, models, out = (Path(a).resolve() for a in args[:4])
AUDIO = (".flac", ".wav", ".mp3", ".m4a", ".ogg")
SONG_FILES = sorted(x for x in Path(opt["songs"]).glob("*") if x.suffix.lower() in AUDIO) if opt.get("songs") else []
if opt.get("songs") and not SONG_FILES:  # 明確要求真實歌曲卻一首都沒有：不要默默只跑合成素材
    sys.exit(f"--songs={opt['songs']} 裡沒有音檔（{'、'.join(AUDIO)}）")
BOX = Sandbox(code, models, "[numeric]", deterministic="normal" not in opt, device=opt.get("device", "auto"))
P, SH = BOX.P, BOX.SH
cond = BOX.cond

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

QUALITY = opt.get("quality", "standard")
CFG = {"auto_recognize_lyrics": True, "karaoke_quality": QUALITY, "vocal_quality": QUALITY, "lyrics_language": ""}
if QUALITY == "standard":
    cond("分離固定用標準模型（vocal_quality、karaoke_quality＝standard）；歌詞用語音辨識（auto_recognize_lyrics）")
else:
    cond(f"分離品質 {QUALITY}；歌詞用語音辨識（auto_recognize_lyrics）")
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
    for i, src in enumerate(SONG_FILES, 1):
        sid = f"song{i:02d}"
        print(sid, flush=True)
        d = P.LIB / sid
        d.mkdir(parents=True)
        shutil.copy2(src, d / f"original{src.suffix.lower()}")
        (d / "meta.json").write_text(
            json.dumps({"id": sid, "title": sid, "created": 1577836800.0, "notes": [],
                        "steps": {"import": False, "separate": False, "pitch": False, "lyrics": False}}),
            "utf-8",
        )
        t0 = time.time()
        P.process_song(sid, ["import", "separate", "pitch", "lyrics"], CFG)(report)
        keep(d, sid, {"process": round(time.time() - t0, 1)})
    problems += BOX.problems(out)
    (out / "run.json").write_text(
        json.dumps(
            {
                "code": str(code),
                "conditions": BOX.conditions,
                "problems": problems,
                "worker_spawns": BOX.spawns,
                "songs": {f"song{i:02d}": hashlib.sha256(f.read_bytes()).hexdigest() for i, f in enumerate(SONG_FILES, 1)},
                "environment": BOX.environment(),
                "models": BOX.models_sha,
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



def write_aborted(e: BaseException) -> None:
    """中途丟例外：照樣寫 run.json（標 incomplete），把例外和沙盒查到的問題、被測程式的 logs/ 留下來給報告。"""
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(
        json.dumps(
            {"code": str(code), "incomplete": True, "conditions": BOX.conditions,
             "problems": [f"執行中斷：{type(e).__name__}: {e}"[:300]] + BOX.problems(out),
             "worker_spawns": BOX.spawns, "environment": BOX.environment(), "models": BOX.models_sha},
            ensure_ascii=False, indent=1),
        "utf-8",
    )


try:
    main()
except BaseException as e:
    write_aborted(e)
    raise
