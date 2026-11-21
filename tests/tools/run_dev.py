"""用已安裝的內嵌 Python 執行這個資料夾裡的程式碼（開發時實際操作程式用）。

已安裝的內嵌 Python 有 ._pth 檔：模組只從安裝資料夾（%LOCALAPPDATA%\\LeihuLiantai）找，不會加入執行的腳本所在的資料夾。
直接用它跑這裡的 desktop.py，`import pipeline`、`import hotkeys` 會抓到安裝資料夾裡的舊版本。
這個啟動器把這個資料夾放到搜尋路徑最前面再啟動 desktop.py；desktop 啟動的伺服器子行程也經過這裡。
不改安裝資料夾的任何檔案；資料（歌曲庫、設定、logs）放在這個資料夾（都在 .gitignore 裡），模型讀這個資料夾的 models/。

用法（PowerShell，在 repo 根目錄）：
  & "$env:LOCALAPPDATA\\LeihuLiantai\\python\\python.exe" tests\\tools\\run_dev.py
"""
import os
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

if len(sys.argv) > 1 and sys.argv[1] == "--serve":   # desktop 啟動的伺服器子行程
    _WorkerPopen = subprocess.Popen

    class _Popen(_WorkerPopen):
        """gpu_worker 的子程式（python -c "import gpu_worker; ..."）也要先把這個資料夾放到搜尋路徑最前面，
        不然會載入安裝資料夾裡的 gpu_worker、pipeline。要用類別：asyncio 等會繼承 subprocess.Popen。"""

        def __init__(self, args, *a, **k):
            if isinstance(args, list) and len(args) == 3 and args[1] == "-c" and "gpu_worker.serve(" in args[2]:
                args = [args[0], "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); " + args[2]]
            super().__init__(args, *a, **k)

    subprocess.Popen = _Popen
    sys.argv = [str(ROOT / "app.py"), *sys.argv[2:]]
    runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
    raise SystemExit

import desktop  # noqa: E402

_start_server, _Popen = desktop.start_server, subprocess.Popen


def start_server(port: int):
    """照 desktop.start_server 啟動伺服器，只把「python app.py」換成「python run_dev.py --serve」。"""
    def popen(args, *a, **k):
        if isinstance(args, list) and len(args) > 1 and Path(args[1]) == ROOT / "app.py":
            args = [args[0], str(Path(__file__).resolve()), "--serve", *args[2:]]
        return _Popen(args, *a, **k)
    subprocess.Popen = popen
    try:
        return _start_server(port)
    finally:
        subprocess.Popen = _Popen


desktop.start_server = start_server
desktop.main()
