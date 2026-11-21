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
VOLATILE = {"meta.json": {"combo_lufs_sig": r"\d+(\|\d+)*"}}  # 欄位 → 合法格式；只把值換成 <sig:段數>
# 每首歌一定要有的輸出（缺了是執行失敗，不是差異）：exact() 只比兩邊都有的檔案，兩邊都漏會看不出來
REQUIRED = {
    "songsing01": ("lead.flac", "back.flac", "inst.flac", "pitch.json", "meta.json", "take_take01.json", "export.wav"),
    "songspeech01": ("lead.flac", "back.flac", "inst.flac", "pitch.json", "meta.json", "lyrics_auto.lrc"),
    "song": ("lead.flac", "back.flac", "inst.flac", "pitch.json", "meta.json"),  # run.py --songs 的 songNN
}
SKIP = ("timing.json", "run.json", "net_blocked.log")  # exact() 不比的檔案（處理時間、執行環境、診斷）


def row(item: str, value, limit: str, ok: bool | None, num: float | None = None) -> dict:
    """value 是給人看的文字；num 是沒有四捨五入的數字（相似度下限用它，不從文字解析）。
    inf、-inf、nan 存成字串 "inf"、"-inf"、"nan"（JSON 沒有這些值；寫檔一律 allow_nan=False）。"""
    if num is not None:
        num = float(num)
        if not np.isfinite(num):
            num = "nan" if np.isnan(num) else ("inf" if num > 0 else "-inf")
    return {"item": item, "value": value, "limit": limit, "ok": None if ok is None else bool(ok), "num": num}


def sdr(ref: np.ndarray, est: np.ndarray) -> float:
    """兩段形狀必須相同（長度不同不默默截齊：呼叫端先用 audio_pair 檢查）。"""
    if ref.shape != est.shape:
        raise ValueError(f"形狀不同：{ref.shape} vs {est.shape}")
    err = np.sum((ref - est) ** 2)
    if err == 0:
        return float("inf")  # 完全相同（兩邊都是靜音也算）
    power = np.sum(ref**2)
    return float("-inf") if power == 0 else float(10 * np.log10(power / err))  # 參考是靜音、另一邊有聲音：最差


def rms_db_diff(ya: np.ndarray, yb: np.ndarray) -> float:
    """兩段的 RMS 差（dB）；兩段都是靜音是 0，只有一段靜音是 inf。"""
    ra, rb = np.sqrt(np.mean(ya**2)), np.sqrt(np.mean(yb**2))
    if ra == 0 and rb == 0:
        return 0.0
    if ra == 0 or rb == 0:
        return float("inf")
    return float(abs(20 * np.log10(ra / rb)))


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


def _volatile(name: str, j):
    """VOLATILE 欄位：格式正確就換成 <sig:段數>（保留欄位存在與結構），格式不對原樣留下（會被比出差異）。"""
    rules = VOLATILE.get(name, {})
    if not isinstance(j, dict) or not rules:
        return j
    out = dict(j)
    for k, rx in rules.items():
        v = out.get(k)
        if isinstance(v, str) and re.fullmatch(rx, v):
            out[k] = f"<sig:{v.count('|') + 1}>"
    return out


def required(d: Path) -> list[str]:
    """一份輸出缺了哪些必要檔案、哪些 JSON 讀不了、音檔打不開（空清單＝完整）。"""
    bad = []
    sids = [*SONGS, *sorted(x.name for x in d.iterdir() if x.is_dir() and re.fullmatch(r"song\d+", x.name))] \
        if d.exists() else list(SONGS)
    for sid in sids:
        for f in REQUIRED.get(sid, REQUIRED["song"]):
            p = d / sid / f
            if not p.exists():
                bad.append(f"{sid}/{f} 不存在")
                continue
            try:
                if p.suffix == ".json":
                    j = json.loads(p.read_text("utf-8"))
                    shape = {"pitch.json": pitch_shape, "take_take01.json": take_shape}.get(f, _dict_shape)(j)
                    bad += [f"{sid}/{f} 格式不對：{x}" for x in shape[:5]]
                elif p.suffix in (".flac", ".wav"):
                    sf.info(str(p))
            except Exception as e:  # noqa: BLE001
                bad.append(f"{sid}/{f} 讀不了：{type(e).__name__}: {e}"[:200])
    return bad


def _dict_shape(j) -> list[str]:
    return [] if isinstance(j, dict) else [f"不是物件（{type(j).__name__}）"]


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and bool(np.isfinite(v))


def pitch_shape(j) -> list[str]:
    """pitch.json：hop 是有限數字；midi 是非空的清單，每格是有限數字或 null；notes 是 [起點, 結束, 音高] 的清單。"""
    if not isinstance(j, dict):
        return [f"不是物件（{type(j).__name__}）"]
    bad = [] if _finite(j.get("hop")) else [f"hop 是 {j.get('hop')!r}"]
    midi = j.get("midi")
    if not isinstance(midi, list) or not midi:
        bad.append("midi 不是非空的清單")
    elif not all(v is None or _finite(v) for v in midi):
        bad.append("midi 有不是數字也不是 null 的值")
    notes = j.get("notes")
    if not isinstance(notes, list) or not all(isinstance(n, list) and len(n) == 3 and all(_finite(x) for x in n)
                                               for n in notes):
        bad.append("notes 不是 [起點, 結束, 音高] 的清單")
    return bad


def exact(a: Path, b: Path) -> list[dict]:  # noqa: C901
    """逐檔核對兩份輸出是否完全相同（必要檔案缺少另由 required() 檢查）。"""
    fa, fb = _files(a), _files(b)
    rows = [row(f, f"只有{'A' if f in fa else 'B'}有這個檔案", "兩邊都有", False) for f in sorted(fa ^ fb)]
    for f in sorted(fa & fb):
        pa, pb = a / f, b / f
        if pa.suffix.lower() in (".flac", ".wav"):
            ia, ib = sf.info(str(pa)), sf.info(str(pb))
            keys = ("format", "samplerate", "channels", "frames", "subtype")
            fmt = [f"{k} {getattr(ia, k)} vs {getattr(ib, k)}" for k in keys if getattr(ia, k) != getattr(ib, k)]
            if fmt:
                rows.append(row(f, "、".join(fmt), "格式與長度相同", False))
                continue
            ya, yb = (sf.read(str(p), dtype="float64", always_2d=True)[0] for p in (pa, pb))
            # 解碼後逐位元比（+0.0 和 -0.0 也算不同）；不比檔頭（libsndfile 寫浮點 WAV 時有時間戳記）
            n = int(np.sum(ya.view(np.uint64) != yb.view(np.uint64)))
            v = "解碼後每個取樣逐位元相同" if n == 0 else f"{n} 個取樣不同（最大差 {np.max(np.abs(ya - yb)):.3g}）"
            rows.append(row(f, v, "完全相同", n == 0))
        elif pa.suffix == ".json":
            ja, jb = (_volatile(pa.name, json.loads(p.read_text("utf-8"))) for p in (pa, pb))
            drop = tuple(VOLATILE.get(pa.name, {}))
            same = _canon(ja) == _canon(jb)
            keys = []
            if not same and isinstance(ja, dict) and isinstance(jb, dict):
                keys = sorted(k for k in ja.keys() | jb.keys() if _canon(ja.get(k)) != _canon(jb.get(k)))
            note = f"（{'、'.join(drop)} 只比欄位與格式）" if drop else ""
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
    rows = [row(f"{sid} 歌詞字元錯誤率", f"{v:.3f}（{len(ta)} 字 vs {len(tb)} 字）", "< 0.01", bool(ta) and v < 0.01, v)]
    if la and len(la) == len(lb):
        diff = float(np.median([abs(x[0] - y[0]) for x, y in zip(la, lb, strict=True)]) * 1000)
        rows.append(row(f"{sid} 歌詞起點差中位數", f"{diff:.0f} ms", "< 80 ms", diff < 80, diff))
    else:
        rows.append(row(f"{sid} 歌詞行數", f"{len(la)} vs {len(lb)}", "相同", False, abs(len(la) - len(lb))))
    return rows


def match_notes(na: list, nb: list, tol: float = 0.1) -> tuple[list, int, int]:
    """依時間順序一對一配對音符（起點差 < tol 秒才算同一個音），回傳 (配對, A 沒配到的數量, B 沒配到的數量)。"""
    i = j = 0
    out = []
    while i < len(na) and j < len(nb):
        d = na[i][0] - nb[j][0]
        if abs(d) < tol:
            out.append((na[i], nb[j]))
            i += 1
            j += 1
        elif d < 0:
            i += 1
        else:
            j += 1
    return out, len(na) - len(out), len(nb) - len(out)


assert match_notes([[0, 1, 60], [0.01, 1, 60], [1, 2, 60]], [[0, 1, 60], [1, 2, 60], [9, 10, 60]])[1:] == (1, 1)


def take_shape(k: dict) -> list[str]:
    """評分 JSON 的必要欄位與型別（缺少、null、型別不對都列出來；空的 lines、midi 是合法的，但欄位一定要在）。"""
    bad = []

    def need(d, key, kind, where):
        v = d.get(key) if isinstance(d, dict) else None
        ok = isinstance(v, kind) and not (kind is not bool and isinstance(v, bool))
        if not ok:
            bad.append(f"{where}{key} 是 {type(v).__name__}")
        return v if ok else None

    for key in ("acc", "mean", "spread", "offset", "frames"):
        if need(k, key, (int, float), "") is not None and not _finite(k[key]):
            bad.append(f"{key} 不是有限數字")
    # key_hint 可以是 null：上游有效音高少於 150 格時 suggest_key() 回傳 None（評分照樣是 done）
    if "key_hint" not in k:
        bad.append("key_hint 不存在")
    hint = need(k, "key_hint", dict, "") if k.get("key_hint") is not None else None
    if hint is not None:
        need(hint, "share", (int, float), "key_hint.")
        for key in ("shift", "suggest"):
            if key not in hint:
                bad.append(f"key_hint.{key} 不存在")
    if "octave" not in k:
        bad.append("octave 不存在")
    lines = need(k, "lines", list, "")
    for i, ln in enumerate(lines or []):
        for key, kind in (("t", (int, float)), ("text", str), ("n", int), ("acc", (int, float)), ("mean", (int, float))):
            need(ln, key, kind, f"lines[{i}].")
    pitch = need(k, "pitch", dict, "")
    if pitch is not None:
        need(pitch, "hop", (int, float), "pitch.")
        need(pitch, "t0", (int, float), "pitch.")
        midi = need(pitch, "midi", list, "pitch.")
        if midi is not None and not all(v is None or _finite(v) for v in midi):
            bad.append("pitch.midi 有不是數字也不是 null 的值")
    return bad


def _num(d: dict, k: str):
    """評分欄位一定要是數字（缺少、null、字串都不是 0）。"""
    v = d.get(k)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


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
            rows.append(row(f"{sid} {stem} 分離 SDR", f"{v:.1f} dB", "> 30 dB", v > 30, v))
        pa, pb = a / sid / "pitch.json", b / sid / "pitch.json"
        if not (pa.exists() and pb.exists()):
            rows.append(row(f"{sid} 音高", "pitch.json 不存在", "", False))
        else:
            ha, hb = (json.loads(p.read_text("utf-8")).get("hop") for p in (pa, pb))
            rows.append(row(f"{sid} 音高 hop", f"{ha} vs {hb}", "相同", ha == hb,
                            abs(ha - hb) if _finite(ha) and _finite(hb) else None))
            ma, mb = _midi(pa), _midi(pb)
            if len(ma) != len(mb):
                rows.append(row(f"{sid} 音高影格數", f"{len(ma)} vs {len(mb)}", "相同", False))
            else:
                if not len(ma):  # 空的曲線算不出一致率：只記失敗，不留假的一致率、差距（required() 也會擋下這種輸出）
                    rows.append(row(f"{sid} 音高影格", "兩邊都沒有音高影格", "有音高", False))
                else:
                    agree = float(np.mean(np.isnan(ma) == np.isnan(mb)))
                    both = ~np.isnan(ma) & ~np.isnan(mb)
                    cents = float(np.median(np.abs(ma[both] - mb[both])) * 100) if both.any() else 0.0
                    rows.append(row(f"{sid} 有聲判斷一致率", f"{agree:.4f}", ">= 0.98", agree >= 0.98, agree))
                    rows.append(row(f"{sid} 音高差中位數", f"{cents:.2f} cents", "< 10 cents", cents < 10, cents))
            na, nb = (json.loads(p.read_text("utf-8"))["notes"] for p in (pa, pb))
            ok = abs(len(na) - len(nb)) <= max(1, 0.02 * len(na))
            rows.append(row(f"{sid} 音符數", f"{len(na)} vs {len(nb)}", "差 <= max(1, 2%)", ok, abs(len(na) - len(nb))))
            if na and nb:
                match, ua, ub = match_notes(na, nb)
                lim = max(1, 0.02 * max(len(na), len(nb)))
                rows.append(row(f"{sid} 沒有配對到的音符", f"A 多 {ua} 個、B 多 {ub} 個", "各 <= max(1, 2%)",
                                ua <= lim and ub <= lim, ua + ub))
                if match:
                    d0 = float(np.median([abs(x[0] - y[0]) for x, y in match]) * 1000)
                    d1 = float(np.median([abs(x[1] - y[1]) for x, y in match]) * 1000)
                    dm = sum(1 for x, y in match if x[2] != y[2])
                    rows.append(row(f"{sid} 音符起點差中位數", f"{d0:.1f} ms", "< 20 ms", d0 < 20, d0))
                    rows.append(row(f"{sid} 音符結束差中位數", f"{d1:.1f} ms", "< 20 ms", d1 < 20, d1))
                    rows.append(row(f"{sid} 音符音高不同的個數", f"{dm}／{len(match)}", "<= max(1, 2%)",
                                    dm <= lim, dm))
        ma_, mb_ = _json(a / sid / "meta.json"), _json(b / sid / "meta.json")
        if ma_ is None or mb_ is None:
            rows.append(row(f"{sid} meta", "meta.json 不存在", "", False))
        else:
            da, db = ({k: m.get(k) for k in META_KEYS} for m in (ma_, mb_))
            bad = {k: f"{da[k]} vs {db[k]}" for k in META_KEYS if _canon(da[k]) != _canon(db[k])}  # True 和 1 不同
            rows.append(row(f"{sid} meta（{'、'.join(META_KEYS)}）", bad or "相同", "相同", not bad, len(bad)))
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
            shape = sorted({f"{w}：{x}" for w, k in (("A", ka), ("B", kb)) for x in take_shape(k)})
            if shape:
                rows.append(row("評分欄位", "；".join(shape)[:300], "必要欄位都在、型別正確", False))
                return rows + _export_rows(a, b)
            d = abs(ka["acc"] - kb["acc"]) * 100
            rows.append(row("評分準確率差", f"{d:.1f} 分（{ka['acc']} vs {kb['acc']}）", "<= 1 分", d <= 1, d))
            d = abs(ka["frames"] - kb["frames"])
            rows.append(row("評分影格數差", f"{d}（{ka['frames']} vs {kb['frames']}）", "<= 2%",
                            d <= 0.02 * max(ka["frames"], kb["frames"], 1), d))
            # key_hint 拆開比：shift、suggest 要相同，share（比例）允許小差距；octave 要相同
            ha, hb = ka.get("key_hint"), kb.get("key_hint")
            if ha is None or hb is None:  # 上游合法的 null（有效音高不夠）：兩邊要一樣是 null
                rows.append(row("評分 key_hint", f"{ha} vs {hb}", "相同", ha is None and hb is None))
                ha = hb = None
            for k in ("shift", "suggest") if ha is not None else ():
                rows.append(row(f"評分 key_hint.{k}", f"{ha.get(k)} vs {hb.get(k)}", "相同",
                                _canon(ha.get(k)) == _canon(hb.get(k))))
            if ha is not None:
                d = abs(ha["share"] - hb["share"])  # take_shape 已確認是數字（null 不再當成 0）
                rows.append(row("評分 key_hint.share 差", f"{d:.3f}（{ha.get('share')} vs {hb.get('share')}）", "<= 0.05",
                                d <= 0.05, d))
            other = sorted((ha.keys() | hb.keys()) - {"shift", "suggest", "share"}) if ha is not None else []
            if other:
                rows.append(row("評分 key_hint 其他欄位", "、".join(other), "沒有其他欄位（新欄位要補比對）", False))
            rows.append(row("評分 octave", f"{ka.get('octave')} vs {kb.get('octave')}", "相同",
                            _canon(ka.get("octave")) == _canon(kb.get("octave"))))
            for k in ("mean", "spread"):
                d = abs(ka[k] - kb[k])
                rows.append(row(f"評分 {k} 差", f"{d:.1f} cents（{ka[k]} vs {kb[k]}）", "<= 3 cents", d <= 3, d))
            d = abs(ka["offset"] - kb["offset"])
            rows.append(row("評分 offset 差", f"{d:.3f} 秒（{ka['offset']} vs {kb['offset']}）", "<= 0.02 秒",
                            d <= 0.02, d))
            rows += _lines_rows(ka.get("lines"), kb.get("lines"))
            rows += _take_pitch_rows(ka.get("pitch"), kb.get("pitch"))
    return rows + _export_rows(a, b)


def _lines_rows(la, lb) -> list[dict]:
    """評分逐句：句數、每句的時間、文字、影格數要相同，準確率、mean 允許小差距（欄位已由 take_shape 檢查）。"""
    if len(la) != len(lb):
        return [row("評分逐句的句數", f"{len(la)} vs {len(lb)}", "相同", False, abs(len(la) - len(lb)))]
    if not la:
        return []
    ident = sum(1 for x, y in zip(la, lb, strict=True)
                if _canon([x.get("t"), x.get("text"), x.get("n")]) != _canon([y.get("t"), y.get("text"), y.get("n")]))
    rows = [row("評分逐句的時間、文字、影格數", "相同" if not ident else f"{ident} 句不同", "相同", ident == 0, ident)]
    try:
        acc = max(abs(x["acc"] - y["acc"]) for x, y in zip(la, lb, strict=True)) * 100
        mean = max(abs(x["mean"] - y["mean"]) for x, y in zip(la, lb, strict=True))
    except (KeyError, TypeError):
        return rows + [row("評分逐句欄位", "acc 或 mean 缺少、不是數字", "都是數字", False)]
    rows.append(row("評分逐句準確率最大差", f"{acc:.1f} 分（{len(la)} 句）", "<= 5 分", acc <= 5, acc))
    rows.append(row("評分逐句 mean 最大差", f"{mean:.1f} cents", "<= 3 cents", mean <= 3, mean))
    return rows


def _take_pitch_rows(pa, pb) -> list[dict]:
    """評分裡錄音的音高曲線：hop、起點、影格數相同，有聲一致率與音高差和 pitch.json 同樣的門檻。"""
    if not (isinstance(pa, dict) and isinstance(pb, dict)):
        return [row("評分錄音音高", f"{type(pa).__name__} vs {type(pb).__name__}", "兩邊都有", False)]
    rows = []
    for k, lim in (("hop", 0.0), ("t0", 0.02)):
        x, y = _num(pa, k), _num(pb, k)
        ok = x is not None and y is not None and abs(x - y) <= lim
        rows.append(row(f"評分錄音音高 {k}", f"{x} vs {y}", "相同" if lim == 0 else f"差 <= {lim:g} 秒", ok,
                        abs(x - y) if x is not None and y is not None else None))
    ma, mb = (np.array([np.nan if v is None else v for v in p["midi"]], dtype=float) for p in (pa, pb))
    if len(ma) != len(mb):
        return rows + [row("評分錄音音高影格數", f"{len(ma)} vs {len(mb)}", "相同", False, abs(len(ma) - len(mb)))]
    if not len(ma):  # 錄音沒有音高影格：兩邊一樣是空的，但這不是可以比的結果
        return rows + [row("評分錄音音高影格", "兩邊都沒有", "有音高", False)]
    agree = float(np.mean(np.isnan(ma) == np.isnan(mb)))
    both = ~np.isnan(ma) & ~np.isnan(mb)
    cents = float(np.median(np.abs(ma[both] - mb[both])) * 100) if both.any() else 0.0
    rows.append(row("評分錄音有聲判斷一致率", f"{agree:.4f}", ">= 0.98", agree >= 0.98, agree))
    rows.append(row("評分錄音音高差中位數", f"{cents:.2f} cents", "< 10 cents", cents < 10, cents))
    return rows


def _export_rows(a: Path, b: Path) -> list[dict]:
    pa, pb = a / "songsing01" / "export.wav", b / "songsing01" / "export.wav"
    if not (pa.exists() and pb.exists()):
        return [row("匯出檔", "export.wav 不存在", "", False)]
    ya, yb, bad = audio_pair(pa, pb)
    if bad:
        return [row("匯出檔", bad, "取樣率、聲道、長度相同", False)]
    v = sdr(ya, yb)
    # 舊分支比的是 LUFS 差 <= 0.1；這裡用 RMS 差（不用 import 被測程式的響度函式）
    rd = rms_db_diff(ya, yb)
    return [row("匯出檔 SDR", f"{v:.1f} dB", "> 30 dB", v > 30, v),
            row("匯出檔 RMS 差", f"{rd:.3f} dB", "<= 0.1 dB", rd <= 0.1, rd)]


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
