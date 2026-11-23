"""fork 的版號（上游版本＋第 4 段序號）與程式內更新：只查 fork 的 release、只接受 fork 的安裝檔。"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORK = "https://github.com/lydek/Leihu-singsing"


def test_version_is_fork_version(app_module):
    assert app_module.VERSION == "2.3.4.2"
    assert (ROOT / "VERSION").read_text("utf-8").strip() == app_module.VERSION
    assert '!define VERSION "2.3.4.2"' in (ROOT / "installer" / "leihu.nsi").read_text("utf-8")


def test_ver_compares_four_parts(app_module):
    v = app_module._ver
    assert v("v2.3.2.2") > v("2.3.2.1") > v("2.3.2")  # 只看 3 段的話 fork 的新版會被當成一樣
    assert v("v2.4.0") > v("2.3.2.1")


@pytest.fixture
def update(app_module, monkeypatch):
    import pipeline as P

    calls = []
    monkeypatch.setattr(app_module.time, "sleep", lambda s: None)

    def release(tag, url):
        def http_json(u, headers):
            calls.append(u)
            return {"tag_name": tag, "assets": [{"name": f"LeihuLiantai-{tag[1:]}-Setup.exe", "browser_download_url": url,
                                                  "size": 1, "digest": "sha256:" + "a" * 64}]}
        monkeypatch.setattr(P, "_http_json", http_json)

    old = dict(app_module.UPDATE), dict(app_module._UPDATE_ASSET)
    yield app_module, release, calls
    app_module.UPDATE.clear()
    app_module.UPDATE.update(old[0])
    app_module._UPDATE_ASSET.clear()
    app_module._UPDATE_ASSET.update(old[1])


def test_update_checks_fork_releases(update):
    app, release, calls = update
    release("v2.3.4.3", f"{FORK}/releases/download/v2.3.4.3/LeihuLiantai-2.3.4.3-Setup.exe")
    app.check_update()
    assert calls == ["https://api.github.com/repos/lydek/Leihu-singsing/releases/latest"]
    assert app.UPDATE["latest"] == "2.3.4.3"
    assert app._UPDATE_ASSET["url"].startswith(FORK + "/releases/download/")
    assert app.UPDATE["url"] == f"{FORK}/releases/latest"


def test_update_ignores_same_version_and_upstream_assets(update):
    app, release, _ = update
    release("v2.3.4.2", f"{FORK}/releases/download/v2.3.4.2/LeihuLiantai-2.3.4.2-Setup.exe")
    app.check_update()
    assert app.UPDATE["latest"] is None  # 和目前一樣：不提示
    release("v2.3.4.9", "https://github.com/LeihuTW/Leihu-singsing/releases/download/v2.3.4.9/x-Setup.exe")
    app.check_update()
    assert "url" not in app._UPDATE_ASSET  # 不是 fork 的安裝檔：不能一鍵更新


def test_open_url_whitelist_points_to_fork(app_module):
    assert f"{FORK}" in app_module.EXTERNAL_URLS
    assert not any("LeihuTW" in u for u in app_module.EXTERNAL_URLS)
    html = (ROOT / "static" / "index.html").read_text("utf-8")
    assert f'href="{FORK}/releases/latest"' in html
    # 授權要求註明原作來源：LICENSE.md、安裝檔的版權聲明仍指向原專案
    assert "https://github.com/LeihuTW/Leihu-singsing" in (ROOT / "LICENSE.md").read_text("utf-8")
