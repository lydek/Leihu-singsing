"""安裝檔的身分：預設（正式版）和上游相同；LEIHU_TEST=1 建的測試版名稱、安裝位置、登錄檔都不同，可以並存。

這台電腦沒有 makensis，這裡只讀 leihu.nsi 的定義，不實際建安裝檔。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NSI = (ROOT / "installer" / "leihu.nsi").read_text("utf-8")


def _defines(test: bool) -> dict[str, str]:
    """照 !ifdef TEST_BUILD／!else／!endif 取出這個版本的 !define。"""
    out, branch = {}, None
    for line in NSI.splitlines():
        s = line.strip()
        if s == "!ifdef TEST_BUILD":
            branch = True
        elif s == "!else" and branch is not None:
            branch = False
        elif s == "!endif":
            branch = None
        elif (m := re.match(r'!define (\w+) "(.*)"$', s)) and (branch is None or branch == test):
            out[m[1]] = m[2]
    for k, v in out.items():  # 展開 ${...}
        out[k] = re.sub(r"\$\{(\w+)\}", lambda m: out.get(m[1], m[0]), v)
    return out


def test_default_identity_matches_upstream():
    d = _defines(test=False)
    assert d["APPNAME"] == "雷虎練唱台"
    assert d["REGKEY"] == r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LeihuLiantai"
    assert d["OUTNAME"] == "雷虎練唱台2.3.2.1.exe"
    assert 'InstallDir "$LOCALAPPDATA\\${INSTNAME}"' in NSI and d["INSTNAME"] == "LeihuLiantai"


def test_test_build_identity_is_separate():
    a, b = _defines(test=False), _defines(test=True)
    for k in ("APPNAME", "INSTNAME", "REGKEY", "OUTNAME"):  # 捷徑、開始功能表資料夾都用 APPNAME
        assert a[k] != b[k], k
    assert "LeihuLiantai" in b["INSTNAME"] and b["INSTNAME"] != "LeihuLiantai"


def test_test_install_disables_in_app_update(tmp_path, monkeypatch, app_module):
    import pipeline as P

    (tmp_path / "python").mkdir()
    (tmp_path / "python" / "pythonw.exe").write_bytes(b"")
    (tmp_path / "uninstall.exe").write_bytes(b"")
    monkeypatch.setattr(P, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    assert app_module._installed_copy()  # 正式版：可以程式內更新（和上游相同）
    (tmp_path / ".test-install").write_text("雷虎練唱台 測試版 2.3.2", "utf-8")
    assert not app_module._installed_copy()  # 測試版：更新會裝上正式版的安裝檔，不做
