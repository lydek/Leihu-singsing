"""雷虎練唱台：本機伺服器。執行後用瀏覽器開 http://127.0.0.1:8765

路由和共用狀態在 leihu/web/（依功能分成 Blueprint），這裡只建立 app 和啟動伺服器。
桌面程式（desktop.py）用 `python app.py` 啟動；安裝程式用 `import app; app.VERSION` 檢查程式本體。
契約沙盒（tests/contract/serve_upstream.py）從這個模組替換啟動時的背景工作，所以 main() 用這裡的名稱呼叫。
"""
from __future__ import annotations

import sys
import threading
import time  # noqa: F401  測試從 app.time 替換 sleep
import webbrowser

import pipeline as P
from leihu.web import create_app
from leihu.web.background import (
    lead_fix_sweep,
    live_light_upgrade,
    loudness_backfill,
    prefetcher,
    queue_saver,
    remove_duet_leftovers,
    restore_queue,
    resume_downloads,
    watch_parent,
)
from leihu.web.common import HOST, PORT, VERSION

# 以下只給測試讀寫（tests/test_fork_update.py、test_env_overrides.py 等用 app.<名稱>）
from leihu.web.live import SETLIST_DIR, SETLIST_PATH  # noqa: F401
from leihu.web.system import EXTERNAL_URLS  # noqa: F401
from leihu.web.update import _UPDATE_ASSET, UPDATE, _installed_copy, _ver, check_update  # noqa: F401

app = create_app()


def main():
    P.LIB.mkdir(exist_ok=True)
    watch_parent()
    if not P.NO_BACKGROUND:
        resume_downloads()
        threading.Thread(target=prefetcher, daemon=True, name="prefetch").start()
        threading.Thread(target=loudness_backfill, daemon=True, name="loudness").start()
        threading.Thread(target=live_light_upgrade, daemon=True, name="live-light").start()
        threading.Thread(target=lead_fix_sweep, daemon=True, name="lead-fix").start()
        restore_queue()   # 先排回上次的佇列，再開始存新的
        threading.Thread(target=queue_saver, daemon=True, name="queue-saver").start()
        threading.Thread(target=remove_duet_leftovers, daemon=True, name="cleanup").start()
        threading.Thread(target=P.clear_false_recheck, daemon=True, name="recheck").start()
        threading.Thread(target=check_update, daemon=True, name="update").start()
    url = f"http://{HOST}:{PORT}"
    print(f"\n  {P.APP_NAME} {VERSION} 已啟動：{url}\n  關閉這個視窗就會結束程式。\n")
    if "--no-browser" not in sys.argv and not P.spawned("browser", url):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
