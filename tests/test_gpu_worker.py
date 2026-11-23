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


# ---- 分離失敗診斷（移植自 9e53d67）：替換 audio_separator，看 t_separate 失敗時的訊息與丟出的錯誤

class _FakeSeparator:
    load_error: BaseException | None = None
    separate_error: BaseException | None = None

    def __init__(self, **kw):
        self.model_instance = None

    def load_model(self, model_filename):
        if self.load_error is not None:
            import logging
            logging.getLogger("audio_separator").error("Error: 載入失敗的原因")
            raise self.load_error

    def separate(self, inp):
        raise self.separate_error


def _run_separate(monkeypatch, tmp_path, **errors):
    import contextlib
    import types

    import gpu_worker as G

    mod = types.ModuleType("audio_separator.separator")
    sep_cls = type("Sep", (_FakeSeparator,), errors)
    mod.Separator = sep_cls
    monkeypatch.setitem(sys.modules, "audio_separator", types.ModuleType("audio_separator"))
    monkeypatch.setitem(sys.modules, "audio_separator.separator", mod)
    (tmp_path / "uvr").mkdir()
    (tmp_path / "uvr" / "m.ckpt").write_bytes(b"x" * 10)
    P = types.SimpleNamespace(MODELS=tmp_path, SR=44100, MODEL_FILES={"m.ckpt": (99, "sha", "url")},
                              _sep_progress=lambda *a: contextlib.nullcontext(), _outputs=lambda o, d: o)
    return G.t_separate(P, lambda *a: None, "m.ckpt", "in.wav", str(tmp_path), 2, True, "分離", 0.0, 1.0)


def test_separate_failure_prints_diagnosis(monkeypatch, tmp_path, capsys):
    """分離途中的錯誤照原樣丟出，app.log 多一段診斷（顯示卡、套件版本、模型檔大小）。"""
    boom = RuntimeError("CUDA error: no kernel image is available")
    with pytest.raises(RuntimeError) as ei:
        _run_separate(monkeypatch, tmp_path, separate_error=boom)
    assert ei.value is boom
    out = capsys.readouterr().out
    assert "[gpu] 分離失敗診斷（m.ckpt）" in out
    assert "10 bytes（大小不符，應為 99）" in out


def test_separate_systemexit_keeps_real_cause(monkeypatch, tmp_path, capsys):
    """分離途中被 sys.exit：改丟 RuntimeError，訊息是 __context__ 裡真正的錯誤，不再只有 SystemExit: 1。"""
    exit_ = SystemExit(1)
    exit_.__context__ = ValueError("bad shape")
    with pytest.raises(RuntimeError, match="ValueError: bad shape") as ei:
        _run_separate(monkeypatch, tmp_path, separate_error=exit_)
    assert isinstance(ei.value.__cause__, ValueError)
    assert "分離失敗診斷" in capsys.readouterr().out


def test_load_model_systemexit_still_upstream_message(monkeypatch, tmp_path, capsys):
    """載入模型時的 sys.exit 照 2.3.5 改成「模型載入失敗」，也會印診斷。"""
    with pytest.raises(RuntimeError, match=r"模型載入失敗（m\.ckpt）：Error: 載入失敗的原因"):
        _run_separate(monkeypatch, tmp_path, load_error=SystemExit(1))
    assert "分離失敗診斷" in capsys.readouterr().out
