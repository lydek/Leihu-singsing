"""只給本機用的檢查（Host、Origin、功能開關、搬移中擋寫入）與錯誤格式。"""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, request

import pipeline as P

from .common import PORT

bp = Blueprint("security", __name__)


@bp.app_errorhandler(P.UserError)
def user_error(e):
    return jsonify(error=str(e)), 400


@bp.before_app_request
def _local_only():
    """練唱台只給這台電腦自己的畫面和程式用。
    瀏覽器裡其他網站的網頁可以對 127.0.0.1 送出 POST（不需要預檢的簡單請求），
    不擋的話任何網站都能趁練唱台開著時改設定、加下載、清歌單；所以：
    1. Host 不是本機位址的一律拒絕（防 DNS rebinding）；
    2. 會改資料的請求（非 GET）如果帶了 Origin，必須是練唱台自己。"""
    host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/").lower() not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            abort(403)
        # 2.3.0 搬移歌曲庫中：會寫進歌曲庫的操作先擋住（不然搬完會少掉這段時間的修改）
        if request.path in YT_GATED and not P.load_config().get("yt_enabled"):
            raise P.UserError("這個功能尚未開啟")
        if P.STORAGE["moving"] and request.method != "GET" and request.path.startswith(MOVE_BLOCK):
            raise P.UserError("正在搬移歌曲庫，搬完才能操作歌曲（大約幾分鐘，進度在處理清單）")


# 2.3.1：YouTube 下載、歌單批次匯入要先開啟（點左上標題五下）才能用
YT_GATED = ("/api/youtube", "/api/youtube/resolve", "/api/youtube/batch", "/api/youtube/sheet", "/api/sheet-template")


MOVE_BLOCK = ("/api/songs", "/api/youtube", "/api/batch", "/api/upgrade", "/api/categories", "/api/sheet")
