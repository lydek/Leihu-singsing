"""C 組的安全與防止資料遺失修正（docs/refactor-port-plan.md 3.13）：M4、M5、config／歌單壞檔、O12、O13、O14。"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installer(tmp_path):
    """installer.py 載入時會在自己的資料夾開 logs/install.log：複製到暫存資料夾再載入，不寫到 repo。"""
    shutil.copy2(ROOT / "installer.py", tmp_path / "installer.py")
    spec = importlib.util.spec_from_file_location("installer_under_test", tmp_path / "installer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    mod.LOG.close()


def _raise(e):
    raise e


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


def test_m5_rename_failure_keeps_file_and_reply(usage, monkeypatch):
    usage.USAGE_PATH.write_text("{壞掉", "utf-8")
    real = Path.replace
    monkeypatch.setattr(Path, "replace", lambda self, t: _raise(PermissionError("被鎖住")) if self == usage.USAGE_PATH
                        else real(self, t))
    entry = usage.record("claude-x", _Usage(), "test", "")
    assert entry["input_tokens"] == 10  # 回覆照常交給呼叫端
    monkeypatch.undo()
    assert usage.USAGE_PATH.read_text("utf-8") == "{壞掉"


def test_m5_unreadable_usage_is_not_overwritten(usage, monkeypatch):
    usage.USAGE_PATH.write_text('{"calls": [{"time": 1}]}', "utf-8")

    def locked():
        raise PermissionError("檔案正被使用")

    monkeypatch.setattr(usage, "_load", locked)
    entry = usage.record("claude-x", _Usage(), "test", "")
    assert entry["input_tokens"] == 10  # 回覆照常交給呼叫端
    assert usage.USAGE_PATH.read_text("utf-8") == '{"calls": [{"time": 1}]}'


# ---------------------------------------------------------------- config.json、setlist.json 壞掉不能被覆寫


@pytest.fixture
def config(app_module):
    import pipeline as P

    yield P
    for f in [P.CONFIG_PATH, *P.CONFIG_PATH.parent.glob("config.json.*.bad")]:
        f.unlink(missing_ok=True)


def test_corrupt_config_is_kept(config):
    P = config
    P.CONFIG_PATH.write_text('{"anthropic_key": "sk-x", 壞', "utf-8")
    cfg = P.load_config()  # 壞掉時讀起來是預設值（和上游相同）
    cfg["font"] = "kai"
    P.save_config(cfg)
    bad = [b.read_text("utf-8") for b in P.CONFIG_PATH.parent.glob("config.json.*.bad")]
    assert bad == ['{"anthropic_key": "sk-x", 壞']
    assert json.loads(P.CONFIG_PATH.read_text("utf-8"))["font"] == "kai"


def test_unreadable_config_is_not_overwritten(config, monkeypatch):
    P = config
    P.CONFIG_PATH.write_text('{"a": 1}', "utf-8")
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: _raise(PermissionError("被鎖住"))
                        if self == P.CONFIG_PATH else real(self, *a, **k))
    with pytest.raises(P.UserError):
        P.save_config({"a": 2})
    monkeypatch.undo()
    assert P.CONFIG_PATH.read_text("utf-8") == '{"a": 1}'


def test_corrupt_setlist_is_kept(app_module):
    p = app_module.SETLIST_PATH
    p.write_text("[壞掉", "utf-8")
    try:
        assert app_module.app.test_client().post("/api/setlist/clear").status_code == 200
        assert [b.read_text("utf-8") for b in p.parent.glob("setlist.json.*.bad")] == ["[壞掉"]
        assert json.loads(p.read_text("utf-8")) == {"started": None, "items": []}
    finally:
        for f in [p, *p.parent.glob("setlist.json.*.bad")]:
            f.unlink(missing_ok=True)


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


def test_o12_existing_model_data_is_verified(tmp_path, monkeypatch, app_module):
    import pipeline as P

    def sha(b):
        return hashlib.sha256(b).hexdigest()

    monkeypatch.setattr(P, "MODELS", tmp_path)
    monkeypatch.setattr(P, "MODEL_DATA", {"ok.json": (2, sha(b"{}"), ["u"]), "old.json": (2, sha(b"[]"), ["u"])})
    (tmp_path / "uvr").mkdir()
    (tmp_path / "uvr" / "ok.json").write_bytes(b"{}")
    (tmp_path / "uvr" / "old.json").write_bytes(b"{}")  # 能解析，但不是釘住的內容（例如舊版從 main 抓的）
    calls = []
    monkeypatch.setattr(P, "fetch_file", lambda urls, p, size=None, sha256=None: calls.append((p.name, p.exists())))
    P.ensure_model_data()
    assert calls == [("old.json", False)]  # 只重抓不符的那個，抓之前先刪掉

    monkeypatch.setattr(P, "fetch_file", lambda *a, **k: _raise(OSError("檢查碼不符")))
    with pytest.raises(P.UserError):  # 下載失敗就停止，不交給 audio-separator 從 main 下載
        P.ensure_model_data()


def test_o12_small_config_file_is_checked_by_sha(tmp_path, monkeypatch, app_module):
    import pipeline as P

    monkeypatch.setattr(P, "MODELS", tmp_path)
    (tmp_path / "uvr").mkdir()
    body = b"model: a\n"
    files = {"c.yaml": (len(body), hashlib.sha256(body).hexdigest(), ["u"])}
    (tmp_path / "uvr" / "c.yaml").write_bytes(body)
    assert P._files_ready(files)
    (tmp_path / "uvr" / "c.yaml").write_bytes(b"model: b\n")  # 同樣大小、內容不同
    assert not P._files_ready(files)
    assert not (tmp_path / "uvr" / "c.yaml").exists()  # 刪掉，下載時重抓


# ---------------------------------------------------------------- O13：PyTorch 套件沒有檢查碼就不直接下載


def test_o13_wheel_without_checksum_is_refused(installer, monkeypatch):
    import urllib.request

    inst = installer

    def page(sha: str):
        def urlopen(req, timeout=None):
            name = req.full_url.rstrip("/").split("/")[-1]
            fname = f"{name}-{inst.TORCH[name]}%2Bcpu-cp311-cp311-win_amd64.whl"
            return io.BytesIO(f'<a href="{fname}{sha}">x</a>'.encode())

        return urlopen

    monkeypatch.setattr(urllib.request, "urlopen", page(""))
    with pytest.raises(RuntimeError, match="沒有檢查碼"):
        inst._wheel_links("cpu")
    monkeypatch.setattr(urllib.request, "urlopen", page("#sha256=" + "a" * 64))
    assert [sha for _, _, sha in inst._wheel_links("cpu")] == ["a" * 64] * len(inst.TORCH)


def test_o13_install_torch_stops_instead_of_pip(installer, monkeypatch):
    inst = installer
    calls = []
    monkeypatch.setattr(inst, "installed_versions", lambda: {})
    monkeypatch.setattr(inst, "pip", lambda *a, **k: calls.append(a) or 0)
    monkeypatch.setattr(inst, "_wheel_links", lambda tag: _raise(inst.NoChecksum("清單裡的 torch 沒有檢查碼")))
    assert inst.install_torch("cpu") is False
    assert calls == []  # pip 用同一份清單，也沒辦法核對：不能改走 pip
    # 其他原因（例如連不上）直接下載失敗：照舊改走 pip（pip 會核對清單裡的檢查碼）
    monkeypatch.setattr(inst, "_wheel_links", lambda tag: _raise(OSError("連不上")))
    assert inst.install_torch("cpu") is True
    assert "--index-url" in calls[0]


# ---------------------------------------------------------------- O14：install.ps1 核對 Python 安裝檔


def test_o14_install_ps1_checks_python_zip():
    ps1 = (ROOT / "setup" / "install.ps1").read_text("utf-8-sig")
    assert "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b" in ps1
    assert ps1.count("Get-Python $tmp") == 2  # 第一次下載和壓縮檔損壞重下，兩處都要核對
    assert 'Get-File "https://www.python.org' not in ps1
    assert "Get-FileHash -Path" not in ps1  # 暫存路徑含 [ ] 時會被當成萬用字元，要用 -LiteralPath


def test_o14_install_ps1_checks_get_pip():
    ps1 = (ROOT / "setup" / "install.ps1").read_text("utf-8-sig")
    assert "pypa/get-pip/c7135ee10cd1b1fcb8fbf63d5d0004885cba7aa4/public/get-pip.py" in ps1
    assert "356e0d0de21cdc348c3394084d90939b85e4246c4d14c76aee6ca4f46646ea98" in ps1
    assert "Get-GetPip $gp" in ps1
    assert "bootstrap.pypa.io" not in ps1  # 會變動的網址、下載完直接執行
