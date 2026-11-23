"""fork：logs/app.log 每一行開頭加上時間。

app.log 是伺服器和顯示卡處理程式的 stdout／stderr 直接寫進檔案（desktop.py），`[gpu]`、`[live]` 這些 print
本來都沒有時間，看不出事情的先後和間隔。伺服器 main() 和顯示卡處理程式 serve() 一開始各呼叫一次 install()。
"""
from __future__ import annotations

import sys
import threading
import time

STAMP = "%m-%d %H:%M:%S "   # app.log 會累積好幾天，連日期一起記


class Stamped:
    """包住原本的串流，只在每一行開頭補時間；其他屬性（fileno、buffer、encoding…）都轉給原本的串流，
    gpu_worker 把 sys.stdout 交給子程式時（Popen 用 fileno）才不會出錯。"""

    def __init__(self, stream):
        self._stream = stream
        self._bol = True    # 下一個字是不是一行的開頭
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text or not isinstance(text, str):
            # 空字串、bytes 照原樣交給原本的串流：click 用 write(b"") 判斷是不是二進位串流，要和原本一樣丟 TypeError
            return self._stream.write(text)
        with self._lock:
            out = []
            for part in text.splitlines(keepends=True):
                if self._bol:
                    out.append(time.strftime(STAMP))
                out.append(part)
                self._bol = part.endswith("\n")
            self._stream.write("".join(out))
        return len(text)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install() -> None:
    for name in ("stdout", "stderr"):
        s = getattr(sys, name)
        if s is not None and not isinstance(s, Stamped):
            setattr(sys, name, Stamped(s))
