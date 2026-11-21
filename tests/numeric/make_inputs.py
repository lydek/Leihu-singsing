"""數值比對的素材（docs/refactor-port-plan.md 3.8 第 1 層）：程式產生，不放真實歌曲。

- sing：已知旋律合成的「人聲」（泛音、顫音、每個音之間留空）＋和弦伴奏，30 秒
- sing_take：同一段旋律整體偏高 30 cents、晚 0.2 秒的錄音（單聲道）
- speech：Windows 內建語音合成（System.Speech）唸的中文句子＋輕柔的伴奏，給歌詞辨識用

語音合成的聲音每台電腦不一定一樣：只在一台電腦產生，再把整個資料夾複製到其他電腦，
run.py 會核對 manifest.json 的 sha256（不一致就列為異常）。沒有 zh-TW 語音時直接失敗，不退回其他語言的聲音。

用法：<python> tests/numeric/make_inputs.py tests/private/numeric/inputs
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

SR = 44100
MELODY = [60, 62, 64, 65, 67, 65, 64, 62, 60, 64, 67, 72, 71, 69, 67, 65, 64, 62, 64, 60]  # 每個音 1.5 秒
NOTE, GAP = 1.5, 0.25
SENTENCES = [
    "今天天氣很好，我們一起去公園散步。",
    "晚上的風輕輕地吹，月亮掛在天空。",
    "請把窗戶打開，讓陽光照進來。",
    "我喜歡在下雨的時候聽音樂。",
]


def _voice(midi: float, dur: float, cents: float = 0.0) -> np.ndarray:
    t = np.arange(int(SR * dur)) / SR
    f0 = 440 * 2 ** ((midi + cents / 100 - 69) / 12) * 2 ** (0.2 / 12 * np.sin(2 * np.pi * 5.5 * t))  # 顫音 ±20 cents
    phase = 2 * np.pi * np.cumsum(f0) / SR
    y = sum(np.sin(k * phase) / k**1.3 for k in range(1, 10))
    env = np.minimum(1, np.minimum(t / 0.05, (dur - t) / 0.08))
    return (y * env / 3).astype(np.float32)


def _melody(cents: float = 0.0, delay: float = 0.0) -> np.ndarray:
    out = np.zeros(int(SR * (len(MELODY) * NOTE + delay + 1)), dtype=np.float32)
    for i, m in enumerate(MELODY):
        a = int(SR * (i * NOTE + delay))
        v = _voice(m, NOTE - GAP, cents)
        out[a : a + len(v)] += v
    return out


def _accomp(n: int) -> np.ndarray:
    t = np.arange(n) / SR
    chords = [(48, 52, 55), (53, 57, 60), (55, 59, 62), (48, 52, 55)]
    y = np.zeros(n, dtype=np.float32)
    for i in range(0, n, int(SR * 3)):
        seg = slice(i, min(n, i + int(SR * 3)))
        for m in chords[(i // int(SR * 3)) % 4]:
            f = 440 * 2 ** ((m - 69) / 12)
            y[seg] += 0.12 * np.sign(np.sin(2 * np.pi * f * t[seg])) * 0.5 + 0.12 * np.sin(2 * np.pi * f * t[seg])
    kick = np.exp(-((t % 0.75) * 30)) * np.sin(2 * np.pi * 60 * t)
    return (y * 0.5 + 0.3 * kick).astype(np.float32)


def _stereo(vocal: np.ndarray, acc: np.ndarray) -> np.ndarray:
    n = max(len(vocal), len(acc))
    vocal, acc = np.pad(vocal, (0, n - len(vocal))), np.pad(acc, (0, n - len(acc)))
    left, right = vocal * 0.5 + acc * 0.6, vocal * 0.5 + np.roll(acc, 300) * 0.6
    y = np.stack([left, right], axis=1)
    return (y / np.max(np.abs(y)) * 0.8).astype(np.float32)


def _tts(text: str, path: Path) -> str:
    ps = (
        "Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'zh-TW' } | Select-Object -First 1; "
        "if (-not $v) { throw '沒有 zh-TW 語音' }; $s.SelectVoice($v.VoiceInfo.Name); Write-Output $v.VoiceInfo.Name; "
        f"$s.SetOutputToWaveFile('{path}'); $s.Speak('{text}'); $s.Dispose()"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=120, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip()


def _speech() -> tuple[np.ndarray, str]:
    parts, voice = [], ""
    with tempfile.TemporaryDirectory() as tmp:
        for i, s in enumerate(SENTENCES):
            p = Path(tmp) / f"{i}.wav"
            voice = _tts(s, p)
            y, sr = sf.read(str(p), dtype="float32", always_2d=True)
            y = resample_poly(y[:, 0], SR, sr).astype(np.float32)
            parts += [np.zeros(int(SR * 1.0), np.float32), y / np.max(np.abs(y)) * 0.6]
    return np.concatenate([*parts, np.zeros(SR, np.float32)]), voice


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    voice = _melody()
    sf.write(str(out / "sing.flac"), _stereo(voice, _accomp(len(voice))), SR, subtype="PCM_24")
    take = _melody(cents=30, delay=0.2) * 0.7
    sf.write(str(out / "sing_take.wav"), take, SR, subtype="PCM_16")
    speech, tts_voice = _speech()
    sf.write(str(out / "speech.flac"), _stereo(speech, _accomp(len(speech)) * 0.3), SR, subtype="PCM_24")
    files = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(out.iterdir()) if f.suffix != ".json"}
    (out / "manifest.json").write_text(
        json.dumps({"tts_voice": tts_voice, "sha256": files}, ensure_ascii=False, indent=1), "utf-8"
    )
    print("素材寫到", out, "；語音：", tts_voice)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
