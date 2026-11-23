"""tests/contract/compare_upstream.py 的預期差異規則：只放行登記的那個差異本身（Codex r4-7）。"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "contract"))
import compare_upstream as C


def _job(i, kind="process", label="fine", song="songnew001"):
    return {"created": "<time>", "error": None, "id": f"<id{i}>", "kind": kind, "label": label, "order": i,
            "progress": 0.0, "song": song, "status": "queued", "step": "排隊中"}


def _status(jobs, total):
    return {"status": 200, "body": {"jobs": jobs, "queued_total": total, "paused": False}}


MINE = [_job(0), _job(1, "export", "mixdown")]
UPSTREAM = [_job(0), _job(1, "process", "all", "<id9>"), _job(2, "export", "mixdown"), _job(3, "process", "youtube", "<id8>")]


def test_d4_accepts_exactly_the_two_deleted_songs_jobs():
    _, expected, unexpected = C.compare({"status_end": _status(MINE, 1)}, {"status_end": _status(UPSTREAM, 3)})
    assert unexpected == {} and list(expected) == ["D4"]


def test_d4_rejects_other_jobs_disappearing():
    """別首歌的工作整批消失（這一邊的清單空了、queued_total 0）不能被 D4 放行。"""
    _, expected, unexpected = C.compare({"status_end": _status([], 0)}, {"status_end": _status(UPSTREAM, 3)})
    assert "status_end" in unexpected and "D4" not in expected


def test_d4_rejects_wrong_total_or_kind():
    _, _, u1 = C.compare({"status_end": _status(MINE, 1)}, {"status_end": _status(UPSTREAM, 4)})   # 少 3 不是 2
    up = copy.deepcopy(UPSTREAM)
    up[1]["kind"] = "variant"   # 多出來的不是 process 工作
    _, _, u2 = C.compare({"status_end": _status(MINE, 1)}, {"status_end": _status(up, 3)})
    assert "status_end" in u1 and "status_end" in u2


def test_d4_other_fields_still_compared():
    """核對通過只換掉 jobs、queued_total，其他欄位的差異照樣列出。"""
    mine = _status(MINE, 1)
    mine["body"]["paused"] = True
    _, expected, unexpected = C.compare({"status_end": mine}, {"status_end": _status(UPSTREAM, 3)})
    assert "D4" in expected and unexpected == {"status_end": ["~ /body/paused: false -> true"]}
