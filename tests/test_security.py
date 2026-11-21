"""C 組的安全與防止資料遺失修正（docs/refactor-port-plan.md 3.13）：M4、M5、O12、O13、O14。

import installer 會在 repo 的 logs/ 開 install.log（上游的副作用，logs/ 在 .gitignore）。
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- M5：usage.json 壞掉不能被覆寫


@pytest.fixture
def usage(app_module):
    import claude_api as C

    yield C
    for f in [C.USAGE_PATH, *C.USAGE_PATH.parent.glob("usage.json.*.bad")]:
        f.unlink(missing_ok=True)


class _Usage:
    input_tokens, output_tokens = 10, 20


def test_m5_corrupt_usage_is_kept(usage):
    usage.USAGE_PATH.write_text("{壞掉", "utf-8")
    assert usage.summary()["total"]["calls"] == 0  # summary 唯讀：和原本一樣當成沒有紀錄
    assert usage.USAGE_PATH.read_text("utf-8") == "{壞掉"
    usage.record("claude-x", _Usage(), "test", "")
    bad = list(usage.USAGE_PATH.parent.glob("usage.json.*.bad"))
    assert len(bad) == 1 and bad[0].read_text("utf-8") == "{壞掉"
    assert len(json.loads(usage.USAGE_PATH.read_text("utf-8"))["calls"]) == 1


def test_m5_wrong_shape_is_kept(usage):
    usage.USAGE_PATH.write_text('{"calls": 3}', "utf-8")
    usage.record("claude-x", _Usage(), "test", "")
    assert [b.read_text("utf-8") for b in usage.USAGE_PATH.parent.glob("usage.json.*.bad")] == ['{"calls": 3}']


def test_m5_unreadable_usage_is_not_overwritten(usage, monkeypatch):
    usage.USAGE_PATH.write_text('{"calls": [{"time": 1}]}', "utf-8")

    def locked():
        raise PermissionError("檔案正被使用")

    monkeypatch.setattr(usage, "_load", locked)
    entry = usage.record("claude-x", _Usage(), "test", "")
    assert entry["input_tokens"] == 10  # 回覆照常交給呼叫端
    assert usage.USAGE_PATH.read_text("utf-8") == '{"calls": [{"time": 1}]}'


# ---------------------------------------------------------------- M4：匯出檔名不能被截斷


def test_m4_out_path_keeps_dots(app_module):
    import share as SH

    base = Path("x") / "S.H.E 中國話_我的演唱_20261121-1200"
    assert SH.out_path(base, ".mp3").name == "S.H.E 中國話_我的演唱_20261121-1200.mp3"
    assert SH.out_path(base, "_成績卡.png").name == "S.H.E 中國話_我的演唱_20261121-1200_成績卡.png"
    assert base.with_suffix(".mp3").name == "S.H.mp3"  # 原本的寫法：S.H.E 的每首歌、每次錄音都撞名、互相覆寫


# ---------------------------------------------------------------- O12：模型設定檔釘版本、核對檢查碼


def test_o12_model_configs_are_pinned_and_checked(app_module):
    import pipeline as P

    for name, (size, sha, urls) in {**P.MODEL_DATA, P._HQ_YAML: P.MODEL_FILES[P._HQ_YAML]}.items():
        assert size and len(sha or "") == 64, f"{name} 沒有大小或檢查碼"
        assert not any("/main/" in u or "@main/" in u for u in urls), f"{name} 抓的是會變動的 main 分支"


def test_o12_fetch_file_rejects_wrong_checksum(tmp_path, monkeypatch, app_module):
    import pipeline as P

    monkeypatch.setattr(P, "_open", lambda url, *a: io.BytesIO(b"tampered"))
    with pytest.raises(IOError):
        P.fetch_file(["http://x/a.json"], tmp_path / "a.json", 8, "0" * 64)
    assert not (tmp_path / "a.json").exists()
    P.fetch_file(["http://x/a.json"], tmp_path / "a.json", 8, hashlib.sha256(b"tampered").hexdigest())
    assert (tmp_path / "a.json").read_bytes() == b"tampered"


def test_o12_model_data_download_passes_checksum(tmp_path, monkeypatch, app_module):
    import pipeline as P

    calls = {}
    monkeypatch.setattr(P, "MODELS", tmp_path)
    monkeypatch.setattr(P, "fetch_file", lambda urls, p, size=None, sha256=None: calls.update({p.name: (size, sha256)}))
    P.ensure_model_data()
    assert calls == {name: (size, sha) for name, (size, sha, _) in P.MODEL_DATA.items()}


# ---------------------------------------------------------------- O13：PyTorch 套件沒有檢查碼就不直接下載


def test_o13_wheel_without_checksum_is_refused(monkeypatch):
    import urllib.request

    import installer as I

    def page(sha: str):
        def urlopen(req, timeout=None):
            name = req.full_url.rstrip("/").split("/")[-1]
            fname = f"{name}-{I.TORCH[name]}%2Bcpu-cp311-cp311-win_amd64.whl"
            return io.BytesIO(f'<a href="{fname}{sha}">x</a>'.encode())

        return urlopen

    monkeypatch.setattr(urllib.request, "urlopen", page(""))
    with pytest.raises(RuntimeError, match="沒有檢查碼"):
        I._wheel_links("cpu")
    monkeypatch.setattr(urllib.request, "urlopen", page("#sha256=" + "a" * 64))
    assert [sha for _, _, sha in I._wheel_links("cpu")] == ["a" * 64] * len(I.TORCH)


# ---------------------------------------------------------------- O14：install.ps1 核對 Python 安裝檔


def test_o14_install_ps1_checks_python_zip():
    ps1 = (ROOT / "setup" / "install.ps1").read_text("utf-8-sig")
    assert "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b" in ps1
    assert ps1.count("Get-Python $tmp") == 2  # 第一次下載和壓縮檔損壞重下，兩處都要核對
    assert 'Get-File "https://www.python.org' not in ps1
