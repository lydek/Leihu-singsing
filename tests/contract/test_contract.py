"""API 契約快照（docs/step0-plan.md 0f、rust-architecture-and-test-plan.md 4.5）。

逐一呼叫 2.3.2 的 92 個路由（93 個扣掉 /api/debug/stacks），比對狀態碼、Content-Type、白名單標頭、
正規化後的 JSON，以及請求前後資料資料夾和下載資料夾的檔案差異。

步驟 0 的範圍：背景工作只錄送出時的回應（Jobs 只排隊不執行），連網的路由只錄參數驗證和錯誤案例。
伺服器擋掉所有對外連線（serve.py），有案例想連外就會在檔案差異裡看到 net_blocked.log。

重新錄製：LIANTAI_SNAPSHOT_UPDATE=1 uv run pytest tests/contract
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import fixture_library as F
import numpy as np
import soundfile as sf
from werkzeug.datastructures import FileStorage
from werkzeug.test import encode_multipart

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SNAPSHOT = HERE / "snapshots" / "contract.json"

S = "/api/songs/{new}"
T = S + "/takes/{done}"


def _wav() -> bytes:
    buf = io.BytesIO()
    sf.write(
        buf,
        (0.1 * np.sin(np.arange(48000) / 48000 * 2 * np.pi * 440)).astype(np.float32),
        48000,
        format="WAV",
        subtype="PCM_16",
    )
    return buf.getvalue()


WAV = ("take.wav", _wav(), "audio/wav")
CSV = ("songs.csv", "歌手,歌名,KEY\n測試歌手,合成測試歌,-2\n".encode("utf-8-sig"), "text/csv")
JSON_CT = {"Content-Type": "application/json"}

# (名稱, 方法, 路徑, 選項)。選項：json、content、headers、files、data、save=(名稱, 回應欄位)、
# raw=True（回應是原檔直接送出，另外比對位元組）、file=路徑（回應要和資料資料夾裡這個檔案的位元組一樣）、
# volatile={欄位}（這個案例裡每次都會不同的欄位）。路徑和 json 裡的 {名稱} 會換成前面存下的編號
CASES = [
    ("page_index", "GET", "/", {}),
    ("page_overlay", "GET", "/overlay", {}),
    ("page_timing", "GET", "/timing", {}),
    ("status", "GET", "/api/status", {}),
    ("config_get", "GET", "/api/config", {}),
    ("config_set", "POST", "/api/config", {"json": {"font": "kai", "balance_usd": "12.5"}}),
    ("config_bad_json", "POST", "/api/config", {"content": "{bad", "headers": JSON_CT}),
    # 2.0.x 仍接受 text/plain 和沒帶 Content-Type 的 JSON；重寫版加 Content-Type 檢查後是預期差異（4.5 第 11 點）
    (
        "config_text_plain",
        "POST",
        "/api/config",
        {"content": '{"font": "sans"}', "headers": {"Content-Type": "text/plain"}},
    ),
    ("config_no_content_type", "POST", "/api/config", {"content": '{"font": "huninn"}'}),
    ("songs", "GET", "/api/songs", {}),
    ("songs_search", "GET", "/api/songs/search?q=合成", {}),
    ("song_get", "GET", S, {}),
    ("song_get_again", "GET", S, {}),  # 會改資料的 GET 連錄兩次（4.5 第 9 點）
    ("song_old_get", "GET", "/api/songs/{old}", {}),
    ("song_old_get_again", "GET", "/api/songs/{old}", {}),
    ("song_bad_id", "GET", "/api/songs/BAD-ID", {}),
    ("song_missing", "GET", "/api/songs/zzzzzz", {}),
    # 歌名帶「.」：後面 take_export_png 回傳的檔名順便涵蓋 M4（不能被 with_suffix 截斷）
    ("song_patch", "PATCH", S, {"json": {"title": "S.H.E 改過的歌名", "key": 2}}),
    ("song_patch_key_text", "PATCH", S, {"json": {"key": "abc"}}),
    ("song_patch_key_float", "PATCH", S, {"json": {"key": 1.5}}),
    ("song_patch_key_bool", "PATCH", S, {"json": {"key": True}}),
    ("song_patch_lyrics_flag", "PATCH", S, {"json": {"lyrics_flag": True}}),  # 2.1.0
    ("remote_open", "POST", "/api/remote/open", {"json": {"id": "{new}", "key": 2}}),
    ("remote_open_key_text", "POST", "/api/remote/open", {"json": {"id": "{new}", "key": "x"}}),
    ("remote_open_key_range", "POST", "/api/remote/open", {"json": {"id": "{new}", "key": 9}}),
    ("remote_open_bad_id", "POST", "/api/remote/open", {"json": {"id": "../x"}}),
    ("remote_open_no_content_type", "POST", "/api/remote/open", {"content": '{"id": "songnew001", "key": 1}'}),
    ("pitch", "GET", S + "/pitch", {"raw": True}),
    ("lyrics_get", "GET", S + "/lyrics", {}),
    ("lyrics_old_get", "GET", "/api/songs/{old}/lyrics", {}),
    ("lyrics_old_get_again", "GET", "/api/songs/{old}/lyrics", {}),
    ("furigana", "POST", "/api/furigana", {"json": {"lines": ["今日は晴れ"]}}),
    ("kana", "POST", S + "/kana", {"json": {"base": "今日", "reading": "きょう"}}),
    ("lyrics_use", "POST", S + "/lyrics/use", {"json": {"which": "auto"}}),
    ("lyrics_put", "PUT", S + "/lyrics", {"json": {"lrc": "[00:00.30]改過的第一句\n[00:01.20]改過的第二句"}}),
    ("lyrics_candidates", "GET", S + "/lyrics/candidates", {}),
    ("lyrics_autoshift_too_few", "POST", S + "/lyrics/autoshift", {"json": {"times": [0.2]}}),
    ("lyrics_autoshift", "POST", S + "/lyrics/autoshift", {"json": {"times": [0.2, 0.7, 1.2, 1.7]}}),
    ("lyrics_apply_empty", "POST", S + "/lyrics/apply", {"json": {"candidate": {}}}),
    ("lyrics_apply", "POST", S + "/lyrics/apply", {"json": {"candidate": F.CANDIDATE}}),
    (
        "lyrics_import",
        "POST",
        S + "/lyrics/import",
        {"json": {"text": "[00:00.40]匯入第一句\n[00:01.30]匯入第二句", "name": "x.lrc"}},
    ),
    ("lyrics_keep", "POST", S + "/lyrics/keep", {}),
    ("lyrics_export", "POST", S + "/lyrics/export", {"json": {"fmt": "srt"}}),
    ("lyrics_search_bad_id", "GET", "/api/songs/BAD-ID/lyrics/search", {}),
    ("lyrics_search_missing", "GET", "/api/songs/zzzzzz/lyrics/search?q=x", {}),
    ("claude_align_empty", "POST", S + "/claude/align", {"json": {"text": ""}}),
    ("claude_coach_unscored", "POST", S + "/claude/coach", {"json": {"take": "{pending}"}}),
    ("audio_lead", "GET", S + "/audio/lead", {"file": "library/{new}/lead.flac"}),
    (
        "audio_lead_range",
        "GET",
        S + "/audio/lead",
        {"headers": {"Range": "bytes=0-99"}, "file": "library/{new}/lead.flac"},
    ),
    ("audio_old_mix", "GET", "/api/songs/{old}/audio/mix", {"file": "library/{old}/mix.wav"}),
    ("audio_bad_stem", "GET", S + "/audio/vocals", {}),
    ("audio_variant_missing", "GET", S + "/audio/lead?key=3&speed=1", {}),
    ("variant", "POST", S + "/variant?key=2&speed=1", {}),
    ("fine", "POST", S + "/fine", {}),
    ("mixdown", "POST", S + "/mixdown", {"json": {"fmt": "wav"}}),
    ("sheet_export", "POST", S + "/sheet", {"json": {"layout": "a4"}}),
    ("process_old", "POST", "/api/songs/{old}/process", {"json": {"steps": ["pitch"]}, "save": ("job", "job")}),
    ("job_get", "GET", "/api/jobs/{job}", {}),
    ("job_missing", "GET", "/api/jobs/nope", {}),
    ("job_move", "POST", "/api/jobs/{job}/move", {"json": {"delta": -1}}),
    # 2.2.0 優先處理：排隊中的移到最前面
    ("job_top", "POST", "/api/jobs/{job}/top", {}),
    ("prioritize_queued", "POST", "/api/songs/{old}/prioritize", {}),
    ("prioritize_missing", "POST", "/api/songs/nope0000/prioritize", {}),
    ("job_cancel", "POST", "/api/jobs/{job}/cancel", {}),
    ("redownload_old", "POST", "/api/songs/{old}/redownload", {}),
    # bytes 是歌曲庫總大小，含 meta.json 裡長度不固定的修改時間（combo_lufs_sig），會差幾個位元組
    ("storage", "GET", "/api/storage", {"volatile": {"bytes"}}),
    # 2.3.0 搬移：只錄不會真的搬的情況（磁碟、大小每台電腦不同）
    (
        "storage_location",
        "GET",
        "/api/storage/location",
        {
            "volatile": {
                "drives",
                "library_size",
                "models_size",
                "library_free",
                "models_free",
                "models",
                "models_default",
            }
        },
    ),
    ("storage_move_no_target", "POST", "/api/storage/move", {"json": {"library": True}}),
    ("storage_adopt_missing", "POST", "/api/storage/adopt", {"json": {"path": ""}}),
    ("storage_delete_old_denied", "POST", "/api/storage/delete-old", {"json": {"path": "C:/Windows"}}),
    ("storage_compact", "POST", "/api/storage/compact", {}),
    ("take_get", "GET", T, {"raw": True}),
    ("take_missing", "GET", S + "/takes/20000101-000000-none", {}),
    ("take_bad_id", "GET", S + "/takes/a.b", {}),
    ("take_audio", "GET", T + "/audio", {"file": "library/{new}/takes/{done}.wav"}),
    (
        "take_audio_range",
        "GET",
        T + "/audio",
        {"headers": {"Range": "bytes=0-99"}, "file": "library/{new}/takes/{done}.wav"},
    ),
    ("take_vocal_missing", "GET", T + "/vocal/lead", {}),
    ("take_vocal_bad_tag", "GET", T + "/vocal/a.b", {}),
    ("take_rescore", "POST", T + "/rescore", {}),
    ("take_mixprep", "POST", T + "/mixprep", {"json": {}}),
    ("take_preview", "POST", T + "/preview", {"json": {}}),
    ("take_preview_file", "GET", T + "/preview.wav", {}),  # 上一個案例產生了試聽檔
    ("take_export_png", "POST", T + "/export", {"json": {"formats": ["png"]}}),
    (
        "take_upload",
        "POST",
        S + "/takes",
        {
            "files": {"file": WAV},
            "data": {"start": "0.5", "key": "0", "speed": "1", "duration": "1"},
            "save": ("take_new", "id"),
        },
    ),
    ("take_upload_empty", "POST", S + "/takes", {}),
    ("take_delete", "DELETE", S + "/takes/{take_new}", {}),
    ("takes_delete_batch", "POST", S + "/takes/delete", {"json": {"ids": ["{pending}"]}}),
    ("categories", "GET", "/api/categories", {}),
    ("categories_add", "POST", "/api/categories", {"json": {"action": "add", "name": "新分類"}}),
    ("usage", "GET", "/api/usage", {}),
    (
        "overlay_set",
        "POST",
        "/api/overlay",
        {
            "json": {
                "sid": "{new}",
                "title": "合成測試歌",
                "artist": "測試歌手",
                "t": 1.0,
                "playing": True,
                "rate": 1,
                "key": 0,
            }
        },
    ),
    ("overlay_get", "GET", "/api/overlay", {"volatile": {"t"}}),  # t 依經過時間推算目前播放位置
    ("hotkeys_get", "GET", "/api/hotkeys", {}),
    ("hotkeys_set", "POST", "/api/hotkeys", {"json": {"combos": {"no_such_action": "Ctrl+Alt+P"}}}),
    ("hotkeys_pause_on", "POST", "/api/hotkeys/pause", {"json": {"on": True}}),
    ("hotkeys_pause_off", "POST", "/api/hotkeys/pause", {"json": {"on": False}}),
    ("setlist_history_empty", "GET", "/api/setlist/history", {}),  # 2.3.0：還沒有任何一場
    ("setlist_add", "POST", "/api/setlist/add", {"json": {"id": "{new}", "title": "合成測試歌", "key": 0}}),
    ("setlist_get", "GET", "/api/setlist", {}),
    ("setlist_remove_none", "POST", "/api/setlist/remove", {"json": {"idx": []}}),  # 2.3.0
    ("setlist_clear", "POST", "/api/setlist/clear", {}),
    ("setlist_get_bad_file", "GET", "/api/setlist?file=..%2Fsetlist.json", {}),
    ("export_setlist_empty", "POST", "/api/export/setlist", {"json": {"text": "x"}}),  # 清空後本場沒有歌
    ("setlist_delete_current", "POST", "/api/setlist/delete", {"json": {}}),
    # 成功時檔名含日期，快照隔天就不同；成功的情況在 tests/test_upstream_232.py
    ("export_songlist_empty", "POST", "/api/export/songlist", {"json": {"format": "csv", "category": "沒有這個分類"}}),
    ("timing_state_put", "POST", "/api/timing/state", {"json": {"owner": "own123", "line": 1}}),
    ("timing_state_get", "GET", "/api/timing/state?owner=own123&after=0", {}),
    ("timing_cmds_first", "GET", "/api/timing/cmds?owner=own123&after=-1", {}),
    ("timing_cmd_put", "POST", "/api/timing/cmd", {"json": {"owner": "own123", "cmd": {"seek": 1}}}),
    ("timing_cmds", "GET", "/api/timing/cmds?owner=own123&after=0", {}),
    # 上游 2.3.1：YouTube 下載、歌單批次匯入、下載範本預設隱藏，開啟後才錄原本的案例
    ("youtube_locked", "POST", "/api/youtube", {"json": {"url": "https://youtu.be/AAAAAAAAAAA"}}),
    ("yt_unlock", "POST", "/api/yt-unlock", {}),
    ("sheet_template", "POST", "/api/sheet-template", {"save": ("template", "path")}),
    ("reveal", "POST", "/api/reveal", {"json": {"path": "{template}"}}),
    ("reveal_outside", "POST", "/api/reveal", {"json": {"path": "C:/Windows/win.ini"}}),
    ("open_url", "POST", "/api/open-url", {"json": {"url": "https://github.com/LeihuTW/Leihu-singsing"}}),
    ("open_url_denied", "POST", "/api/open-url", {"json": {"url": "https://evil.example"}}),
    ("open_overlay", "POST", "/api/open-overlay", {"json": {"query": "font=kai"}}),
    ("open_overlay_bad", "POST", "/api/open-overlay", {"json": {"query": "a b<"}}),
    ("open_app", "POST", "/api/open-app", {}),
    ("update_install", "POST", "/api/update/install", {}),
    ("youtube_bad_url", "POST", "/api/youtube", {"json": {"url": "not a url"}}),
    (
        "youtube_add",
        "POST",
        "/api/youtube",
        {"json": {"url": "https://youtu.be/AAAAAAAAAAA", "category": "測試分類"}, "save": ("yt_song", "id")},
    ),
    ("youtube_add_duplicate", "POST", "/api/youtube", {"json": {"url": "https://youtu.be/AAAAAAAAAAA"}}),
    ("youtube_resolve_bad", "POST", "/api/youtube/resolve", {"json": {"lines": "x"}}),
    (
        "youtube_batch",
        "POST",
        "/api/youtube/batch",
        {"json": {"items": [{"url": "https://www.youtube.com/watch?v=BBBBBBBBBBB", "title": "批次"}, {"url": "bad"}]}},
    ),
    ("youtube_sheet", "POST", "/api/youtube/sheet", {"files": {"file": CSV}}),
    ("youtube_sheet_empty", "POST", "/api/youtube/sheet", {}),
    ("youtube_resume", "POST", "/api/youtube/resume", {}),
    (
        "song_upload",
        "POST",
        "/api/songs",
        {"files": {"file": WAV}, "data": {"category": "測試分類"}, "save": ("uploaded", "id")},
    ),
    ("song_delete_uploaded", "DELETE", "/api/songs/{uploaded}", {}),
    ("song_delete_youtube", "DELETE", "/api/songs/{yt_song}", {}),
    ("upgrade_separation_not_ready", "POST", "/api/upgrade-separation", {}),  # 2.1.0：測試環境沒有模型
    ("status_end", "GET", "/api/status", {}),
    ("songs_end", "GET", "/api/songs", {}),
    # 2.1.0 直播：會改變之後的 live 狀態（直播模式 20 秒、照常處理一小時），放在最後
    ("status_live", "GET", "/api/status?live=1", {}),
    ("live_override", "POST", "/api/live/override", {}),
    ("status_override", "GET", "/api/status", {}),
    # 2.2.0 全部暫停：狀態存在設定裡，最後再繼續，不影響其他案例
    ("jobs_pause_all", "POST", "/api/jobs/pause-all", {"json": {"on": True}}),
    ("status_paused", "GET", "/api/status", {}),
    ("jobs_pause_toggle", "POST", "/api/jobs/pause-all", {}),
]

HEADERS = ("Cache-Control", "Accept-Ranges", "Content-Range", "Allow")
TEXT_SUFFIXES = {".json", ".lrc", ".log", ".csv", ".srt", ".txt"}
VOLATILE_KEYS = {"root", "version", "session_cost", "update"}  # 4.5 第 5 點
TIME_WINDOW = 3 * 86400  # 秒：離執行當下這麼近的數字才當作動態時間


class Normalizer:
    """把每次執行都會不同的值換成佔位字：暫存路徑、埠號、時間、隨機編號。"""

    def __init__(self, data: Path, downloads: Path, port: int):
        self.paths = []
        for p, tag in ((data, "<DATA>"), (downloads, "<DOWNLOADS>"), (ROOT, "<ROOT>")):
            for form in {str(p), str(p.resolve()), p.as_posix(), p.resolve().as_posix()}:
                self.paths.append((form, tag))
        self.paths.sort(key=lambda x: -len(x[0]))
        self.port = str(port)
        self.ids: dict[str, str] = {}
        # 只有執行當下附近產生的時間才換成 <time>／<ns>；fixture 裡固定的時間（2017、2020 年）原樣保留，改錯才看得出來
        self.t0 = time.time()

    def _id(self, kind: str, value: str) -> str:
        return self.ids.setdefault(value, f"<{kind}{sum(v.startswith('<' + kind) for v in self.ids.values()) + 1}>")

    def text(self, s: str) -> str:
        for form, tag in self.paths:
            s = s.replace(form, tag)
        s = s.replace(f":{self.port}", ":<PORT>")
        s = re.sub(r"([?&]t=)\d+", r"\1<t>", s)  # 防快取的時間參數（4.5 第 5 點）
        s = re.sub(r"\d{15,}", "<ns>", s)
        s = re.sub(r"(?<!\d)20[2-9]\d{5}-\d{6}-[0-9a-f]{4}(?![0-9a-z])", lambda m: self._id("take", m[0]), s)
        s = re.sub(r"(?<!\d)20[2-9]\d{5}-\d{4,6}", "<ts>", s)
        s = re.sub(r"(?<![0-9a-z])[0-9a-f]{10}(?![0-9a-z])", lambda m: self._id("id", m[0]), s)
        return s.replace("app-window", "browser")  # 有沒有裝 Edge 會不同

    def value(self, x, key=None, volatile=frozenset()):
        if key in VOLATILE_KEYS or key == "app" or key in volatile:
            return "<v>"
        if isinstance(x, dict):
            return {self.text(k): self.value(v, k, volatile) for k, v in x.items()}
        if isinstance(x, list):
            return [self.value(v, None, volatile) for v in x]
        if isinstance(x, bool) or x is None:
            return x
        if isinstance(x, (int, float)):
            if abs(x - self.t0) < TIME_WINDOW:
                return "<time>"
            if x > 1e15 and abs(x / 1e9 - self.t0) < TIME_WINDOW:
                return "<ns>"
            return round(x, 4) if isinstance(x, float) else x
        if isinstance(x, str):
            return self.text(x)
        return x


def _files(base: Path) -> dict[str, str]:
    out = {}
    for p in sorted(base.rglob("*")):
        if p.is_file():
            out[p.relative_to(base).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def audio_summary(src) -> dict:
    """音訊的格式、長度、解碼後取樣的雜湊（反相、左右聲道對調都分得出來），加上整體與 16 段的 RMS、峰值當診斷。
    雜湊比的是解碼後的取樣，不是檔案位元組（不受檔頭影響）；契約快照和比對都在同一台電腦上跑。"""
    info = sf.info(src)
    if hasattr(src, "seek"):
        src.seek(0)
    y = sf.read(src, dtype="float64", always_2d=True)[0]

    def db(x):
        return round(float(20 * np.log10(np.sqrt(np.mean(x**2)) + 1e-9)), 1) if x.size else None

    return {
        "format": info.format,
        "subtype": info.subtype,
        "frames": info.frames,
        "samplerate": info.samplerate,
        "channels": info.channels,
        "rms_db": db(y),
        "peak_db": round(float(20 * np.log10(np.max(np.abs(y)) + 1e-9)), 1) if y.size else None,
        "rms_db_16": [db(b) for b in np.array_split(y, 16)] if len(y) >= 16 else [],
        "pcm_sha256": hashlib.sha256(np.ascontiguousarray(y).tobytes()).hexdigest()[:16],
    }


def image_summary(p: Path) -> dict:
    """圖片的尺寸、色彩模式和像素的雜湊（同一台電腦上旋律譜等圖片要完全相同）。"""
    from PIL import Image

    with Image.open(p) as im:
        return {"size": list(im.size), "mode": im.mode,
                "pixels_sha256": hashlib.sha256(im.tobytes()).hexdigest()[:16]}


def _content(p: Path, n: Normalizer):
    if p.suffix not in TEXT_SUFFIXES:
        if p.suffix.lower() in (".wav", ".flac"):
            return {"audio": audio_summary(str(p))}
        if p.suffix.lower() in (".png", ".jpg", ".jpeg"):
            return {"image": image_summary(p)}
        return {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()[:16]}
    text = p.read_text("utf-8-sig", errors="replace")
    try:
        if p.suffix == ".json":
            return n.value(json.loads(text))
        if p.name == "spawn.log":  # 一行一個 JSON：先解析，路徑裡的反斜線才不會是跳脫過的
            return [n.value(json.loads(line)) for line in text.splitlines() if line]
    except ValueError:
        pass
    return n.text(text)


def _diff(before: dict, after: dict, roots: dict[str, Path], n: Normalizer) -> dict:
    out = {}
    for tag, base in roots.items():
        b, a = before[tag], after[tag]
        for k in sorted(a.keys() - b.keys()):
            out[n.text(f"{tag}/+{k}")] = _content(base / k, n)
        for k in sorted(k for k in a.keys() & b.keys() if a[k] != b[k]):
            out[n.text(f"{tag}/~{k}")] = _content(base / k, n)
        for k in sorted(b.keys() - a.keys()):
            out[n.text(f"{tag}/-{k}")] = None
    return out


def _fill(x, ids: dict):
    if isinstance(x, str):
        return re.sub(r"\{(\w+)\}", lambda m: ids[m[1]], x)
    if isinstance(x, dict):
        return {k: _fill(v, ids) for k, v in x.items()}
    if isinstance(x, list):
        return [_fill(v, ids) for v in x]
    return x


@dataclass
class Resp:
    status_code: int
    headers: dict
    content: bytes

    def json(self):
        return json.loads(self.content)


def _request(
    port: int, method: str, path: str, json_body=None, content=None, headers=None, files=None, data=None
) -> Resp:
    """用標準函式庫送請求（測試環境沒有 httpx／requests），不因 4xx、5xx 丟例外。"""
    h = dict(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    elif content is not None:
        body = content.encode("utf-8")
    elif files or data:
        values = dict(data or {})
        for k, (name, blob, ctype) in (files or {}).items():
            values[k] = FileStorage(io.BytesIO(blob), filename=name, content_type=ctype)
        boundary, body = encode_multipart(values)
        h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request(method, urllib.parse.quote(path, safe="/?=&-_.~%"), body=body, headers=h)
        r = conn.getresponse()
        return Resp(r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
    finally:
        conn.close()


def _record(r: Resp, n: Normalizer, raw: bool, volatile=frozenset()) -> dict:
    ctype = r.headers.get("content-type", "")
    rec = {
        "status": r.status_code,
        "content_type": ctype,
        "headers": {h: n.text(r.headers[h.lower()]) for h in HEADERS if h.lower() in r.headers},
    }
    if "Content-Range" in rec["headers"]:  # bytes 0-99/<總長度>：總長度跟著編碼器走
        rec["headers"]["Content-Range"] = re.sub(r"/\d+$", "/<total>", rec["headers"]["Content-Range"])
    if ctype.startswith("text/html"):
        pass  # 頁面內容跟著前端改，契約只看狀態碼和型別
    elif ctype.startswith("application/json"):
        if raw and r.status_code == 200:  # pitch.json、錄音 JSON 是原檔直接送出，位元組也要一樣（4.5 第 4 點）
            rec["sha256"] = hashlib.sha256(r.content).hexdigest()[:16]
        rec["body"] = n.value(r.json(), volatile=volatile)
    elif ctype.startswith("audio/"):
        # 不比位元組：FLAC 編碼和混音的浮點運算在不同 CPU 上位元組可能不同，行為卻一樣。
        # 完整檔案比解碼後的格式與長度；Range 請求比回傳的長度（總長度在 Content-Range 裡，已正規化）
        if r.status_code == 200:
            rec["audio"] = audio_summary(io.BytesIO(r.content))
        else:
            rec["bytes"] = len(r.content)
    else:
        rec["bytes"] = len(r.content)
        rec["sha256"] = hashlib.sha256(r.content).hexdigest()[:16]
    return rec


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FINISHED = ("done", "error", "cancelled")


def _wait_jobs(port: int, body: dict, n: Normalizer, name: str = "") -> dict:
    """等回應裡的工作（job 或 need_job）結束，再等所有工作都停下來（工作結束後可能又排了別的，
    例如處理完自動預跑移調），檔案差異才固定。回傳正規化後的最終工作狀態。"""
    jid = body.get("job") or body.get("need_job")
    assert jid, f"{name}：回應裡沒有工作編號：{body}"
    end = time.time() + 120
    while time.time() < end:
        job = _request(port, "GET", f"/api/jobs/{jid}").json()
        busy = [j for j in _request(port, "GET", "/api/status").json()["jobs"] if j["status"] not in FINISHED]
        if job.get("status") in FINISHED and not busy:
            return n.value(job)
        time.sleep(0.1)
    raise TimeoutError(f"工作 {jid} 沒有在時間內結束")


def run_contract(tmp: Path, cases=None, env_extra=None, serve_args=(), setup=None, server=None) -> dict:
    """cases 預設是步驟 0 的 CASES；setup(data) 在建好測試歌曲庫之後、啟動伺服器之前呼叫。
    server 是啟動伺服器的指令（預設 serve.py；和上游比對時用 serve_upstream.py）。"""
    data, downloads = tmp / "data", tmp / "downloads"
    downloads.mkdir(parents=True)
    F.build(data)
    if setup:
        setup(data)
    port = _free_port()
    env = dict(
        os.environ,
        LIANTAI_DATA=str(data),
        LIANTAI_DOWNLOADS=str(downloads),
        LIANTAI_PORT=str(port),
        LIANTAI_NO_BACKGROUND="1",
        LIANTAI_NO_JOBS="1",
        LIANTAI_NO_SPAWN="1",
        PYTHONIOENCODING="utf-8",
    )
    env.pop("ANTHROPIC_API_KEY", None)  # 開發機有設的話 claude_ready、has_key 會不同（app.py 讀環境變數）
    env.update(env_extra or {})
    env = {k: v for k, v in env.items() if v is not None}  # env_extra 裡值為 None 的變數拿掉
    log = open(tmp / "server.log", "w", encoding="utf-8")  # noqa: SIM115  伺服器整段期間都在寫
    proc = subprocess.Popen(
        server or [sys.executable, str(HERE / "serve.py"), *serve_args], env=env, stdout=log, stderr=subprocess.STDOUT
    )
    n = Normalizer(data, downloads, port)
    roots = {"DATA": data, "DOWNLOADS": downloads}
    ids = {"new": F.NEW, "old": F.OLD, "done": F.TAKE_DONE, "pending": F.TAKE_PENDING}
    out = {}
    try:
        for _ in range(120):
            try:
                _request(port, "GET", "/api/songs")
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError("伺服器啟動失敗：" + (tmp / "server.log").read_text("utf-8")[-2000:]) from None
                time.sleep(0.25)
        spawn = data / "spawn.log"
        for name, method, path, opt in cases or CASES:
            before = {t: _files(b) for t, b in roots.items()}
            n_spawn = len(spawn.read_text("utf-8").splitlines()) if spawn.exists() else 0
            r = _request(
                port,
                method,
                _fill(path, ids),
                json_body=_fill(opt.get("json"), ids),
                content=opt.get("content"),
                headers=opt.get("headers"),
                files=opt.get("files"),
                data=_fill(opt.get("data"), ids),
            )
            rec = _record(r, n, opt.get("raw", False), opt.get("volatile", frozenset()))
            if "file" in opt:  # 原檔直接送出：回應位元組要和磁碟上的檔案一樣（Range 請求比開頭那段）
                disk = (data / _fill(opt["file"], ids)).read_bytes()
                rec["same_as_file"] = (
                    bool(r.content)
                    and r.content == disk[: len(r.content)]
                    and (r.status_code == 206 or len(r.content) == len(disk))
                )
            if opt.get("wait"):  # 背景工作跑到完成，錄最終狀態（4.5 第 7 點）；檔案差異在工作結束後才算
                rec["job"] = _wait_jobs(port, r.json(), n, name)
            rec["files"] = _diff(before, {t: _files(b) for t, b in roots.items()}, roots, n)
            for k in rec["files"]:
                if k.endswith("spawn.log"):  # 只記這個請求新增的行：整份累積的話，一處不同會讓後面所有案例都不同
                    new = spawn.read_text("utf-8").splitlines()[n_spawn:]
                    rec["files"][k] = [n.value(json.loads(x)) for x in new if x]
            out[name] = rec
            if "save" in opt:
                ids[opt["save"][0]] = r.json()[opt["save"][1]]
    finally:
        proc.terminate()
        proc.wait(10)
        log.close()
    return out


def _json(x) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, indent=1)


def test_contract_snapshot(tmp_path, app_module):
    result = run_contract(tmp_path)

    blocked = {k: v for rec in result.values() for k, v in rec["files"].items() if "net_blocked" in k}
    assert not blocked, f"有案例對外連線（步驟 0 的快照不能連網）：{blocked}"

    # 全部路由都要涵蓋（2.1.0 加了 /api/live/override、/api/upgrade-separation；2.2.0 加了 /api/jobs/pause-all、/top、/prioritize）
    adapter = app_module.app.url_map.bind(f"127.0.0.1:{app_module.PORT}")
    ids = {
        "new": F.NEW,
        "old": F.OLD,
        "done": F.TAKE_DONE,
        "pending": F.TAKE_PENDING,
        "job": "0123456789",
        "take_new": "x",
        "template": "x",
        "uploaded": "x",
        "yt_song": "x",
    }
    hit = {adapter.match(_fill(p, ids).split("?")[0], method=m)[0] + " " + m for _, m, p, _ in CASES}
    rules = {
        f"{r.endpoint} {m}"
        for r in app_module.app.url_map.iter_rules()
        if r.endpoint not in ("static", "debug_stacks", "system.debug_stacks")  # 扁平結構、步驟 1 拆 Blueprint 之後
        for m in r.methods - {"HEAD", "OPTIONS"}
    }
    assert rules - hit == set(), f"沒有案例的路由：{sorted(rules - hit)}"
    # 2.2.0 加了 /api/jobs/pause-all、/api/jobs/<jid>/top、/api/songs/<sid>/prioritize；
    # 2.3.0 加了 /api/storage/location、move、adopt、delete-old，/api/setlist/history，/api/export/setlist、songlist，/api/setlist/remove、delete；2.3.1 加了 /api/yt-unlock
    assert len(rules) == 92

    compare_snapshot(result, SNAPSHOT)


def compare_snapshot(result: dict, path: Path) -> None:
    # 快照已經存在時，LIANTAI_SNAPSHOT_UPDATE 不會覆寫：要重建基準得另外設 LIANTAI_SNAPSHOT_REBASE=1（明確的另一個動作）
    rebuild = not path.exists() or os.environ.get("LIANTAI_SNAPSHOT_REBASE") == "1"
    if os.environ.get("LIANTAI_SNAPSHOT_UPDATE") == "1" and rebuild:
        path.parent.mkdir(exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_json(result) + "\n")
        return
    assert path.exists(), "還沒有快照：先用 LIANTAI_SNAPSHOT_UPDATE=1 錄一次"
    expected = json.loads(path.read_text("utf-8"))
    # 比 JSON 字串：True 和 1、1 和 1.0 在 Python 相等，JSON 契約卻不同
    changed = [k for k in expected.keys() | result.keys() if _json(expected.get(k)) != _json(result.get(k))]
    detail = "\n".join(
        f"--- {k}\n預期：{_json(expected.get(k))[:1500]}\n實際：{_json(result.get(k))[:1500]}"
        for k in sorted(changed)[:5]
    )
    assert not changed, f"{len(changed)} 個案例和快照不同：{sorted(changed)}\n{detail}"
