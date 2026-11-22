"""測試共用設定。

環境變數要在這裡（模組載入時）設好，不能放在 fixture 裡：pytest 會先 import 所有測試檔，
pipeline.DATA、app.PORT、Jobs 在 import 時就固定了。測試檔不要在模組頂端 import app、pipeline，
一律從下面的 fixture 取得。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# S28：使用者資料放暫存資料夾、不跑背景工作、佇列只排隊、不開外部程式
_TMP = Path(tempfile.mkdtemp(prefix="liantai-test-"))
(_TMP / "data" / "library").mkdir(parents=True)  # main() 才會建 library/，測試不跑 main()
(_TMP / "downloads").mkdir()
os.environ.update(
    LIANTAI_DATA=str(_TMP / "data"),
    LIANTAI_DOWNLOADS=str(_TMP / "downloads"),
    LIANTAI_NO_BACKGROUND="1",
    LIANTAI_NO_JOBS="1",
    LIANTAI_NO_SPAWN="1",
)
os.environ.pop("LIANTAI_PORT", None)  # 用預設的 8765

# 沒設 LIANTAI_DATA 時，使用者資料放在安裝資料夾的這些位置。只檢查這些：掃整個資料夾的話，import pipeline 把
# ffmpeg 複製到 bin/、編輯器、索引工具或同一資料夾裡開著的練唱台（tests/tools/run_dev.py）寫檔，都會被誤判成測試寫的
_USER_DATA = (
    "library",
    "config.json",
    "usage.json",
    "setlist.json",
    "setlists",
    "queue.json",
    "hotkeys_status.json",
    ".hotkeys_pause",
    ".appwin",
    ".duet_cleaned",
    "spawn.log",
    "logs/lyrics.log",
    "logs/claude.log",
    "models/gpu_probe.json",
)


def _root_files() -> dict[str, object]:
    out = {}
    for p in [ROOT / n for n in _USER_DATA]:
        for q in [p, *p.rglob("*")] if p.is_dir() else [p]:
            try:
                st = q.stat()
            except OSError:
                continue
            out[str(q.relative_to(ROOT))] = (st.st_mtime_ns, st.st_size) if q.is_file() else "dir"
    return out


@pytest.fixture(scope="session", autouse=True)
def _root_untouched():
    """設了 LIANTAI_DATA 之後，測試不能動到安裝資料夾的使用者資料（真的歌曲庫、設定檔）。"""
    before = _root_files()
    yield
    after = _root_files()
    added = sorted(after.keys() - before.keys())
    changed = sorted(k for k in after.keys() & before.keys() if after[k] != before[k])
    removed = sorted(before.keys() - after.keys())  # 刪掉真的歌曲庫或設定檔是最嚴重的情況
    assert not added and not changed and not removed, (
        f"測試動到了安裝資料夾的使用者資料：新增 {added}，修改 {changed}，刪除 {removed}"
    )


@pytest.fixture(scope="session")
def app_module():
    import app

    return app


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)
