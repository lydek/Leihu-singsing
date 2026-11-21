"""數值比對（docs/refactor-port-plan.md 3.8）：比兩次 run.py 的輸出，並把每次的結果和素材的標準答案比。

exact(a, b)：逐檔核對是否完全相同（第一階段、CPU 的判定）。音檔比取樣率、聲道、長度、格式和每一個取樣
  （不比檔頭：libsndfile 寫浮點 WAV 時檔頭有時間戳記）；JSON 去掉 VOLATILE 的快取欄位後，連型別一起比；
  其他檔案比原始位元組。檔案清單也要相同（timing.json、run.json、net_blocked.log、app-logs/ 不比）。
pair(a, b)：兩份輸出之間的差距，每一項都有門檻（沿用舊分支 tests/test_numeric.py 的門檻，補上音符結束、
  音符音高、pitch hop、評分 mean／spread／offset／逐句）。用在顯示卡、第二階段等不要求完全相同的比對。
quality(d)：一份輸出和標準答案的差距，只記數值、不判斷通過。合成素材的旋律、錄音偏高 30 cents 晚 0.2 秒、
  語音句子、伴奏都是已知的（make_inputs.py），所以 CPU 和顯示卡的結果可以各自和答案比，不必互相比。
  （CPU 的音高分析用 torchcrepe tiny、顯示卡用 full，兩台電腦的數值本來就不會逐值相同。）

用法：<python> tests/numeric/compare.py <輸出 A> <輸出 B> [--similar]
  預設先逐檔核對（exact）再印差距表（pair），兩者有一項不通過結束代碼就是 1；
  --similar 只看差距表（顯示卡、第二階段用）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_inputs as MI  # noqa: E402

SONGS = ("songsing01", "songspeech01")
AUDIO = (".flac", ".wav", ".mp3", ".m4a", ".ogg")  # run.py --songs、similarity.py 認的音檔
META_KEYS = ("separation", "vocal_model", "karaoke", "acapella", "language", "lyrics_unreliable", "harmony")
# JSON 裡每次都會變、和輸出內容無關的欄位：combo_lufs_sig 是用檔案 mtime（奈秒）組成的響度快取簽章
VOLATILE = {"meta.json": ("combo_lufs_sig",)}
SKIP = ("timing.json", "run.json", "net_blocked.log")  # exact() 不比的檔案（處理時間、執行環境、診斷）


def row(item: str, value, limit: str, ok: bool | None) -> dict:
    return {"item": item, "value": value, "limit": limit, "ok": None if ok is None else bool(ok)}  # numpy bool 不能寫進 JSON


def sdr(ref: np.ndarray, est: np.ndarray) -> float:
    """兩段形狀必須相同（長度不同不默默截齊：呼叫端先用 audio_pair 檢查）。"""
    if ref.shape != est.shape:
        raise ValueError(f"形狀不同：{ref.shape} vs {est.shape}")
    err = np.sum((ref - est) ** 2)
    return float("inf") if err == 0 else float(10 * np.log10(np.sum(ref**2) / err))


def audio_pair(pa: Path, pb: Path) -> tuple[np.ndarray, np.ndarray, str]:
    """讀兩個音檔；取樣率、聲道、長度不同時回傳的說明不是空字串（不截齊、不重新取樣）。"""
    ia, ib = sf.info(str(pa)), sf.info(str(pb))
    pairs = (("取樣率", ia.samplerate, ib.samplerate), ("聲道", ia.channels, ib.channels), ("長度", ia.frames, ib.frames))
    bad = [f"{what} {x} vs {y}" for what, x, y in pairs if x != y]
    ya, yb = (sf.read(str(p), dtype="float64", always_2d=True)[0] for p in (pa, pb))
    return ya, yb, "、".join(bad)


def _canon(v) -> str:
    """JSON 的正規形式：True 和 1、1 和 1.0 寫出來不同，所以連型別一起比。"""
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _files(d: Path) -> set[str]:
    return {
        p.relative_to(d).as_posix()
        for p in d.rglob("*")
        if p.is_file() and p.name not in SKIP and "app-logs" not in p.parts
    }


def exact(a: Path, b: Path) -> list[dict]:  # noqa: C901
    """逐檔核對兩份輸出是否完全相同。"""
    fa, fb = _files(a), _files(b)
    rows = [row(f, f"只有{'A' if f in fa else 'B'}有這個檔案", "兩邊都有", False) for f in sorted(fa ^ fb)]
    for f in sorted(fa & fb):
        pa, pb = a / f, b / f
        if pa.suffix.lower() in (".flac", ".wav"):
            ia, ib = sf.info(str(pa)), sf.info(str(pb))
            keys = ("samplerate", "channels", "frames", "subtype")
            fmt = [f"{k} {getattr(ia, k)} vs {getattr(ib, k)}" for k in keys if getattr(ia, k) != getattr(ib, k)]
            if fmt:
                rows.append(row(f, "、".join(fmt), "格式與長度相同", False))
                continue
            ya, yb = (sf.read(str(p), dtype="float64", always_2d=True)[0] for p in (pa, pb))
            n = int(np.sum(ya != yb))
            v = "每個取樣都相同" if n == 0 else f"{n} 個取樣不同（最大差 {np.max(np.abs(ya - yb)):.3g}）"
            rows.append(row(f, v, "完全相同", n == 0))
        elif pa.suffix == ".json":
            ja, jb = (json.loads(p.read_text("utf-8")) for p in (pa, pb))
            drop = VOLATILE.get(pa.name, ())
            if isinstance(ja, dict) and isinstance(jb, dict):
                ja, jb = ({k: v for k, v in x.items() if k not in drop} for x in (ja, jb))
            same = _canon(ja) == _canon(jb)
            keys = []
            if not same and isinstance(ja, dict) and isinstance(jb, dict):
                keys = sorted(k for k in ja.keys() | jb.keys() if _canon(ja.get(k)) != _canon(jb.get(k)))
            note = f"（不比 {'、'.join(drop)}）" if drop else ""
            rows.append(row(f, "相同" + note if same else f"不同：{'、'.join(keys) or '內容'}", "完全相同", same))
        else:
            same = pa.read_bytes() == pb.read_bytes()
            rows.append(row(f, "位元組相同" if same else "位元組不同", "完全相同", same))
    return rows


def _midi(p: Path) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in json.loads(p.read_text("utf-8"))["midi"]], dtype=float)


def _lrc(p: Path) -> list[tuple[float, str]]:
    out = []
    for line in p.read_text("utf-8").splitlines() if p.exists() else []:
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", line)
        if m:
            out.append((int(m[1]) * 60 + float(m[2]), m[3]))
    return out


def _plain(s: str) -> str:
    return re.sub(r"[\W_]+", "", s)


def cer(ref: str, hyp: str) -> float:
    """字元錯誤率（編輯距離 ÷ 參考長度）。"""
    prev = list(range(len(hyp) + 1))
    for i, ca in enumerate(ref, 1):
        cur = [i]
        for j, cb in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / max(1, len(ref))


def _json(p: Path) -> dict | None:
    return json.loads(p.read_text("utf-8")) if p.exists() else None


def songs(a: Path, b: Path) -> list[str]:
    """合成素材的兩首，加上 run.py --songs 處理的真實歌曲（song01、song02…，任一邊有就列，缺的那邊會報檔案不存在）。"""
    extra = {x.name for d in (a, b) if d.exists() for x in d.iterdir() if x.is_dir() and re.fullmatch(r"song\d+", x.name)}
    return [*SONGS, *sorted(extra)]


def _lyrics(a: Path, b: Path, sid: str) -> list[dict]:
    la, lb = (_lrc(d / sid / "lyrics_auto.lrc") for d in (a, b))
    ta, tb = ("".join(t for _, t in x) for x in (la, lb))
    v = cer(ta, tb)
    rows = [row(f"{sid} 歌詞字元錯誤率", f"{v:.3f}（{len(ta)} 字 vs {len(tb)} 字）", "< 0.01", bool(ta) and v < 0.01)]
    if la and len(la) == len(lb):
        diff = float(np.median([abs(x[0] - y[0]) for x, y in zip(la, lb, strict=True)]) * 1000)
        rows.append(row(f"{sid} 歌詞起點差中位數", f"{diff:.0f} ms", "< 80 ms", diff < 80))
    else:
        rows.append(row(f"{sid} 歌詞行數", f"{len(la)} vs {len(lb)}", "相同", False))
    return rows


def pair(a: Path, b: Path) -> list[dict]:  # noqa: C901
    rows: list[dict] = []
    for sid in songs(a, b):
        for stem in ("lead", "back", "inst"):
            pa, pb = a / sid / f"{stem}.flac", b / sid / f"{stem}.flac"
            if not (pa.exists() and pb.exists()):
                rows.append(row(f"{sid} {stem} 分離", "檔案不存在", "> 30 dB", False))
                continue
            ya, yb, bad = audio_pair(pa, pb)
            if bad:
                rows.append(row(f"{sid} {stem} 分離", bad, "取樣率、聲道、長度相同", False))
                continue
            v = sdr(ya, yb)
            rows.append(row(f"{sid} {stem} 分離 SDR", f"{v:.1f} dB", "> 30 dB", v > 30))
        pa, pb = a / sid / "pitch.json", b / sid / "pitch.json"
        if not (pa.exists() and pb.exists()):
            rows.append(row(f"{sid} 音高", "pitch.json 不存在", "", False))
        else:
            ha, hb = (json.loads(p.read_text("utf-8")).get("hop") for p in (pa, pb))
            rows.append(row(f"{sid} 音高 hop", f"{ha} vs {hb}", "相同", ha == hb))
            ma, mb = _midi(pa), _midi(pb)
            if len(ma) != len(mb):
                rows.append(row(f"{sid} 音高影格數", f"{len(ma)} vs {len(mb)}", "相同", False))
            else:
                agree = float(np.mean(np.isnan(ma) == np.isnan(mb)))
                both = ~np.isnan(ma) & ~np.isnan(mb)
                cents = float(np.median(np.abs(ma[both] - mb[both])) * 100) if both.any() else 0.0
                rows.append(row(f"{sid} 有聲判斷一致率", f"{agree:.4f}", ">= 0.98", agree >= 0.98))
                rows.append(row(f"{sid} 音高差中位數", f"{cents:.2f} cents", "< 10 cents", cents < 10))
            na, nb = (json.loads(p.read_text("utf-8"))["notes"] for p in (pa, pb))
            ok = abs(len(na) - len(nb)) <= max(1, 0.02 * len(na))
            rows.append(row(f"{sid} 音符數", f"{len(na)} vs {len(nb)}", "差 <= max(1, 2%)", ok))
            if na and nb:
                # 每個 A 的音符配對 B 裡起點最近的音符，比起點、結束、音高（音符數不同時也能比）
                sb = np.array([n[0] for n in nb])
                match = [(n, nb[int(np.argmin(np.abs(sb - n[0])))]) for n in na]
                d0 = float(np.median([abs(x[0] - y[0]) for x, y in match]) * 1000)
                d1 = float(np.median([abs(x[1] - y[1]) for x, y in match]) * 1000)
                dm = sum(1 for x, y in match if x[2] != y[2])
                rows.append(row(f"{sid} 音符起點差中位數", f"{d0:.1f} ms", "< 20 ms", d0 < 20))
                rows.append(row(f"{sid} 音符結束差中位數", f"{d1:.1f} ms", "< 20 ms", d1 < 20))
                rows.append(row(f"{sid} 音符音高不同的個數", f"{dm}／{len(match)}", "<= max(1, 2%)",
                                dm <= max(1, 0.02 * len(match))))
        ma_, mb_ = _json(a / sid / "meta.json"), _json(b / sid / "meta.json")
        if ma_ is None or mb_ is None:
            rows.append(row(f"{sid} meta", "meta.json 不存在", "", False))
        else:
            da, db = ({k: m.get(k) for k in META_KEYS} for m in (ma_, mb_))
            bad = {k: f"{da[k]} vs {db[k]}" for k in META_KEYS if _canon(da[k]) != _canon(db[k])}  # True 和 1 不同
            rows.append(row(f"{sid} meta（{'、'.join(META_KEYS)}）", bad or "相同", "相同", not bad))
        # 合成旋律那首沒有人聲：Whisper 每次「聽到」的幻覺字句不同，不比歌詞，由 meta 的 lyrics_unreliable 比對
        if sid != "songsing01":
            rows += _lyrics(a, b, sid)
    ka, kb = (_json(d / "songsing01" / "take_take01.json") for d in (a, b))
    if not (ka and kb):
        rows.append(row("評分", "take_take01.json 不存在", "", False))
    else:
        ok = ka.get("status") == kb.get("status") == "done"
        rows.append(row("評分狀態", f"{ka.get('status')} vs {kb.get('status')}", "都是 done", ok))
        if ok:
            d = abs(ka["acc"] - kb["acc"]) * 100
            rows.append(row("評分準確率差", f"{d:.1f} 分（{ka['acc']} vs {kb['acc']}）", "<= 1 分", d <= 1))
            same = ka.get("key_hint") == kb.get("key_hint") and ka.get("octave") == kb.get("octave")
            rows.append(row("評分 key_hint、octave", "相同" if same else f"{ka.get('key_hint')}/{ka.get('octave')} vs "
                            f"{kb.get('key_hint')}/{kb.get('octave')}", "相同", same))
            for k in ("mean", "spread"):
                d = abs((ka.get(k) or 0) - (kb.get(k) or 0))
                rows.append(row(f"評分 {k} 差", f"{d:.1f} cents（{ka.get(k)} vs {kb.get(k)}）", "<= 3 cents", d <= 3))
            d = abs((ka.get("offset") or 0) - (kb.get("offset") or 0))
            rows.append(row("評分 offset 差", f"{d:.3f} 秒（{ka.get('offset')} vs {kb.get('offset')}）", "<= 0.02 秒",
                            d <= 0.02))
            la_, lb_ = ka.get("lines") or [], kb.get("lines") or []
            if len(la_) != len(lb_):
                rows.append(row("評分逐句的句數", f"{len(la_)} vs {len(lb_)}", "相同", False))
            elif la_:
                worst = max(abs(x["acc"] - y["acc"]) for x, y in zip(la_, lb_, strict=True)) * 100
                rows.append(row("評分逐句準確率最大差", f"{worst:.1f} 分（{len(la_)} 句）", "<= 5 分", worst <= 5))
    pa, pb = a / "songsing01" / "export.wav", b / "songsing01" / "export.wav"
    if pa.exists() and pb.exists():
        ya, yb, bad = audio_pair(pa, pb)
        if bad:
            rows.append(row("匯出檔", bad, "取樣率、聲道、長度相同", False))
            return rows
        v = sdr(ya, yb)
        rows.append(row("匯出檔 SDR", f"{v:.1f} dB", "> 30 dB", v > 30))
        # 舊分支比的是 LUFS 差 <= 0.1；這裡用 RMS 差（不用 import 被測程式的響度函式）
        rd = abs(20 * np.log10(np.sqrt(np.mean(ya**2)) / np.sqrt(np.mean(yb**2))))
        rows.append(row("匯出檔 RMS 差", f"{rd:.3f} dB", "<= 0.1 dB", rd <= 0.1))
    else:
        rows.append(row("匯出檔", "export.wav 不存在", "", False))
    return rows


def _truth_sing() -> tuple[np.ndarray, np.ndarray]:
    """sing.flac 裡的人聲與伴奏（和 make_inputs 同樣的混音與正規化），各是 (取樣, 2)。"""
    voice = MI._melody()
    acc = MI._accomp(len(voice))
    n = max(len(voice), len(acc))
    voice, acc = np.pad(voice, (0, n - len(voice))), np.pad(acc, (0, n - len(acc)))
    v2 = np.stack([voice * 0.5, voice * 0.5], axis=1)
    a2 = np.stack([acc * 0.6, np.roll(acc, 300) * 0.6], axis=1)
    k = 0.8 / np.max(np.abs(v2 + a2))
    return v2 * k, a2 * k


def _truth_midi(n: int, hop: float, margin: float = 0.05) -> np.ndarray:
    """每個影格的正確音高；音和音之間、頭尾 margin 秒內是 NaN（不計）。"""
    t = np.arange(n) * hop
    k = np.floor(t / MI.NOTE).astype(int)
    pos = t - k * MI.NOTE
    out = np.full(n, np.nan)
    on = (k < len(MI.MELODY)) & (pos > margin) & (pos < MI.NOTE - MI.GAP - margin)
    out[on] = np.array(MI.MELODY)[k[on]]
    return out


def quality(d: Path) -> list[dict]:
    rows: list[dict] = []
    s = d / "songsing01"
    if (s / "lead.flac").exists():
        voice, acc = _truth_sing()
        for stem, ref in (("lead", voice), ("inst", acc)):
            y = sf.read(str(s / f"{stem}.flac"), dtype="float64", always_2d=True)[0]
            v = f"{sdr(ref, y):.1f} dB" if y.shape == ref.shape else f"長度或聲道和正確音軌不同：{y.shape} vs {ref.shape}"
            rows.append(row(f"sing {stem} 和正確音軌的 SDR", v, "", None))
        back = sf.read(str(s / "back.flac"), dtype="float64")[0]
        rows.append(row("sing back（應該是空的）RMS", f"{20 * np.log10(np.sqrt(np.mean(back**2)) + 1e-12):.1f} dBFS", "", None))
    if (s / "pitch.json").exists():
        pj = json.loads((s / "pitch.json").read_text("utf-8"))
        m = _midi(s / "pitch.json")
        truth = _truth_midi(len(m), pj.get("hop", 0.01))
        on = ~np.isnan(truth)
        hit = on & ~np.isnan(m)
        silent = np.isnan(_truth_midi(len(m), pj.get("hop", 0.01), margin=-0.05))  # 音和音之間再往內縮 0.05 秒
        rows.append(row("sing 有聲影格找到的比例", f"{np.mean(~np.isnan(m[on])):.4f}", "", None))
        rows.append(row("sing 音和音之間判為有聲的比例（程式會把有聲段落往前後延伸最多 0.25 秒，空隙正好 0.25 秒）", f"{np.mean(~np.isnan(m[silent])):.4f}", "", None))
        rows.append(row("sing 音高誤差中位數", f"{np.median(np.abs(m[hit] - truth[hit])) * 100:.1f} cents", "", None))
        rows.append(row("sing 音符數（旋律 20 個音）", len(pj["notes"]), "", None))
    k = _json(s / "take_take01.json")
    if k:
        rows.append(row("評分狀態／準確率", f"{k.get('status')}／{k.get('acc')}", "", None))
        rows.append(row("評分偵測到的偏差（答案 +30 cents）", f"{k.get('mean')} cents", "", None))
        rows.append(row("評分偵測到的時間差 offset（錄音晚 0.2 秒；正負號依程式定義）", f"{k.get('offset')} 秒", "", None))
    lrc = _lrc(d / "songspeech01" / "lyrics_auto.lrc")
    if (d / "songspeech01").exists():
        hyp, ref = _plain("".join(t for _, t in lrc)), _plain("".join(MI.SENTENCES))
        rows.append(row("speech 歌詞和原句的字元錯誤率", f"{cer(ref, hyp):.3f}（辨識 {len(hyp)} 字／原句 {len(ref)} 字）", "", None))
    for sid in SONGS:
        t = _json(d / sid / "timing.json") or {}
        if t:
            rows.append(row(f"{sid} 處理時間（不是效能基準；執行緒數見執行條件）", "、".join(f"{k} {v} 秒" for k, v in t.items()), "", None))
    return rows


def table(rows: list[dict], with_limit: bool = True) -> list[str]:
    if with_limit:
        out = ["| 項目 | 數值 | 門檻 | 結果 |", "|---|---|---|---|"]
        out += [f"| {r['item']} | {r['value']} | {r['limit']} | {'通過' if r['ok'] else '**超過門檻**'} |" for r in rows]
    else:
        out = ["| 項目 | 數值 |", "|---|---|"] + [f"| {r['item']} | {r['value']} |" for r in rows]
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a, b = Path(sys.argv[1]), Path(sys.argv[2])
    ex = [] if "--similar" in sys.argv else exact(a, b)
    if ex:
        print("## 逐檔核對\n\n" + "\n".join(table(ex)) + "\n\n## 差距\n")
    rs = pair(a, b)
    print("\n".join(table(rs)))
    sys.exit(0 if all(r["ok"] for r in ex + rs) else 1)
