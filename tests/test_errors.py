"""docs/next-version-plan.md 第 3 批：API 輸入驗證（D1、D3，step2-plan.md）與錯誤格式統一（S15）。"""

from __future__ import annotations

import json
import shutil

import pytest

SID = "errtest001"


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


@pytest.fixture
def song(app_module):
    import pipeline as P

    d = P.LIB / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": SID, "title": "t", "key": 3}), "utf-8")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- S15：錯誤一律回 JSON


def _json_error(r, code):
    assert r.status_code == code
    assert r.is_json, r.get_data(as_text=True)[:200]
    assert r.get_json() == {"error": f"請求失敗（{code}）"}


def test_s15_unknown_route_404(client):
    _json_error(client.get("/api/no-such-route"), 404)


def test_s15_missing_take_404(client, song):
    _json_error(client.get(f"/api/songs/{SID}/takes/20990101-000000-none"), 404)


def test_s15_405_keeps_allow_header(client):
    r = client.get("/api/jobs/abc/cancel")
    _json_error(r, 405)
    assert "POST" in r.headers["Allow"]


def test_s15_bad_json_400(client, song):
    r = client.patch(f"/api/songs/{SID}", data="{not json", content_type="application/json")
    _json_error(r, 400)


def test_s15_forbidden_host_403(client):
    _json_error(client.get("/api/status", headers={"Host": "evil.example"}), 403)


def test_s15_unhandled_exception_500_is_logged(client, monkeypatch, caplog):
    import claude_api as C
    import pipeline as P

    logged = []
    monkeypatch.setattr(C, "summary", lambda: 1 / 0)
    monkeypatch.setattr(P, "log_error", lambda where, e: logged.append((where, type(e).__name__)))
    _json_error(client.get("/api/usage"), 500)
    assert logged == [("GET /api/usage", "ZeroDivisionError")]
    # traceback 照舊經 app.logger 寫進 app.log（伺服器的 stderr）
    assert any(r.exc_info and r.exc_info[0] is ZeroDivisionError for r in caplog.records)


def test_s15_user_error_keeps_message(client):
    """UserError 維持 400 和原本的訊息（song_dir 找不到歌不是 404）。"""
    r = client.get("/api/songs/BAD!/takes/x")
    assert r.status_code == 400
    assert r.get_json() == {"error": "歌曲編號不正確"}


# ---------------------------------------------------------------- D1：PATCH 歌曲的 KEY


@pytest.mark.parametrize("key", ["abc", 9, 1.5, True, "7"])
def test_d1_invalid_key_is_rejected(client, song, key):
    """不合法的 KEY 原本悄悄存成 0、回 200，使用者設的 KEY 就不見了。"""
    import pipeline as P

    r = client.patch(f"/api/songs/{SID}", json={"key": key})
    assert r.status_code == 400
    assert "KEY" in r.get_json()["error"]
    assert P.read_meta(song)["key"] == 3


@pytest.mark.parametrize(("key", "saved"), [("", 0), (None, 0), (0, 0), ("+2", 2), (-6, -6), ("＋３", 3)])
def test_d1_valid_or_empty_key_is_saved(client, song, key, saved, monkeypatch):
    """空字串、null 照舊當成清除；合法的照舊存。"""
    from leihu.web import library

    monkeypatch.setattr(library, "prerender_key", lambda sid: None)  # 不排移調工作
    r = client.patch(f"/api/songs/{SID}", json={"key": key})
    assert r.status_code == 200
    assert r.get_json()["key"] == saved


# ---------------------------------------------------------------- D3：新增錄音的 key、speed、start、duration


def _post_take(client, **form):
    import io

    data = {"file": (io.BytesIO(b"RIFF"), "take.wav"), "start": "0", "key": "0", "speed": "1", "duration": "1"}
    data.update(form)
    return client.post(f"/api/songs/{SID}/takes", data=data, content_type="multipart/form-data")


@pytest.mark.parametrize(
    "form",
    [
        {"key": "x"},
        {"key": "2.5"},
        {"key": "9"},
        {"speed": "abc"},
        {"speed": "0"},
        {"speed": "3"},
        {"start": "nan"},
        {"start": "-1"},
        {"duration": "inf"},
        {"duration": "abc"},
    ],
)
def test_d3_invalid_take_fields_are_rejected_before_saving(client, song, form):
    """不是數字原本回 500；超出範圍照存，之後評分可能除以零。要在存錄音檔之前擋下，不留下檔案。"""
    r = _post_take(client, **form)
    assert r.status_code == 400
    assert r.get_json()["error"]
    assert not (song / "takes").exists() or not any((song / "takes").iterdir())


def test_d3_valid_take_is_accepted(client, song, monkeypatch):
    from leihu.web import common

    monkeypatch.setattr(common.jobs, "state", {})
    monkeypatch.setattr(common.jobs, "pending", [])
    r = _post_take(client, start="12.345", key="-2", speed="0.8", duration="30.5")
    assert r.status_code == 200
    info = json.loads((song / "takes" / f"{r.get_json()['id']}.json").read_text("utf-8"))
    assert (info["start"], info["key"], info["speed"], info["duration"]) == (12.345, -2, 0.8, 30.5)
