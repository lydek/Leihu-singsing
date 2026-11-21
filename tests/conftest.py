"""測試共用設定。

2.3.2 只讀 LIANTAI_PORT，沒有 LIANTAI_DATA 之類的覆寫（S28），所以這裡不設環境變數：
設了也不會生效，只會讓人以為資料有隔離。測試只能 import app 和讀取，不能寫使用者資料。
測試檔不要在模組頂端 import app、pipeline，一律從下面的 fixture 取得。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 2.3.2 寫在安裝資料夾的使用者資料。只檢查這些：掃整個資料夾的話，import pipeline 把 ffmpeg 複製到 bin/、
# 編輯器、索引工具或同一資料夾裡開著的練唱台（tests/tools/run_dev.py）寫檔，都會被誤判成測試寫的
_USER_DATA = (
    "library",
    "config.json",
    "usage.json",
    "setlist.json",
    "hotkeys_status.json",
    ".hotkeys_pause",
    ".appwin",
    ".duet_cleaned",
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
    """測試不能動到安裝資料夾的使用者資料（真的歌曲庫、設定檔）。"""
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
