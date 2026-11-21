"""desktop.find_instance：找已在跑的練唱台不能逐一等空埠逾時（Windows 每個空埠要等 2 秒，10 個就是 20 秒）。"""

from __future__ import annotations

import importlib
import socket
import sys
import time

import pytest


@pytest.fixture
def desktop(monkeypatch):
    out, err = sys.stdout, sys.stderr
    monkeypatch.chdir(".")  # desktop 匯入時會 chdir 到程式資料夾
    mod = importlib.import_module("desktop")
    mod._log.flush()
    sys.stdout, sys.stderr = out, err  # 匯入時把 stdout/stderr 導到 logs/desktop.log
    return mod


def _closed_ports(n: int) -> int:
    """找一段連續 n 個都沒人聽的埠，回傳第一個。"""
    for start in range(47000, 60000, 50):
        if all(_free(p) for p in range(start, start + n)):
            return start
    pytest.skip("找不到連續的空埠")


def _free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def test_no_instance_is_fast_and_skips_http(desktop, monkeypatch):
    asked = []
    monkeypatch.setattr(desktop, "running_instance", lambda p: asked.append(p) or False)
    start = _closed_ports(10)
    t = time.perf_counter()
    assert desktop.find_instance(start) is None
    assert time.perf_counter() - t < 5  # 改之前是 10 × 2 秒
    assert asked == []  # 沒人聽的埠不發 HTTP


def test_picks_first_listening_port_that_is_this_install(desktop, monkeypatch):
    start = _closed_ports(10)
    socks = []
    for p in (start + 2, start + 5, start + 7):  # 三個埠有程式在聽；只有 +5、+7 是這份練唱台
        s = socket.socket()
        s.bind(("127.0.0.1", p))
        s.listen()
        socks.append(s)
    asked = []
    monkeypatch.setattr(desktop, "running_instance", lambda p: asked.append(p) or p in (start + 5, start + 7))
    try:
        assert desktop.find_instance(start) == start + 5
    finally:
        for s in socks:
            s.close()
    assert asked == [start + 2, start + 5]  # 依埠的順序問，找到就停
