"""啟動時開的背景工作（app.main 啟動；契約沙盒從 app 模組替換掉）。"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import pipeline as P

from .common import after_process, jobs, prerender_key, queue_fine, queue_max, vjobs, yt_submit
from .live import _SETLIST_LOCK, _setlist_read


def prefetcher() -> None:
    """背景預先下載：佇列裡接下來的兩首 YouTube 歌，趁顯示卡在處理別首時先下載好。"""
    while True:
        time.sleep(3)
        try:
            if P.YT_PAUSE["on"]:
                continue
            for sid in jobs.upcoming("youtube", 2):
                P.prefetch_youtube(sid)
        except Exception as e:
            P.log_error("prefetcher", e)


def resume_downloads() -> None:
    """程式關掉時還在排隊的 YouTube 下載，重新開啟後自動接著排隊（失敗、取消的不會自動重試）。"""
    try:
        for m in sorted(P.pending_downloads(), key=lambda m: m.get("created") or 0):
            if m["dl_state"] == "queued":
                yt_submit(m["id"], m["source_url"])
    except Exception as e:
        P.log_error("resume_downloads", e)


# ---------------------------------------------------------------- 啟動

def watch_parent() -> None:
    """桌面版啟動時帶 --parent-pid：視窗程式結束（包含被強制關閉）時，伺服器跟著結束。"""
    if "--parent-pid" not in sys.argv:
        return
    pid = int(sys.argv[sys.argv.index("--parent-pid") + 1])

    def wait():
        if sys.platform == "win32":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
            if h:
                k.WaitForSingleObject(h, 0xFFFFFFFF)
        else:
            while True:
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(2)
        os._exit(0)

    threading.Thread(target=wait, daemon=True).start()


def remove_duet_leftovers() -> None:
    """1.7.0 測試版的「男女對唱」已經拿掉：清掉它留下的模型（約 530 MB）、每首歌的男聲／女聲軌與聲部標記。
    只需要做一次，做完留一個記號，之後啟動不用再掃整個歌曲庫。"""
    import shutil
    flag = P.DATA / ".duet_cleaned"
    if flag.exists():
        return
    for pat in ("model_chorus_bs_roformer_ep_267_sdr_24.1275.ckpt*", "config_chorus_male_female_bs_roformer.yaml*"):
        for f in P.MODELS.glob(pat):
            try:
                f.unlink()
            except OSError:
                pass
    try:
        dirs = [d for d in P.LIB.iterdir() if d.is_dir()]
    except OSError:
        return
    for d in dirs:
        found = False
        for f in [*d.glob("male.*"), *d.glob("female.*"), *d.glob("parts.json"), *d.glob("v*/male.*"), *d.glob("v*/female.*")]:
            try:
                f.unlink() if f.is_file() else shutil.rmtree(f, ignore_errors=True)
                found = True
            except OSError:
                pass
        if found and (d / "meta.json").exists():
            try:
                with P._META_LOCK:
                    m = P.read_meta(d)
                    if "parts_stems" in m:
                        m.pop("parts_stems", None)
                        P.write_meta(d, m)
            except Exception as e:
                P.log_error("remove_duet_leftovers", e)
    try:
        flag.write_text("1", "utf-8")
    except OSError:
        pass


def lead_fix_sweep() -> None:
    """2.3.2：檢查「主唱被分到合音」（疊錄的副歌、對唱、多人輪流唱）。舊歌檢查一次；
    之後每 5 分鐘看一次有沒有新處理好、或歌詞換過的歌（逐句檢查要等歌詞套用後才準）。
    只讀檔判斷，沒有問題的歌直接記下已檢查；有問題的排一個背景工作搬回主唱並重新分析旋律（直播中暫停）。"""
    time.sleep(60)
    while True:
        _lead_fix_scan()
        time.sleep(300)


def _lead_fix_scan() -> None:
    try:
        with _SETLIST_LOCK:
            sl = {i.get("id") for i in _setlist_read()["items"]}
        songs = sorted(P.list_songs(), key=lambda m: (-P.usage_score(m, sl), -(m.get("created") or 0)))
        for s in songs:
            if not P.lead_fix_needed(s):
                continue
            while P.live_status()["hold"] or P.STORAGE.get("moving"):
                time.sleep(30)
            sid = s["id"]
            if any(jobs.find("process", sid, lb) for lb in ("upgrade", "max", "fine", "all", "youtube", "leadfix")):
                continue
            d = P.song_dir(sid)
            try:
                lead = P.read_audio(P.stem_path(d, "lead"), sr=P.PITCH_SR, mono=True)
                back = P.read_audio(P.stem_path(d, "back"), sr=P.PITCH_SR, mono=True)
                g, sec, stats = P._lead_gap_gain(lead, back, P.PITCH_SR, P._lyric_spans(d))
            except Exception as e:
                P.log_error("lead_fix_sweep", e)
                continue
            if g is None:
                P._split_note(d, stats)
                P.update_meta(d, lead_fix=P.LEAD_FIX_V, lead_fix_sec=0, lead_fix_lrc=P._lrc_stamp(d))
            else:
                print(f"[separate] {s.get('title') or sid}：主唱約 {sec:.0f} 秒被分到合音，排入背景修正", flush=True)
                jobs.submit("process", sid, P.lead_fix_job(sid, after=prerender_key), label="leadfix")
            time.sleep(0.5)
    except Exception as e:
        P.log_error("lead_fix_sweep", e)


def live_light_upgrade() -> None:
    """直播中匯入、先用輕量模型處理的歌：直播結束後自動用高品質模型重新分離＋分析旋律（歌詞、錄音不動）。
    upgrade 是背景工作，萬一又開播會自動暫停。"""
    time.sleep(90)
    tried: set[str] = set()   # 這次開程式已經排過的（失敗或被按停止就不再自動排）
    while True:
        try:
            cfg = P.load_config()
            if not P.live_status(cfg)["hold"] and P.hq_models_ready(cfg):
                for s in P.list_songs():
                    sid = s["id"]
                    if not s.get("live_light") or sid in tried:
                        continue
                    if any(jobs.find("process", sid, lb) for lb in ("upgrade", "max", "fine", "all", "youtube")):
                        continue
                    proc = P.process_song(sid, ["separate", "pitch"])

                    def run(report, sid=sid, proc=proc):
                        proc(report)
                        try:
                            prerender_key(sid)   # 重新分軌會清掉舊的移調版本
                        except Exception as e:
                            P.log_error("live_light_upgrade prerender", e)
                    jobs.submit("process", sid, run, label="upgrade")
                    tried.add(sid)
        except Exception as e:
            P.log_error("live_light_upgrade", e)
        time.sleep(120)


def loudness_backfill() -> None:
    """舊歌曲在背景補量各音軌組合的響度（每首約 2～3 秒）。
    有處理工作在跑時先暫停，不和分軌、辨識搶資源；沒補到的歌第一次開啟時會當場量。"""
    time.sleep(60)
    try:
        dirs = [d for d in P.LIB.iterdir() if d.is_dir() and (d / "meta.json").exists()]
    except OSError:
        return
    for d in dirs:
        while any(j["status"] == "running" for j in jobs.active() + vjobs.active()):
            time.sleep(30)
        try:
            meta = P.read_meta(d)
            if meta.get("steps", {}).get("separate") and not meta.get("combo_lufs"):
                P.measure_stem_loudness(d)
                time.sleep(1)
        except Exception as e:
            P.log_error("loudness_backfill", e)


# ---------------------------------------------------------------- 佇列跨重開保存（2.3.3）
# 佇列只存在記憶體裡：以前重開練唱台，還在排的匯入、精細處理、最高品質、重新處理都會不見，
# 歌曲停在「旋律未完成」或一直顯示「排隊中」。現在每 3 秒把排隊中的工作（歌曲＋種類）存到檔案，
# 重開後照原本的順序排回去。YouTube 下載、主唱檢查、下播後補做高品質本來就會自己接續，不在這裡。
QUEUE_FILE = P.DATA / "queue.json"   # fork（S28）：使用者資料放 DATA；沒設 LIANTAI_DATA 時和上游一樣是程式資料夾
_QUEUE_READY = threading.Event()


def _restorable(label: str) -> bool:
    if label in ("all", "fine", "max", "upgrade"):
        return True
    parts = label.split(",") if label else []
    return bool(parts) and all(x in ("import", "separate", "pitch", "lyrics") for x in parts)


def _queue_snapshot() -> list[dict]:
    with jobs.lock:
        out, seen = [], set()
        order = [j for j in jobs.running] + [j for j, _ in jobs.pending]
        for jid in order:
            st = jobs.state.get(jid) or {}
            key = (st.get("song"), st.get("label"))
            if (st.get("kind") == "process" and st.get("song") and _restorable(st.get("label") or "")
                    and st.get("status") in ("queued", "running") and not st.get("cancel") and key not in seen):
                seen.add(key)
                out.append({"song": st["song"], "label": st["label"]})
        return out


def queue_saver() -> None:
    _QUEUE_READY.wait()
    last = None
    while True:
        try:
            snap = _queue_snapshot()
            if snap != last:
                P.atomic_write_text(QUEUE_FILE, json.dumps(snap, ensure_ascii=False))
                last = snap
        except Exception as e:
            P.log_error("queue_saver", e)
        time.sleep(3)


def _resubmit(sid: str, label: str) -> None:
    if not P.song_dir(sid).exists() or jobs.find("process", sid, label):
        return
    meta = P.read_meta(P.song_dir(sid))
    if label == "fine":
        if not meta.get("fine_done"):
            queue_fine(sid)
        return
    if label == "max":
        try:
            queue_max(sid)
        except P.UserError:
            pass   # 最高品質模型不在了：不排
        return
    if label == "upgrade":
        proc = P.process_song(sid, ["separate", "pitch"])

        def run(report, sid=sid, proc=proc):
            proc(report)
            prerender_key(sid)
        jobs.submit("process", sid, run, label="upgrade")
        return
    if label == "all":
        steps = ["import", "separate", "pitch", "lyrics"]
        proc = P.process_song(sid, steps)

        def run(report, sid=sid, proc=proc):
            proc(report)
            try:
                after_process(sid)
            except Exception as e:
                P.log_error("after_process", e)
        jobs.submit("process", sid, run, label="all")
        return
    jobs.submit("process", sid, P.process_song(sid, label.split(",")), label=label)


def restore_queue() -> None:
    """重開後把上次還在排的工作照順序排回去（只看 queue.json）。"""
    try:
        try:
            saved = json.loads(QUEUE_FILE.read_text("utf-8")) if QUEUE_FILE.exists() else []
        except Exception:
            saved = []
        n = 0
        for it in saved:
            try:
                _resubmit(str(it["song"]), str(it["label"]))
                n += 1
            except Exception as e:
                P.log_error("restore_queue", e)
        # 舊版留下的「精細處理排隊中」標記（佇列早就不見了）：清掉標記，不自動補排，
        # 免得一次冒出幾十首已經處理好的歌重跑；歌曲清單會改回「建議精細處理」按鈕，要做再按
        for m in P.list_songs():
            if m.get("fine_queued") and not jobs.find("process", m["id"], "fine"):
                P.update_meta(P.song_dir(m["id"]), fine_queued=False)
        if n:
            print(f"[job] 上次關掉時還在排的 {n} 個工作已重新排回佇列", flush=True)
    except Exception as e:
        P.log_error("restore_queue", e)
    finally:
        _QUEUE_READY.set()
