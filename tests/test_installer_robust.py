"""docs/next-version-plan.md 第 4 批：安裝程式的 O21（整體逾時）、M3（probe 的路徑）、38 的 PowerShell 路徑。"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installer(tmp_path):
    """installer.py 載入時會在自己的資料夾開 logs/install.log：複製到暫存資料夾再載入，不寫到 repo。"""
    shutil.copy2(ROOT / "installer.py", tmp_path / "installer.py")
    spec = importlib.util.spec_from_file_location("installer_robust", tmp_path / "installer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    mod.LOG.close()


def test_o21_run_has_overall_timeout(installer, monkeypatch):
    """pip 等子程序卡住（網路斷掉沒有回應）時不會永遠停在這裡：逾時就連同子程序結束，回傳非 0。"""
    monkeypatch.setattr(installer, "RUN_TIMEOUT", 1)
    monkeypatch.setattr(installer, "say", lambda *a, **k: None)
    t0 = time.time()
    code = installer.run([sys.executable, "-c", "import time; time.sleep(60)"])
    assert code != 0
    assert time.time() - t0 < 20


def test_m3_probe_passes_paths_by_argv(installer, monkeypatch, tmp_path):
    """路徑嵌進程式碼字串時，使用者名稱含「'」會 SyntaxError，也能被注入程式碼；改用 argv 傳。"""
    monkeypatch.setattr(installer, "PY", Path(sys.executable))
    odd = tmp_path / "O'Brien [x]" / "model.onnx"
    ok, out = installer.probe("print(sys.argv[2])", odd)
    assert ok, out
    assert out == str(odd)


def test_m3_no_path_embedded_in_probe_code():
    """installer.py 裡不再有把路徑用 r'...' 嵌進 probe 程式碼的寫法。"""
    src = (ROOT / "installer.py").read_text("utf-8")
    assert not re.search(r"r'\{|r'%s'", src)


def test_stop_app_passes_prefix_by_env(installer, monkeypatch):
    """路徑用環境變數傳、用 StartsWith 比對：嵌進單引號字串時路徑含「'」會語法錯誤，-like 還會把 [ ] 當萬用字元。"""
    seen = {}

    def powershell(script, **env):
        seen.update(script=script, env=env)
        return ""

    monkeypatch.setattr(installer, "powershell", powershell)
    installer.stop_app()
    prefix = str(Path(sys.prefix))
    assert prefix not in seen["script"]
    assert seen["env"]["LEIHU_PREFIX"].rstrip("\\/") == prefix
    assert "StartsWith($env:LEIHU_PREFIX" in seen["script"]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 只在 Windows")
def test_powershell_env_handles_quote_and_brackets(installer):
    odd = r"C:\Users\O'Brien [x]\LeihuLiantai\python" + "\\"
    out = installer.powershell(
        "[string]('C:\\Users\\O''Brien [x]\\LeihuLiantai\\python\\python.exe'"
        ".StartsWith($env:LEIHU_T, [StringComparison]::OrdinalIgnoreCase))",
        LEIHU_T=odd,
    )
    assert out == "True"


# ---------------------------------------------------------------- 續傳時 .part 已經下載完（antigravity 第二輪審查 6）


class _Resp:
    def __init__(self, data: bytes, status: int = 200):
        self.status, self.headers, self._buf = status, {"Content-Length": str(len(data))}, [data, b""]

    def read(self, n=-1):
        return self._buf.pop(0) if self._buf else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(data: bytes, seen: list):
    import urllib.error

    def urlopen(req, timeout=None):
        rng = req.headers.get("Range")
        seen.append(rng)
        if rng and int(rng.split("=")[1].rstrip("-")) >= len(data):
            raise urllib.error.HTTPError(req.full_url, 416, "Range Not Satisfiable", {}, None)
        return _Resp(data)

    return urlopen


@pytest.mark.parametrize("part", [b"wheel-bytes", b"broken-bytes"])
def test_download_full_part_file_416(installer, monkeypatch, tmp_path, part):
    """上次中斷在 .part 寫滿、還沒改名時：Range 從檔尾開始，伺服器回 416，原本連試 4 次都失敗。
    .part 核對正確就直接用；不正確就刪掉從頭下載。"""
    import hashlib
    import urllib.request

    data = b"wheel-bytes"
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(data, seen))
    monkeypatch.setattr(installer, "progress", lambda *a, **k: None)
    monkeypatch.setattr(installer.time, "sleep", lambda s: None)
    dest = tmp_path / "dl" / "torch.whl"
    dest.parent.mkdir()
    (dest.parent / "torch.whl.part").write_bytes(part)
    out = installer.download("https://example.invalid/torch.whl", dest, hashlib.sha256(data).hexdigest())
    assert out.read_bytes() == data
    assert seen[0] == f"bytes={len(part)}-"
    assert len(seen) <= 2
