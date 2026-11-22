"""和上游比對用的伺服器：在沙盒裡執行上游版本的 app.py（docs/upstream-compare.md）。

上游沒有步驟 0 加的環境變數覆寫（LIANTAI_DATA、LIANTAI_NO_BACKGROUND、LIANTAI_NO_JOBS、LIANTAI_NO_SPAWN），
這裡在 import 之後把同樣的條件補上，讓上游和重構版在相同條件下跑同一組契約案例：
- 上游的程式碼由比對腳本複製進 LIANTAI_DATA（ROOT＝資料夾，和實際安裝時一樣）；
- 擋掉對外連線（netblock）；
- 不跑背景工作（佇列只排隊不執行，啟動時的背景執行緒不開，模型、讀音字典不下載）；
- 不真的開外部程式：檔案總管、瀏覽器、app 視窗、安裝程式改成寫進 DATA/spawn.log（和重構版 LIANTAI_NO_SPAWN 同格式），
  只放行 ffmpeg 和查 Windows 版本的 ver；
- 下載資料夾改成 LIANTAI_DOWNLOADS，偵測直播軟體固定回傳沒有；
- 不探測顯示卡（固定為沒有可用的顯示卡）、不跑 nvidia-smi。
每一項替換和找不到的目標都寫進 shim.log（LEIHU_SHIM_LOG），路由清單寫進 LEIHU_ROUTES_OUT。
"""

import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 內嵌 Python（._pth）不會自動加入腳本所在資料夾
import netblock  # noqa: E402, F401  import 就擋掉對外連線

DATA = Path(os.environ["LIANTAI_DATA"])
DOWNLOADS = Path(os.environ["LIANTAI_DOWNLOADS"])
sys.path.insert(0, str(DATA))
os.chdir(DATA)


def _spawned(kind: str, target) -> None:
    with open(DATA / "spawn.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, "target": target}, ensure_ascii=False) + "\n")


_Popen = subprocess.Popen


def _kind(args) -> tuple[str | None, object]:
    """回傳 (記錄的種類, 目標)；種類是 None 表示放行（ffmpeg）。"""
    a = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
    head = Path(a[0].strip('"').split('" ')[0]).name.lower() if a else ""
    if "ffmpeg" in head or head == "ver":  # ffmpeg；ver 是函式庫查 Windows 版本（platform 模組），不會開任何東西
        return None, None
    if head in ("explorer", "explorer.exe") and len(a) >= 3:
        return "reveal", a[2]
    app = next((x[len("--app=") :] for x in a if x.startswith("--app=")), None)
    if app:
        return "app-window", app
    if isinstance(args, str) and "/S /D=" in args:
        return "installer", args
    return "spawn", a


class _SandboxPopen(_Popen):
    """擋掉的指令改跑一個什麼都不做的 Python（保留呼叫端的參數，wait／communicate 照常）。
    要用類別：asyncio 等標準函式庫會繼承 subprocess.Popen，換成函式會讓它們 import 失敗。subprocess.run 也經過這裡。"""

    def __init__(self, args, *a, **k):
        kind, target = _kind(args)
        if kind is not None:
            _spawned(kind, target)
            args, k = [sys.executable, "-c", "pass"], {**k, "shell": False, "executable": None}
        super().__init__(args, *a, **k)


subprocess.Popen = _SandboxPopen
webbrowser.open = lambda url, *a, **k: (_spawned("browser", url), True)[1]
if hasattr(os, "startfile"):
    os.startfile = lambda path, *a, **k: _spawned("reveal", str(path))

# 沙盒做了哪些替換、哪些目標在這個版本找不到，都寫進 shim.log（比對報告會原樣列出，不能靜默略過）
SHIM_LOG = Path(os.environ.get("LEIHU_SHIM_LOG") or DATA / "shim.log")


def _note(kind: str, msg: str) -> None:
    with open(SHIM_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, "msg": msg}, ensure_ascii=False) + "\n")


def _stub(mod, names, value=lambda *a, **k: None, why: str = "") -> None:
    for name in names:
        if hasattr(mod, name):
            setattr(mod, name, value)
            _note("stub", f"{mod.__name__}.{name}：{why}")
        else:
            _note("missing", f"{mod.__name__}.{name} 在這個版本不存在，沒有替換（{why}）")


import pipeline as P  # noqa: E402

_stub(P.Jobs, ["_run"], lambda self: None, "佇列只排隊不執行（= LIANTAI_NO_JOBS）")
_stub(P, ["downloads_dir"], lambda: DOWNLOADS, "下載資料夾改成 LIANTAI_DOWNLOADS")
_stub(P, ["_running_apps"], lambda names: [], "直播軟體偵測固定回傳沒有")
_stub(P, ["start_hq_download", "start_hqv_download", "start_kana_install", "_upgrade_ytdlp"],
      why="= LIANTAI_NO_BACKGROUND：不下載模型、不裝套件")
_stub(P, ["clear_false_recheck"], lambda: 0, "啟動時的背景檢查不跑（2.3.2）")
_stub(P, ["_sample_gpu_load"], why="不跑 nvidia-smi：顯示卡被其他程式佔用時結果會不同（2.3.2）")
if hasattr(P, "_GPU_INFO"):
    # 不開 gpu_worker 探測顯示卡：契約快照固定代表「沒有可用的顯示卡」，和電腦有沒有 NVIDIA 顯示卡無關
    P._GPU_INFO = {"ok": False, "note": "", "cc": None, "torch": ""}
    _note("forced", "pipeline._GPU_INFO 固定為沒有可用的顯示卡（不開 gpu_worker 探測）")
else:
    _note("missing", "pipeline._GPU_INFO 在這個版本不存在，顯示卡探測沒有固定")

sys.argv = [str(DATA / "app.py"), "--no-browser"]
import app  # noqa: E402

_stub(
    app,
    [
        "resume_downloads",
        "prefetcher",
        "loudness_backfill",
        "live_light_upgrade",
        "lead_fix_sweep",
        "restore_queue",
        "queue_saver",
        "remove_duet_leftovers",
        "check_update",
        "watch_parent",
    ],
    why="啟動時的背景執行緒不開",
)
# 路由清單給比對腳本檢查涵蓋率（規則字串＋方法，不看 endpoint 名稱）
routes = sorted(
    {f"{r.rule} {m}" for r in app.app.url_map.iter_rules() for m in r.methods - {"HEAD", "OPTIONS"}}
)
Path(os.environ.get("LEIHU_ROUTES_OUT") or DATA / "routes.json").write_text(
    json.dumps(routes, ensure_ascii=False, indent=1), "utf-8"
)
app.main()
