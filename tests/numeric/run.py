"""用指定的程式碼處理數值比對素材，輸出各步驟的結果（docs/refactor-port-plan.md 3.8）。

要用裝好 torch、audio-separator、faster-whisper、torchcrepe 的 Python 執行（已安裝版本的內嵌 Python），
通常由 tests/numeric/baseline.py 呼叫：它把這支程式的輸出（含顯示卡子程式的訊息）存成 log、再比對結果。
執行環境（暫存 ROOT、模型 junction、子程式命令、替換掉的背景功能）見 sandbox.py，這裡用 deterministic=True。

為了讓同一份程式連跑兩次結果相同，這裡的執行條件和平常不同（全部寫進 <輸出>/run.json 的 conditions，報告會列出）：
- OMP_NUM_THREADS、MKL_NUM_THREADS＝1（排除浮點加總順序的差異；所以這裡的處理時間不是效能基準）；
- torchcrepe 的隨機抖動（±20 cents）關掉；
- 分離固定用標準模型（vocal_quality、karaoke_quality＝standard），兩台電腦、有沒有顯示卡都用同一組模型；
- 直播軟體偵測固定回傳沒有、不跑 nvidia-smi、不搜尋線上歌詞、不下載模型。

用法：<python> tests/numeric/run.py <程式碼資料夾> <素材資料夾> <模型資料夾> <輸出資料夾>
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

code, inputs, models, out = (Path(a).resolve() for a in sys.argv[1:5])
BOX = Sandbox(code, models, "[numeric]", deterministic=True)
P, SH = BOX.P, BOX.SH
cond = BOX.cond

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

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
    problems += BOX.problems(out)
    (out / "run.json").write_text(
        json.dumps(
            {
                "code": str(code),
                "conditions": BOX.conditions,
                "problems": problems,
                "worker_spawns": BOX.spawns,
                "environment": BOX.environment(),
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
