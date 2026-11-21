"""契約快照的測試資料：合成的歌曲庫（不放商業歌曲，見 rust-architecture-and-test-plan.md 4.2）。

- songnew001：新格式（FLAC 分軌、lufs、三份歌詞、線上歌詞候選、兩次錄音）
- songold002：舊格式（WAV 分軌、沒有 lufs、留著舊的 v_k* 移調資料夾、只有 lyrics.lrc）
每首都放 lyrics_candidates.json：缺這個檔又有 lyrics_auto.lrc 的歌，GET lyrics/candidates 會連網。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

NEW, OLD = "songnew001", "songold002"
# 用 2000 年：快照正規化只把 2020 年以後的時間戳記換掉，固定的錄音編號才會原樣留在快照裡
TAKE_DONE, TAKE_PENDING = "20000101-000000-fix1", "20000101-000000-fix2"
SR = 44100
DUR = 2.0
FAKE_KEY = "sk-ant-api03-TESTKEY-0000000000000000-abcd"
CANDIDATE = {
    "source": "LRCLIB",
    "id": "lrclib:1",
    "track": "合成測試歌",
    "artist": "測試歌手",
    "album": "",
    "duration": 2.0,
    "synced": "[00:00.20]線上第一句\n[00:01.10]線上第二句",
    "plain": "線上第一句\n線上第二句",
}


def _tone(freq: float, stereo: bool = True) -> np.ndarray:
    t = np.arange(int(SR * DUR)) / SR
    y = (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([y, y], axis=1) if stereo else y


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")


def _pitch() -> dict:
    hop = 0.01
    n = int(DUR / hop)
    midi = [None if i < 20 else round(60 + (i // 50), 2) for i in range(n)]
    return {"hop": hop, "midi": midi, "notes": [[0.2, 0.7, 60], [0.7, 1.2, 61], [1.2, 1.7, 62]]}


def _take(tid: str, done: bool) -> dict:
    info = {
        "id": tid,
        "created": 1577836800.0,
        "status": "done" if done else "scoring",
        "start": 0.0,
        "key": 0,
        "speed": 1.0,
        "duration": DUR,
    }
    if done:
        info.update(
            offset=0.0,
            acc=0.75,
            mean=-12.0,
            spread=30.0,
            frames=150,
            octave=0,
            algo=4,
            lines=[
                {"t": 0.2, "text": "第一句", "acc": 0.8, "mean": -10.0},
                {"t": 1.1, "text": "第二句", "acc": 0.7, "mean": -14.0},
            ],
            pitch={"t0": 0.0, "hop": 0.01, "midi": [None, 60.1, 60.2, None]},
            key_hint={"shift": 0, "share": 0.1, "suggest": 0},
        )
    else:
        info.update(acc=None)
    return info


def build(data: Path) -> None:
    lib = data / "library"
    lrc = "[00:00.20]第一句\n[00:01.10]第二句\n"

    d = lib / NEW
    (d / "takes").mkdir(parents=True)
    for name, f in (("mix", 220), ("lead", 440), ("back", 330), ("inst", 110)):
        sf.write(d / f"{name}.flac", _tone(f), SR, subtype="PCM_16")
    _write(
        d / "meta.json",
        json.dumps(
            {
                "id": NEW,
                "title": "合成測試歌",
                "artist": "測試歌手",
                "created": 1577836800.0,
                "duration": DUR,
                "steps": {"import": True, "separate": True, "pitch": True, "lyrics": True},
                "separation": "UVR-MDX-NET-Inst_HQ_3.onnx",
                "lyrics_source": "online",
                "lyrics_language": "zh",
                "notes": [],
                "lufs": -18.0,
                "key": 0,
            },
            ensure_ascii=False,
        ),
    )
    _write(d / "pitch.json", json.dumps(_pitch(), separators=(",", ":")))
    _write(d / "lyrics.lrc", lrc)
    _write(d / "lyrics_auto.lrc", "[00:00.20]辨識第一句\n[00:01.10]辨識第二句\n")
    _write(d / "lyrics_online.lrc", lrc)
    _write(d / "lyrics_candidates.json", json.dumps([CANDIDATE], ensure_ascii=False))
    for tid, done in ((TAKE_DONE, True), (TAKE_PENDING, False)):
        sf.write(d / "takes" / f"{tid}.wav", _tone(440, stereo=False), 48000, subtype="PCM_16")
        _write(d / "takes" / f"{tid}.json", json.dumps(_take(tid, done), ensure_ascii=False))

    d = lib / OLD
    (d / "v_k+2").mkdir(parents=True)
    for name, f in (("mix", 247), ("lead", 494), ("back", 370), ("inst", 123)):
        sf.write(d / f"{name}.wav", _tone(f), SR, subtype="PCM_16")
    sf.write(d / "v_k+2" / "lead.wav", _tone(554), SR, subtype="PCM_16")
    _write(
        d / "meta.json",
        json.dumps(
            {
                "id": OLD,
                "title": "舊格式的歌",
                "created": 1500000000.0,
                "duration": DUR,
                "steps": {"import": True, "separate": True, "pitch": False, "lyrics": True},
                "notes": [],
            },
            ensure_ascii=False,
        ),
    )
    _write(d / "lyrics.lrc", lrc)
    _write(d / "lyrics_candidates.json", "[]")

    _write(data / "config.json", json.dumps({"anthropic_api_key": FAKE_KEY, "karaoke_quality": "auto"}))
    _write(lib / "categories.json", json.dumps(["測試分類"], ensure_ascii=False))
