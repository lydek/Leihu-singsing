"""效能基準：用指定的程式碼把一首歌從匯入做到匯出一次，記錄每個階段的起訖時間（docs/refactor-port-plan.md 3.8）。

由 tests/perf/baseline.py 呼叫；記憶體、顯示卡記憶體由外面那支取樣（這個行程和它底下的所有子程式），
這裡只寫 <輸出>/phases.json（各階段的 time.time() 起訖）和 <輸出>/run.json（執行條件、問題、實際用的模型）。

執行環境見 tests/numeric/sandbox.py（deterministic=False：執行緒數、隨機抖動都照平常）。和平常使用不同的地方：
- 歌詞用語音辨識（auto_recognize_lyrics＝True）：平常預設是上網搜尋，這裡不連網，改量語音辨識的時間；
- 直播軟體偵測固定回傳沒有、不跑 nvidia-smi、不下載模型（沒有下載好的高品質模型就用標準模型，會寫進條件）；
- 評分用的「錄音」是這首歌自己分出來的主唱（單聲道），長度和整首歌一樣。
階段：idle_before（啟動後閒置 3 秒）、import、separate、pitch、lyrics（process_song 裡的三個步驟，各自計時）、
variant（移調 +2）、score、export（wav）、idle_after（等顯示卡子程式閒置結束後再量 5 秒）。

用法：<python> tests/perf/run_perf.py <程式碼資料夾> <歌曲檔> <模型資料夾> <輸出資料夾> [--quality=auto|standard]
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "numeric"))
from sandbox import Sandbox  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
opt = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
code, song_file, models, out = (Path(a).resolve() for a in args[:4])
quality = opt.get("quality", "auto")
out.mkdir(parents=True, exist_ok=True)
PHASES: list[dict] = []


def phase(name: str):
    class _P:
        def __enter__(self):
            self.t = time.time()
            print(f"[perf] {name} 開始", flush=True)

        def __exit__(self, *exc):
            PHASES.append({"name": name, "start": self.t, "end": time.time(), "ok": exc[0] is None})
            print(f"[perf] {name} 結束 {time.time() - self.t:.1f} 秒", flush=True)
            (out / "phases.json").write_text(json.dumps(PHASES, indent=1), "utf-8")
            return False

    return _P()


BOX = Sandbox(code, models, "[perf]", deterministic=False, device=opt.get("device", "auto"))
BOX.cond("計時前已經把模型檔完整讀過一遍（算 sha256），模型載入的時間是檔案在系統快取裡的情況，不是冷啟動")
P, SH = BOX.P, BOX.SH
t0 = time.time()
import app  # noqa: E402, F401  只 import（啟動伺服器要 app.main()）；量 import 的時間

app_import = round(time.time() - t0, 2)
BOX.check_sources("app")
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

CFG = {"auto_recognize_lyrics": True, "lyrics_language": ""}
BOX.cond("歌詞用語音辨識（auto_recognize_lyrics＝True）；平常預設是上網搜尋")
if quality != "auto":
    CFG.update(vocal_quality=quality, karaoke_quality=quality)
    BOX.cond(f"分離品質固定為 {quality}（平常預設 auto）")
for ready, what in (("hqv_ready", "高品質人聲分離模型（BS-Roformer）"), ("hq_ready", "高品質主唱分離模型（Roformer karaoke）")):
    if hasattr(P, ready) and not getattr(P, ready)():
        BOX.cond(f"{what}不在模型資料夾：要用高品質時會改用標準模型（平常會在背景下載）")
BOX.cond("評分用的錄音是這首歌分出來的主唱")
for name in ("step_separate", "step_pitch", "step_lyrics"):  # 只包一層計時，不改行為
    f = getattr(P, name)
    setattr(P, name, (lambda f, n: lambda *a, **k: _timed(n, f, *a, **k))(f, name[5:]))


def _timed(name, f, *a, **k):
    with phase(name):
        return f(*a, **k)


def report(step, p):
    print(f"  {p:5.2f} {step}", flush=True)


def main() -> None:
    problems: list[str] = []
    with phase("idle_before"):
        time.sleep(3)
    sid = "perfsong"
    d = P.LIB / sid
    d.mkdir(parents=True)
    shutil.copy2(song_file, d / f"original{song_file.suffix.lower()}")
    (d / "meta.json").write_text(
        json.dumps({"id": sid, "title": sid, "created": 1577836800.0, "notes": [],
                    "steps": {"import": False, "separate": False, "pitch": False, "lyrics": False}}),
        "utf-8",
    )
    with phase("import"):
        P.step_import(d, report)
    P.process_song(sid, ["separate", "pitch", "lyrics"], CFG)(report)
    with phase("variant"):
        P.step_variant(d, 2, 1.0, report)
    td = d / "takes"
    td.mkdir(exist_ok=True)
    lead = P.read_audio(P.stem_path(d, "lead"))
    sf.write(str(td / "take01.wav"), np.asarray(lead).mean(axis=0), P.SR, subtype="PCM_16")
    (td / "take01.json").write_text(
        json.dumps({"id": "take01", "key": 0, "speed": 1.0, "start": 0.0, "status": "scoring"}), "utf-8"
    )
    with phase("score"):
        P.score_take(d, "take01", report)
    with phase("export"):
        SH.export_take(d, "take01", {"formats": ["wav"], "range": "full"}, BOX.tmp / "export" / "take01", report)
    import gpu_worker as G

    wait = getattr(G, "IDLE_EXIT", 30.0) + 8
    print(f"[perf] 等 {wait:.0f} 秒讓顯示卡子程式閒置結束", flush=True)
    time.sleep(wait)
    if G.POOL.count() or G.CPU_POOL.count():
        problems.append("顯示卡子程式閒置後沒有結束，idle_after 的記憶體包含子程式")
    with phase("idle_after"):
        time.sleep(5)
    meta = json.loads((d / "meta.json").read_text("utf-8"))
    take = json.loads((td / "take01.json").read_text("utf-8"))
    problems += BOX.problems(out)
    if take.get("status") != "done":
        problems.append(f"評分沒有完成：{take.get('status')} {take.get('error', '')}")
    (out / "run.json").write_text(
        json.dumps(
            {
                "code": str(code),
                "conditions": BOX.conditions,
                "problems": problems,
                "worker_spawns": BOX.spawns,
                "environment": BOX.environment(),
                "models": BOX.models_sha,
                "startup": {"import_pipeline_share": BOX.import_seconds, "import_app": app_import},
                "song": {"duration": meta.get("duration"), "separation": meta.get("separation"),
                         "vocal_model": meta.get("vocal_model"), "karaoke": meta.get("karaoke"),
                         "lyrics_unreliable": meta.get("lyrics_unreliable")},
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
