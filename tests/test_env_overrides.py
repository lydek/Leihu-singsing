"""S28：環境變數覆寫。環境變數在 conftest.py 設好；沒設時的預設值和開關由另開的 Python 檢查。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _spawn_log(P) -> list[dict]:
    f = P.DATA / "spawn.log"
    return [json.loads(x) for x in f.read_text("utf-8").splitlines()] if f.exists() else []


def test_data_paths_follow_liantai_data(app_module):
    import claude_api as C
    import pipeline as P

    data = Path(os.environ["LIANTAI_DATA"])
    assert data == P.DATA
    assert P.LIB == P.DEFAULT_LIB == data / "library"
    assert data / "config.json" == P.CONFIG_PATH
    assert data / "usage.json" == C.USAGE_PATH
    assert data / "setlist.json" == app_module.SETLIST_PATH
    assert data / "setlists" == app_module.SETLIST_DIR
    assert P.ROOT == ROOT  # 程式本身的位置不跟著改
    assert P.MODELS == P.DEFAULT_MODELS == ROOT / "models"  # 模型不是使用者資料，留在程式資料夾


def _py(code: str, cwd: Path = ROOT, **env) -> object:
    """另開一個 Python 執行 code，回傳最後一行印出的 JSON。環境拿掉 LIANTAI_*（這個行程的已經被 conftest 改掉）。"""
    base = {k: v for k, v in os.environ.items() if not k.startswith("LIANTAI_")}
    code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); " + code
    out = subprocess.run([sys.executable, "-c", code], cwd=cwd, env={**base, "PYTHONIOENCODING": "utf-8", **env},
                         capture_output=True, text=True, encoding="utf-8", timeout=120, check=True).stdout
    out = out.strip().splitlines()[-1]
    return json.loads(out)


def test_defaults_without_env():
    """沒設環境變數時，路徑和開關都和上游一樣。LIB 會被 ROOT/config.json 的 library_dir 改掉，所以看 DEFAULT_LIB。"""
    got = _py(
        "import json, pipeline as P;"
        "print(json.dumps([str(P.DATA), str(P.DEFAULT_LIB), str(P.CONFIG_PATH), P.NO_BACKGROUND, P.NO_JOBS,"
        " P.NO_SPAWN, P.spawned('browser', 'x')]))"
    )
    assert got == [str(ROOT), str(ROOT / "library"), str(ROOT / "config.json"), False, False, False, False]


def test_without_env_still_opens_programs():
    """沒設 LIANTAI_NO_SPAWN 時照原本開瀏覽器（換成假的記下呼叫），也不寫 spawn.log；佇列照常開執行緒。"""
    code = textwrap.dedent("""
        import json, threading, webbrowser
        calls = []
        webbrowser.open = lambda url, *a, **k: calls.append(url)
        import app, pipeline as P
        url = app.EXTERNAL_URLS[0]
        app.app.test_client().post("/api/open-url", json={"url": url})
        n = threading.active_count()
        P.Jobs()
        print(json.dumps([calls == [url], threading.active_count() - n, (P.DATA / "spawn.log").exists()]))
    """)
    assert _py(code) == [True, 1, False]


def test_relative_and_missing_data_dir(tmp_path):
    """LIANTAI_DATA 是相對路徑、資料夾還不存在：依呼叫端的目錄轉成絕對路徑寫回環境變數（gpu_worker 子程式 cwd 是
    ROOT，繼承後才會指到同一個地方），並建立資料夾。"""
    code = "import json, os, pipeline as P; print(json.dumps([str(P.DATA), os.environ['LIANTAI_DATA'], P.DATA.is_dir()]))"
    got = _py(code, cwd=tmp_path, LIANTAI_DATA=os.path.join("new", "data"))
    want = str((tmp_path / "new" / "data").resolve())
    assert got == [want, want, True]


def test_downloads_dir_override(app_module):
    import pipeline as P

    assert P.downloads_dir() == Path(os.environ["LIANTAI_DOWNLOADS"])


def test_jobs_only_queue(app_module):
    import pipeline as P

    ran = []
    jobs = P.Jobs()
    jid = jobs.submit("test", "song01", lambda report: ran.append(1))
    time.sleep(0.3)
    assert not ran and jobs.get(jid)["status"] == "queued"


def test_background_downloads_are_noops(app_module):
    import pipeline as P

    hq, hqv, kana = dict(P.HQ_DL), dict(P.HQV_DL), dict(P.KANA)
    P.start_hq_download()
    P.start_hqv_download()
    P.start_kana_install()
    assert (hq, hqv, kana) == (P.HQ_DL, P.HQV_DL, P.KANA)


def test_no_spawn_logs_instead_of_opening(app_module):
    import pipeline as P

    c = app_module.app.test_client()
    url = app_module.EXTERNAL_URLS[0]
    assert c.post("/api/open-url", json={"url": url}).status_code == 200
    assert _spawn_log(P)[-1] == {"kind": "browser", "target": url}

    old = P.CONFIG_PATH.read_bytes() if P.CONFIG_PATH.exists() else None
    P.CONFIG_PATH.write_text(json.dumps({"yt_enabled": True}), "utf-8")  # 2.3.1 起歌單範本要先開啟 YouTube 功能
    try:
        r = c.post("/api/sheet-template")
    finally:
        P.CONFIG_PATH.write_bytes(old) if old is not None else P.CONFIG_PATH.unlink()
    assert r.status_code == 200
    out = Path(r.get_json()["path"])
    assert out.is_relative_to(Path(os.environ["LIANTAI_DOWNLOADS"])) and out.exists()
    assert _spawn_log(P)[-1] == {"kind": "reveal", "target": str(out)}

    assert c.post("/api/reveal", json={"path": str(out)}).status_code == 200
    assert _spawn_log(P)[-1] == {"kind": "reveal", "target": str(out.resolve())}

    r = c.post("/api/open-app")
    assert r.status_code == 200 and _spawn_log(P)[-1]["kind"] in ("app-window", "browser")

    assert c.post("/api/open-overlay", json={"query": "a=1"}).status_code == 200
    assert _spawn_log(P)[-1] == {"kind": "browser", "target": f"http://127.0.0.1:{app_module.PORT}/overlay?a=1"}


def test_hotkey_files_follow_data(app_module):
    import pipeline as P

    c = app_module.app.test_client()
    assert c.post("/api/hotkeys/pause", json={"on": True}).status_code == 200
    assert (P.DATA / ".hotkeys_pause").exists()
    assert c.post("/api/hotkeys/pause", json={"on": False}).status_code == 200
    assert not (P.DATA / ".hotkeys_pause").exists()
