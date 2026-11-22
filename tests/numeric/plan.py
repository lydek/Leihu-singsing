"""測試計畫（JSON）：每種基準跑幾次、跑哪幾首真實歌曲（docs/refactor-port-plan.md 3.8）。

不掃描資料夾：歌曲用路徑指定（相對路徑以計畫檔所在的資料夾為準），一定要附 sha256，開始前逐首核對。
計畫檔列的是自己的真實歌曲，放 tests/private/plans/（不進 git）；git 裡的摘要不記真實歌曲的任何資料（編號、sha256、逐首結果只在 tests/private/baselines/ 的完整版）；
編號用沒有意義的名稱（例如 real01），說明寫在 label（只在本機）。
合成素材（tests/numeric/make_inputs.py 產生）每次都會跑，不寫在計畫裡。格式：

{
  "numeric":    {"runs": 1, "songs": ["song01", "song02"]},
  "perf":       {"runs": 3, "songs": ["song01"]},
  "similarity": {"runs": 3, "songs": ["song01", "song02"]},
  "songs": {
    "song01": {"label": "自己看的說明", "path": "../songs/xxx.flac", "sha256": "..."}
  }
}

沒用到的段落可以省略（用到時才報錯）。歌曲編號會當成被測程式裡的歌曲編號和輸出資料夾名稱，
所以要符合 pipeline.song_dir 的規則 [a-z0-9]{6,16}，也不能和合成素材同名。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SYNTH = ("songsing01", "songspeech01", "synth")
SONG_ID = re.compile(r"[a-z0-9]{6,16}")
MIN_RUNS = {"numeric": 1, "perf": 1, "similarity": 2}  # 相似度要兩兩比較


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path: str | Path, kind: str) -> dict:  # noqa: C901
    """讀計畫的 kind 段落，核對每首歌的路徑和 sha256。有任何問題就結束（SystemExit），不默默少跑。
    回傳 {"name": 計畫檔名（不含副檔名）, "path": 絕對路徑, "runs": 次數, "songs": [{"id", "label", "path", "sha256"}]}。"""
    p = Path(path).resolve()
    try:
        plan = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"讀不了測試計畫 {p}：{e}") from None
    sec = plan.get(kind) if isinstance(plan, dict) else None
    if not isinstance(sec, dict):
        raise SystemExit(f"測試計畫 {p.name} 沒有「{kind}」段落")
    runs = sec.get("runs")
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < MIN_RUNS[kind]:
        raise SystemExit(f"測試計畫 {p.name} 的 {kind}.runs 要是 {MIN_RUNS[kind]} 以上的整數（現在是 {runs!r}）")
    ids = sec.get("songs", [])
    if not isinstance(ids, list):
        raise SystemExit(f"測試計畫 {p.name} 的 {kind}.songs 要是歌曲編號的清單")
    catalog = plan.get("songs") or {}
    bad, songs = [], []
    if len(set(map(str, ids))) != len(ids):
        bad.append("歌曲編號重複")
    for sid in ids:
        s = catalog.get(sid) if isinstance(catalog, dict) else None
        if not isinstance(sid, str) or not SONG_ID.fullmatch(sid) or sid in SYNTH:
            bad.append(f"{sid!r}：編號要符合 [a-z0-9]{{6,16}}，也不能和合成素材同名")
        elif not isinstance(s, dict) or not s.get("path") or not s.get("sha256"):
            bad.append(f"{sid}：songs 裡沒有它的 path、sha256")
        elif not (f := (p.parent / s["path"]).resolve()).is_file():
            bad.append(f"{sid}：找不到 {f}")
        elif (h := sha256(f)) != str(s["sha256"]).lower():
            bad.append(f"{sid}：{f.name} 的 sha256 和計畫不同（實際 {h[:12]}…），可能換了檔案")
        else:
            songs.append({"id": sid, "label": s.get("label", ""), "path": f, "sha256": h})
    if bad:
        raise SystemExit(f"測試計畫 {p.name} 的 {kind} 歌曲有問題：\n" + "\n".join(f"- {b}" for b in bad))
    return {"name": p.stem, "path": p, "runs": runs, "songs": songs}


def _selftest() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        song = Path(d) / "a.flac"
        song.write_bytes(b"x")
        good = {"numeric": {"runs": 1, "songs": ["abcdef"]},
                "songs": {"abcdef": {"path": "a.flac", "sha256": sha256(song), "label": "L"}}}
        f = Path(d) / "p.json"
        f.write_text(json.dumps(good), "utf-8")
        r = load(f, "numeric")
        assert r["runs"] == 1 and r["songs"][0]["path"] == song.resolve() and r["name"] == "p"
        for broken in ({**good, "numeric": {"runs": 0, "songs": []}},                       # 次數不夠
                       {**good, "songs": {"abcdef": {"path": "a.flac", "sha256": "0" * 64}}},  # sha256 不同
                       {**good, "numeric": {"runs": 1, "songs": ["Bad-ID"]}},              # 編號不合規則
                       {**good, "numeric": {"runs": 1, "songs": ["abcdef", "abcdef"]}}):   # 重複
            f.write_text(json.dumps(broken), "utf-8")
            try:
                load(f, "numeric")
            except SystemExit:
                continue
            raise AssertionError(f"應該失敗：{broken}")
        try:
            load(f, "perf")
        except SystemExit:
            pass
        else:
            raise AssertionError("沒有 perf 段落應該失敗")


if __name__ == "__main__":
    _selftest()
    print("plan.py 自我檢查通過")
