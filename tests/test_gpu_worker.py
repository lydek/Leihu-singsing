"""gpu_worker.py 的修正（docs/next-version-plan.md 第 1 批）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "win32", reason="SetPriorityClass 只有 Windows")
def test_set_priority_really_changes_priority_class():
    """GetCurrentProcess 回傳的虛擬控制代碼是 -1：沒設型別時 ctypes 當 32 位元整數傳，64 位元 Windows 上變成
    0xFFFFFFFF，SetPriorityClass 失敗、又沒檢查回傳值，低優先順序從來沒生效。在子行程裡改，不影響跑測試的行程。"""
    code = (
        "import ctypes, sys; from ctypes import wintypes; sys.path.insert(0, '.'); import gpu_worker as G\n"
        "k = ctypes.WinDLL('kernel32')\n"  # 另一個實例：不改到受測程式用的 windll.kernel32
        "k.GetCurrentProcess.restype = wintypes.HANDLE\n"
        "k.GetPriorityClass.argtypes = (wintypes.HANDLE,)\n"
        "G._set_priority(True); low = k.GetPriorityClass(k.GetCurrentProcess())\n"
        "G._set_priority(False); normal = k.GetPriorityClass(k.GetCurrentProcess())\n"
        "print(hex(low), hex(normal))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["0x4000", "0x20"]  # BELOW_NORMAL、NORMAL
