"""音高曲線的後處理：補短空隙、修八度、去滑音、切音符（從 pipeline.py 原封搬出；pipeline 轉出同樣的名稱）。"""
from __future__ import annotations

import numpy as np

PITCH_SR = 16000
HOP = 160            # 10 ms
HOP_S = HOP / PITCH_SR


def bridge_gaps(midi: np.ndarray, max_gap: int = 12, tol: float = 1.0) -> np.ndarray:
    """前後音高接近（1 個半音內）而且只斷了很短（0.12 秒內）的地方直接連起來。"""
    out = midi.copy()
    n, i = len(out), 0
    while i < n:
        if not np.isnan(out[i]):
            i += 1
            continue
        j = i
        while j < n and np.isnan(out[j]):
            j += 1
        if 0 < i and j < n and j - i <= max_gap and abs(out[i - 1] - out[j]) <= tol:
            out[i:j] = np.linspace(out[i - 1], out[j], j - i + 2)[1:-1]
        i = j
    return out


def octave_low_score(y16: np.ndarray, a: int, b: int, low_midi: float) -> float | None:
    """看這段聲音的泛音：低八度那個音是不是真的存在。

    回傳（第 1＋3 泛音）÷（第 2＋4 泛音）。真的是低音時第 1、3 泛音明顯（約 0.4 以上）；
    其實是高八度時，低音位置幾乎沒能量（約 0.1 以下）。"""
    s0, s1 = a * HOP_S * PITCH_SR, b * HOP_S * PITCH_SR
    seg = y16[int(s0):int(s1)]
    if len(seg) < 400:
        return None
    S = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n=1 << 15))
    fr = np.fft.rfftfreq(1 << 15, 1 / PITCH_SR)
    f = 440.0 * 2 ** ((low_midi - 69) / 12)
    h = [float(S[(fr > k * f * 0.97) & (fr < k * f * 1.03)].max(initial=0.0)) for k in (1, 2, 3, 4)]
    if h[1] + h[3] <= 0:
        return None
    return (h[0] + h[2]) / (h[1] + h[3])


def fix_octaves(midi: np.ndarray, rms: np.ndarray | None = None, y16: np.ndarray | None = None,
                ctx_s: float = 2.0) -> np.ndarray:
    """修正整段被判成高／低一個八度的音（常見在句尾漸弱的尾音）。

    找出和周圍（前後 2 秒，或緊接在前面的長音）差了將近一個八度的短音或弱音，
    再用泛音確認：低八度的音確實存在才往下移，確定是高音就保留；
    泛音看不出來時，只有「比周圍明顯小聲」或「句尾跟在長音後面的短尾音」才移。"""
    out = midi.copy()
    n = len(out)
    level = rms[:n] if rms is not None and len(rms) >= n else None
    runs, i = [], 0
    while i < n:
        if np.isnan(out[i]):
            i += 1
            continue
        j = i
        while j < n and not np.isnan(out[j]):
            j += 1
        runs.append([i, j])
        i = j
    if len(runs) < 3:
        return out
    win = int(ctx_s / HOP_S)
    done = set()          # 每段最多移一次，避免第二輪又被移回去
    for _ in range(2):    # 修一次後周圍的參考也會變準，再看一次
        meds = [float(np.median(out[a:b])) for a, b in runs]
        for k, (a, b) in enumerate(runs):
            if k in done or (b - a) > 60:
                continue
            near = [q for q, (c, d) in enumerate(runs) if q != k and d > a - win and c < b + win]
            wts = [runs[q][1] - runs[q][0] for q in near]
            if sum(wts) < 30:
                continue
            vals = [meds[q] for q in near]
            order = np.argsort(vals)
            cum = np.cumsum(np.array(wts)[order])
            ctx = float(np.array(vals)[order][np.searchsorted(cum, cum[-1] / 2)])   # 依長度加權的中位數
            diff = meds[k] - ctx
            tail = False
            if k > 0:
                # 句尾的短尾音：跟緊接在前面（0.5 秒內）、比較長的那個音比
                pa, pb = runs[k - 1]
                pd = meds[k] - meds[k - 1]
                if a - pb <= 50 and (pb - pa) > 2 * (b - a) and (b - a) <= 30 and 10 <= abs(pd) <= 14:
                    diff, tail = pd, True
            if not 10 <= abs(diff) <= 14:
                continue
            weak = False
            if level is not None and near:
                ctx_lvl = float(np.median(np.concatenate([level[runs[q][0]:runs[q][1]] for q in near])))
                weak = float(np.median(level[a:b])) < 0.5 * ctx_lvl
            low = meds[k] - 12 if diff > 0 else meds[k]   # 兩個候選音裡比較低的那個
            score = octave_low_score(y16, a, b, low) if y16 is not None else None
            if score is not None and diff > 0 and score <= 0.12:
                continue      # 低音位置沒有能量：真的是高音
            if score is not None and diff < 0 and score >= 0.35:
                continue      # 低音確實存在：真的是低音
            sure = score is not None and ((diff > 0 and score >= 0.35) or (diff < 0 and score <= 0.12))
            if sure or weak or tail:
                shift = 12 * np.sign(diff)
                out[a:b] -= shift
                meds[k] -= shift
                done.add(k)
    return out


def trim_glides(midi: np.ndarray, slope: float = 0.9, k: int = 3) -> np.ndarray:
    """去掉每段聲音開頭與結尾「快速滑動」的部分（30 ms 內變化超過 slope 個半音）。

    只從兩端往內修，句子中間正常的換音、滑音不受影響。"""
    out = midi.copy()
    n = len(out)
    i = 0
    while i < n:
        if np.isnan(out[i]):
            i += 1
            continue
        j = i
        while j < n and not np.isnan(out[j]):
            j += 1
        s, e = i, j
        while s + k < e and abs(out[s] - out[s + k]) > slope:
            s += 1
        while e - k - 1 >= s and abs(out[e - 1] - out[e - 1 - k]) > slope:
            e -= 1
        # 第二步：從兩端找最近一段穩定的音（8 格內起伏小於 0.6 半音），
        # 比它偏離超過 1.5 半音的頭尾視為滑入／滑落（慢慢掉下去的尾音也能去掉）
        w = 8
        for q in range(e - w, s - 1, -1):
            seg = out[q:q + w]
            if seg.max() - seg.min() < 0.6:
                med = float(np.median(seg))
                t = q + w
                while t < e and abs(out[t] - med) <= 1.5:
                    t += 1
                e = t
                break
        for q in range(s, e - w + 1):
            seg = out[q:q + w]
            if seg.max() - seg.min() < 0.6:
                med = float(np.median(seg))
                t = q
                while t > s and abs(out[t - 1] - med) <= 1.5:
                    t -= 1
                s = t
                break
        out[i:s] = np.nan
        out[e:j] = np.nan
        if e - s < 6:
            out[s:e] = np.nan
        i = j
    return out


def clean_pitch(f0: np.ndarray) -> np.ndarray:
    n = len(f0)
    out = np.full(n, np.nan, dtype=np.float32)
    for i in range(n):
        v = f0[i]
        if np.isnan(v):
            continue
        w = f0[max(0, i - 3): i + 4]
        w = w[~np.isnan(w)]
        med = float(np.median(w))
        if abs(v - med - 12) < 1.5:
            v -= 12
        elif abs(v - med + 12) < 1.5:
            v += 12
        out[i] = med if abs(v - med) > 2.5 else v
    i = 0
    while i < n:
        if np.isnan(out[i]):
            i += 1
            continue
        j = i
        while j < n and not np.isnan(out[j]):
            j += 1
        if j - i < 6:
            out[i:j] = np.nan
        i = j
    return out


def segment_notes(f0: np.ndarray) -> list[list[float]]:
    notes = []

    def close(cur, end):
        if cur and end - cur["s"] >= 0.08 and float(np.median(cur["v"])) >= 38:
            notes.append([round(cur["s"], 3), round(end, 3), int(round(float(np.median(cur["v"]))))])

    cur = None
    dev = 0
    for i, v in enumerate(f0):
        t = i * HOP_S
        if np.isnan(v):
            if cur:
                close(cur, t)
                cur = None
            continue
        if cur is None:
            cur = {"s": t, "v": [float(v)]}
            dev = 0
            continue
        mean = sum(cur["v"]) / len(cur["v"])
        if abs(v - mean) > 0.7:
            dev += 1
            if dev >= 5:
                split = t - 4 * HOP_S
                tail = cur["v"][-4:]
                cur["v"] = cur["v"][:-4]
                close(cur, split)
                cur = {"s": split, "v": tail + [float(v)]}
                dev = 0
                continue
        else:
            dev = 0
        cur["v"].append(float(v))
    if cur:
        close(cur, len(f0) * HOP_S)
    return notes
