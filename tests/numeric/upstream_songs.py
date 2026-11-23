r"""用已安裝的上游雷虎練唱台下載過的歌跑數值基準，不用重新下載（docs/refactor-port-plan.md 3.8）。

找上游的安裝位置（登錄檔 HKCU\...\Uninstall\LeihuLiantai 的 InstallLocation、%LOCALAPPDATA%\LeihuLiantai，
或 --upstream=<資料夾>[;<資料夾>…] 指定），只認程式裡連到 github.com/LeihuTW/Leihu-singsing 的安裝
（fork 的正式版也裝在 LeihuLiantai，要排除）。歌曲庫是 config.json 的 library_dir，沒設就是安裝資料夾裡的 library/。
每首歌用原始檔 original.*（上游轉成 mix 後會刪掉），沒有就用 mix.flac／mix.wav（44.1 kHz 立體聲）。

只讀上游的檔案：這裡只算 sha256，寫一份測試計畫到 tests/private/plans/upstream-library.json（不進 git，
路徑指向上游的音檔），再交給 baseline.py；run.py 會把音檔複製進沙盒再處理，輸出照常在
tests/private/upstream/<版本>/numeric/<標籤>-upstream-library/。上游的歌曲庫不會被寫入或移動。

用法（repo 根目錄，用已安裝版本的內嵌 Python）：
  <python> tests/numeric/upstream_songs.py [git 參照] [--songs=<編號>,…] [--runs=1] [--upstream=<資料夾>]
  [--plan-only] [其他 baseline.py 的選項：--label、--against、--timeout、--device、--floor-version]
--songs：只跑這幾首（上游歌曲庫的資料夾名稱）；沒給就跑全部（一首 3～4 分鐘的歌每次要 3～6 分鐘）。
--plan-only：只寫測試計畫、不跑基準（之後可以給 baseline.py、perf 以外的工具用 --plan=）。
找不到上游安裝或沒有歌時結束代碼是 2，不寫計畫。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import plan as PLAN  # noqa: E402

PLAN_PATH = ROOT / "tests" / "private" / "plans" / "upstream-library.json"
UPSTREAM_URL = "github.com/LeihuTW/Leihu-singsing"
FORK_URL = "github.com/lydek/Leihu-singsing"
AUDIO = ("original.*", "mix.flac", "mix.wav")


def candidates() -> list[Path]:
    """上游可能的安裝位置：登錄檔記的（最後一次安裝）和預設位置。"""
    out = []
    try:
        import winreg

        key = "\\".join(["Software", "Microsoft", "Windows", "CurrentVersion", "Uninstall", "LeihuLiantai"])
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as h:
            out.append(Path(winreg.QueryValueEx(h, "InstallLocation")[0]))
    except (ImportError, OSError):
        pass
    if os.environ.get("LOCALAPPDATA"):
        out.append(Path(os.environ["LOCALAPPDATA"]) / "LeihuLiantai")
    return out


def is_upstream(d: Path) -> bool:
    """程式碼連到上游的 GitHub、沒有連到 fork 的才算上游安裝。"""
    text = ""
    for f in [*d.glob("*.py"), *d.glob("leihu/**/*.py")]:
        try:
            text += f.read_text("utf-8", errors="replace")
        except OSError:
            continue
    return UPSTREAM_URL in text and FORK_URL not in text


def library(d: Path) -> Path:
    try:
        lib = json.loads((d / "config.json").read_text("utf-8")).get("library_dir")
    except (OSError, ValueError, AttributeError):
        lib = None
    return d / lib if lib else d / "library"   # 相對路徑以安裝資料夾為準（d / 絕對路徑 就是那個絕對路徑）


def installs(paths: list[Path]) -> list[Path]:
    """存在、是上游、有歌曲庫的安裝（同一個資料夾只算一次）。"""
    seen, out = set(), []
    for d in paths:
        k = os.path.normcase(os.path.abspath(d))
        if k in seen:
            continue
        seen.add(k)
        if d.is_dir() and is_upstream(d) and library(d).is_dir():
            out.append(d)
    return out


def songs(lib: Path) -> list[tuple[str, Path, str]]:
    """歌曲庫裡每首歌的 (編號, 原始音檔, 歌名)；沒有音檔、編號不合 plan.py 規則的略過。"""
    out = []
    for d in sorted(lib.iterdir()):
        if not d.is_dir() or not PLAN.SONG_ID.fullmatch(d.name) or d.name in PLAN.SYNTH:
            continue
        audio = next((f for pat in AUDIO for f in sorted(d.glob(pat)) if f.is_file()), None)
        if audio is None:
            continue
        try:
            title = json.loads((d / "meta.json").read_text("utf-8")).get("title") or ""
        except (OSError, ValueError, AttributeError):
            title = ""
        out.append((d.name, audio, title))
    return out


def build_plan(libs: list[Path], runs: int, only: list[str] | None = None) -> tuple[dict, list[str]]:
    """測試計畫（plan.py 格式，路徑是上游音檔的絕對路徑）和略過的說明。同一個音檔（sha256 相同）只放一次。"""
    catalog: dict = {}
    hashes: set[str] = set()
    notes = []
    for lib in libs:
        for sid, audio, title in songs(lib):
            if only is not None and sid not in only:
                continue
            h = PLAN.sha256(audio)
            if h in hashes:
                notes.append(f"{sid}（{audio}）和前面的歌是同一個音檔，略過")
            elif sid in catalog:
                notes.append(f"{sid}（{audio}）和前面的歌同一個編號，略過")
            else:
                hashes.add(h)
                # 計畫裡的相對路徑會被 plan.py 當成相對計畫檔所在的資料夾：一律寫絕對路徑
                catalog[sid] = {"label": f"上游：{title}", "path": str(audio.resolve()), "sha256": h}
    if only is not None:
        notes += [f"上游歌曲庫裡沒有 {s}" for s in only if s not in catalog]
    return {"說明": "upstream_songs.py 產生：上游歌曲庫的歌（只讀）", "numeric": {"runs": runs, "songs": list(catalog)},
            "songs": catalog}, notes


def main(argv: list[str] | None = None, plan_path: Path = PLAN_PATH) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mine = ("upstream", "songs", "runs", "plan-only")
    opt = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in argv if a.startswith("--"))
    rest = [a for a in argv if not (a.startswith("--") and a[2:].split("=", 1)[0] in mine)]
    if "plan" in opt:
        print("--plan 由這支程式產生，不能另外指定")
        return 2
    paths = [Path(p).resolve() for p in opt["upstream"].split(";") if p] if opt.get("upstream") else candidates()
    found = installs(paths)
    if not found:
        print("沒有找到已安裝的上游雷虎練唱台（找過：" + "、".join(map(str, paths)) + "）：略過。"
              "裝在別的位置時用 --upstream=<安裝資料夾> 指定。")
        return 2
    only = [s for s in opt["songs"].split(",") if s] if opt.get("songs") else None
    plan, notes = build_plan([library(d) for d in found], int(opt.get("runs", 1)), only)
    for d in found:
        print(f"上游安裝：{d}（版本 {(d / 'VERSION').read_text('utf-8').strip() if (d / 'VERSION').exists() else '?'}）"
              f"；歌曲庫 {library(d)}")
    for n in notes:
        print(f"- {n}")
    if not plan["songs"]:
        print("上游歌曲庫裡沒有可以用的歌：略過。")
        return 2
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), "utf-8")
    print(f"測試計畫：{plan_path}（{len(plan['songs'])} 首，{plan['numeric']['runs']} 次）")
    if "plan-only" in opt:
        return 0
    return subprocess.call([sys.executable, str(HERE / "baseline.py"), *rest, f"--plan={plan_path}"])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
