"""契約測試用：擋掉所有對外連線（本機以外的主機），目標寫進 DATA/net_blocked.log。

import 這個模組就生效。serve.py（重構版）和 serve_upstream.py（上游版本）共用。
"""

import os
import socket
from pathlib import Path

_LOCAL = {"127.0.0.1", "localhost", "::1"}
# LEIHU_NET_LOG：數值、效能沙盒用（不能設 LIANTAI_DATA，S28 之後被測程式會把資料搬過去）
_BLOCKED_LOG = Path(os.environ.get("LEIHU_NET_LOG") or Path(os.environ["LIANTAI_DATA"]) / "net_blocked.log")


def _block(host) -> None:
    with open(_BLOCKED_LOG, "a", encoding="utf-8") as f:
        f.write(f"{host}\n")
    raise OSError(f"契約測試不允許對外連線：{host}")


_connect = socket.socket.connect
_getaddrinfo = socket.getaddrinfo


def connect(self, addr):
    host = addr[0] if isinstance(addr, tuple) else addr
    if host not in _LOCAL:
        _block(host)
    return _connect(self, addr)


def getaddrinfo(host, *args, **kwargs):
    if host not in _LOCAL and host is not None:
        _block(host)
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect = connect
socket.getaddrinfo = getaddrinfo
