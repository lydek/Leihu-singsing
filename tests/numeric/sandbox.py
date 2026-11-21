"""數值基準（run.py）和效能基準（tests/perf/run_perf.py）共用的執行環境：照實際安裝的樣子排出上游程式。

上游（2.2.0 起）的分離、音高、語音辨識都在 gpu_worker.py 開的子程式裡跑，子程式以程式碼資料夾為 ROOT 重新 import pipeline。
所以這裡不改 pipeline 的路徑，而是：
- 程式碼複製到暫存資料夾（ROOT），歌曲寫在 ROOT/library，主程式和子程式看到的是同一份；
- ROOT/models 是新的空資料夾，裡面的 uvr、whisper 用 junction 指到 <模型資料夾>（不複製 2 GB；
  顯示卡探測結果 gpu_probe.json 寫在暫存的 ROOT/models，不會沿用上一次的）。結束時比對模型檔，有變動就回報；
- 子程式的啟動命令前面先插入：把 ROOT 放到 sys.path 最前面（內嵌 Python 的 ._pth 會把安裝資料夾放進 sys.path，
  不插的話子程式會 import 到已安裝版本的程式）、擋掉對外連線，並印出載入的 gpu_worker 路徑（報告會檢查）；
- 直播軟體偵測固定回傳沒有、不跑 nvidia-smi、不搜尋線上歌詞、不下載模型。
deterministic=True（數值基準）另外：OMP_NUM_THREADS、MKL_NUM_THREADS＝1，主程式和子程式都關掉 torchcrepe 的隨機抖動。
每一項和平常不同的設定都記在 conditions，由呼叫端寫進輸出。
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NETBLOCK_DIR = HERE.parent / "contract"
PREFIX = "leihu-numeric-"
_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT：junction、符號連結


STAGE = "leihu-nstage-"  # make_tmp 還沒改名前的名稱（只有 owner.pid）
LIVE_KEEP = "保留（行程可能還在跑）"  # clean_stale 的訊息字首：有這種訊息時 Sandbox 不開始量測


def remove_tree(d: Path, allowed: tuple[Path, ...] = ()) -> str:
    """刪掉這個工具建立的資料夾：先把裡面所有 junction／符號連結只拆連結（不跟進去），全部拆掉才 rmtree，
    不可能刪到模型資料夾。只接受系統暫存資料夾底下 PREFIX／STAGE 開頭的資料夾（或 allowed 裡的上層資料夾底下的），
    而且本身不能是連結。回傳空字串＝刪乾淨（或本來就不在）；否則是沒刪掉的原因（資料夾保留）。"""
    d = Path(d)
    try:
        if not os.path.lexists(d):
            return ""
        parent = d.parent.resolve()
        in_tmp = parent == Path(tempfile.gettempdir()).resolve() and d.name.startswith((PREFIX, STAGE))
        if not in_tmp and parent not in [Path(a).resolve() for a in allowed]:
            return f"{d} 不是這個工具建立的資料夾，不刪"
        if getattr(os.lstat(d), "st_file_attributes", 0) & _REPARSE:
            return f"{d} 本身是 junction／連結，不跟進去，不刪"
    except FileNotFoundError:
        return ""  # 剛被別的清理刪掉
    except OSError as err:
        return f"讀不到 {d} 的屬性（{err}），保留不刪"
    stack = [d]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except FileNotFoundError:
            continue
        except OSError as err:
            return f"讀不到 {cur}（{err}），整個資料夾保留不刪"
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
                if getattr(st, "st_file_attributes", 0) & _REPARSE:
                    try:
                        os.rmdir(e.path)  # 目錄 junction／目錄連結：只拆連結
                    except NotADirectoryError:
                        os.unlink(e.path)  # 檔案連結
                elif e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
            except FileNotFoundError:
                continue
            except OSError as err:
                return f"{e.path} 處理不了（{err}），整個資料夾保留不刪"
    for _ in range(20):  # 子程式被結束後要一點時間才放掉檔案
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            return ""
        time.sleep(0.5)
    return "刪不掉（可能還有行程在用）"


def process_start(pid: int) -> int | None | str:
    """行程的建立時間（FILETIME 整數，用來分辨 PID 被重用）；行程不存在回傳 None；沒有權限查回傳 "unknown"。"""
    import ctypes
    from ctypes import wintypes

    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None if k.GetLastError() == 87 else "unknown"  # 87＝參數錯誤（沒有這個 PID）；5＝拒絕存取
    try:
        code = wintypes.DWORD()
        if k.GetExitCodeProcess(wintypes.HANDLE(h), ctypes.byref(code)) and code.value != 259:  # 259＝STILL_ACTIVE
            return None
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(wintypes.HANDLE(h), *(ctypes.byref(x) for x in t)):
            return "unknown"
        return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
    finally:
        k.CloseHandle(wintypes.HANDLE(h))


def owner_text() -> str:
    """寫進 owner.pid 的內容：PID 和建立時間。查不到自己的建立時間就直接失敗（寫了也沒辦法分辨）。"""
    start = process_start(os.getpid())
    if not isinstance(start, int):
        raise RuntimeError(f"查不到自己的行程建立時間（{start}），無法寫 owner.pid")
    return f"{os.getpid()} {start}"


def owner_state(d: Path) -> str:
    """'alive'＝建立它的行程還在；'dead'＝確定不在了；其他字串＝無法判斷的原因（保留）。"""
    try:
        pid, start = (int(x) for x in (Path(d) / "owner.pid").read_text("utf-8").split())
    except (OSError, ValueError):
        return "沒有可讀的 owner.pid，無法確定是不是別的工作正在用（確認沒有在跑再手動刪）"
    now = process_start(pid)
    if now == "unknown":
        return f"查不到 PID {pid} 的狀態（權限不足），無法確定是不是還在用"
    return "alive" if now is not None and now == start else "dead"


def clean_stale(parent: Path | None = None, pattern: str | None = None, allowed: tuple[Path, ...] = ()) -> list[str]:
    """被強制結束（工作管理員、逾時）留下的資料夾：確定建立它的行程已經不在（PID 不存在，或 PID 被別的行程重用、
    建立時間不同）才刪。沒有 owner、讀不到、查不到行程狀態的都保留，只寫進報告。預設清系統暫存資料夾裡的
    PREFIX、STAGE；parent／pattern 用來清別的地方（例如 export() 的 exports/）。"""
    out = []
    targets = [(Path(parent), pattern)] if parent else [(Path(tempfile.gettempdir()), PREFIX + "*"),
                                                         (Path(tempfile.gettempdir()), STAGE + "*")]
    for base, pat in targets:
        for d in sorted(base.glob(pat)):
            try:
                state = owner_state(d)
                if state == "alive":
                    continue  # 建立它的行程還在跑（例如另一個基準），不要動
                keep = d / "keep.json"
                if state == "dead" and keep.exists():  # 上次逾時沒確認停止：裡面記的行程都停了才刪
                    import json

                    tree = [tuple(x) for x in json.loads(keep.read_text("utf-8")).get("tree", [])]
                    left = still_alive(tree) if tree else []
                    if left or not tree:
                        out.append(f"{LIVE_KEEP}{d.name}：上次逾時沒確認停止，" + (f"PID {left} 還在跑或查不到" if left
                                   else "沒有記到行程清單，無法確認") + "（確認沒有在跑再手動刪）")
                        continue
                if state != "dead":
                    out.append(f"保留 {d.name}：{state}")
                    continue
                why = remove_tree(d, allowed)
                out.append(f"清掉上一次留下的 {d.name}" + (f"：沒刪成功，{why}" if why else ""))
            except Exception as e:  # noqa: BLE001  清理出錯不能讓整個基準中止
                out.append(f"清理 {d.name} 時出錯，保留不刪：{type(e).__name__}: {e}")
    return out


def make_tmp() -> Path:
    """先在 STAGE 名稱底下建好資料夾、寫好 owner.pid，再改名成 PREFIX 開頭：clean_stale 不會看到沒有 owner 的空窗。
    改名失敗（例如防毒軟體暫時鎖住）重試幾次；最後還是失敗就刪掉 stage 並丟出例外。"""
    stage = Path(tempfile.mkdtemp(prefix=STAGE))
    try:
        (stage / "owner.pid").write_text(owner_text(), "utf-8")
        final = stage.with_name(PREFIX + stage.name[len(STAGE):])
        for n in range(10):
            try:
                stage.rename(final)
                return final
            except PermissionError:
                if n == 9:
                    raise
                time.sleep(0.5)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)  # 只有 owner.pid，沒有連結
        raise
    raise AssertionError("不會到這裡")


def tree_with_start(root: int) -> list[tuple[int, int | str]] | None:
    """root 和它所有子孫行程的 (PID, 建立時間)；之後用來確認是不是真的都結束了（PID 重用也分得出來）。
    查不到建立時間的行程記成 "unknown"（不能當成已經停止）；行程快照建不起來回傳 None。"""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return None
    e = Entry()
    e.dwSize = ctypes.sizeof(e)
    kids: dict[int, list[int]] = {}
    ok = k.Process32FirstW(wintypes.HANDLE(snap), ctypes.byref(e))
    while ok:
        kids.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
        ok = k.Process32NextW(wintypes.HANDLE(snap), ctypes.byref(e))
    k.CloseHandle(wintypes.HANDLE(snap))
    out, todo, seen = [], [root], {root}
    while todo:
        pid = todo.pop()
        start = process_start(pid)
        if start is not None:  # 已經不在的不用記；查不到的記 "unknown"
            out.append((pid, start))
        for c in kids.get(pid, []):
            if c not in seen:
                seen.add(c)
                todo.append(c)
    return out


def still_alive(tree: list[tuple[int, int | str]]) -> list[int]:
    """tree_with_start 記下的行程裡，還在跑或無法確認的 PID（建立時間相同才算同一個行程；
    當初或現在查不到狀態的都算無法確認）。"""
    out = []
    for pid, start in tree:
        now = process_start(pid)
        if now == "unknown" or start == "unknown" and now is not None or now == start:
            out.append(pid)
    return out


def mark_keep(log_text: str, tree, why: str) -> str:
    """逾時後沒確認全部停止：在那次的沙盒裡寫 keep.json（還在跑的行程和原因），之後任何 clean_stale 都不會只因為
    建立者死了就刪掉它；要等 keep.json 裡的行程都停了才刪。回傳寫在哪裡（找不到沙盒路徑就說明）。"""
    import json
    import re

    m = re.search(r"\[(?:numeric|perf)\] 沙盒 (.+)", log_text)
    if not m:
        return "log 裡沒有沙盒路徑，沒辦法標記保留（如果還有行程在跑，下一次清理可能刪掉它的沙盒）"
    d = Path(m.group(1).strip()).parent
    try:
        (d / "keep.json").write_text(json.dumps({"why": why, "tree": tree or []}, ensure_ascii=False), "utf-8")
    except OSError as err:
        return f"寫不進 {d / 'keep.json'}（{err}）"
    return f"已在 {d.name} 寫 keep.json，行程都停了才會被清掉"


class Sandbox:
    def __init__(self, code: Path, models: Path, tag: str, deterministic: bool, device: str = "auto"):
        self.models, self.tag = Path(models).resolve(), tag  # 之後會 chdir 到沙盒：相對路徑要先轉成絕對路徑
        self.conditions: list[str] = []
        self.stale = clean_stale()  # 先清上一次被強制結束留下的（problems() 會列出來）
        live = [m for m in self.stale if m.startswith(LIVE_KEEP)]
        if live:  # 殘留的行程會吃 CPU／顯示卡、可能還在寫它的沙盒：這時量出來的數字不能信
            raise SystemExit("上一次逾時留下的行程可能還在跑，這次不開始量測（先結束它們，或確認後手動刪掉暫存資料夾）：\n"
                             + "\n".join(live))
        self.tmp = make_tmp()
        self.root = self.tmp / "app"
        self.junctions: list[Path] = []
        self.spawns = 0
        atexit.register(self.cleanup)  # 建好暫存資料夾就登記：複製到一半失敗也會清
        shutil.copytree(
            Path(code).resolve(), self.root,
            # config.json 是使用者設定：library_dir、models_dir 會把資料導到沙盒外，不複製（用預設值）
            ignore=shutil.ignore_patterns("library", "models", "logs", "tests", "__pycache__", ".git", "config.json"),
        )
        (self.root / "models").mkdir()
        import _winapi  # Windows 的目錄 junction（一般使用者就能建，不需要開發人員模式）

        for sub in ("uvr", "whisper"):
            if (self.models / sub).is_dir():
                _winapi.CreateJunction(str(self.models / sub), str(self.root / "models" / sub))
                self.junctions.append(self.root / "models" / sub)
        self.models_before = self.model_files()
        self.models_sha = self.model_hashes()  # 模型的指紋：寫進 run.json，兩台電腦、前後兩次可以核對
        # netblock 把擋下的連線寫到 tmp/net_blocked.log。不設 LIANTAI_*：上游不認，但有 S28 的版本會把歌曲庫、設定搬過去，
        # 和上游的 ROOT/library 不同。子程式會繼承這些設定
        for k in [k for k in os.environ if k.startswith("LIANTAI_")]:  # 殼層留下的 S28 開關也不能影響有 S28 的版本
            os.environ.pop(k)
        os.environ.update(LEIHU_NET_LOG=str(self.tmp / "net_blocked.log"), HF_HUB_OFFLINE="1")
        print(f"{tag} 沙盒 {self.root}", flush=True)  # 報告檢查子程式載入的程式都在這個資料夾裡
        self.cond("顯示卡子程式裡裝了 child_hook（記錄實際裝置）：ONNX session 的 run() 多包一層、每次呼叫後讀一次"
                  " get_providers()，在計時範圍內；參數與回傳值不變")
        if device == "cpu":  # 一定要在 import torch 之前；子程式繼承
            os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
            self.cond("強制只用 CPU（CUDA_VISIBLE_DEVICES=-1，主程式與子程式）")
        elif device != "auto":
            raise SystemExit(f"--device 只能是 auto 或 cpu：{device}")
        if deterministic:
            os.environ.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
            self.cond("OMP_NUM_THREADS、MKL_NUM_THREADS＝1（主程式與子程式）；處理時間不是效能基準")
        sys.path[:0] = [str(self.root), str(NETBLOCK_DIR)]
        os.chdir(self.root)
        import netblock  # noqa: F401  主程式不連網

        nodither = ""
        if deterministic:
            import torchcrepe.convert

            torchcrepe.convert.dither = lambda cents: cents
            nodither = "import torchcrepe.convert as _c; _c.dither = lambda cents: cents; "
            self.cond("torchcrepe 隨機抖動關掉（主程式與子程式）")
            # Whisper 解碼品質不夠時會改用溫度取樣重試，用到 CTranslate2 的亂數：固定種子，兩次結果才會一樣
            import ctranslate2

            ctranslate2.set_random_seed(0)
            nodither += "import ctranslate2 as _ct; _ct.set_random_seed(0); "
            self.cond("CTranslate2 亂數種子固定為 0（主程式與子程式；Whisper 溫度取樣重試時才會用到）")
        t0 = time.time()
        import pipeline as P
        import share as SH

        self.import_seconds = round(time.time() - t0, 2)  # 效能基準記錄的啟動時間之一

        self.P, self.SH = P, SH
        self.check_sources("pipeline", "share")
        for what, d in (("歌曲庫", P.LIB), ("模型", P.MODELS)):  # 寫入位置一定要在沙盒裡
            assert Path(d).resolve().is_relative_to(self.root), f"{what}資料夾不在沙盒裡：{d}"
        prefix = (
            f"import sys; sys.path[:0] = [{str(self.root)!r}, {str(NETBLOCK_DIR)!r}, {str(HERE)!r}]; import netblock; "
            + nodither
            + f"import gpu_worker as _g; print('{tag} 子程式載入', _g.__file__, flush=True); "
            + "import child_hook; "  # 每一步實際用的裝置印成 [device]（child_hook.py）
        )
        box = self

        class _Popen(subprocess.Popen):
            """只改 gpu_worker 開子程式的那個命令；ffmpeg 等其他命令照原樣。要用類別：asyncio 等會繼承 subprocess.Popen。"""

            def __init__(self, args, *a, **k):
                if isinstance(args, list) and len(args) == 3 and args[1] == "-c" and "gpu_worker.serve(" in args[2]:
                    args = [args[0], "-c", prefix + args[2]]
                    box.spawns += 1
                super().__init__(args, *a, **k)

        subprocess.Popen = _Popen
        self.stub("search_lyrics_later", lambda *a, **k: None, "不搜尋線上歌詞（要連網，和推論無關）")
        self.stub("_running_apps", lambda names: [], "直播軟體偵測固定回傳沒有")
        self.stub("_sample_gpu_load", lambda: None, "不跑 nvidia-smi（顯示卡被其他程式佔用時結果會不同）")
        for n in ("start_hq_download", "start_hqv_download", "start_kana_install", "_upgrade_ytdlp"):
            self.stub(n, lambda *a, **k: None, "不下載模型、不裝套件")
        if hasattr(P, "init_runtime"):  # 重構版才有；上游 import 時就做完
            P.init_runtime()

    def cond(self, msg: str) -> None:
        self.conditions.append(msg)
        print(f"{self.tag} 條件：{msg}", flush=True)

    def stub(self, name: str, value, why: str) -> None:
        if not hasattr(self.P, name):
            self.cond(f"找不到 pipeline.{name}，沒有替換（{why}）")
            return
        setattr(self.P, name, value)
        self.cond(f"pipeline.{name}：{why}")

    def check_sources(self, *names: str) -> None:
        """被測模組一定要從沙盒 ROOT 載入（內嵌 Python 的 sys.path 裡有安裝資料夾，缺檔時會默默載入安裝版）。"""
        for n in names:
            f = Path(sys.modules[n].__file__).resolve()
            assert f.parent == self.root, f"{n} 不是從沙盒載入的：{f}"

    def model_files(self) -> dict[str, list]:
        """執行前後比對用：大小和 mtime（奈秒）；任何寫入都會改 mtime_ns。"""
        return {
            p.relative_to(self.models).as_posix(): [p.stat().st_size, p.stat().st_mtime_ns]
            for p in sorted(self.models.rglob("*"))
            if p.is_file()
        }

    def model_hashes(self) -> dict[str, str]:
        import hashlib

        out = {}
        for p in sorted(self.models.rglob("*")):
            if p.is_file():
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 22), b""):
                        h.update(chunk)
                out[p.relative_to(self.models).as_posix()] = h.hexdigest()
        return out

    def problems(self, out: Path) -> list[str]:
        """執行完要回報的問題：模型檔被改、想對外連線（log 複製到 out）、被測程式的 logs/、沒開過顯示卡子程式。"""
        now = self.model_files()
        if now == self.models_before and self.model_hashes() != self.models_sha:  # 大小和 mtime 沒變但內容變了
            now = {**now, "（內容）": "sha256 和執行前不同"}
        bad = [f"模型資料夾的檔案在執行中有變動：{k}" for k in sorted(self.models_before.keys() | now.keys())
               if self.models_before.get(k) != now.get(k)]
        blocked = self.tmp / "net_blocked.log"
        if blocked.exists():
            shutil.copy2(blocked, out / "net_blocked.log")
            bad.append(f"有程式想對外連線（已擋下），見 {out / 'net_blocked.log'}")
        logs = self.root / "logs"
        if logs.is_dir() and any(logs.iterdir()):
            # 被測程式自己的錯誤紀錄（log_error 寫 logs/lyrics.log：例如混合精度分離失敗後改用一般精度重做）
            shutil.copytree(logs, out / "app-logs", dirs_exist_ok=True)
            for f in sorted(logs.glob("*.log")):
                lines = [x for x in f.read_text("utf-8", errors="replace").splitlines() if x.strip()]
                bad += [f"被測程式記錄的錯誤（logs/{f.name}）：{x[:200]}" for x in lines[:5]]
                if len(lines) > 5:
                    bad.append(f"logs/{f.name} 還有 {len(lines) - 5} 行，見 {out / 'app-logs'}")
        bad += self.stale  # 上一次被強制結束留下的暫存資料夾（這次開始前清掉的）
        if self.spawns == 0:
            bad.append("沒有開過顯示卡子程式：分離、音高、語音辨識可能都在主程式裡做")
        return bad

    def environment(self) -> dict:
        import json
        from importlib.metadata import version

        env = {"python": sys.version.split()[0], "executable": sys.executable}
        for pkg in ("torch", "onnxruntime", "onnxruntime-gpu", "audio-separator", "faster-whisper", "ctranslate2",
                    "torchcrepe"):
            try:
                env[pkg] = version(pkg)
            except Exception:  # noqa: S112  沒裝的套件就不記
                continue
        try:
            import onnxruntime

            env["onnx_providers"] = onnxruntime.get_available_providers()
        except Exception as e:  # noqa: BLE001
            env["onnx_providers"] = f"讀不到：{e}"
        probe = self.root / "models" / "gpu_probe.json"
        env["gpu_probe"] = json.loads(probe.read_text("utf-8")) if probe.exists() else None
        return env

    def cleanup(self) -> None:
        """先拿掉 junction（只刪連結），再刪暫存資料夾，不會動到模型資料夾。"""
        G = sys.modules.get("gpu_worker")
        for pool in (getattr(G, "POOL", None), getattr(G, "CPU_POOL", None)):
            try:
                pool and pool.shutdown()
            except Exception as e:  # noqa: BLE001
                print(f"{self.tag} 關閉子程式失敗：{e}", flush=True)
        self.junctions = []
        os.chdir(HERE)  # 這個行程的 cwd 在 ROOT：不先離開的話 ROOT 刪不掉
        why = remove_tree(self.tmp)
        if why:  # 報告的異常規則會抓「暫存資料夾刪不掉」
            print(f"{self.tag} 暫存資料夾刪不掉：{self.tmp}：{why}", flush=True)
