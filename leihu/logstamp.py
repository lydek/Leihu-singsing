"""fork：logs/app.log 每一行開頭加上時間。

app.log 是伺服器和顯示卡處理程式的 stdout／stderr 直接寫進檔案（desktop.py），`[gpu]`、`[live]` 這些 print
本來都沒有時間，看不出事情的先後和間隔。伺服器 main() 和顯示卡處理程式 serve() 一開始各呼叫一次 install()。

一整行才寫出去：print 會把文字和換行分兩次寫，多個執行緒、或 stdout 和 stderr 寫同一個檔案時，
別人的一行才不會插在中間、時間也不會落在行中。沒寫完的一行（沒有換行）留到換行或 flush 時才寫出。
"""
from __future__ import annotations

import os
import sys
import threading
import time

STAMP = "%m-%d %H:%M:%S "   # app.log 會累積好幾天，連日期一起記


class _Dest:
    """同一個目的地共用的狀態：stdout、stderr 寫同一個 app.log 時共用一份。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.bol = True    # 下一個字是不是一行的開頭


class Stamped:
    """包住原本的串流，只在每一行開頭補時間；其他屬性（fileno、buffer、encoding…）都轉給原本的串流，
    gpu_worker 把 sys.stdout 交給子程式時（Popen 用 fileno）才不會出錯。"""

    def __init__(self, stream, dest: _Dest | None = None):
        self._stream = stream
        self._dest = dest or _Dest()
        self._pending: dict[int, str] = {}   # 執行緒 → 還沒寫完的一行

    def write(self, text: str) -> int:
        if not text or not isinstance(text, str):
            # 空字串、bytes 照原樣交給原本的串流：click 用 write(b"") 判斷是不是二進位串流，要和原本一樣丟 TypeError
            return self._stream.write(text)
        tid = threading.get_ident()
        with self._dest.lock:
            buf = self._pending.pop(tid, "") + text
            cut = buf.rfind("\n") + 1
            if cut < len(buf):
                self._pending[tid] = buf[cut:]
            if cut:
                self._emit(buf[:cut])
        return len(text)

    def flush(self):
        """沒寫完的行（所有執行緒的）也寫出去：程式結束、print(flush=True)、進度條更新時都會呼叫。"""
        with self._dest.lock:
            rest, self._pending = list(self._pending.values()), {}
            for s in rest:
                self._emit(s)
        return self._stream.flush()

    def _emit(self, text: str) -> None:   # 呼叫前要拿著 self._dest.lock
        out = []
        for part in text.splitlines(keepends=True):
            if self._dest.bol:
                out.append(time.strftime(STAMP))
            out.append(part)
            self._dest.bol = part.endswith("\n")
        self._stream.write("".join(out))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _same_file(a, b) -> bool:
    try:
        return os.path.samestat(os.fstat(a.fileno()), os.fstat(b.fileno()))
    except (AttributeError, OSError, ValueError):
        return False


def install() -> None:
    out, err = sys.stdout, sys.stderr
    if isinstance(out, Stamped) or isinstance(err, Stamped):
        return
    shared = _Dest() if out is not None and err is not None and _same_file(out, err) else None
    if out is not None:
        sys.stdout = Stamped(out, shared)
    if err is not None:
        sys.stderr = Stamped(err, shared)
