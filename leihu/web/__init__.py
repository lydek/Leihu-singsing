"""本機 HTTP 伺服器：app factory 與 Blueprint。路由的網址、參數、回應都和 2.3.2 相同（契約快照守住）。"""
from __future__ import annotations

import logging

from flask import Flask

import pipeline as P


class _QuietPolls(logging.Filter):
    """畫面每 1～5 秒會來問一次狀態，這些成功的例行請求不寫進 logs/app.log（不然一個月就幾十 MB，
    也蓋掉真正有用的訊息）。失敗的照樣記。"""
    QUIET = ('"GET /api/status', '"GET /api/overlay', '"GET /api/hotkeys/', '"GET /static/',
             '"POST /api/overlay')   # fork：畫面每 2～5 秒把播放狀態送給直播歌詞，筆電的 app.log 大半是這行

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not (any(q in msg for q in self.QUIET) and ('" 200 ' in msg or '" 304 ' in msg))


logging.getLogger("werkzeug").addFilter(_QuietPolls())


def create_app() -> Flask:
    from . import library, live, lyrics, security, storage, system, takes, update

    app = Flask("app", static_folder=str(P.ROOT / "static"), static_url_path="/static")   # 名稱沿用拆檔前的 app 模組
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024
    for mod in (security, system, update, storage, library, lyrics, takes, live):
        app.register_blueprint(mod.bp)
    return app
