# 從 pipeline.py 搬出（tests/tools/split_moves.py，docs/pipeline-split-plan.md 方式一）。
# pipeline._load_part 把這個檔案載入 pipeline 的命名空間，等於這段程式寫在 pipeline.py 裡；不能單獨 import。


def ffmpeg_path() -> str | None:
    """找 ffmpeg：系統安裝的，或 imageio-ffmpeg 附帶的。並讓 librosa/audioread 也找得到。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None
    bindir = ROOT / "bin"
    bindir.mkdir(exist_ok=True)
    dst = bindir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dst)
    os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
    return str(dst)


YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")


def clean_youtube_url(url: str) -> str:
    from urllib.parse import parse_qs, urlparse
    url = (url or "").strip()
    u = urlparse(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise UserError("這不是 YouTube 網址")
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif u.path.startswith(("/shorts/", "/live/")):
        vid = u.path.split("/")[2]
    else:
        vid = (parse_qs(u.query).get("v") or [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or ""):
        raise UserError("網址裡找不到影片編號，請貼上單一影片的網址")
    return f"https://www.youtube.com/watch?v={vid}"


# ---------------------------------------------------------------- 批次匯入（多個網址、歌單、歌名搜尋、Excel）

BATCH_MAX = 1000   # 一次最多幾首（表單可能有好幾百首）


def _ydl():
    import yt_dlp
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                             "extract_flat": "in_playlist", "socket_timeout": 20})


def _entry(e: dict) -> dict:
    vid = e.get("id") or ""
    return {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": e.get("title") or "",
            "duration": e.get("duration"), "channel": e.get("channel") or e.get("uploader") or ""}


def _playlist_id(line: str) -> str | None:
    from urllib.parse import parse_qs, urlparse
    u = urlparse(line if "://" in line else "https://" + line)
    if (u.hostname or "").lower() not in YOUTUBE_HOSTS:
        return None
    lid = (parse_qs(u.query).get("list") or [""])[0]
    # RD 開頭是 YouTube 自動產生的「合輯」，不是真的歌單：只取那一首
    if lid and not lid.startswith("RD") and (u.path.startswith("/playlist") or lid):
        return lid
    return None


def resolve_line(line: str) -> list[dict]:
    """把一行輸入變成要下載的影片清單：影片網址、歌單網址，或「歌手 歌名」（到 YouTube 搜尋第一筆）。"""
    line = re.sub(r"\s+", " ", line.replace("\t", " ")).strip()
    if not line:
        return []
    is_url = re.match(r"^(https?://)?([\w-]+\.)*(youtube\.com|youtu\.be)/", line, re.I)
    with _ydl() as ydl:
        if is_url and _playlist_id(line):
            info = ydl.extract_info(f"https://www.youtube.com/playlist?list={_playlist_id(line)}", download=False)
            items = [_entry(e) for e in (info.get("entries") or []) if e and e.get("id")]
            for it in items:
                it["from"] = f"歌單：{info.get('title') or ''}"
            return items[:BATCH_MAX]
        if is_url:
            url = clean_youtube_url(line)
            try:
                info = ydl.extract_info(url, download=False, process=False)
                it = _entry(info)
            except Exception:
                it = {"id": url[-11:], "url": url, "title": "", "duration": None, "channel": ""}
            return [it]
        info = ydl.extract_info(f"ytsearch1:{line}", download=False)
        items = [_entry(e) for e in (info.get("entries") or []) if e and e.get("id")]
        for it in items:
            it["from"] = f"搜尋：{line}"
        return items[:1]


def resolve_lines(lines: list, rows=None) -> list[dict]:
    """多行一起解析（同時查 4 行）。每一行的結果依原本順序排好；失敗的行也會列出來，附上原因。
    rows：從表單讀到的資料（以 line 對應）。表單有 YouTube 網址的直接用，不去搜尋；
    歌曲庫裡已經有同一個網址的歌，標成「更新資訊」（套用表單的歌手、曲名、KEY，不重新下載）。"""
    from concurrent.futures import ThreadPoolExecutor
    if isinstance(rows, list):   # 表單：每一列各自對應（同名的歌也不會混在一起）
        pairs = [(str(r.get("line") or "").strip(), r, r.get("row") or i + 2) for i, r in enumerate(rows) if isinstance(r, dict)]
        pairs = [p for p in pairs if p[0]][:BATCH_MAX]
    else:
        rows = rows or {}
        pairs = [(l, rows.get(l) or {}, None) for l in (str(x).strip() for x in lines) if l][:BATCH_MAX]
    by_url = {m.get("source_url"): m for m in list_songs() if m.get("source_url") and (m.get("steps") or {}).get("import")}
    # 已經下載好或正在排隊的算重複；之前下載失敗／取消的不算（勾選後會用原本那一項重新下載）
    existing = {m.get("source_url"): m.get("title") for m in list_songs()
                if m.get("source_url") and ((m.get("steps") or {}).get("import") or m.get("dl_state") not in ("failed", "cancelled"))}

    def one(pair):
        line, row, rowno = pair
        meta = {k: row.get(k) for k in ("artist", "song", "key", "lang", "category") if row.get(k) not in (None, "")}
        try:
            if row.get("url"):
                items = [{"id": row["url"][-11:], "url": row["url"], "title": row.get("song") or line,
                          "duration": None, "channel": row.get("artist") or "", "from": "表單網址"}]
            else:
                items = resolve_line(line)
            return [dict(it, input=line, sheet=meta or None, row=rowno) for it in items] \
                or [{"input": line, "error": "找不到符合的影片", "row": rowno}]
        except UserError as e:
            return [{"input": line, "error": str(e), "row": rowno}]
        except Exception as e:
            log_error(f"resolve({line})", e)
            return [{"input": line, "error": f"查詢失敗：{type(e).__name__}", "row": rowno}]
    with ThreadPoolExecutor(max_workers=4) as pool:
        groups = list(pool.map(one, pairs))
    out, seen = [], set()
    for g in groups:
        for it in g:
            if it.get("url"):
                it["dup"] = existing.get(it["url"]) or ("（清單裡重複）" if it["url"] in seen else None)
                old = by_url.get(it["url"])
                if old and it.get("sheet") and it["url"] not in seen:   # 舊歌：用表單資料更新
                    it["dup"] = None
                    it["update"] = {"id": old["id"], "title": old.get("title")}
                seen.add(it["url"])
            out.append(it)
    return out[:BATCH_MAX]


def read_sheet(data: bytes, name: str) -> list[dict]:
    """讀 Excel（.xlsx）或 CSV。認得「歌手」「曲名」「備註／KEY」「語言」和 YouTube 網址欄：
    回傳每一列 {line, artist, song, key, lang, url}。line 是給搜尋用的「歌手 曲名」（沒有網址時才會去搜尋）。"""
    import csv
    import io
    ext = Path(name).suffix.lower()
    rows: list[list[str]] = []
    nums: list[int] = []   # 每一列在表單裡是第幾列（給使用者對照）
    if ext == ".xlsx":
        import zipfile
        import xml.etree.ElementTree as ET
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise UserError("讀不到這個 Excel 檔，請另存成 .xlsx 或 .csv 再試")

        def xml(name: str):   # fork：XML 壞掉原本丟 ParseError（500）
            try:
                return ET.fromstring(z.read(name))
            except ET.ParseError:
                raise UserError("讀不到這個 Excel 檔，請另存成 .xlsx 或 .csv 再試") from None
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in xml("xl/sharedStrings.xml").findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
        sheets = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheets:
            raise UserError("Excel 檔裡沒有工作表")
        first = "xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml" in sheets else sheets[0]
        for r in xml(first).iter("{%s}row" % ns["m"]):
            cells = {}
            col = 0
            for c in r.findall("m:c", ns):
                ref = re.match(r"([A-Z]+)", c.get("r") or "")
                if ref:
                    col = 0
                    for ch in ref.group(1):
                        col = col * 26 + ord(ch) - 64
                else:   # fork：規格允許省略 r，接在前一格後面（原本省略時都算 A 欄，r="" 丟 AttributeError）
                    col += 1
                t, v = c.get("t"), c.find("m:v", ns)
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
                else:
                    val = v.text if v is not None else ""
                cells[col] = (val or "").strip()
            if cells:
                rows.append([cells.get(i, "") for i in range(1, max(cells) + 1)])
                nums.append(int(r.get("r") or len(rows)))
    elif ext in (".csv", ".txt", ".tsv"):
        text = None
        for enc in ("utf-8-sig", "big5", "gbk"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise UserError("讀不到這個檔案的文字編碼")
        dialect = "excel-tab" if ext == ".tsv" or ("\t" in text and "," not in text) else "excel"
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect=dialect)]
        nums = list(range(1, len(rows) + 1))
    elif ext == ".xls":
        raise UserError("舊版 Excel（.xls）請先另存成 .xlsx 或 .csv")
    else:
        raise UserError("只支援 .xlsx、.csv 或 .txt")
    keep = [i for i, r in enumerate(rows) if any(r)]
    rows, nums = [rows[i] for i in keep], [nums[i] for i in keep]
    if not rows:
        return []
    head = [c.lower() for c in rows[0]]
    find = lambda keys: next((i for i, c in enumerate(head) if any(k in c for k in keys)), None)
    ai = find(["歌手", "演唱", "藝人", "artist", "singer"])
    ti = find(["曲名", "歌名", "歌曲", "標題", "title", "song", "name"])
    ki = find(["key", "升降", "調"])
    if ki is None:
        ki = find(["備註", "note", "memo"])   # 雷虎的歌單：備註欄放的是升降 KEY
    li = find(["語言", "language", "lang"])
    ui = find(["ytlink", "youtube", "yt"])
    cell = lambda r, i: r[i].strip() if i is not None and i < len(r) else ""
    yt_re = re.compile(r"(https?://)?([\w-]+\.)*(youtube\.com|youtu\.be)/\S+", re.I)
    out = []
    if ai is not None or ti is not None:
        for r, rowno in zip(rows[1:], nums[1:]):
            artist, song = cell(r, ai), cell(r, ti)
            if not (artist or song):
                continue
            url = ""
            for c in ([cell(r, ui)] if ui is not None else []) + list(r):   # 先看網址欄，沒有再找整列
                m = yt_re.search(c or "")
                if m:
                    try:
                        url = clean_youtube_url(m.group(0))
                        break
                    except UserError:
                        continue
            lang = sheet_language(cell(r, li))
            out.append({"line": " ".join(x for x in (artist, song) if x), "artist": artist, "song": song,
                        "key": parse_key(cell(r, ki)), "lang": lang[0] if lang else None,
                        "category": lang[1] if lang else None, "url": url, "row": rowno})
    else:
        out = [{"line": " ".join(c for c in r if c)} for r in rows]
    return out[:BATCH_MAX]
