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


class Sandbox:
    def __init__(self, code: Path, models: Path, tag: str, deterministic: bool):
        self.models, self.tag = models, tag
        self.tmp = Path(tempfile.mkdtemp(prefix="leihu-numeric-"))
        self.root = self.tmp / "app"
        self.conditions: list[str] = []
        self.junctions: list[Path] = []
        self.spawns = 0
        shutil.copytree(
            code, self.root, ignore=shutil.ignore_patterns("library", "models", "logs", "tests", "__pycache__", ".git")
        )
        (self.root / "models").mkdir()
        atexit.register(self.cleanup)  # 中途失敗也要清
        import _winapi  # Windows 的目錄 junction（一般使用者就能建，不需要開發人員模式）

        for sub in ("uvr", "whisper"):
            if (models / sub).is_dir():
                _winapi.CreateJunction(str(models / sub), str(self.root / "models" / sub))
                self.junctions.append(self.root / "models" / sub)
        self.models_before = self.model_files()
        # LIANTAI_DATA 只給 netblock 寫擋下的連線（tmp/net_blocked.log）；上游不認這個變數。子程式會繼承這些設定
        os.environ.update(LIANTAI_DATA=str(self.tmp), HF_HUB_OFFLINE="1")
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
        t0 = time.time()
        import pipeline as P
        import share as SH

        self.import_seconds = round(time.time() - t0, 2)  # 效能基準記錄的啟動時間之一

        assert Path(P.__file__).resolve().parent == self.root, f"import 到別的 pipeline：{P.__file__}"
        self.P, self.SH = P, SH
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

    def model_files(self) -> dict[str, list]:
        return {
            str(p.relative_to(self.models)): [p.stat().st_size, int(p.stat().st_mtime)]
            for p in sorted(self.models.rglob("*"))
            if p.is_file()
        }

    def problems(self, out: Path) -> list[str]:
        """執行完要回報的問題：模型檔被改、想對外連線（log 複製到 out）、被測程式的 logs/、沒開過顯示卡子程式。"""
        now = self.model_files()
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
        for j in self.junctions:
            if j.exists():
                os.rmdir(j)
        self.junctions = []
        os.chdir(HERE)  # 這個行程的 cwd 在 ROOT：不先離開的話 ROOT 刪不掉
        for _ in range(20):  # 子程式被結束後要一點時間才放掉檔案
            shutil.rmtree(self.tmp, ignore_errors=True)
            if not self.tmp.exists():
                break
            time.sleep(0.5)
        else:
            print(f"{self.tag} 暫存資料夾刪不掉：{self.tmp}", flush=True)
