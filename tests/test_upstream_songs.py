"""tests/numeric/upstream_songs.py：找上游安裝、只讀它的歌曲庫、測試計畫寫到指定位置。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "numeric"))

import plan as PLAN  # noqa: E402
import upstream_songs as U  # noqa: E402


def _install(d: Path, url: str = U.UPSTREAM_URL) -> Path:
    d.mkdir(parents=True)
    (d / "app.py").write_text(f'RELEASES_URL = "https://{url}/releases/latest"\n', "utf-8")
    (d / "VERSION").write_text("2.1.0", "utf-8")
    lib = d / "library"
    for sid, files in {"0a2aad6d6a": {"original.wav": b"orig", "mix.wav": b"mixw"},
                       "12efd4016d": {"mix.flac": b"song2"},
                       "deadbeef00": {"lyrics.lrc": b"x"},     # 沒有音檔
                       "Bad-ID": {"mix.flac": b"song3"}}.items():  # 編號不合規則
        (lib / sid).mkdir(parents=True)
        (lib / sid / "meta.json").write_text(json.dumps({"title": sid}), "utf-8")
        for name, data in files.items():
            (lib / sid / name).write_bytes(data)
    return d


def _snapshot(d: Path) -> dict:
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in d.rglob("*") if p.is_file()}


def test_detects_only_upstream_installs(tmp_path):
    up = _install(tmp_path / "up")
    fork = _install(tmp_path / "fork", U.FORK_URL)
    assert U.installs([up, fork, tmp_path / "missing", up]) == [up]


def test_library_dir_from_config(tmp_path):
    up = _install(tmp_path / "up")
    assert U.library(up) == up / "library"
    (up / "config.json").write_text(json.dumps({"library_dir": str(tmp_path / "elsewhere")}), "utf-8")
    assert U.library(up) == tmp_path / "elsewhere"


def test_plan_points_at_upstream_audio_and_never_writes_there(tmp_path):
    up = _install(tmp_path / "up")
    before = _snapshot(up)
    out = tmp_path / "private" / "plans" / "upstream-library.json"
    assert U.main([f"--upstream={up}", "--plan-only"], plan_path=out) == 0
    assert _snapshot(up) == before  # 上游的檔案沒有被改、刪、加
    plan = json.loads(out.read_text("utf-8"))
    assert plan["numeric"]["songs"] == ["0a2aad6d6a", "12efd4016d"]
    assert Path(plan["songs"]["0a2aad6d6a"]["path"]) == up / "library" / "0a2aad6d6a" / "original.wav"  # 原始檔優先
    loaded = PLAN.load(out, "numeric")  # baseline.py 讀得了，sha256 對得上
    assert [s["path"] for s in loaded["songs"]] == [up / "library" / "0a2aad6d6a" / "original.wav",
                                                    up / "library" / "12efd4016d" / "mix.flac"]


def test_songs_filter_and_duplicates(tmp_path):
    a, b = _install(tmp_path / "a"), _install(tmp_path / "b")
    plan, notes = U.build_plan([U.library(a), U.library(b)], 1, ["12efd4016d", "ffffffffff"])
    assert list(plan["songs"]) == ["12efd4016d"]
    assert any("同一個音檔" in n for n in notes) and any("ffffffffff" in n for n in notes)


def test_no_upstream_skips_without_writing(tmp_path, capsys):
    out = tmp_path / "plan.json"
    assert U.main([f"--upstream={tmp_path / 'missing'}"], plan_path=out) == 2
    assert not out.exists()
    assert "沒有找到已安裝的上游" in capsys.readouterr().out
