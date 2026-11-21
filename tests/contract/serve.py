"""契約快照用的伺服器：擋掉所有對外連線後執行這個 repo 的 app.py（S28 之後才能用）。

快照不能連網：有任何程式碼想連外，連線會失敗，並把目標寫進 DATA/net_blocked.log；
這個檔案會出現在檔案差異裡，測試看到就判定失敗。
背景工作、佇列、外部程式、下載資料夾由 run_contract 設的 LIANTAI_* 環境變數（S28）處理；
這裡只換掉 serve_upstream.py 也換掉、但沒有環境變數可以關的部分，兩邊才比得起來。
"""

import os
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 內嵌 Python（._pth）不會自動加入腳本所在資料夾
import netblock  # noqa: F401  import 就擋掉對外連線

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import pipeline as P  # noqa: E402

P._running_apps = lambda names: []  # 開發機開著 OBS 等直播軟體時，/api/status 的 live 會不同，快照錄不穩
P._sample_gpu_load = lambda: None  # 開發機的顯示卡被其他程式佔滿時 live 也會不同
# 不開 gpu_worker 探測顯示卡：快照固定代表「沒有可用的顯示卡」（同 serve_upstream.py）
P._GPU_INFO = {"ok": False, "note": "", "cc": None, "torch": ""}
sys.argv = [str(ROOT / "app.py"), "--no-browser"]
runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
