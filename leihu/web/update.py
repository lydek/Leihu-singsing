"""程式內更新：查 GitHub 最新版本、下載並執行安裝檔。"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from flask import Blueprint, jsonify

import pipeline as P

from .common import RELEASES_URL, VERSION

bp = Blueprint("update", __name__)


# GitHub 上有比較新的版本時，latest 是新版的版本號。can_install＝可以在程式裡一鍵更新；
# state：""、downloading（下載安裝檔中，progress 0～1）、installing（已啟動安裝檔）、error
UPDATE = {"latest": None, "url": RELEASES_URL, "can_install": False, "state": "", "progress": 0.0, "error": ""}


_UPDATE_ASSET: dict = {}   # 新版安裝檔：name、url、size、digest


_update_lock = threading.Lock()


ASSET_PREFIX = "https://github.com/lydek/Leihu-singsing/releases/download/"


def _ver(s: str) -> tuple:
    # fork 的版號有 4 段（2.3.2.1）：只看前 3 段的話 fork 的新版會被當成和 2.3.2 一樣
    return tuple(int(x) for x in re.findall(r"\d+", s)[:4])


def _installed_copy() -> bool:
    """是用安裝檔裝的（有內建的 Python 和解除安裝程式）。開發用的原始碼資料夾不做自動更新。
    測試版（.test-install）也不做：更新會裝上正式版的安裝檔，寫正式版的登錄檔與捷徑。"""
    return sys.platform == "win32" and (P.ROOT / "python" / "pythonw.exe").exists() \
        and (P.ROOT / "uninstall.exe").exists() and not (P.ROOT / ".test-install").exists()


def check_update() -> None:
    """啟動時到 GitHub 查一次最新版本，有新版就在畫面上方提示。沒網路或查不到就什麼都不顯示。"""
    time.sleep(5)
    try:
        r = P._http_json("https://api.github.com/repos/lydek/Leihu-singsing/releases/latest",
                         {"Accept": "application/vnd.github+json"})
        tag = str(r.get("tag_name") or "")
        if _ver(tag) <= _ver(VERSION):
            return
        # 安裝檔要有 GitHub 提供的 SHA-256 檢查碼，下載完才能確認檔案完整、沒被換掉
        asset = next((a for a in r.get("assets") or [] if str(a.get("name", "")).endswith("-Setup.exe")
                      and str(a.get("browser_download_url", "")).startswith(ASSET_PREFIX)
                      and str(a.get("digest") or "").startswith("sha256:")), None)
        if asset:
            _UPDATE_ASSET.update(name=asset["name"], url=asset["browser_download_url"],
                                 size=int(asset.get("size") or 0), digest=asset["digest"])
        UPDATE.update(latest=tag.lstrip("vV"),   # 下載頁連結固定用 releases/latest（開啟網址有白名單）
                      can_install=bool(asset) and _installed_copy())
        print(f"[update] 有新版本 {tag}（目前 {VERSION}，一鍵更新：{UPDATE['can_install']}）", flush=True)
    except Exception as e:
        P.log_error("check_update", e)


def _download_and_install() -> None:
    """下載新版安裝檔、核對檢查碼，再用安靜模式執行。安裝檔會先關閉程式，裝完自動重新開啟。"""
    import hashlib
    import urllib.request
    a = _UPDATE_ASSET
    try:
        folder = Path(tempfile.gettempdir()) / "LeihuLiantai-update"
        folder.mkdir(exist_ok=True)
        exe, part = folder / a["name"], folder / (a["name"] + ".part")
        h, got = hashlib.sha256(), 0
        req = urllib.request.Request(a["url"], headers={"User-Agent": P.USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                UPDATE["progress"] = round(min(got / a["size"], 1.0), 3) if a["size"] else 0.0
        if f"sha256:{h.hexdigest()}" != a["digest"]:
            part.unlink(missing_ok=True)
            raise P.UserError("下載的安裝檔不完整（檢查碼不符），請再試一次或到下載頁手動下載")
        part.replace(exe)
        UPDATE.update(state="installing", progress=1.0)
        print(f"[update] 啟動安裝檔 {exe}", flush=True)
        # /S＝安靜模式（沿用已下載的套件與模型，只更新需要的部分）；/D 一定要放最後、不能加引號
        cmd = f'"{exe}" /S /D={P.ROOT}'
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：程式關閉時安裝檔不會跟著結束
        if P.spawned("installer", cmd):
            return
        try:
            subprocess.Popen(cmd, creationflags=flags | 0x01000000, close_fds=True)   # CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(cmd, creationflags=flags, close_fds=True)
    except Exception as e:
        UPDATE.update(state="error", error=str(e) if isinstance(e, P.UserError) else f"更新失敗：{e}")
        P.log_error("update", e)


@bp.post("/api/update/install")
def update_install():
    """畫面上按「更新」：在背景下載並執行新版安裝檔。"""
    if not UPDATE["can_install"]:
        raise P.UserError("這個版本沒辦法在程式裡更新，請到下載頁下載新版安裝檔")
    with _update_lock:
        if UPDATE["state"] in ("downloading", "installing"):
            return jsonify(ok=True)
        UPDATE.update(state="downloading", progress=0.0, error="")
    threading.Thread(target=_download_and_install, daemon=True, name="update").start()
    return jsonify(ok=True)
