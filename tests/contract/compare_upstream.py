r"""上游（基準）與目前這一邊的契約比對（docs/refactor-port-plan.md 3.7）。

用同一組契約案例（test_contract.py 的 CASES）在沙盒裡跑基準版本（serve_upstream.py），
逐案例和目前這一邊的快照（snapshots/contract.json）比 JSON 結構，依 RULES 把差異分成「預期」和「未預期」，
報告寫到 tests/private/upstream/<名稱>/contract-report.md。有未預期差異或異常時結束代碼是 1。

用法（repo 根目錄）：python tests/contract/compare_upstream.py [git 參照，預設 v2.3.2] [--twice]
--record：錄 contract.json。用 serve_upstream.py 跑指定的已 commit 參照，得到候選結果；
  快照已經存在時，再現場跑 --base=<參照>（預設 v2.3.2）比對，所有差異都符合 RULES 才覆寫，否則 exit 1、保留原快照。
  快照還不存在時（第一次建立基準）直接寫入。只能用在步驟 1 之前（serve_upstream.py 要改得到 app 的背景函式）。
--rebase：和 --record 一起用，重建基準本身（參照就是 --base，例如測試工具改了快照格式）：和舊快照的差異列在報告裡，
  不擋寫入。沒有 --rebase 時，重錄基準本身遇到未登記的差異一樣不寫入。
--twice：同一個版本連跑兩次，結果要完全相同（穩定性檢查）；不同就列為異常，--record 也不寫入。
報告一定有「異常與沙盒條件」一節：伺服器 log 的 Traceback、顯示卡退回／改用 CPU 的訊息、沙盒替換了什麼、
哪些替換目標在這個版本找不到、對外連線、沒有案例的路由、兩次結果不同的案例。沒有就寫「無」。
--leak：漏移檢查（upstream-sync-plan.md 第 7 節步驟 5）。前端與安裝檔和上游比只能剩 OWN_CHANGES；
  上游在 --since..<參照> 之間新增到 app.py、pipeline.py 的名稱，重構版都要找得到（刻意不移植的列在 NOT_PORTED）。
  --since=<參照> 預設是上次的同步點（git merge-base HEAD <參照>）；已經 merge -s ours 過的版本要自己給（例如 v2.1.0 是 v2.0.0）。
--code=<資料夾>：不從 git 匯出，直接比對這份程式碼（名稱用資料夾名稱；例如檢查比對工具抓不抓得到故意改過的地方）。
數值比對是另一支工具：tests/numeric/baseline.py（舊的 --numeric 依賴 2.0.0 重構版的 ml-worker，已拿掉）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT), str(ROOT / "tests"), str(ROOT / "tests" / "numeric")]

from test_contract import CASES, SNAPSHOT, run_contract  # noqa: E402

OUT = ROOT / "tests" / "private" / "upstream"

BASE = "v2.3.2"  # 第一階段的基準（refactor-port-plan.md 3.8）

D4_CASES = ("status_end", "status_live", "status_override", "status_paused")
D4_NOTE = "~ /body/jobs: 已核對：上游多 2 個刪掉的歌的 process 工作（all、youtube），其他工作相同，queued_total 少 2"


def _align_d4(case: str, snap_c, res_c):
    """D4（下一版第 2 批）：上游的 /api/status 還列著被刪掉的兩首歌的排隊工作。只有多出來的剛好是那 2 個
    process 工作（all、youtube）、其他工作逐一相同、queued_total 剛好多 2 時，才把上游那兩個欄位換成這一邊的，
    並回傳 D4_NOTE（記成預期差異）；不符合就原樣回傳，差異照常列為未預期（Codex r4-7）。"""
    if case not in D4_CASES:
        return res_c, None
    try:
        sj, rj = snap_c["body"]["jobs"], res_c["body"]["jobs"]
        st, rt = snap_c["body"]["queued_total"], res_c["body"]["queued_total"]
    except (KeyError, TypeError):
        return res_c, None

    def key(j):   # id、order、created 是遮蔽過的流水號或時間，兩邊的編號會因為少了工作而錯開
        return json.dumps({k: v for k, v in j.items() if k not in ("id", "order", "created")}, sort_keys=True,
                          ensure_ascii=False)

    rest = list(rj)
    for j in sj:
        k = key(j)
        hit = next((n for n, r in enumerate(rest) if key(r) == k), None)
        if hit is None:
            return res_c, None
        rest.pop(hit)
    ok = (len(rest) == 2 and all(r.get("kind") == "process" and r.get("status") == "queued" for r in rest)
          and sorted(r.get("label") for r in rest) == ["all", "youtube"]
          and [key(r) for r in rj if r not in rest] == [key(j) for j in sj] and rt - st == 2)
    if not ok:
        return res_c, None
    body = dict(res_c["body"], jobs=sj, queued_total=st)
    return dict(res_c, body=body), D4_NOTE


# 預期差異：(名稱, 說明, 判斷函式(案例, 差異字串) -> bool)。差異字串的格式見 diff()：
# "+ /路徑=值"（基準沒有、目前有）、"- /路徑=值"（基準有、目前沒有）、"~ /路徑: 基準 -> 目前"
# 從空的開始：舊分支的 S15、D1、R2、D2/D4 是 2.0.0 重構版的差異，D1 還用案例名稱字首整個放行，留著會蓋掉真的差異。
# 每加一條都要寫明問題編號和原因；判斷式只放行那個差異本身，不要用案例名稱整個放行。
RULES: list[tuple[str, str, object]] = [
    (
        "F1",
        "fork：GitHub 專案與更新連結改指向 lydek/Leihu-singsing，開啟網址的白名單不再有上游專案網址"
        "（docs/refactor-port-plan.md 3.15）",
        lambda c, d: c == "open_url",
    ),
    (
        "D4",
        "下一版第 2 批：刪掉的歌的排隊工作改成已取消，不再列在 /api/status（docs/next-version-plan.md）。"
        "song_delete_uploaded、song_delete_youtube 刪掉的兩首歌的 all、youtube 工作從清單消失，queued_total 少 2。"
        "Codex r4-7：原本只看欄位名稱，別首的工作整批消失也會放行；改成 compare() 先核對（_align_d4：上游多出來的"
        "剛好是 2 個 process 的 all／youtube、其他工作逐一相同、queued_total 剛好少 2），核對通過才記成這一條",
        lambda c, d: c in D4_CASES and d == D4_NOTE,
    ),
    (
        "S15",
        "下一版第 3 批：404、405、400（壞掉的 JSON）等 HTTP 錯誤改回 {\"error\": \"請求失敗（代碼）\"}，不回 HTML；"
        "狀態碼不變（docs/next-version-plan.md）",
        lambda c, d: re.fullmatch(r'~ /content_type: "(application/json|text/html; charset=utf-8)" -> '
                                  r'"(application/json|text/html; charset=utf-8)"', d) is not None
        or re.fullmatch(r'[+-] /body=\{"error": "請求失敗（[0-9]{3}）"\}', d) is not None,
    ),
    (
        "D1",
        "下一版第 3 批：PATCH 歌曲帶不合法的 KEY（文字、1.5、true）回 400，不再悄悄存成 0（docs/next-version-plan.md）",
        lambda c, d: c in ("song_patch_key_text", "song_patch_key_float", "song_patch_key_bool")
        and re.match(r"(~ /status: (400 -> 200|200 -> 400)|[+-] /body/|[+-] /files/DATA/~library/songnew001/meta\.json=)", d)
        is not None,
    ),
    (
        "D1-後續",
        "D1 的連帶差異：上面三個案例不再把 songnew001 的 KEY 改成 0，之後案例讀到的 KEY 維持 2",
        lambda c, d: re.fullmatch(
            r"~ (/body(\[\d+\])?|/files/DATA/~library/songnew001/meta\.json)/key: (2 -> 0|0 -> 2)", d
        ) is not None,
    ),
]

# 刻意不移植的上游名稱 → 原因（refactor 從 v2.3.2 開始，目前沒有）
NOT_PORTED: dict[str, str] = {}
# 目前這一邊對前端、安裝檔的改動：(檔案, 改動行要符合的 regex)；和上游比，其他改動行都算漏移
FRONT = ["static", "hotkeys.py", "VERSION", "installer/leihu.nsi"]
OWN_CHANGES: list[tuple[str, str]] = [
    # fork 版號 2.3.5.1（3.15）
    ("VERSION", r"-2\.3\.5"),
    ("VERSION", r"\+2\.3\.5\.1"),
    ("static/help.html", r"[-+].*2\.3\.5.*"),  # 標題、版號、安裝檔名
    ("static/index.html", r"[-+].*id=\"ver\">2\.3\.5(\.\d+)?<.*"),
    # 更新提示改連 fork 的 release（3.15）
    ("static/index.html", r"[-+].*href=\"https://github\.com/(LeihuTW|lydek)/Leihu-singsing/releases/latest\".*"),
    # 安裝檔：版號與測試版開關（3.14、3.15）
    ("installer/leihu.nsi", r"[-+]\s*!define (APPNAME|VERSION|REGKEY|INSTNAME|OUTNAME) .*"),
    ("installer/leihu.nsi", r"\+!(ifdef TEST_BUILD|else|endif)"),
    ("installer/leihu.nsi", r"\+\s*;.*"),
    ("installer/leihu.nsi", r"[-+](OutFile|InstallDir|VIProductVersion) .*"),
    ("installer/leihu.nsi", r"\+\s*File(Open|Write|Close) \$0.*"),
    ("installer/leihu.nsi", r"\+\s*Delete \"\$INSTDIR\\\.test-install\""),
    # E 組步驟 1：程式拆到 leihu/，修復與解除安裝時清掉舊的（3.19）
    ("installer/leihu.nsi", r"\+\s*RMDir /r \"\$INSTDIR\\leihu\""),
    # 下一版第 4 批 38：停止程式時路徑用環境變數傳給 PowerShell、用 StartsWith 比對（next-version-plan.md）
    ("installer/leihu.nsi", r"-\s*nsExec::Exec .*ExecutablePath -like '\$\{DIR\}\\python\\\*'.*"),
    ("installer/leihu.nsi", r"\+\s*System::Call 'kernel32::SetEnvironmentVariable\(t \"LEIHU_STOP_DIR\".*"),
    ("installer/leihu.nsi", r"\+\s*nsExec::Exec .*StartsWith\(\$\$env:LEIHU_STOP_DIR.*"),
    # 下一版第 2 批 D2：刪除處理中、下載中的歌回 400，顯示錯誤（next-version-plan.md）
    ("static/app.js", r"[-+]\s*(try \{ )?await api\(`/api/songs/\$\{s\.id\}`, \{ method: \"DELETE\" \}\);.*"),
    # Codex r5-7：羅馬拼音 nn 後面接母音時第二個 n 屬於下一個音節（2.3.5 的 roma2hira，next-version-plan.md）
    ("static/app.js", r"[-+]\s*if \(c === \"n\" && .*\{ out \+= \"ん\"; i \+= .*"),
    ("static/app.js", r"\+\s*// fork（Codex r5-7）：nn .*"),
]

# 伺服器 log 裡代表「這次執行條件和平常不同」的訊息：一律列進報告，不然結果會被誤解
LOG_ALERTS = [
    ("Traceback", "Python 例外"),
    ("改在主程式裡執行", "顯示卡子程式開不起來，改在主程式裡做"),
    ("改用 CPU", "改用 CPU"),
    ("退回", "退回較低品質的模型或做法"),
    ("改用標準模型", "高品質模型失敗，改用標準模型"),
    ("卡住", "偵測到卡住"),
    ("逾時", "逾時"),
    ("out of memory", "記憶體不足"),
    ("暫存資料夾刪不掉", "測試工具的暫存資料夾沒有刪乾淨"),
    ("上一次逾時留下的行程可能還在跑", "上一次逾時的行程可能還在，這次沒有開始量測"),
    ("could not activate requested provider", "ONNX 沒有啟用要求的執行裝置（例如顯示卡），改用別的"),
    ("Falling back to", "onnxruntime 執行失敗，改用別的執行裝置重試"),
    ("執行中改用", "ONNX session 執行中改用別的執行裝置"),
]
# 案例本身故意造成的 log 訊息：(訊息片段, 原因)。報告照樣列出，但不算異常
LOG_EXPECTED = [
    ("[live] 直播模式開著", "案例 status_live（GET /api/status?live=1）故意開直播模式，之後新的分離、音高分析改用 CPU"),
]
_NAME_PATTERNS = (
    r"^def (\w+)",
    r"^class (\w+)",
    r"^([A-Z_][A-Z0-9_]+) =",
    r'^\s+"(\w+)": ',
    r'@app\.(?:get|post|patch|delete)\("([^"]+)"',
    r"^    def (\w+)",
)


# 測試工具有沒有未 commit 的修改：不算錄出來的結果（快照、摘要），不然先錄的那份會讓後面每份都標成有修改
TOOL_PATHS = ("--", "tests", ":!tests/contract/snapshots", ":!tests/numeric/baselines", ":!tests/perf/baselines",
              ":!tests/private")


def scrub(x):
    """要進 git 的摘要：把 repo、暫存資料夾、使用者資料夾的絕對路徑換成 <repo>、<tmp>、<home>（含使用者名稱）。
    報告（tests/private/）保留完整路徑。"""
    if isinstance(x, dict):
        return {scrub(k): scrub(v) for k, v in x.items()}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    if not isinstance(x, str):
        return x
    places = [(str(ROOT), "<repo>"), (tempfile.gettempdir(), "<tmp>"), (os.path.expanduser("~"), "<home>")]
    for p, tag in sorted(places, key=lambda t: -len(t[0])):  # 長的先換（repo 可能在 home 底下）
        parts = [re.escape(s) for s in re.split(r"[\\/]+", p) if s]
        x = re.sub(r"[\\/]+".join(parts), tag, x, flags=re.IGNORECASE)  # 分隔符號：任意數量的 / 或 \（混用、多層跳脫）
    return x


def user_leaks(text: str) -> list[str]:
    """scrub 之後還看得到的使用者資料夾路徑（任何帳號：C:\\Users\\<名稱>、/Users/<名稱>、/home/<名稱>，
    分隔符號混用、多層跳脫都算）。不用使用者名稱當單字比對：名稱是 cpu 之類的常見字時會誤擋。"""
    rx = r"(?i)(?:[a-z]:)?[\\/]+(?:users|home)[\\/]+(?!public\b)[^\\/\s\"'<>:]+"
    return [m.group(0) for m in re.finditer(rx, text)]


assert user_leaks("x C:\\\\Users\\\\alice\\\\y") and user_leaks("/home/bob/x") and not user_leaks("desktop-9900x3d-cpu <home>\\a")


_DROP = object()


def public_view(x, songs: tuple[str, ...]):
    """要進 git 的那份摘要：拿掉真實歌曲的一切資料（以歌曲編號當 key 的項目、編號開頭的比對列與 floor 項目、
    清單裡的編號），只留合成素材的結果。完整的那份寫在 tests/private/baselines/（不進 git）。"""
    heads = tuple(f"{s}{sep}" for s in songs for sep in ("/", " "))

    def hit(v) -> bool:
        return isinstance(v, str) and (v in songs or v.startswith(heads))

    def walk(v):
        if isinstance(v, dict):
            if hit(v.get("item")):
                return _DROP
            return {k: w for k, val in v.items() if not hit(k) and (w := walk(val)) is not _DROP}
        if isinstance(v, list):
            return [w for val in v if not hit(val) and (w := walk(val)) is not _DROP]
        return v

    return walk(x) if songs else x


assert public_view({"songs": {"synth": 1, "real01": 2}, "rows": [{"item": "real01/lead.flac"}, {"item": "sing x"}],
                    "floor": {"real01 音符數差": 1, "songsing01 音符": 2}, "ids": ["real01", "x"]}, ("real01",)) == \
    {"songs": {"synth": 1}, "rows": [{"item": "sing x"}], "floor": {"songsing01 音符": 2}, "ids": ["x"]}


def private_copy(path: Path) -> Path:
    """摘要在本機的完整版：tests/private/baselines/<numeric|perf>/<同一個檔名>。"""
    return ROOT / "tests" / "private" / "baselines" / path.parent.parent.name / path.name


def write_summary(path: Path, summary: dict, songs: tuple[str, ...] = ()) -> str:
    """寫要進 git 的摘要：先 scrub，再確認沒有使用者名稱、沒有 inf／nan。有問題就不寫，回傳原因（呼叫端列為異常）。
    有真實歌曲（songs＝計畫裡的編號）時，完整版寫到 private_copy(path)，git 那份用 public_view 拿掉歌曲資料。"""
    targets = [(path, public_view(summary, songs) | ({"real_songs": {"count": len(songs), "full": "tests/private/baselines/"}}
                                                       if songs else {}))]
    if songs:
        targets.append((private_copy(path), summary))
    for p, data in targets:
        text = json.dumps(scrub(data), ensure_ascii=False, allow_nan=False, indent=1)
        leaks = user_leaks(text)
        if leaks:
            return f"摘要裡還有使用者名稱，沒有寫入 {p.name}：{leaks[0]!r}"
    for p, data in targets:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(scrub(data), ensure_ascii=False, allow_nan=False, indent=1), "utf-8")
    return ""


def tools_dirty() -> bool:
    return bool(_git("status", "--porcelain", *TOOL_PATHS).strip())


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    ).stdout


def leak_check(ref: str, since: str | None) -> list[str]:
    """回傳漏移的項目（空的表示沒有）。"""
    problems = []
    cur = None
    for line in _git("diff", "-U0", ref, "--", *FRONT).splitlines():  # 和工作目錄比：移植完、提交前就要能檢查
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            cur = line[6:] if line.startswith("+++ b/") else cur or line[6:]
            continue
        if line.startswith(("+++", "---", "@@", "diff ", "index ", "new file", "deleted file")) or line[:1] not in (
            "+",
            "-",
        ):
            continue
        if not any(f == cur and re.fullmatch(rx, line) for f, rx in OWN_CHANGES):
            problems.append(f"前端／安裝檔 {cur}：{line[:150]}")
    base = since or _git("merge-base", "HEAD", ref).strip()
    if _git("rev-parse", base).strip() == _git("rev-parse", ref).strip():
        return problems  # 上次同步點就是這個版本：後端沒有新的上游改動要檢查
    added = [
        ln[1:]
        for ln in _git("diff", base, ref, "--", "app.py", "pipeline.py").splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    ]
    names = {m.group(1) for ln in added for rx in _NAME_PATTERNS if (m := re.search(rx, ln))}
    files = [f for f in _git("ls-files").split() if f.endswith(".py") and not f.startswith("tests/")]
    code = "".join((ROOT / f).read_text("utf-8") for f in files if (ROOT / f).exists())
    for n in sorted(names):
        if n not in code and n not in NOT_PORTED:
            problems.append(f"後端：上游新增的 {n} 在重構版找不到")
    return problems


def export(ref: str) -> tuple[str, Path]:
    """每次都從 git 重新匯出到這個行程自己的資料夾 OUT/<名稱>/exports/<pid>-<時間>（不沿用、不共用：
    解壓中斷、被改過、tag 改指別的 commit、同時跑的另一個工具刪掉，都不會測到錯的程式），行程結束時刪掉。
    完整 commit 寫在 <code>/.leihu-commit，報告用 commit_of() 讀。"""
    full, name = ref_name(ref)
    import atexit
    import time

    from sandbox import clean_stale, owner_text, remove_tree, split_cleanup

    exports = OUT / name / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    cleaned, rest = split_cleanup(clean_stale(exports, "*", allowed=(exports,)))
    EXPORT_CLEANED.extend(f"開始前匯出資料夾：{m}（建立它的行程確定已結束）" for m in cleaned)
    EXPORT_NOTES.extend(f"匯出資料夾：{m}" for m in rest)
    fresh = exports / f"{os.getpid()}-{time.time_ns()}"
    fresh.mkdir()
    (fresh / "owner.pid").write_text(owner_text(), "utf-8")  # 被強制結束時，下一次 export() 會清掉

    def _drop(d=fresh):
        why = remove_tree(d, allowed=(exports,))
        if why:
            print(f"匯出資料夾刪不掉：{d}：{why}", flush=True)

    atexit.register(_drop)
    blob = subprocess.run(["git", "archive", "--format=tar", full], cwd=ROOT, capture_output=True, check=True).stdout
    with tempfile.TemporaryFile() as f:
        f.write(blob)
        f.seek(0)
        with tarfile.open(fileobj=f) as t:
            t.extractall(fresh, filter="data")
    (fresh / ".leihu-commit").write_text(full, "utf-8")
    return name, fresh


EXPORT_NOTES: list[str] = []  # export() 清理舊匯出時發生的問題，報告列為異常
EXPORT_CLEANED: list[str] = []  # export() 清掉的舊匯出（建立它的行程確定已結束），報告寫進執行條件
LOCK_NOTES: list[str] = []  # acquire_recording()：接手上一次沒有正常結束的錄製鎖，報告寫進執行條件


def ref_name(ref: str) -> tuple[str, str]:
    """git 參照的完整 commit 和輸出資料夾名稱（剛好是 tag 就用 tag，否則用 7 碼 commit）。"""
    full = subprocess.run(["git", "rev-parse", f"{ref}^{{commit}}"], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()
    tag = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", full], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    return full, tag or full[:7]


def commit_of(code: Path) -> str:
    """export() 匯出的完整 commit；--code 指定的資料夾沒有這個檔，回傳說明。"""
    f = code / ".leihu-commit"
    return f.read_text("utf-8").strip() if f.exists() else "（不是 export() 匯出的，commit 不明）"


def diff(old, new, path: str, out: list[str]) -> None:
    """old＝重構版快照、new＝上游。"""
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(old.keys() | new.keys()):
            p = f"{path}/{k}"
            if k not in new:
                out.append(f"+ {p}={json.dumps(old[k], ensure_ascii=False)[:120]}")
            elif k not in old:
                out.append(f"- {p}={json.dumps(new[k], ensure_ascii=False)[:120]}")
            else:
                diff(old[k], new[k], p, out)
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (a, b) in enumerate(zip(old, new, strict=False)):
            diff(a, b, f"{path}[{i}]", out)
    elif type(old) is not type(new) or old != new:  # True == 1、1 == 1.0 在 Python 成立，JSON 契約卻不同
        out.append(
            f"~ {path}: {json.dumps(new, ensure_ascii=False)[:120]} -> {json.dumps(old, ensure_ascii=False)[:120]}"
        )


def classify(case: str, d: str) -> str | None:
    for name, _, pred in RULES:
        if pred(case, d):
            return name
    return None


_IDS = {
    "new": "x",
    "old": "x",
    "done": "x",
    "pending": "x",
    "job": "0123456789",
    "take_new": "x",
    "template": "x",
    "uploaded": "x",
    "yt_song": "x",
}


# --code 指到 repo 本身時，.venv、tests/private（模型約 2 GB，junction 會被當成資料夾複製）等不能複製進沙盒
_COPY_IGNORE = shutil.ignore_patterns("config.json", ".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "private")


def run_once(name: str, code: Path, tag: str) -> dict:
    """用 serve_upstream.py 跑一次 CASES。回傳結果、伺服器 log、沙盒紀錄、路由清單；log 另存在 OUT/name。"""
    tmp = Path(tempfile.mkdtemp(prefix="leihu-upstream-"))
    shim, routes = tmp / "shim.log", tmp / "routes.json"
    try:
        result = run_contract(
            tmp,
            CASES,
            env_extra={
                "PYTHONDONTWRITEBYTECODE": "1",
                "LEIHU_SHIM_LOG": str(shim),
                "LEIHU_ROUTES_OUT": str(routes),
            },
            server=[sys.executable, str(HERE / "serve_upstream.py")],
            # 不複製使用者設定：config.json 的 library_dir、models_dir 會把資料導到沙盒外（fixture 已經寫好測試設定）
            setup=lambda data: shutil.copytree(code, data, dirs_exist_ok=True, ignore=_COPY_IGNORE),
        )
        log = (tmp / "server.log").read_text("utf-8", errors="replace")
        notes = [json.loads(x) for x in shim.read_text("utf-8").splitlines() if x] if shim.exists() else []
        rules = json.loads(routes.read_text("utf-8")) if routes.exists() else None
    finally:
        if (tmp / "server.log").exists():
            shutil.copy2(tmp / "server.log", OUT / name / f"server-{tag}.log")
        shutil.rmtree(tmp, ignore_errors=True)
    (OUT / name / f"contract-{tag}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    return {"result": result, "log": log, "notes": notes, "routes": rules}


def uncovered(routes: list[str] | None) -> list[str]:
    """CASES 沒有打到的路由（規則字串＋方法），排除 /static 和 /api/debug/stacks。拿不到路由清單時回傳說明。"""
    from test_contract import _fill
    from werkzeug.routing import Map, Rule

    if routes is None:
        return ["沙盒沒有寫出路由清單（routes.json），涵蓋率沒有檢查"]
    want = {r for r in routes if not r.startswith(("/static/", "/api/debug/stacks "))}
    by_rule: dict[str, set[str]] = {}
    for r in want:
        rule, method = r.rsplit(" ", 1)
        by_rule.setdefault(rule, set()).add(method)
    adapter = Map([Rule(rule, methods=ms, endpoint=rule) for rule, ms in by_rule.items()]).bind("127.0.0.1")
    hit = set()
    for _, method, path, _ in CASES:
        try:
            hit.add(f"{adapter.match(_fill(path, _IDS).split('?')[0], method=method)[0]} {method}")
        except Exception:  # noqa: S112  打到不存在的路由（404 案例）不算涵蓋
            continue
    return sorted(want - hit)


def alerts(run: dict, label: str, known: list[str]) -> list[str]:
    """一次執行裡所有「條件和平常不同」的地方。LOG_EXPECTED 符合的訊息放進 known（照樣列在報告）。"""
    out = []
    for line in run["log"].splitlines():
        why = next((w for n, w in LOG_EXPECTED if n in line), None)
        if why:
            known.append(f"{label} 伺服器 log：{line.strip()[:200]}（{why}）")
            continue
        for needle, what in LOG_ALERTS:
            if needle in line:
                out.append(f"{label} 伺服器 log（{what}）：{line.strip()[:200]}")
                break
    out += [f"{label} 沙盒：{n['msg']}" for n in run["notes"] if n["kind"] == "missing"]
    for case, rec in run["result"].items():
        for k, v in rec["files"].items():
            if "net_blocked" in k:
                out.append(f"{label} 案例 {case} 想對外連線（已擋下）：{json.dumps(v, ensure_ascii=False)[:200]}")
            if k.endswith("spawn.log"):
                out += [f"{label} 案例 {case} 開了沒有分類的外部程式：{e}" for e in v if e.get("kind") == "spawn"]
    out += [f"{label} 沒有案例的路由：{r}" for r in uncovered(run["routes"])]
    return out


def raw_diff(a: dict, b: dict) -> dict[str, list[str]]:
    """兩份結果逐案例的差異，不套用 RULES（同一版跑兩次的穩定性檢查用）。"""
    a, b = (json.loads(json.dumps(x, ensure_ascii=False)) for x in (a, b))
    out: dict[str, list[str]] = {}
    for case in sorted(a.keys() | b.keys()):
        if case not in a or case not in b:
            out[case] = ["只有一次有這個案例"]
            continue
        d: list[str] = []
        diff(a[case], b[case], "", d)
        if d:
            out[case] = d
    return out


def compare(snap: dict, result: dict) -> tuple[list[str], dict, dict]:
    """snap＝目前這一邊、result＝基準。回傳（完全相同的案例、預期差異、未預期差異）。案例集合不同也算未預期。"""
    same, expected, unexpected = [], {}, {}
    snap, result = (json.loads(json.dumps(x, ensure_ascii=False)) for x in (snap, result))  # 兩邊都用 JSON 的型別（tuple→list）
    for case in sorted(snap.keys() | result.keys()):
        if case not in snap or case not in result:
            unexpected.setdefault(case, []).append("只有" + ("基準" if case in result else "目前這一邊") + "有這個案例")
            continue
        out: list[str] = []
        res_c, note = _align_d4(case, snap[case], result[case])
        diff(snap[case], res_c, "", out)
        if note:
            out.append(note)
        if not out:
            same.append(case)
        for d in out:
            rule = classify(case, d)
            (expected.setdefault(rule, []) if rule else unexpected.setdefault(case, [])).append(
                f"{case}: {d}" if rule else d
            )
    return same, expected, unexpected


def meta(name: str, code: Path, runs: int) -> list[str]:
    """報告開頭：被測程式、測試工具、執行環境。"""
    import platform

    tool = _git("rev-parse", "--short", "HEAD").strip()
    dirty = tools_dirty()
    return [
        f"- 被測程式：`{name}`（commit {commit_of(code)}；{code}）",
        f"- 測試工具：commit `{tool}`" + ("，`tests/` 有未 commit 的修改" if dirty else ""),
        f"- Python：{sys.version.split()[0]}（{sys.executable}）",
        f"- 電腦：{platform.platform()}；{platform.processor() or '（CPU 型號讀不到）'}",
        f"- 執行次數：{runs}；時間：{__import__('time').strftime('%Y-%m-%d %H:%M:%S')}",
        "- 沙盒條件：背景工作不執行、不連網、不開外部程式、固定為沒有可用的顯示卡；完整清單在下方「沙盒替換」",
        "- 正規化的盲點：15 位以上的長數字、10 位十六進位字串、執行當下 3 天內的時間會換成佔位字，"
        "這些位置的值改錯看不出來（同一次執行內的引用關係有保留）",
    ]


def main() -> int:  # noqa: C901
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = {a.split("=", 1)[0]: (a.split("=", 1)[1] if "=" in a else "") for a in sys.argv[1:] if a.startswith("--")}
    if "--rebase" in opt:  # 參數錯誤先擋下，不要等到報告寫了「異常：無」之後才加
        if "--code" in opt or "--record" not in opt:
            print("--rebase 只能和 --record 一起用在 git 參照上（--code 的資料夾無法確認是哪個 commit）")
            return 2
        ref_full, base_full = (_git("rev-parse", f"{r}^{{commit}}").strip() for r in (args[0] if args else BASE,
                                                                                    opt.get("--base") or BASE))
        if ref_full != base_full:
            print(f"--rebase 只能重建基準本身：參照 {ref_full[:12]} 和 --base {base_full[:12]} 不是同一個 commit")
            return 2
        # 之後一律用驗證過的完整 commit 匯出（分支、tag 在中途移動也不會匯出別的 commit）
        args = [ref_full, *args[1:]]
        opt["--base"] = base_full
    from sandbox import acquire_recording

    LOCK_NOTES.extend(acquire_recording("compare_upstream.py " + " ".join(sys.argv[1:])))
    if "--code" in opt:
        code = Path(opt["--code"]).resolve()
        name = code.name
        (OUT / name).mkdir(parents=True, exist_ok=True)
    else:
        name, code = export(args[0] if args else BASE)
    runs = [run_once(name, code, "run1")]
    if "--twice" in opt:
        runs.append(run_once(name, code, "run2"))
    result = runs[0]["result"]

    known: list[str] = []
    problems = alerts(runs[0], "第 1 次", known) + EXPORT_NOTES
    if len(runs) > 1:
        problems += alerts(runs[1], "第 2 次", known)
        for c, ds in raw_diff(runs[1]["result"], result).items():  # 同一版：不套用 RULES，要完全相同
            problems.append(f"兩次結果不同的案例 {c}：{ds[0][:200]}")
    stubs = [n["msg"] for n in runs[0]["notes"] if n["kind"] in ("stub", "forced")]

    recording = "--record" in opt
    if recording and SNAPSHOT.exists():
        base_name, base_code = export(opt.get("--base") or BASE)
        if commit_of(base_code) == commit_of(code):  # 同一個 commit 才算重錄基準本身（不看資料夾名稱）
            snap, base_result = json.loads(SNAPSHOT.read_text("utf-8")), result  # 重錄基準本身：和舊快照比
        else:
            base_run = run_once(base_name, base_code, "base")
            problems += alerts(base_run, f"基準 {base_name}", known)
            snap, base_result = result, base_run["result"]
    elif recording:
        snap, base_result = result, result  # 第一次建立基準
    else:
        if not SNAPSHOT.exists():
            print(f"還沒有快照 {SNAPSHOT}：先用 --record 錄一次")
            return 1
        snap, base_result = json.loads(SNAPSHOT.read_text("utf-8")), result
    same, expected, unexpected = compare(snap, base_result)

    title = f"錄製 {name} 的契約快照" if recording else f"基準 {name} 與目前快照的契約比對"
    lines = [
        f"# {title}",
        "",
        *meta(name, code, len(runs)),
        "",
        f"案例 {len(snap)} 個：完全相同 {len(same)}、有差異 {len(snap) - len(same)}；"
        f"未預期差異的案例 {len(unexpected)} 個。",
        "",
        "## 異常",
        "",
        *([f"- {p}" for p in problems] or ["無"]),
        "",
        "## 案例本身造成的訊息（不算異常）",
        "",
        *([f"- {k}" for k in known] or ["無"]),
        "",
    ]
    if unexpected:
        lines += ["## 未預期的差異", ""]
        for case, ds in unexpected.items():
            lines += [f"### {case}", "", "```", *ds, "```", ""]
    if expected:
        lines += ["## 預期的差異", ""]
        for rule, ds in expected.items():
            desc = next(r[1] for r in RULES if r[0] == rule)
            lines += [
                f"### {rule}：{desc}（{len(ds)} 項）",
                "",
                "```",
                *ds[:8],
                *(["……"] if len(ds) > 8 else []),
                "```",
                "",
            ]
    lines += ["## 沙盒替換", "", *[f"- {s}" for s in stubs], ""]
    if LOCK_NOTES or EXPORT_CLEANED:
        lines += ["## 開始前的清理（不算異常）", "", *[f"- {s}" for s in LOCK_NOTES + EXPORT_CLEANED], ""]

    # 重錄基準本身而且明確加了 --rebase（上面已確認參照和 --base 是同一個 commit）：和舊快照的差異只供參考
    rebase = recording and "--rebase" in opt and SNAPSHOT.exists() and commit_of(code) == opt.get("--base")
    blocking = bool(problems) or (bool(unexpected) and not rebase)
    if recording:
        if blocking:
            lines += ["**沒有寫入快照**：有異常或未登記的差異，先處理再錄。", ""]
        else:
            SNAPSHOT.parent.mkdir(exist_ok=True)
            with open(SNAPSHOT, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
            lines += [f"已寫入 {SNAPSHOT.relative_to(ROOT).as_posix()}。", ""]
    report = OUT / name / "contract-report.md"
    report.write_text("\n".join(lines), "utf-8")
    print(
        f"契約{'錄製' if recording else '比對'}：相同 {len(same)}、有差異 {len(snap) - len(same)}、"
        f"未預期 {len(unexpected)} 個案例、異常 {len(problems)} 項"
        + ("（未寫入快照）" if recording and blocking else "")
        + f"；報告：{report}"
    )
    rc = 1 if blocking else 0
    if "--leak" in opt:
        problems = leak_check(args[0] if args else BASE, opt.get("--since") or None)
        body = "\n".join(f"- {p}" for p in problems) or "沒有漏移。"
        kept = "\n".join(f"- `{k}`：{v}" for k, v in NOT_PORTED.items())
        (OUT / name / "leak-report.md").write_text(
            f"# 上游 {name} 漏移檢查\n\n{body}\n\n刻意不移植：\n\n{kept}\n", "utf-8"
        )
        print(f"漏移檢查：{len(problems)} 項；報告：{OUT / name / 'leak-report.md'}")
        rc = max(rc, 1 if problems else 0)
    return rc


if __name__ == "__main__":
    sys.exit(main())
