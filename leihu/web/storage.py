"""佔用空間、2.3.0 歌曲庫／模型搬移。"""
from __future__ import annotations

import time

from flask import Blueprint, jsonify, request

import pipeline as P

from .common import jobs, vjobs

bp = Blueprint("storage", __name__)


@bp.get("/api/storage")
def storage():
    return jsonify(P.storage_report())


@bp.post("/api/storage/compact")
def storage_compact():
    """把舊歌曲的 WAV 分軌轉成 FLAC（無損）、刪掉不需要的原始檔。排進處理佇列，和處理歌曲不會同時進行。"""
    j = jobs.find("process", "", "compact")
    if j:
        return jsonify(job=j)
    busy = lambda sid: any(x.get("song") == sid and x.get("status") in ("queued", "running") for x in vjobs.active())
    jid = jobs.submit("process", "", lambda report: P.compact_library(report, busy), label="compact")
    return jsonify(job=jid)


# ---------------------------------------------------------------- 2.3.0 歌曲庫／模型搬移
@bp.get("/api/storage/location")
def storage_location():
    return jsonify(P.storage_info())


@bp.post("/api/storage/move")
def storage_move():
    """把歌曲庫（和可選的模型）搬到新位置。排進處理佇列：等手上的工作停下來才開始；
    搬移中處理佇列暫停、會寫進歌曲庫的操作先擋住，搬完恢復原本的暫停狀態。"""
    b = request.get_json(force=True) or {}
    target = str(b.get("target") or "").strip()
    what = [k for k in ("library", "models") if b.get(k)]
    if not target:
        raise P.UserError("請選擇或輸入新的位置")
    if not what:
        raise P.UserError("請至少選一項要搬移的東西")
    if P.STORAGE["moving"] or jobs.find("storage", "", "move"):
        raise P.UserError("已經在搬移中")
    if "models" in what and (P.HQ_DL.get("state") == "downloading" or P.HQV_DL.get("state") == "downloading"):
        raise P.UserError("模型還在下載中，下載完成後再搬移")
    was_paused = jobs.paused
    jobs.set_paused(True)   # 處理中的歌先停下、讓出歌曲庫

    def run(report):
        P.STORAGE["moving"] = True
        try:
            t0 = time.time()
            while vjobs.active() and any(j["status"] == "running" for j in vjobs.active()):
                report("等待 KEY 轉換完成", 0.0)   # 移調版本寫在歌曲庫裡，等它寫完
                time.sleep(1)
                if time.time() - t0 > 600:
                    break
            out = P.move_storage(target, what, report)
            try:
                import gpu_worker as G
                G.POOL.shutdown(); G.CPU_POOL.shutdown()   # 顯示卡處理程式重開，才會用新的模型位置
            except Exception:
                pass
            print(f"[storage] 搬移完成：{out}", flush=True)
        finally:
            P.STORAGE["moving"] = False
            jobs.set_paused(was_paused)
    jid = jobs.submit("storage", "", run, label="move")
    return jsonify(job=jid)


@bp.post("/api/storage/adopt")
def storage_adopt():
    """改用已經有歌曲庫的資料夾（換電腦、外接硬碟），不複製檔案。"""
    if P.STORAGE["moving"] or jobs.running:
        raise P.UserError("還有工作在處理，請先按「全部暫停」等它停下來，或等處理完再切換")
    b = request.get_json(force=True) or {}
    out = P.adopt_storage(str(b.get("path") or ""))
    try:
        import gpu_worker as G
        G.POOL.shutdown(); G.CPU_POOL.shutdown()
    except Exception:
        pass
    return jsonify(out)


@bp.post("/api/storage/delete-old")
def storage_delete_old():
    b = request.get_json(force=True) or {}
    P.delete_old_storage(str(b.get("path") or ""))
    return jsonify(ok=True)
