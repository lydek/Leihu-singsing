# 舊重構分支搬到 `refactor` 的 review 計畫

建立日期：2026-11-21。

- **來源**：`sync/upstream-2.3.2`（`0fcc37f`），worktree 在 `.claude/worktrees/sync-latest-version-f53395`。
- **目的地**：`refactor`，從 `v2.3.2`（`5ddd046`）開始。GitHub ruleset 規定歷史要線性，只能用 rebase merge。

## 1. 現況

- 舊分支是從 **v2.0.0**（`9703211`）開出來的，不是從 2.3.2。
- 上游 2.1.0、2.2.1～2.3.2 是逐 commit 移植進重構後的程式，2.2.0 是一次移植（`4596bdd`），各自再用 merge commit 掛上上游 commit（`964e33b`、`24379a9`、`1731902`）。
- `v2.3.2..sync/upstream-2.3.2` 共 73 個 commit，扣掉 3 個 merge 剩 **70 個**。
- `refactor` 已經是原生的上游 2.3.2，所以搬遷的方向和舊分支相反：
  - 上游的功能都已經在了，不用搬。
  - 要搬的是 fork 自己的成果：安全網、修正、重構、測試、工具、文件。
  - 這些成果原本都是寫在 2.0.0 的程式上，搬過來要重新套到 2.3.2 的程式上。
- 抽查結果：上游在 2.3.2 已經自己修了部分步驟 0 的問題，所以**不能直接照搬**，每個 commit 都要先確認問題還在不在。
  - M1、M2：`app.py:131` 的 `_local_only` 已經會檢查 Host 和 Origin。
  - M5：`claude_api.py:45` 的 `_load` 仍然吞掉所有例外，問題還在。
  - S19：`safe_name` 仍然有三份（`app.py:1301` 的 `_safe_name`、`score_sheet.py:291`、`share.py:638`），問題還在。

## 2. 分類與初判

每個 commit 只會落在下面一個判定：

| 判定 | 意思 |
|---|---|
| **搬** | 內容幾乎不用改，直接 cherry-pick 或照抄 |
| **重做** | 目的仍然成立，但要對 2.3.2 的程式重新寫一次 |
| **併入** | 不單獨搬，跟另一組一起重做（例如寫在 `leihu/` 上的修正，跟重構一起處理） |
| **不搬** | 2.3.2 已經有了，或只是同步過程的紀錄 |
| **不做** | 目的已經不成立，或同步上游的成本大於效益（3.4） |
| **延後** | 目的成立，但會改變行為或屬於效能調整，依 3.1 放到後面的版本 |
| **待查** | 要看 diff 才能判斷 |

表中的「#」是 `git rev-list --reverse --no-merges v2.3.2..sync/upstream-2.3.2` 的順序。

### A. 評估與規劃文件
| # | commit | 內容 | 初判 | 備註 |
|---|---|---|---|---|
| 1 | `19ef655` | 評估文件、交接說明 | 搬 | `檔案:行號` 的基準是 2.0.0，要加註或改成 2.3.2 |
| 2 | `15e4312` | 步驟 0 計畫 | 重做 | M1/M2 等項目要對照 2.3.2 現況重新整理 |
| 23 | `4981130` | 步驟 5 前端計畫（暫緩） | 搬 | |
| 30 | `bb78455` | 改善程度與行為差異文件 | 重做 | 比較基準要從 v2.1.0 改成 v2.3.2，數字要重量 |

### B. 開發環境、CI、測試骨架
| # | commit | 內容 | 初判 | 備註 |
|---|---|---|---|---|
| 3 | `447489e` | uv、CI、測試骨架 | 重做 | 改用：`conftest` 不設 `LIANTAI_*`、只檢查使用者資料；路由數 93；CI 加上舊分支後來補的防護；`uv.lock` 重新產生。原因：2.3.2 不讀 `LIANTAI_DATA`（S28 在 C 組），見 3.11 |
| 27 | `407b2a7` | ruff 設定 | 搬 | 照舊，只搬 `pyproject.toml` 的設定；CI 不跑（3.1） |
| 31 | `cbb0fbf` | 開發用啟動器（用已安裝的內嵌 Python） | 搬 | 照舊，只搬 `tests/tools/run_dev.py` 和 `.gitignore` 的 `.duet_cleaned` |
| 33 | `ebae3ee` | makensis `-INPUTCHARSET UTF8` | 搬 | 照舊；2.3.2 的 `installer/build.sh:12` 仍然沒有指定 |
| 41 | `cf7a96e` | CI 加 ruff、`.git-blame-ignore-revs` | 重做 | 延到 E 組清完違規之後；格式化 commit 的 hash 會不一樣 |
| 40 | `c666a91` | ruff format（只改格式） | 重做 | 同 41；不要 cherry-pick，在新程式上重跑 `ruff format` |

### C. 步驟 0：對上游程式的修正（先在扁平結構上做）
這個版本做 S28 和安全、防止資料遺失的修正；其餘都是改變行為的修正，依 3.1 的原則延後。

| # | commit | 內容 | 初判 | 備註 |
|---|---|---|---|---|
| 4 | `a142c98` | 環境變數覆寫 S28、import 副作用 S31 | 重做 | 2.3.2 新增了 `library_dir`、`models_dir`，覆寫順序要重新設計。S31 延後：會改變行為（import 時的 `SetErrorMode`、`ffmpeg_path()` 搬走） |
| 5 | `e2168c4` | M1–M7 修正與回歸測試 | 重做 | M4、M5 這版做；M1/M2 不做；M3、M7 延後（3.13）；M6 在下一版做（`next-version-plan.md` 第 1 批） |
| 6 | `7ee7791` | R1–R3 資源浪費 | 不做 | R1（具名 mutex 判斷已有執行個體）不做：慢的問題 3.18 已用同時檢查連接埠解決；延後：效能調整（R2 `/api/status` 不 import torch、R3 `OPENBLAS_NUM_THREADS`）（3.20） |
| 7 | `da59e07` | 錯誤格式統一 S15 | 重做 | 延後：會改變行為（錯誤回應的內容）；以後做時，2.3.2 新增的路由要一起統一 |
| 9 | `dee8f6b` | 步驟 0 審查修正 | 不做 | R1 接手跟著 #6 不做；`claude_api.record` 寫檔失敗照常回傳已在 C 組 M5 做（3.13）；試聽檔在錄音刪除後才寫和 M7 同類，延後：會改變行為（3.20） |
| 10 | `dfcb0b9` | yt-dlp 取消／逾時、檢查碼、pip 逾時、`safe_name` 合併 | 重做 | 檢查碼（O12–O14）這版做；S8、O21、S19 延後（3.13） |

### D. 契約快照與比對工具
| # | commit | 內容 | 初判 | 備註 |
|---|---|---|---|---|
| 8 | `8c9bd95` | API 契約快照 | 重做 | 工具搬，快照在原版 `v2.3.2` 上重錄（3.7），C 組的改動逐一登記 |
| 24 | `74e690b` | `netblock.py`、`run_contract` 可指定啟動指令 | 搬 | 跟 8 一起 |
| 20 | `50a90b1` | 上游 2.1.0 同步計畫文件；數值比對配合 s2tw（小改） | 併入 | 數值比對工具本體（`tests/numeric/`、`tests/test_numeric.py`）在 `c84046a` 新增，從那裡搬；參考輸出要對 2.3.2 重新產生 |
| 25 | `8119d4b` | 上游與重構版的比對測試 | 搬 | 重構時用來證明行為跟 2.3.2 一樣 |
| 26 | `1e2fae3` | 搬移、搬移後內容比對、漏移檢查工具 | 搬 | 同上 |
| 42 | `180072d` | 比對工具登記 `leihu.nsi` 的改動 | 併入 | 跟重構一起 |

### E. 重構本體（`leihu/` 套件、ml-worker）
| # | commit | 內容 | 初判 | 備註 |
|---|---|---|---|---|
| 11 | `90d0357` | 步驟 1：`app.py` 拆成 `leihu/web` Blueprint | 重做 | 2.3.2 的 `app.py` 和 2.0.0 差很多，沒辦法 cherry-pick；已完成（3.19） |
| 12 | `3d0fd5f` | 步驟 2：值物件、`update_song`、背景工作佇列 | 不做 | 上游 2.3.2 已有 `meta.json` 的鎖、原子寫入、`update_meta` 和佇列；延後：會改變行為（`step2-plan.md` 的 API 變更 D1–D6、`step_import` 補鎖，3.19） |
| 13 | `c84046a` | 步驟 3：ml-worker、外部網址、伺服器記憶體 | 不做 | 沿用上游 `gpu_worker.py`，不做 ml-worker（3.6）；延後：效能調整（伺服器不載入 librosa／scipy／numba，3.19）；`LIANTAI_URL_BASE` 不做；`LIANTAI_NO_JOBS`、數值比對工具已做（3.19） |
| 21 | `7247929` | 步驟 4：拆出 config、dsp、media、lyrics、llm、render | 重做 | 改用：只拆上游 v1.6 以來沒改過的純運算（音高後處理、歌詞文字），原因：上游常改 `pipeline.py`，整個拆開同步成本太高（3.19） |
| 22 | `34b292d` | BS-Roformer 補強 | 延後 | HF 網址上游 2.2.0 已修；主唱與合音分離失敗退回、讓位（`JobDeferred` 是 `JobCancelled` 的子類別）不標壞混合精度，上游都已有；延後：會改變行為（第一段分離的兩層退回、auto 模式 6 GB 門檻）（3.20） |
| 45 | `07c4be0` | 拆 `read_sheet`、`sheet.render` | 不做 | 步驟 4 改成只拆穩定區塊（3.19），`read_sheet` 在 v1.6 之後改過，不在其中 |

### F. 寫在 `leihu/` 上的審查修正
改變行為的修正依 3.1 的原則延後，上游本來就有的問題也一樣，安全和防止資料遺失的修正除外；只有不改行為的拆函式、改名跟著 E 組做（39 拆 `process_song`、45 拆 `read_sheet` 後來決定不做，3.19）。

| # | commit | 內容 | 初判 | 備註 |
|---|---|---|---|---|
| 28 | `70d30ce` | 審查修正 A–F：並行、輸入驗證、ext_url、rms 等 | 併入 | |
| 29 | `1e9f23c` | 審查修正 G：桌面啟動、打包、CI | 不做 | G1–G3 是 #6 R1 mutex 做法的修正，跟著不做；`leihu.nsi` 更新時刪 `leihu` 已在步驟 1 做；`build.sh`、CI、漏移檢查的改動屬於舊分支的結構（3.20） |
| 36 | `891b78e` | 第二次審查：錄音負的開始時間、混合精度 OOM、`.server_port` 擁有者 | 延後 | 錄音負的開始時間、`.server_port` 擁有者是舊分支自己造成的，不適用；混合精度 OOM 上游已有（3.6）；其餘延後：會改變行為（重疊處理 OOM、佇列、下載、歌單、匯出影片、`LIANTAI_PORT` 等）（3.20） |
| 38 | `73590fd` | `.partK`、PowerShell 路徑、pip 檢查碼、濾波分段 | 延後 | pip 檢查碼 C 組 O14 已做（3.13）；PowerShell 路徑和 M3 同類，延後：會改變行為；取消下載後寫 `.partK` 延後：會改變行為（下載完成時有 SHA-256 核對，不會留下壞檔）；濾波分段延後：效能調整（3.20） |
| 39 | `e38525f` | ruff 違規清到 0、拆 `process_song`、修取消下載的 TypeError | 併入 | TypeError 是修正，延後；拆 `process_song` 不做：上游很常改這個函式，步驟 4 不拆（3.19） |
| 46 | `1b5074b` | ml-worker 設定優先順序（錯誤 6） | 重做 | 上游 `gpu_worker._set_priority` 有同一個 bug。延後：會改變行為（直播中的低優先順序才會生效）（3.6） |
| 68 | `dcd5ce4` | Codex：三個移植問題 | 延後 | WorkerPool、`cuda_ok` 兩項是 ml-worker 才有，不做（3.6）；主唱補回和移調互斥上游沒有，延後：會改變行為（3.20） |
| 69 | `340914c` | Codex：五個上游既有問題（搬移、主唱補回、補做標記） | 延後 | 沒有找到使用者資料遺失的路徑（3.20）；延後：會改變行為（搬移時的寫入者計數、主唱補回清 KEY 版本、補做標記、`live_light`）。以後修時會改分軌，沒有數值參考輸出之前不合併 |
| 70 | `0fcc37f` | Codex 第二次：搬移隔離、KEY 版本失效、旋律過期、兩軌一致 | 延後 | 同 69（3.20）；延後：會改變行為（搬移隔離、KEY 版本失效、旋律過期、兩軌一起換、`read_audio(fallback=False)`） |

### G. 測試、量測、紀錄文件
| # | commit | 內容 | 初判 | 備註 |
|---|---|---|---|---|
| 32 | `bfc3f0d` | 量測真實歌曲的速度和記憶體 | 搬 | 腳本搬；數字要重量 |
| 43 | `2f3d2f2` | 補測試：歌詞文字、Claude 價格、歌單解析、旋律譜匯出 | 搬 | 已搬：`tests/test_lyrics_text.py`、`tests/test_sheet_io.py`，import 改成 `leihu.lyrics_text`、`pipeline`、`claude_api`、`score_sheet`，原本的斷言都保留，在目前的程式上全部通過；未知版面那個測試另外多驗圖片尺寸；另加 `tests/test_pitch.py`（`leihu/pitch.py` 的全部函式） |
| 47 | `6705dd4` | 效能分析、量測腳本 | 重做 | `tests/perf/` 的 `profile_song.py`、`load_profile.py`、`worker_capped.py` 依賴 ml-worker（3.6 不做）；改成量 `gpu_worker` 的父子程式，舊數字只當舊架構的紀錄 |
| 37、44 | `c9c1411`、`f063ed7` | 修正紀錄、ruff 文件 | 重做 | 跟著修正一起寫，不單獨搬 |

### H. 移植上游與同步紀錄：不搬
`refactor` 已經是原生的 2.3.2，所以下面這些不搬。

- 移植上游功能：14–19、34、49–66（`93e6ac8` 到 `7d913d9`）。
- 同步紀錄：35、48、67（`30b882d`、`2929da8`、`85db775`）。
- merge commit：`964e33b`、`24379a9`、`1731902`。

不搬但要留下的東西：

- 舊分支移植時做過的**決定**（`docs/upstream-sync-2.3.2.md` 第 2 節的 D3–D5）在 E 組重做時還是要照做。D1、D2 和「`gpu_worker.py` 不保留」已由 3.6 取代，「伺服器不載入 torch」見第 4 節。重做時拿這些 commit 當參考答案。
- 移植時另外補的**測試**（`tests/test_upstream_220.py`、`tests/test_upstream_232.py`、`tests/test_live.py` 等）測的是上游行為，但 import 的是 `leihu.jobs`、`leihu.inference`（`WorkerPool`、`WorkerClient`）、`leihu.web.*`，也依賴 S28 的環境變數（`tests/conftest.py`），**不能直接在 2.3.2 上跑**。當成測試案例清單：隨 E 組對應步驟改寫成測 `pipeline.Jobs`（步驟 2 不做，3.19）、`gpu_worker`，`gpu_worker` 用 3.6 的 monkeypatch；只用 `pipeline` 的案例可以在 S28 完成後提早搬。

## 3. 建議做法

### 3.1 順序
每一組各開工作分支、各發 PR、用 rebase 合併進 `refactor`。

**原則（2026-11-21 決定）：這個版本只做搬遷和行為等價的結構調整。** 延到後面版本的有兩類：會改變程式行為的修改（修上游既有的 bug，C、F 組大部分），以及效能調整（3.8 第二階段）。效能調整主要用 CPU 基準驗證，顯示卡只有筆電的一份基準（3.10）。**例外：安全和防止資料遺失的修正這個版本做**，照 3.3 的步驟：先確認 2.3.2 還有，加回歸測試，快照的差異先在 `RULES` 登記。遇到要延後的項目，在第 2 節備註寫「延後：會改變行為（……）」或「延後：效能調整（……）」，列進第 4 節。

1. **B 組**：開發環境、pytest CI、ruff 設定。幾乎只新增檔案（另改 `.gitignore`、`installer/build.sh`），衝突最少；`tests/test_smoke.py` 的路由數要從 78 改成 93。完整的 ruff 檢查（`cf7a96e` 的 `ruff check .`、`ruff format --check .`）不在這一步啟用：2.3.2 有既有違規（例如 `pipeline.py:118`、`pipeline.py:308` 的 `has = lambda` 會觸發 E731），清違規的 `e38525f` 排在 E 組。先記錄固定 ruff 版本下的違規數，清完再啟用；不要為了讓 B 組通過混入大批程式清理。
2. **A 組**：文件，同時把基準改成 2.3.2。
3. **D 組（安全網，先於任何程式修改）**：搬比對工具，在**原版 `v2.3.2`** 上錄功能基準、效能基準和相似度基準（做法見 3.7、3.8）。H 組的測試不在這一步搬（見 H 組）。
   - 數值工具要先改成配合 `gpu_worker`，不能只重新產生輸出。舊的 `tests/numeric/run.py` 只改父行程的 `P.MODELS` 和 `torchcrepe.convert.dither`，只有 `hasattr(P, "INFER")` 時才換 worker；上游 `gpu_worker` 會開新的 Python、重新 import pipeline，父行程的修改不會帶過去。
   - 兩邊都要在獨立的暫存程式碼和資料夾執行（2.3.2 不讀 `LIANTAI_DATA`，目前只有上游那一側會複製程式碼），模型路徑在父、子程式都要生效，dither 要在真正跑推論的子程式裡關掉。
   - 先證明原版 `v2.3.2` 自己跑兩次結果相同，再錄參考輸出。
4. **C 組只做不改行為的部分和安全修正**：S28 環境變數覆寫（不設定時和上游相同）要在這一步完成（3.7）；安全和防止資料遺失的修正（例如 M1/M2 的 port 比對、M5 吞例外）也在這一步做。S31（`a142c98` 把 `SetErrorMode`、`ffmpeg_path()` 從 `import pipeline` 搬到 `init_runtime()`）會改變 `gpu_worker` 子程式等每個 `import pipeline` 的行為，延後。修上游既有問題的項目（C 組其餘、F 組可以提早的部分、69、70）依上面的原則延後：先確認 2.3.2 還有，再在第 2 節備註寫「延後：會改變行為（……）」。
5. **E 組**：重構，只搬動程式、不改行為。**開始條件（2026-11-21 決定，2026-11-22 改用測試計畫）：基準全部錄完才開始**：用 `tests/private/plans/refactor.json`（3.8）錄 `v2.3.2` 和重構起點（C 組完成後的 HEAD `23925a7`）的數值、效能基準，重構起點的數值輸出要和 `v2.3.2` 逐檔相同（`baseline.py --against=v2.3.2`），效能不能明顯變慢；顯示卡基準在筆電錄（3.10，2026-11-22 已錄）。F 組只有不改行為的部分跟著 E 組做，其餘延後（39 拆 `process_song`、45 拆 `read_sheet` 後來決定不做，3.19）。每做完一步都要跑契約快照和比對測試，只能出現已登記的差異。動到 `pipeline.py` 或 `gpu_worker` 呼叫的步驟（3、4），另外跑 `tests/numeric/baseline.py <參照> --plan=tests/private/plans/refactor.json --against=v2.3.2`（CPU 逐位元組，3.8）和 `tests/perf/baseline.py <參照> --plan=tests/private/plans/refactor.json`（不能明顯變慢）；E 組全部完成後用 `tests/private/plans/full.json` 再跑一次。
6. **G 組**：量測、更新紀錄文件。#43 裡測既有函式的測試（例如 `read_sheet`、旋律譜匯出）改好 import 後，提前到 E 組對應的拆分之前加入。

### 3.2 E 組做法：照步驟 1–4 重做（2026-11-21 決定）
- 舊分支最後的檔案（`sync/upstream-2.3.2` 的 `leihu/`、`ml_worker/`）拿來當參考答案，但程式要從 2.3.2 的 `app.py`、`pipeline.py` 重新拆。
- 每一步都是可以獨立發布的 PR，合併前契約快照和比對測試都要過。
- 不採用的做法：直接搬舊分支最後的 `leihu/` 樹。原因是中間的 commit 可能跑不起來，也看不出跟 2.3.2 的差別。

F 組的修正跟著程式落點放進對應的步驟；改變行為的修正依 3.1 的原則延後，下表只用來知道以後要放哪裡。下表是依 commit 標題和檔案清單做的初步對應，review 時再修正：

| 步驟 | 舊 commit | 一起處理的 F 組修正 | 要對照的上游 2.3.2 差異 |
|---|---|---|---|
| 1 web Blueprint | `90d0357` | 28 的輸入驗證、42 | 2.3.2 新增的路由（歌單、搬移、搜尋範圍、直播歌單）、`_local_only` 已有的 Host／Origin 檢查 |
| 2 值物件、背景工作佇列（不做，3.19） | `3d0fd5f` | 28 的並行、68 的 leadfix 互斥（依 3.1 延後） | 上游 2.2.0 之後的重疊處理、全部暫停、優先處理 |
| 3 推論子程式（這個版本不實作，3.19） | `c84046a` 的非 ml-worker 部分 | 46 的優先順序修正（延後，3.6） | **沿用上游 `gpu_worker.py`，不做 ml-worker**（評估見 3.6） |
| 4 只拆音高後處理、歌詞文字（改用，3.19） | `7247929` | 45、39 的拆 `process_song` 不做；22、28 的 rms／ext_url、38 的濾波分段沒有進這一步，之後逐項看 | 2.3.2 的 `library_dir`、`models_dir`、搬移流程（`docs/upstream-sync-2.3.2.md` 的 D3） |

步驟 4 拆 `pipeline.py` 時，要保留 `gpu_worker.py` 用到的名稱：`P._sep_progress`、`P._outputs`、`P._track_pitch_local`、`P._whisper_model`、`P.pick_language`、`P._collect_words`、`P.cuda_ok`、`P.MODELS`、`P.SR`、`P.PITCH_SR` 等（`git show v2.3.2:gpu_worker.py | grep -oE 'P\.[A-Za-z_]+'`）。優先在 `pipeline` 保留這些名稱；如果改了 worker 的 import 或呼叫，要另外測 TASKS 的分離、音高、歌詞路徑。契約測試會把 worker 換成假的，不能證明這些依賴還能用。

### 3.3 每個 commit 的 review 步驟
1. 看 `git show <commit>`，確認它想解決什麼問題。
2. 在 `refactor` 上確認問題還在：用 `git grep`，或寫一個會失敗的測試。
3. 評估有沒有比舊 commit 更好的做法，判斷依據見 3.4。
4. 判定是搬、重做、併入、不搬、不做、延後或待查，把結果填回本文件第 2 節。如果改用別的做法，在備註寫「改用：……，原因：……」。
5. 判定要搬或重做的：開分支，實作加測試，`pytest` 過了才發 PR；`ruff` 在 E 組清完違規後才要求（3.1）。
6. commit message 用繁體中文。

### 3.4 評估更好的做法
舊 commit 是在 2.0.0 上、還不知道後來會發生什麼的情況下寫的。現在多了兩樣東西可以參考：上游 2.1–2.3.2 的做法，以及後來的審查修正。review 每個 commit 時，依序問下面四個問題：

1. **上游有沒有已經解決了？** 有的話優先沿用上游的寫法，只補不足的地方。這樣跟上游的差異小，以後同步比較容易。
2. **後來有沒有修正這個 commit？** 舊分支有不少審查修正是在修重構時產生的問題，例如 28 的並行和輸入驗證、36。重做時要一開始就寫對，不要先重現問題再修。
3. **以後同步上游要花多少成本？** 搬動或改名越多，每次上游更新時要對應的地方就越多。如果效益不夠大，就不要搬動。
4. **有沒有更小的改法？** 優先用現成的程式、標準函式庫、已安裝的套件，不加沒人要求的抽象。

評估結果有三種，寫在第 2 節的備註：
- **照舊**：舊做法仍然最好。
- **改用**：寫明新做法和原因。
- **不做**：目的已經不成立，或同步上游的成本大於效益（3.4 第 3 點），寫明原因。

如果評估結果改變了某一步的範圍（例如步驟 3 不做了），先更新 3.2 的表格，再開始實作。

### 3.5 目前已知要評估的替代做法
| 範圍 | 舊做法 | 候選做法 | 要比較的地方 |
|---|---|---|---|
| 步驟 3 ml-worker | 自己寫 `ml_worker/` 和 `leihu/inference.py`，上游的 `gpu_worker.py`（670 行）整個不保留 | 沿用上游的 `gpu_worker.py` | **已評估，改用候選做法**，見 3.6 |
| 步驟 0 M1、M2 | 自己加 `before_request` | 沿用上游 `_local_only`（`app.py:131`），只補不足的地方（例如比對 port；安全修正，這個版本做，3.1） | 回歸測試照樣加 |
| 步驟 1 Blueprint | `app.py`（2.3.2 是 2164 行）整個拆成 `leihu/web/*` | 先只拆出新功能需要的部分，或依路由群組分批拆 | **已評估：整個一次拆，已完成**，見 3.19 |
| 步驟 4 拆模組 | `pipeline.py`（2.3.2 是 4801 行）拆成 config、dsp、media、lyrics、llm、render | 依上游最常改的區塊決定先拆哪裡，或保留 `pipeline.py` 當相容層 | **已評估，改用候選做法（只拆穩定區塊，`pipeline.py` 轉出名稱）**，見 3.19 |
| 步驟 0 S28 環境變數 | 自訂覆寫順序 | 配合 2.3.2 的 `library_dir`、`models_dir` 重新設計 | 不能跟上游的搬移功能衝突 |

### 3.6 步驟 3 評估結果（2026-11-21）
**結論：改用上游的 `gpu_worker.py`，不做 ml-worker。** 舊步驟 3 裡跟 ml-worker 無關的部分照原計畫評估。

**兩邊的做法：**
- **上游 `gpu_worker.py`（2.2.0 起）**
  - 子程式執行 `gpu_worker.serve()`，裡面 `import pipeline`、設 `IN_WORKER=True`，直接呼叫 pipeline 的函式。
  - 用 `multiprocessing.connection` 加隨機 authkey 溝通。
  - 有兩個池：`POOL` 用顯示卡，`CPU_POOL` 只用 CPU。顯示卡記憶體不到 10 GB 時用 `_GPU_GATE` 讓一次只跑一個顯示卡工作。
  - 卡住時丟 `GpuStalled`，閒置 30 秒結束，用 Windows Job Object 讓子程式跟著主程式結束。
  - 子程式開不起來時，改在主程式裡做（`_local`），接下來 10 分鐘都不開子程式。
- **舊分支的 ml-worker（`ml_worker/main.py` 410 行，加上 `leihu/inference.py` 584 行）**
  - 子程式不 import pipeline，用 stdin／stdout 一行一個 JSON 溝通，大陣列用暫存 `.npy` 傳。
  - 有 `Inference` Protocol 和假 worker，不裝 torch 也能測。
  - 沒有在主程式裡做的備援：伺服器永遠不載入 torch。

**改用上游的理由：**
1. **上游已經在 2.3.2 裡了，不用搬。** `gpu_worker.py` 在 2.2.0 新增（`2f5cbbb`），之後改過 7 次，其中 3 次在 2.3.x（`7133978`、`4184304`、`7fc9a0b`），是上游還在積極改的地方。舊做法每次都要重新移植進 ml-worker，Codex 也確實在移植中找到問題（`dcd5ce4` 修的三個問題裡，兩個在 WorkerPool 和 `cuda_ok`）。
2. **上游有在 NVIDIA 顯示卡上實際用過，舊 ml-worker 沒有。** 舊分支的 `docs/improvements.md` 寫明「ml-worker 用顯示卡」只能在有 NVIDIA 顯示卡的電腦上測，開發機沒有顯示卡，所以從來沒測過。
3. **省記憶體的效益上游已經有了。** `improvements.md` 的 R4 數字（處理完伺服器從 819／1108 MB 降到 56／137 MB）是跟上游 **v2.1.0** 比的，那時上游還沒有 `gpu_worker`。上游 2.2.0 起推論也在子程式裡跑，ml-worker 相對 2.3.2 還剩多少差距沒有量過。

**改用上游會失去什麼、怎麼補：**

| 舊 ml-worker 有的 | 怎麼處理 |
|---|---|
| 不裝 torch 也能測（Protocol 加假 worker） | 測試用 monkeypatch 換掉 `gpu_worker.call`、`gpu_worker.POOL`、`gpu_worker.CPU_POOL`（`pipeline.py:177` 的 `POOL.call("probe")`、`pipeline.py:3289` 的 `POOL.count()`；`app.py:1430`、`app.py:1450` 的 `POOL.shutdown()`、`CPU_POOL.shutdown()`），不需要另外的介面 |
| 子程式不 import pipeline，啟動比較輕 | 子程式的時間和記憶體主要花在 torch 和模型上，差距沒有量過；需要時再量（`measure_r4.py` 不搬，3.19） |
| 伺服器永遠不載入 torch | 上游只有在子程式開不起來時才在主程式裡做。**要決定**：保留上游的備援（不用改，子程式壞了還能處理歌曲），還是拿掉（遵守舊分支 `docs/upstream-sync-2.2.0.md` 第 2 節「伺服器不載入 torch」，但子程式壞了就完全不能處理） |
| 長音訊的旋律分析逾時依音訊長度放寬（`leihu/inference.py` 的 `max(STALL["crepe"], 3 * 音訊秒數)`） | 上游 GPU、CPU worker 都固定 600 秒（`gpu_worker.py:524` 的 `STALL.get(name, 600.0)`），長歌在 CPU 上可能逾時失敗。在 `gpu_worker` 加這項保護會改變行為，延後（3.1）；不用搬回 ml-worker |
| 歌詞辨識拆成三個呼叫、同一個 worker 做完 | 上游是一個工作做完三步，結果一樣，不搬 |
| JSON 協定（不用 pickle） | 兩邊都只在本機溝通，上游有隨機 authkey，不搬 |

**在上游 `gpu_worker.py` 上要補的修正（延後，3.1 的原則）：**
- **`_set_priority` 的優先順序從來沒生效**（舊分支 `1b5074b`）。上游 `gpu_worker.py:42` 一樣沒有設 `GetCurrentProcess` 的 `restype` 和 `SetPriorityClass` 的 `argtypes`，64 位元 Windows 上 `SetPriorityClass` 會失敗（回傳 0，不丟例外），上游沒有檢查回傳值，所以完全沒有訊息。直播中的低優先順序在上游也沒有生效。修了會讓直播中的低優先順序真的生效，屬於改變行為，這個版本不修。以後要修時，修法照 `1b5074b` 搬，放在 `gpu_worker.py`。回歸測試也照搬，但上游沒有失敗訊息，只保留「子行程讀回 `GetPriorityClass` 是 `0x4000`、`0x20`」的斷言，載入的檔案改成 `gpu_worker.py`、呼叫 `_set_priority`。
- `dcd5ce4` 的 GpuStalled 放鎖順序，commit 說明寫「和上游 `gpu_worker.call` 一致」，所以上游不用修。`cuda_ok` 交握卡住是 ml-worker 才有的問題，上游 `track_pitch` 判斷用 CPU 時不會呼叫 `cuda_ok`，不用處理。
- 36 的混合精度 OOM：commit 說明寫「上游 2.2.0 漏移植」，上游 `pipeline.py:2069` 遇到 out of memory 不標壞、交給外層退回標準模型，預計不搬，review 時確認。

**連帶影響：**
- F 組：46 延後，以後改修在 `gpu_worker._set_priority`；68 的 WorkerPool 和 `cuda_ok` 兩項不做；68 的 leadfix 互斥照原計畫查，上游沒有的話延後（3.1）。
- 舊步驟 3 的其他部分照原計畫逐項評估：數值比對與量測工具（`tests/numeric/`、`tests/test_numeric.py`，D 組要用；評估結果：`measure_r4.py` 不搬，`child_hook.py` 取代 `worker_nodither.py`，見 3.19）、`LIANTAI_URL_BASE`、背景工作契約快照、伺服器不載入 librosa／scipy／numba（`leihu/dsp.py`、soxr）。其中 dsp 改寫會跟上游 pipeline 的改動衝突，要用 3.4 第 3 點判斷；改寫若讓輸出數值改變，就是效能調整，延後。
- 刻意不移植清單裡的上游 `_local_only` 和「2.2.0 `gpu_worker` 機制的 7 個名稱」都不再成立，比對工具（`tests/contract/compare_upstream.py`）的登記要拿掉。
- 3.6 推翻了舊分支 2026-11-17 的決定（`docs/upstream-sync-2.2.0.md` 第 2 節：保留 ml-worker）。

**沒有驗證的：**
- 兩種做法的啟動時間和記憶體都沒有實際量過。
- 上游 `gpu_worker` 在顯示卡上的表現，只根據上游 commit 說明裡的實測描述。
- `_set_priority` 的 bug 只看了程式碼，還沒在 `v2.3.2` 上用測試重現。

### 3.7 安全網：用舊分支的上游比對工具，不重做行為比對計畫（2026-11-21 評估）
舊的「原版與重構版行為比對計畫」（分支 `claude/original-refactor-behavior-comparison-d469de` 的 `30a4774`，`docs/behavior-comparison-plan.md`，基準 2.0.0）不改成 2.3.2 基準再做一次。原因是它的核心已經在舊分支實作，而且對 2.3.2 跑過：

- 計畫的主體是用外掛 shim 幫原版補測試接縫。舊分支的 `tests/contract/serve_upstream.py`（92 行）就是這個 shim：擋對外連線、佇列只排隊不執行、關掉啟動時的背景執行緒、開外部程式改寫進 `spawn.log`、下載資料夾導到 `LIANTAI_DOWNLOADS`、直播偵測固定回傳沒有。
- 舊分支 `docs/upstream-sync-2.3.2.md` 5.4 節：`compare_upstream.py v2.3.2` 跑了 141 個契約案例，117 個相同，24 個有差異且都已登記（S15、D1、D2/D4），沒有未預期的差異。
- 2.3.2 仍然只有 `LIANTAI_PORT`，沒有其他測試接縫，所以 shim 仍然必要。

做法：
1. 搬 `tests/contract/`：`test_contract.py`（`CASES`、`Normalizer`、`run_contract`）、`fixture_library.py`、`netblock.py`、`serve_upstream.py`、`compare_upstream.py`、`serve.py`（步驟 1 之後才用；`--fake` 段依賴假推論，先拿掉）、`snapshots/contract.json`（在原版上重錄）。`contract_jobs.json`、`test_contract_jobs.py`、`fake_net.py` 依賴假推論，先不搬，等 3.6 的背景工作契約快照評估。`run_contract` 的環境要排除 `ANTHROPIC_API_KEY`（2.3.2 的 `app.py:89`、`app.py:181` 會讀它，開發機有設就會換一台機器出現差異）。
2. 基準就是 `compare_upstream.py v2.3.2` 現場在原版上跑出來的結果，不另存；`contract.json` 是「目前這一邊」的紀錄。
   - 舊分支沒有指令能用 `serve_upstream.py` 錄 `contract.json`：`LIANTAI_SNAPSHOT_UPDATE=1` 只有 `test_contract.py` 的 `compare_snapshot` 會讀，而它只在 `test_contract_snapshot` 裡呼叫，那個測試用 `serve.py` 加 `conftest` 的 S28 環境變數。
   - 所以 D 組要在 `compare_upstream.py` 補 `--record`：匯出指定的 git 參照，用 `serve_upstream.py` 跑 `CASES`，在 `LIANTAI_SNAPSHOT_UPDATE=1` 下呼叫 `compare_snapshot(result, SNAPSHOT)` 寫出 `contract.json`。
   - D 組完成時跑 `compare_upstream.py v2.3.2 --record` 錄一次，這時兩邊相同。舊的 `contract.json` 是重構版的輸出，不能沿用。
   - `RULES` 要先清成空 list：舊的 S15、D1、R2、D2/D4 是 2.0.0 重構版的差異，其中 D1 用案例名稱的字首整個放行，留著會讓未登記的差異被當成預期。
   - 之後 C 組、E 組除了安全修正都不改行為（3.1）；出現差異時（例如安全修正、沙盒要替換的名稱變了），先在 `RULES` 登記並寫原因，再更新快照。除了第一次建立基準，更新都要先寫出候選結果，確認案例集合一致，並和現場跑的 `v2.3.2` 比對；所有差異都符合 `RULES` 才覆寫 `contract.json`，否則 exit 1、保留原快照。
3. 步驟 1 之前，`refactor` 這邊用 `compare_upstream.py <已 commit 的參照> --record`，以 `serve_upstream.py` 執行匯出的程式，兩邊只要一個啟動器。步驟 1 之後改用 `serve.py` 比對目前工作樹（內嵌 Python 跑 `tests/contract/check_snapshot.py`，見 3.12），更新快照的指令到時再補，不再用 `--record`；`serve.py` 的 `ROOT` 固定是 repo 本身，如果要錄重構後的任意參照，要先讓它接受程式碼資料夾參數。上一點「先比對、全部登記才覆寫」的規則兩種錄法都要做到。`test_contract.py` 同時是工具庫和唯一的 pytest 測試，所以 B 組讓 CI 不收集 `test_contract_snapshot`（3.11 的 `addopts`）；S28 完成後仍不收集，原因見 3.12。
4. 步驟 1 會把 `prefetcher`、`lead_fix_sweep` 等背景函式從 `app` 搬走，`serve_upstream.py` 的 `hasattr(app, name)` 找不到就靜默略過，背景執行緒會真的啟動。所以 S28 要在步驟 1 之前完成，之後 `refactor` 這邊改用 `serve.py`。（步驟 1 實際完成時，`app.py` 仍轉出這些名稱，`serve_upstream.py` 仍然有效，見 3.19）

從行為比對計畫補過來的做法：
- 穩定性檢查：每個版本先自己跑兩次，兩次 `run_contract` 的結果要完全相同。
- `spawn.log` 只記這個請求新增的行，不累積整份內容。
- 路由涵蓋：舊分支已有 `endpoint` 加方法的檢查（`test_contract.py` 的 `test_contract_snapshot`），但只查重構版。補的是上游這一側：`serve_upstream.py` 在 `app.main()` 之前把 `app.app.url_map`（Flask 物件）寫進 `<DATA>/routes.json`，兩邊改成比對「路由規則字串加 HTTP 方法」，都排除 `/static/<path:filename>` 和 `/api/debug/stacks`。上游的 `@app.get`、`@app.post` 等裝飾器在 2.0.0 有 78 個、2.3.2 有 93 個，扣掉 `/api/debug/stacks` 是 77 和 92 條，每條都要有案例。
- 不直接覆寫快照：先登記，才能更新。舊分支的 `compare_upstream.py` 已經對未登記差異 exit 1，但 `LIANTAI_SNAPSHOT_UPDATE=1` 會直接覆寫、不先分類差異；補的是「先寫候選結果、比對通過才覆寫」（上面第 2 點）。

shim 要補的地方：
- 2.3.2 的 `main()` 會啟動 `lead_fix_sweep` 和 `P.clear_false_recheck`，`serve_upstream.py` 沒有關掉。
- `gpu_info(block=False)`（`pipeline.py:170`）在裝了 torch、`models/gpu_probe.json` 不存在時，會在背景經 `pipeline.py:177` 開 `gpu_worker`，要換掉 `gpu_worker.POOL`（3.6）。
- `pipeline.py:3285` 的 `_sample_gpu_load` 會跑 `nvidia-smi`，要換成 `lambda: None`（舊分支 `serve.py:70` 已有）。
- 這幾項會不會讓結果每次不一樣，用穩定性檢查確認。

測不到的部分：
- 背景工作跑完的結果：上游沒有可以替換的假推論。改用 `gpu_worker` 後可以替換 `gpu_worker.call`，有機會補上。
- 前端：契約快照不看頁面內容。
- 顯示卡路徑：開發機沒有 NVIDIA 顯示卡。
- 數值比對：舊分支沒有在 2.3.2 上跑過，要有模型和素材的電腦。

### 3.8 三種基準與兩個階段（2026-11-21 決定）
**原則：先證明功能和上游沒有差異，再調整效能。** 調整效能（換模型精度、平行處理、改演算法）一定會讓輸出不同，最多只能做到相似。兩者混在一起的話，看到差異時分不出是 bug 還是效能調整造成的。

- **第一階段（行為等價）**：B、A、D 組，C 組不改行為的部分和安全修正（3.1），E 組。只允許 `RULES` 登記過的差異，不做任何會改變輸出數值的效能調整。
- **第二階段（效能調整）**：延到後面的版本（3.1），第一階段完成後才開始。每項調整都要對照效能基準和相似度基準，寫明快了多少、輸出差多少。

工作分支：`test/upstream-baseline`（從 `refactor` 開），先放 D 組的測試和基準，暫不發 PR。

**三種基準，都在原版 `v2.3.2` 上錄：**

| 基準 | 內容 | 比對方式 | 用在哪個階段 |
|---|---|---|---|
| 功能基準 | 契約快照（3.7）；數值參考輸出：分離三軌、`pitch.json`、評分 JSON、歌詞、`export.wav` | 契約快照：只允許已登記的差異。數值：CPU 路徑、固定執行緒數、關掉 dither，逐位元組相同（`tests/numeric/baseline.py`）；另外記錄和合成素材標準答案的差距 | 第一階段 |
| 效能基準 | 每個步驟（分離、音高、歌詞辨識、移調）的時間；伺服器和 worker 的尖峰記憶體、閒置記憶體；顯示卡記憶體尖峰；啟動時間 | 同一台電腦、同一組歌曲、同一版套件，每項跑多次取中位數，記下變異 | 第一階段當回歸檢查（不能明顯變慢）；第二階段當改善的依據 |
| 相似度基準 | 輸出之間的距離：分軌訊號相似度、音高音分差、有聲／無聲一致率、歌詞字錯誤率、評分差 | 先量上游**自己跑多次**的差異當下限，第二階段的門檻不能比這個嚴 | 第二階段 |

**CPU 和顯示卡要分開錄：**
- CPU 路徑可以做到逐位元組相同，是第一階段的主要依據。
- 顯示卡路徑就算是上游自己連跑兩次也不一定逐位元組相同（浮點運算順序、混合精度），所以顯示卡的功能基準要用相似度比對，門檻取上游自己跑多次的差異。
- **比對規則（2026-11-22 決定）：顯示卡對顯示卡、CPU 對 CPU。** 之後每個步驟的結果只和同一台電腦、同一種路徑的基準比（開發機 CPU 對開發機 CPU 基準，筆電顯示卡對筆電顯示卡基準），不拿 CPU 和顯示卡的數字互比，也不拿兩台電腦的時間互比；差異是否太大，只看同一條路徑上和基準的差距。
- 上游 2.3.2 只支援 NVIDIA（CUDA）。這台開發機是 AMD Radeon RX 9070 XT，所有推論都會走 CPU；顯示卡基準在筆電錄（3.10）。

**錄基準的電腦：**

| 用途 | CPU | 顯示卡 | 記憶體 |
|---|---|---|---|
| CPU 基準（開發機） | AMD Ryzen 9 9900X3D（12 核） | AMD Radeon RX 9070 XT（上游不支援，推論走 CPU） | 31 GB |

**每份基準都要記錄：** 程式的 commit、測試工具的 commit、電腦規格（CPU、記憶體、顯示卡、驅動）、Python 和主要套件版本（torch、onnxruntime、faster-whisper、audio-separator）、模型檔的 sha256、歌曲清單和 sha256、執行次數。基準結果和本機素材放 `tests/private/`，不進 git；git 裡只放工具、歌曲清單和錄製方法。

**記錄規則：執行條件和平常不同的地方，一律寫進輸出紀錄。** 例如顯示卡探測失敗、改用 CPU、改在主程式裡做、退回標準模型、記憶體不足、逾時、沙盒替換了什麼、哪些替換目標找不到、對外連線、兩次結果不同。沒有就寫「無」，不能省略，否則數字會被誤解。

**測試音檔分三層：**

| 層 | 來源 | 放哪裡 | 用在 |
|---|---|---|---|
| 1 合成 | `tests/numeric/make_inputs.py`（從舊分支搬來） 用程式產生：已知旋律的合成人聲加和弦伴奏、偏高 30 音分且晚 0.2 秒的錄音、Windows 語音合成唸的中文句子 | git 只放產生程式，產生的檔案放 `tests/private/` | 功能基準的數值比對（逐位元組）；音高有標準答案 |
| 2 真實歌曲 | 自己擁有的音檔：自己錄的歌、自己買的音樂轉檔 | 只放 `tests/private/songs/`，**不進 git** | 效能基準、相似度基準（真實長度、合音、對唱） |
| 3 開放授權（可選） | 允許再散布的有人聲歌曲（CC0、CC BY；CC BY-NC 只限非商業） | 不放音檔，git 放下載網址、授權、sha256 | 讓別人也能重現第 2 層的結果 |

- 測試計畫（格式見 `tests/numeric/plan.py`，2026-11-22）：每種基準（數值、效能、相似度）跑幾次、跑哪幾首真實歌曲。歌曲用路徑（相對於計畫檔）和 sha256 指定，工具不掃描資料夾；編號同時是被測程式裡的歌曲編號和輸出資料夾名稱，要符合 `[a-z0-9]{6,16}`。計畫檔列的是自己的歌，放 `tests/private/plans/`，**不進 git**；git 裡的摘要只留合成素材的結果、計畫名稱、次數和真實歌曲的首數；逐首結果、編號、sha256 只在 `tests/private/baselines/` 的完整版（`write_summary` 分兩份寫）。
  - `refactor.json`：E 組每一步用。數值挑少數幾首會走不同程式路徑的歌跑 1 次，效能挑 2 首跑 3 次。
  - `full.json`：全部的真實歌曲，數值、效能、相似度各 3 次；E 組全部完成後、發版前跑。
- 每次錄基準都核對 sha256（`plan.load` 不符就不開始），兩台電腦必須用同一批檔案。
- 從 YouTube 下載的歌只能自己留著測試，不能放進 repo 或分享。

### 3.9 D 組進度（`test/upstream-baseline`）
- **契約基準已錄**（`72aee1d`）：`compare_upstream.py v2.3.2 --record --twice`，141 個案例兩次完全相同，92 條路由都有案例，異常「無」。快照在 `tests/contract/snapshots/contract.json`，報告在 `tests/private/upstream/v2.3.2/contract-report.md`。
- **安全網驗證**：把原版的 `/api/categories` 故意改成多回一個分類，用 `--code=` 比對，抓到 1 個未預期差異，exit 1。
- 契約快照固定「沒有可用的顯示卡」，所以只代表 CPU 這一側的 API 行為；顯示卡的差異由數值和效能基準處理。
- 工具的改動：`RULES`、`NOT_PORTED`、`OWN_CHANGES` 清空；`--record`（先比對、全部登記才寫入）、`--twice`；報告一定有「異常」「案例本身造成的訊息」「沙盒替換」三節；沙盒補關 `lead_fix_sweep`、`clear_false_recheck`、`_sample_gpu_load`，固定顯示卡探測結果，找不到的替換目標會列為異常；寫出路由清單檢查涵蓋率；`spawn.log` 只記每個請求新增的行；`run_contract` 排除 `ANTHROPIC_API_KEY`。`serve.py` 要 S28 才能用，先不搬。舊的 `--numeric`（依賴 2.0.0 重構版的 ml-worker 和 `tests/test_numeric.py`）拿掉，改用 `tests/numeric/baseline.py`。
- 用安裝版的內嵌 Python（`%LOCALAPPDATA%\LeihuLiantai\python\python.exe`，`PYTHONUTF8=1`）執行；它的 `._pth` 不會自動加入腳本所在資料夾，`serve_upstream.py` 已自己加。
- **數值基準（CPU，開發機）已錄**：`tests/numeric/baseline.py v2.3.2 --label=desktop-9900x3d-cpu`，測試工具 `790ec0a`。合成素材跑兩次，比對的 23 項全部逐位元組相同（SDR 為 inf、差距 0），異常「無」。摘要在 `tests/numeric/baselines/v2.3.2-desktop-9900x3d-cpu.json`，報告和輸出在 `tests/private/upstream/v2.3.2/numeric/desktop-9900x3d-cpu/`。
  - 工具：`make_inputs.py`（合成素材；沒有 zh-TW 語音就失敗；寫 `manifest.json` 的 sha256）、`run.py`（處理一次）、`compare.py`（兩次輸出的差距，門檻沿用舊分支；和標準答案的差距）、`baseline.py`（匯出版本、跑 N 次、寫報告與摘要）。
  - 配合 `gpu_worker` 的做法：程式碼複製到暫存資料夾當 ROOT，`ROOT/models/uvr`、`whisper` 用 junction 指到 `tests/private/models`；子程式的啟動命令前面插入 `sys.path`（**內嵌 Python 的 `._pth` 含 `..`，不插的話子程式會 import 已安裝的 2.1.0**）、netblock、關掉 dither，並印出載入的 `gpu_worker` 路徑，報告檢查它在暫存資料夾裡。
  - 和標準答案比（CPU，第 1、2 次相同）：lead SDR 11.5 dB、inst 8.1 dB、合音軌（應該是空的）-32.8 dBFS；音高誤差中位數 6.0 cents（torchcrepe tiny）、音符 25 個（旋律 20 個音）；評分偵測到偏差 +21.1 cents（答案 +30）、offset -0.21 秒（錄音晚 0.2 秒）；語音歌詞字元錯誤率 0.000。
  - Whisper 在 CPU 上把 float16 模型自動轉成 float32（ctranslate2 的 warning），列在報告「實際使用的裝置與模型」。
  - 比對工具自我檢查：把一份輸出的音高調高 20 cents、改一個歌詞字，兩項都超過門檻，exit 1。
  - 這裡的處理時間是單執行緒（為了可重現），不是效能基準。
- **效能基準（CPU，開發機）已錄**：`tests/perf/baseline.py v2.3.2 --label=desktop-9900x3d-cpu`，測試工具 `511f1b0`，合成歌（`sing.flac` 接 7 次，217 秒）跑 3 次，異常「無」。摘要在 `tests/perf/baselines/v2.3.2-desktop-9900x3d-cpu.json`，報告、log、取樣在 `tests/private/upstream/v2.3.2/perf/desktop-9900x3d-cpu/`。
  - 工具：`tests/numeric/sandbox.py`（數值和效能共用的執行環境，從 `run.py` 抽出；抽完重跑數值結果和 `790ec0a` 逐位元組相同）、`tests/perf/run_perf.py`（一首歌從匯入到匯出，記各階段起訖）、`tests/perf/baseline.py`（每首跑 N 次，每 0.25 秒取樣主程式和子程式的記憶體，有 nvidia-smi 時記顯示卡記憶體）。
  - 和平常使用不同的地方（報告逐項列出）：歌詞用語音辨識（平常預設上網搜尋）；模型資料夾沒有高品質主唱分離模型，顯示卡上 `auto` 會改用標準模型；評分的錄音是歌曲自己分出來的主唱；直播偵測、nvidia-smi 取樣、下載都關掉。
  - 結果（中位數，秒；記憶體是主程式＋子程式的 private bytes 尖峰）：匯入 2.6、分離 85.7（子程式裡人聲 49.0＋主唱 32.5）、音高 7.4、歌詞 15.3、移調 +2 7.9、評分 13.4、匯出 1.3；記憶體尖峰在分離 8420 MB，閒置 1722 MB（子程式閒置 30 秒後結束）。3 次的差距：分離 85.6～86.5、歌詞 14.0～15.5。
  - 合成歌沒有歌詞，語音辨識的時間不具代表性；啟動時間只量 import（pipeline、share 0.14 秒，app 0.19 秒），沒有量伺服器開好要多久。
- **相似度基準（CPU，開發機）已錄**：`tests/numeric/similarity.py v2.3.2 --label=desktop-9900x3d-cpu`，測試工具 `5465c83`，平常條件（`run.py --normal`：執行緒數照平常、torchcrepe 抖動開著、標準模型）跑 3 次、兩兩比較，異常「無」。摘要在 `tests/numeric/baselines/v2.3.2-desktop-9900x3d-cpu-similarity.json`，報告在 `tests/private/upstream/v2.3.2/similarity/desktop-9900x3d-cpu/`。
  - 每次都相同：分離三軌（SDR inf）、有聲判斷、合成旋律的音符、歌詞。
  - 平常就會變（第二階段門檻不能比這個嚴）：音高差中位數 8 cents（抖動造成）；語音那首音符數差 3～4 個（3 組有 2 組超過數值基準的 max(1, 2%)）；評分準確率差最多 0.2 分；評分的 `key_hint` 裡 `share` 差 0.01～0.02（`shift`、`suggest`、`octave` 相同，但整個 dict 比較所以 3 組都算不同）。
  - 匯出檔：這次 3 次的評分 offset 都是 -0.2 秒，匯出檔逐位元組相同；前一次試錄有一次是 -0.19 秒，匯出檔 SDR 只剩 -2 dB 但 RMS 只差 0.002 dB。所以匯出檔用 SDR 比會被 0.01 秒的對齊差異放大，第二階段要改比響度或先對齊。
  - 真實歌曲：`tests/private/songs/fetch.py`（只在本機，不進 git）把 `urls.txt` 的 YouTube 歌下載成 `<影片 id>.flac`，`index.json` 記 id 和歌名的對照；要跑哪幾首寫在本機的測試計畫（3.8），工具不再自動掃描這個資料夾。
- **Codex CLI 審查後的修正與重錄（2026-11-21）**：審查報告在 `tests/private/reviews/codex-test-review-2026-11-21.md`（不進 git），22 項裡修了第一、二批 18 項（`a80f3c5`、`1493613`、`33bca48`），上面各基準的數字以這次重錄為準：
  - 會假通過的地方：數值基準改成逐檔核對（`compare.exact`：音檔比格式與每個取樣，JSON 連型別比並排除 `combo_lufs_sig`，其他比位元組，檔案清單要相同），第 2 次起都和第 1 次比；音訊比對不再截齊長度、忽略取樣率；補比 pitch `hop`、音符結束與音高、評分 `mean`／`spread`／`offset`／逐句，`key_hint` 拆欄位比；契約比對連型別（`True` 和 `1` 不同）。
  - 報告：「改用標準模型」和被測程式 `logs/` 的錯誤列為異常；每一步實際用的裝置由子程式回報（`tests/numeric/child_hook.py` 的 `[device]`），不再從顯示卡探測推定；子程式的 `pipeline` 來源也回報並檢查。
  - 契約快照格式改變（用 `--rebase` 重建）：只有執行當下 3 天內的數字才當動態時間（fixture 固定時間保留），音檔記 RMS、峰值與 16 段 RMS，圖片記像素雜湊，其他二進位記 sha256。和舊快照差的 29 個案例全部是這些格式改變。
  - 其他：`export()` 每次從 git 重新匯出並記完整 commit；模型記 sha256；沙盒不複製 `config.json`，檢查 `pipeline`、`share`、`app` 從沙盒載入；`--songs` 沒有音檔時失敗；執行次數下限；效能每次清空輸出、只統計成功的幾次；`tools_dirty()` 不把快照、摘要算成工具修改。
- **Codex CLI 第二到第五次審查（2026-11-21）**：報告在 `tests/private/reviews/codex-test-review{2,3,4,5}-2026-11-21.md`（不進 git）。每輪修完都重錄，主要修正：
  - 清理與結束行程：暫存資料夾記 owner（PID＋建立時間），只刪確定沒人用的；`remove_tree` 先只拆 junction、不跟進連結；逾時後確認整棵行程樹都停了才清理，沒確認就寫 `keep.json`、停止後面的執行，下一次錄製看到行程還在就不開始；`export()` 每個行程一個資料夾。
  - 比對：`required()` 檢查必要產物、結構和有限值，壞的那次不拿來比；音符一對一配對；評分補比 frames、逐句、錄音音高，`key_hint` 可以是 null；靜音、NaN 有明確定義；契約音訊加解碼取樣雜湊；`--rebase` 依完整 commit 判斷。
  - 顯示卡路徑：子程式回報每一步最後的實際裝置（「實際=」，含 ONNX 執行中改用、Whisper 重建 CPU 模型）；數值基準有步驟在顯示卡上時，和同一台電腦用 `similarity.py --fixed` 錄的下限比，並核對條件、套件、模型、素材、真實歌曲；`compare.py --similar` 一定要給 `--floor`。
  - 數值基準實際抓到的不穩定：Whisper 對沒有人聲的合成旋律做溫度取樣重試時用了 CTranslate2 亂數，兩次 `lyrics_quality` 不同；固定條件下改成 `set_random_seed(0)`。
  - 刻意沒修的小問題：`user_leaks()` 會誤擋 URL 裡的 `/users/<名稱>`（N42）；其他列在第五次報告的小問題清單（都要特定的失敗組合才會發生）。
- **目前的開發機 CPU 基準（測試工具 `fe9431f`，契約快照用 `--rebase` 重建，異常都是「無」）**：
  - 契約：141 個案例兩次相同。
  - 數值：跑 3 次，第 2、3 次各 14／14 個檔案和第 1 次完全相同；子程式回報每一步實際都在 CPU（MDX 兩個 ONNX session、Whisper VAD 都是 `CPUExecutionProvider`，torchcrepe tiny，Whisper float32）。
  - 效能（中位數，秒／private 尖峰 MiB）：匯入 2.6／1994、分離 87.2／8397、音高 7.4／3990、歌詞 15.5／7383、移調 8.3／6054、評分 13.4／4178、匯出 1.3／4838、閒置 1720 MiB。
  - 相似度（平常條件 3 次）：語音那首音符數差 2～3 個、沒配對到的音符 5～6 個、音高不同 5～6 個；匯出檔 SDR 在評分 offset 差 0.01 秒時約 -2 dB（3 組有 2 組）。
- **E 組開始前的基準（2026-11-22，測試計畫 `refactor`，3.8）**：測試工具 `8bbed4c`（refactor 改寫後是 `7690122`，`tests/` 內容相同）。
  - 錄的時候 refactor 的歷史還沒改寫：重構起點 `23925a7` 就是改寫後的 `f3bf3df`，程式完全相同，只差 `tests/` 裡一份相似度摘要（沙盒不複製 `tests/`），摘要裡記的是改寫前的雜湊。
  - 契約：`compare_upstream.py 23925a7 --twice`，141 個案例兩次相同，和 `v2.3.2` 沒有差異，異常「無」。
  - 數值：`v2.3.2`、`23925a7` 各跑 1 次（合成素材加計畫裡的真實歌曲），異常都是「無」；`23925a7` 用 `--against=v2.3.2` 逐檔核對，38／38 個檔案完全相同。C 組的安全修正沒有改變數值輸出，不用登記 `RULES`。
  - 效能（各 3 次中位數，秒）：合成歌 `v2.3.2`／`23925a7`：匯入 3.1／2.9、分離 89.9／88.2、音高 7.5／7.8、歌詞 15.5／15.3、移調 9.1／8.4、評分 13.5／13.6、匯出 1.4／1.4，合計 139.9／137.7（-1.6%）。真實歌曲 2 首合計 -2.7%、-2.5%；個別小階段（移調、評分）差到 ±10%，但 3 次的範圍重疊；記憶體尖峰差 0.5% 以內，只有合成歌的匯入階段多 7%（1994→2140 MiB）。結論：沒有變慢。
  - 先前用 `--songs` 跑全部真實歌曲的數值基準（3 次，150 項逐檔相同）中途有兩筆殘留清理被記成異常，工具修正後改用測試計畫重錄；那份結果只留在本機 `tests/private/baselines/`。
  - 真實歌曲的資料不進 git：git 歷史已經改寫（相似度摘要的逐首資料移除，force push `refactor`、`ci/installer-build`、`perf/desktop-port-check`，刪除已合併的 `test/upstream-baseline`）。PR #1～#3 的 `refs/pull/*/head` 還留著舊 commit，要請 GitHub Support 清除。
  - **E 組開始條件已滿足**（3.1 第 5 點；顯示卡基準 2026-11-22 已在筆電錄，3.10）。
- **還沒做**：`full.json`（全部真實歌曲、各 3 次）在 E 組全部完成後跑。`test_contract_snapshot` 在 uv 環境比不起來（見 3.12），pytest 不收集。

### 3.10 顯示卡基準：開發機不錄，改在筆電錄（2026-11-22）
- 開發機沒有 NVIDIA 顯示卡，所以第一階段的行為等價仍以開發機 CPU 基準為主。顯示卡路徑（`gpu_worker` 的顯示卡分支、CUDA 失敗時的退回、高品質模型 BS-Roformer、顯示卡記憶體不足時的行為）只有下面這份筆電基準，重構時改到這些地方要特別小心。
- 工具支援顯示卡的部分：`--device=cpu|auto`、`similarity.py --fixed`、`child_hook.py` 回報每一步的實際裝置、`tests/private/laptop-gpu.ps1`（筆電上照 CPU 基準的做法錄；腳本和結果都在 `tests/private/`，不進 git）。
- **筆電 GPU 基準（標籤 `laptop-3060-gpu`、測試計畫 `refactor`；`v2.3.2` 和重構起點 `f3bf3df`）**：數值 4 首真實歌曲加合成素材各 1 次，效能 2 首真實歌曲加合成歌各 3 次，測試工具 `9656fc5`。4 份報告在 `tests/private/upstream/<版本>/{numeric,perf}/laptop-3060-gpu-refactor/`。
  - **數值：正常。** 兩版異常都是「無」；分離（兩個 onnx）、torchcrepe（full）、Whisper（float16）都 `實際=cuda`，`silero_vad_v6.onnx` 照上游固定用 CPU；沒有退回 CPU 或改用標準模型的訊息。分離模型：`mdx`（`UVR-MDX-NET-Inst_HQ_3.onnx`）加 `UVR_MDXNET_KARA_2.onnx`。`f3bf3df` 用 `--against=v2.3.2` 逐檔核對 **38／38 個檔案完全相同**（另外自己算雜湊：分軌 flac、`lyrics_auto.lrc`、`pitch.json` 位元組相同；`meta.json` 只差 `combo_lufs_sig`；`export.wav` 只差 PEAK 區塊的 2 個位元組時間戳），所以顯示卡路徑上 C 組安全修正也沒有改變數值輸出。
  - 和標準答案的差距：SDR 11.5／8.1 dB、音高誤差中位數 6.0 cents、語音歌詞字元錯誤率 0.000，和 CPU 相同；不同的是 torchcrepe 用 full（CPU 是 tiny）：音符 24 個（CPU 25）、有聲比例 0.3414（0.3815）、評分偏差 +21.5 cents（+21.1）。
  - **效能：兩版沒有差異。** 兩版的異常、裝置、模型完全相同。`f3bf3df` 相對 `v2.3.2`（中位數）合計：第 1 首 +2.4%（372.0 對 363.1 秒）、第 2 首 +1.1%（495.6 對 490.1 秒）；各階段差 ±1.5% 以內，只有分離慢 4.0%、2.4%（3 次的範圍不重疊）。兩版程式碼除 `tests/` 外相同，`f3bf3df` 排在後面約 1 小時才錄，判斷是筆電降頻或溫度的噪音（沒有溫度資料，只是推測）。記憶體尖峰（主程式＋子程式 private）6.9 GB 對 6.9 GB，顯示卡記憶體尖峰都是 2718 MiB。
  - **合成歌的效能在顯示卡上錄不到：** 兩版各 3 次都在匯出失敗（`UserError: 這次錄音還沒有評分完成，或沒有偵測到歌聲，無法匯出`，報告列為異常）。原因（從 log 推斷）：效能基準的分離品質是 `auto`，筆電上選了 `bs_roformer` 當主唱模型，把合成音當成非人聲，Whisper 辨識出 0 個字，評分找不到歌聲。CPU 用 `mdx` 時合成歌能跑完。顯示卡的效能基準因此只有真實歌曲 2 首。
  - **不能拿來算「顯示卡加速倍數」（所以之後只做同路徑比對，見 3.8）：** 效能基準的分離模型在顯示卡上是 `bs_roformer`、CPU 是 `mdx`；音高 GPU 是 torchcrepe full、CPU 是 tiny；筆電 CPU（8 執行緒）也比開發機慢。GPU 對 CPU 的各階段倍數（`v2.3.2` 真實歌曲）：匯入 0.5×、分離 0.5×、音高 0.25×、歌詞 3.5～4.9×、移調 0.2×、評分 0.4×、匯出 0.6×、合計 0.5～0.6×；只有同樣是 Whisper 的歌詞階段可比。
  - **跨機器差異（2026-11-22，參考值）**：用 `tests/numeric/compare.py` 的 `pair()` 比開發機 CPU 和筆電 GPU 的 `v2.3.2` 數值輸出（各 1 次，98 項，18 項超過門檻）。門檻是為同機器多次執行設計的，跨機器偏嚴，所以失敗不代表有問題，這些數字只當「換機器本來就會差多少」的參考，不能當同機器的門檻。
    - 分離（兩邊都是 `mdx`）差異很小：三軌 SDR 36～66 dB（門檻 30 dB，合成素材最低 36.4 dB，真實歌曲都在 49 dB 以上）；匯出檔 SDR 49.5 dB、RMS 差 0.001 dB。
    - 差異集中在音高分析（CPU torchcrepe tiny、GPU full）：音高差中位數都是 0.00 cents；音符起點、結束差中位數 10 ms；有聲判斷一致率合成素材 0.982、0.983，真實歌曲 0.920～0.969（門檻 0.98，4 首都不過）；真實歌曲沒有配對到的音符每邊多 33～82 個、音符音高不同 43～52 個（總音符約 360～490 個）；語音合成素材音符數 41 對 43。
    - 歌詞：字元錯誤率 5 首中 4 首 0.000～0.005，1 首真實歌曲 0.020（202 字對 199 字），推測是 Whisper float16 對 float32，沒有逐字比對證實。
    - 沒有顯示卡自己跑多次的下限（`similarity.py --fixed` 沒錄），所以分不出這些差異有多少是顯示卡本身的不確定性。之後的比對只做同路徑（3.8）：看到的差異遠小於上面的數字，才表示程式沒有造成額外差異。
  - Whisper 在顯示卡上（float16）跨次不穩定：第 2 首真實歌曲效能基準的第 3 次兩版都出現「同一句連續重複太多次」（`lyrics_unreliable: true`），其他次正常。兩版相同，不是程式差異；顯示卡的歌詞輸出不能當位元組級基準。數值基準的歌詞行數和 CPU 一致，只有 1 首差 3 個字元。
  - 開發機 CPU 基準裡重構起點的目錄是舊雜湊 `23925a7`（程式和 `f3bf3df` 相同，見 3.9），所以這裡的 CPU 比較只用 `v2.3.2`。顯示卡記憶體不足時的行為和 CUDA 失敗時的退回沒有被這份基準觸發。

### 3.11 B 組執行計畫
接在 `test/upstream-baseline` 上做。一個舊 commit 對一個新 commit，依下面的順序；舊 commit 裡的文件（`docs/step0-plan.md`、`docs/improvements.md`、`docs/ruff-handoff.md`、`docs/HANDOFF.md`）不搬，跟 A、G 組一起處理。

1. **`447489e` 重做**：
   - `pyproject.toml`、`.python-version`、`uv.lock`：`requires-python`、`[tool.uv]`（`package = false`、`default-groups`、只解 `win32`）和 `test` 群組（扣掉推論和視窗套件，版本照 `requirements-win.txt`）照 `447489e`。不要複製 `sync/upstream-2.3.2` 的版本：它多了 2.3.2 用不到的 `soxr`。`uv lock` 重新產生。
   - `[tool.pytest.ini_options]`：`pythonpath = ["."]`、`testpaths`、`markers` 照舊；`addopts = "-m 'not models' --ignore=tests/private --deselect tests/contract/test_contract.py::test_contract_snapshot"`。`tests/private` 會放匯出的程式碼，裡面有它自己的測試。deselect 對不存在的測試沒有作用，D 組有沒有先進來都加；C 組完成 S28 後拿掉。
   - `tests/conftest.py`：只留 `_root_untouched` 和 `app_module`。`_root_untouched` 照 `sync/upstream-2.3.2` 只檢查使用者資料清單（清單換成 2.3.2 實際會寫的檔案），不掃整個 repo：`import pipeline` 沒有系統 ffmpeg 時會把它複製到 `bin/`（`pipeline.py:3869` 在模組載入時執行的 `ffmpeg_path()`），`run_dev.py` 也把資料寫在 repo 裡。不設 `LIANTAI_*`：2.3.2 只讀 `LIANTAI_PORT`（3.7），設了會讓人以為資料有隔離。S28 完成後再補。
   - `tests/test_smoke.py`：`app` 可以載入，`url_map` 扣掉 `static` 是 93 條（含 `/api/debug/stacks`，契約快照的 92 條不含）；加上 `sync/upstream-2.3.2` 的 `test_installer_check_still_works`（`VERSION` 檔等於 `app.VERSION`）。
   - `tests/test_requirements_sync.py` 照搬。`.github/workflows/test.yml` 用 `sync/upstream-2.3.2` 的版本、拿掉 `ruff` job：保留 `permissions: contents: read`、`timeout-minutes: 30`、`push` 只跑 `main`。
2. **`407b2a7` 搬**：只搬 `pyproject.toml` 的 ruff 設定，CI 不跑 ruff。
3. **`cbb0fbf` 搬**：`tests/tools/run_dev.py`（docstring 拿掉 `import leihu` 的說明）；`.gitignore` 加 `.duet_cleaned`（`app.py:1928` 會寫）。
4. **`ebae3ee` 搬**：`installer/build.sh` 的 makensis 加 `-INPUTCHARSET UTF8`。

驗收：
- CI 和本機的 `uv run pytest` 都通過。
- 除了 `tests/` 和設定檔，只改 `installer/build.sh` 的 makensis 參數；被測程式沒變，基準不用重錄。
- 有 makensis 就跑 `sh installer/build.sh` 確認能建立安裝檔；沒有就在 PR 寫明沒驗證。
- PR 寫明 `uvx ruff@0.17.5 check .` 在 2.3.2 的違規數（只記錄；版本和舊分支的 CI 相同）。

不在 B 組做：
- CI 跑 ruff（`cf7a96e`、`c666a91`）：延到 E 組清完違規之後（3.1）。
- CI 跑契約快照（`compare_upstream.py v2.3.2`）：要先確認快照在 CI 的機器上兩次相同（例如音檔的 `pcm_sha256` 可能因 CPU 不同而不同），另外評估。

順帶發現，延到後面的版本（修了會改變行為，見 3.1 的原則）：`pipeline.py` 沒有在模組頂端 `import subprocess`，`pipeline.py:3263`（`_running_apps` 在 Windows API 失敗時改用 `tasklist`）和 `pipeline.py:3294`（`_sample_gpu_load` 跑 `nvidia-smi`）會丟 `NameError`，被 `except Exception` 吞掉：`tasklist` 備援和 `nvidia-smi` 取樣一直沒有作用、也沒有訊息（Windows API 那條直播偵測正常）。契約、數值、效能的沙盒都換掉了這兩個函式，基準測不到。

**進度（2026-11-21）**：接在 `test/upstream-baseline` 上做完，四個 commit 依序對應上面四步（`f8538e4`、`e234dd4`、`5d3db0d`、`5d430b7`）。
- 本機 `uv run pytest`：4 個通過、1 個不收集（`test_contract_snapshot`）。`import app` 沒有寫到使用者資料。
- `uvx ruff@0.17.5 check .`：上游程式（不含 `tests/`）277 個違規，`format --check` 有 10 個檔案要格式化；D 組工具（`tests/`）32 個違規；B 組新增的檔案沒有違規。F821 證實 `pipeline.py` 缺 `import subprocess`。
- CI（2026-11-22，PR lydek/Leihu-singsing#1 → `refactor`）：`windows-latest` 上 `uv run pytest` 通過。第一次跑時 O14 的 PowerShell 測試失敗，修了兩處測試環境的問題：
  - 子程式 Windows PowerShell 5.1 不帶 `PSModulePath`：GitHub Actions 用 PowerShell 7 跑步驟，這個變數指向 7 的模組。
  - PowerShell 用 UTF-8 輸出、Python 用 UTF-8 讀取：CI 的地區編碼不是 UTF-8，中文例外訊息解碼失敗、stdout 變成 `None`。
  - 實際安裝由 NSIS 在一般環境開 `install.ps1`，不受這兩點影響。
- action 升到 Node.js 24 的版本並釘 commit SHA：`actions/checkout` v7.0.1、`astral-sh/setup-uv` v10.4.0；checkout 加 `persist-credentials: false`。升級後 CI 通過、沒有 annotation。
- 沒驗證：這台電腦沒有 makensis，`build.sh` 沒有實際建立安裝檔。

### 3.12 C 組進度：S28（2026-11-21）
- 環境變數：`LIANTAI_DATA`（使用者資料：歌曲庫、`config.json`、`usage.json`、`setlist.json`、`setlists/`、快捷鍵狀態檔、`.appwin`、`.duet_cleaned`、`logs/lyrics.log`、`logs/claude.log`）、`LIANTAI_DOWNLOADS`、`LIANTAI_NO_BACKGROUND`（啟動時的背景工作、背景下載高品質模型、裝套件、升級 yt-dlp、背景探測顯示卡）、`LIANTAI_NO_JOBS`（佇列只排隊）、`LIANTAI_NO_SPAWN`（開瀏覽器、檔案總管、app 視窗、安裝檔改記到 `DATA/spawn.log`，記法和 `serve_upstream.py` 相同）。
- 沒設時和 2.3.2 相同；相對路徑轉成絕對路徑寫回環境變數（`gpu_worker` 子程式繼承），資料夾不存在就建立。模型、`bin/`、安裝位置留在程式資料夾。
- 只給伺服器（`app.py`）的測試用：桌面版（`desktop.py` 只有快捷鍵狀態檔、讀設定跟著 `LIANTAI_DATA`；共用伺服器只比對程式資料夾、不認 `NO_SPAWN`）和安裝程式（讀程式資料夾的 `config.json`）都不支援。S31 依 3.1 延後。
- 測試：`tests/test_env_overrides.py`；`conftest` 改成設這些環境變數、資料放暫存資料夾。`tests/contract/serve.py` 從舊分支搬來（拿掉 `--fake`）。
- 數值、效能沙盒（`tests/numeric/sandbox.py`）原本把 `LIANTAI_DATA` 設成暫存資料夾給 netblock 寫 log，有 S28 的版本會把歌曲庫搬過去；改成清掉所有 `LIANTAI_*`、netblock 改讀 `LEIHU_NET_LOG`。`compare_upstream.py --code=` 不再把 `.venv`、`tests/private` 複製進沙盒。
- `tests/tools/run_dev.py`（B 組）補上：`gpu_worker` 子程式也先把 repo 放到搜尋路徑最前面，不然會載入安裝版。
- 驗證：
  - `uv run pytest`：13 個通過。
  - 內嵌 Python 跑 `tests/contract/check_snapshot.py`（`serve.py`，141 個案例兩次）和 `compare_upstream.py v2.3.2 --code=.`（上游沙盒）都和快照相同，異常 0。
  - ruff：上游程式的違規數不變（277）。
- `test_contract_snapshot` 留在 `addopts` 不收集：快照是用有推論套件的內嵌 Python 錄的，uv 環境的 `/api/status` 的 `modules` 不同。本機改用 `check_snapshot.py`；CI 不跑快照。
  - 數值基準（`7dcfd96`，2 次）：異常「無」，兩次 14／14 個檔案相同；和 `v2.3.2` 的第 1 次逐檔比對（`compare.py`），40 項全部相同。
  - 效能基準（`7dcfd96`，3 次）：異常「無」；中位數分離 89.2 秒（`v2.3.2` 87.2）、其他階段差不到 0.3 秒，記憶體尖峰差不到 1%，在 3 次之間的變異範圍內。
- 沒驗證：`run_dev.py` 的修正沒有實際開過桌面版。

### 3.13 C 組進度：安全與防止資料遺失的修正（2026-11-21）
逐項對照 `e2168c4`（M1–M7）、`dfcb0b9`（下載檢查碼等）和 2.3.2 現況：

| 項目 | 2.3.2 現況 | 處理 |
|---|---|---|
| M1、M2 Host／Origin | `_local_only` 已擋非本機 Host 和外站 Origin（Origin 有比對 port） | 不做：只差 Host 不比對 port，瀏覽器的 Host 標頭不影響連到哪個 port，補了也擋不到新的攻擊 |
| M5 `usage.json` 壞掉被覆寫 | `_load` 吞掉所有例外，`record` 接著覆寫成只剩一筆 | 做：壞掉時改名成 `usage.json.<時間>.bad` 保留；讀不到（`OSError`）時這次不記、不覆寫，Claude 的回覆照常回傳（舊 commit 是往上丟，會讓已計費的回覆丟掉）；`summary` 和原本一樣當成沒有紀錄 |
| M4 匯出檔名被 `with_suffix` 截斷 | 歌名有「.」時，不同錄音、不同歌匯出成同一個檔名、互相覆寫 | 做：`share.out_path`，`app.py` 回傳的檔名清單一起改 |
| O12 模型設定檔 | 高品質主唱模型的 yaml 沒有大小和 SHA-256；三個模型設定 JSON 抓 main 分支 | 做：yaml 釘大小與 SHA-256；JSON 改用固定 commit 的網址並核對 SHA-256。數值都下載或用本機檔案核對過 |
| O13 PyTorch 套件清單沒有檢查碼 | 直接下載、不核對 | 做：清單抓不到或沒有檢查碼就停止；清單有檢查碼、直接下載失敗時，才改走強制核對檢查碼的 pip |
| O14 `install.ps1` 下載 Python | 沒有核對 | 做：核對 SHA-256（下載核對過），不符重下一次，再不符就停止安裝 |

驗證：`tests/test_security.py`；`uv run pytest` 22 個通過；`check_snapshot.py` 141 個案例兩次都和快照相同（契約案例沒有歌名含「.」的匯出，M4 只靠單元測試）。數值基準沒有重跑：這幾項不碰分離、音高、歌詞、評分。

Codex 第 7 次審查後補的（同類問題和繞過路徑）：
- M5：壞檔改名失敗（例如被鎖住）時也保留原檔、這次不記，回覆照常回傳。
- 同類的資料遺失：`config.json`、`setlist.json` 壞掉時讀起來是預設值／空歌單，下一次存檔會蓋掉原本的金鑰與場次。新增 `pipeline.keep_corrupt`：覆寫前內容壞掉（含不是有效的 UTF-8）就改名成 `.bad` 保留，讀不到就不存（回錯誤）；log 只記檔名，不記內容（`config.json` 有 API 金鑰）。`save_config`、目前這一場歌單的寫入都先經過它。
- O12：已經存在的模型設定 JSON、小於 1 MB 的設定檔（yaml）也核對 SHA-256，不符就刪掉重抓（yaml 在 `ensure_model` 的模型鎖裡重抓；狀態輪詢用的 `_files_ready` 只讀、不刪檔）；自己下載失敗時停止這次分離，不再交給 audio-separator 從 main 下載。本機現有的檔案都核對過和釘住的值相同。
- O13：清單抓不到（重試 3 次）或沒有檢查碼時停止安裝 PyTorch，不改走 pip：pip 預設遇到沒有檢查碼的連結也會安裝。清單有檢查碼、直接下載失敗時才改走 pip，並用清單的檢查碼加 `--require-hashes`。
- O14：`Get-FileHash` 改用 `-LiteralPath`（暫存路徑含 `[`、`]` 時不會被當成萬用字元）。下載後直接執行的 `get-pip.py`（上游既有）改抓 pypa/get-pip 固定 commit `c7135ee` 並核對 SHA-256，和當天 `bootstrap.pypa.io` 的檔案相同。它執行時仍會從 PyPI 安裝 pip，所以 pip 版本沒有固定（`b181dd9` 的 commit 訊息寫「pip 26.3」不準確）。
- 測試工具：`check_snapshot.py` 設了 `LIANTAI_SNAPSHOT_UPDATE`／`REBASE` 就拒絕執行（原本會直接覆寫快照），並補上路由涵蓋檢查（和 `test_contract_snapshot` 共用 `uncovered_routes`）；`run_contract` 不帶入 `LEIHU_NET_LOG`，擋下的連線一定寫在沙盒裡。
- 測試：`tests/test_security.py` 補上這幾項的回歸測試，installer 改從暫存副本載入（不再寫到 repo 的 `logs/`）；`test_without_env_still_opens_programs` 改成看有沒有新的 `Jobs._run` 執行緒（CPU 很忙時其他執行緒的增減會讓計數不準）。`uv run pytest` 30 個通過；`check_snapshot.py` 141 個案例兩次相同、92 條路由都有案例。
- Codex 第 8 次審查後再修：上面 O12、O13、`keep_corrupt` 的說明是修正後的版本；O14 補上實際執行 `install.ps1` 下載函式的測試（假的下載、含 `[ ]` 的路徑、檢查碼不符兩次就停止並刪檔）。第 9 次審查後再補：O13 重試次數的測試、`_files_ready` 只 stat 一次（檔案剛好被 `ensure_model` 刪掉時當成還沒好，狀態 API 不會 500）。`uv run pytest` 36 個通過。

其他項目不是安全或資料遺失，依 3.1 延後：M3（installer 的 probe 把路徑嵌進程式碼字串）、M6（旋律譜切點無限迴圈）、M7（錄音刪除後才寫回的 JSON）、S8（yt-dlp 取消與逾時）、O21（installer 整體逾時）、S19（三份 `safe_name`）。

### 3.14 測試版安裝檔（2026-11-21）
- 為了和上游的正式版裝在同一台電腦測試，`installer/build.sh` 加開關：`LEIHU_TEST=1 sh installer/build.sh` 用 `makensis -DTEST_BUILD` 建測試版。不加開關時，安裝檔的名稱、安裝位置、登錄檔和原本完全相同。
- 測試版：名稱「雷虎練唱台 測試版」（桌面捷徑、開始功能表資料夾、應用程式清單都用這個名稱）、安裝位置 `%LOCALAPPDATA%\LeihuLiantaiTest`、登錄檔 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\LeihuLiantaiTest`、輸出檔 `雷虎練唱台測試版2.3.2.exe`。
- 測試版安裝時寫 `.test-install`，程式看到它就不做程式內更新：更新會下載上游正式版的安裝檔裝進測試資料夾，寫正式版的登錄檔與捷徑。解除安裝（保留歌曲庫）時一併刪掉。
- 程式本身原本就以安裝資料夾區分：關閉程式只停自己資料夾的 python、共用伺服器前比對 `root`、8765 被占用時改用別的埠；兩個版本的全域快捷鍵會搶同一組按鍵（後開的略過被占用的組合）。
- 測試：`tests/test_installer_identity.py` 讀 `leihu.nsi` 的定義確認預設值不變、測試版每一項都不同，以及 `.test-install` 關掉程式內更新。
- 沒驗證：這台電腦沒有 makensis，兩種安裝檔都沒有實際建出來、安裝過。

### 3.15 fork 的版號與更新來源（2026-11-22 決定）
這是使用者決定的刻意差異（不是 3.1 的行為等價）：fork 自己發版，程式內更新改查 fork。
- 版號：上游版本加第 4 段 fork 序號，目前 `2.3.2.1`（之後跟上游 2.4.0 就是 `2.4.0.1`）。改了 `VERSION`、`app.VERSION`、`leihu.nsi` 的 `VERSION`／`VIProductVersion`、`pipeline.USER_AGENT`、說明頁與標題列的初始值。
- `app._ver` 改成看 4 段數字：只看前 3 段的話，fork 的新版（`2.3.2.2`）會被當成和 `2.3.2` 一樣、不提示更新。
- 更新來源與連結改成 `lydek/Leihu-singsing`：查最新版本的 API、`ASSET_PREFIX`（只接受 fork 的安裝檔）、`RELEASES_URL`、開啟網址白名單裡的專案網址、前端的更新提示連結。
- 保留原專案網址：`LICENSE.md`、`installer/license.txt`、說明頁的授權說明。授權（附加許可第 2 點）要求散布修改版時註明原作來源並保留授權檔。
- 發版時：tag 用 `v2.3.2.1` 這種格式；安裝檔名稱要以 `-Setup.exe` 結尾（例如 `LeihuLiantai-2.3.2.1-Setup.exe`，`build.sh` 產生的 `雷虎練唱台2.3.2.1.exe` 上傳時改名）；GitHub 會自動提供 SHA-256，程式內更新靠它核對。
- 契約快照：`open_url` 案例送的是上游專案網址，現在不在白名單裡（200 → 400），登記成 `RULES` 的 `F1` 後用 `--record` 重錄。前端與安裝檔的改動登記在 `OWN_CHANGES`（`--leak` 檢查為空）。
- 測試：`tests/test_fork_update.py`（版號、4 段比較、只查 fork 的 release、不接受上游的安裝檔、白名單、授權檔仍指向原專案）。

### 3.16 安裝檔的建置與發版（2026-11-22）
- `.github/workflows/installer.yml`：
  - `refactor` 有新 commit（只改文件時不建）、PR 改到會打包的檔案、或手動觸發：在 `windows-latest` 建正式版和測試版，存成 artifact（保留 30 天）。runner 沒有 NSIS 就用 `choco install nsis` 安裝，版本沒有固定，log 會印出 `makensis -VERSION`。
  - 在 `refactor` 上打 `v<版號>` tag：另外建 draft release，附正式版安裝檔和 `.sha256`，由人確認後才發佈。release job 先檢查 tag 等於 `v` 加上 `VERSION`、tag 指到的 commit 在 `refactor` 上，否則不發。只有這個 job 有 `contents: write`。
  - 正式版改名成 `LeihuLiantai-<版號>-Setup.exe`（程式內更新只接受 `-Setup.exe` 結尾）；測試版叫 `LeihuLiantai-Test-<版號>.exe`，萬一被放進 release，程式內更新也不會選到它。
  - release 說明寫明原作來源（授權要求）。
- `.gitattributes`：`*.sh` 一律 LF。Windows 上 `core.autocrlf` 會把 `build.sh` 轉成 CRLF，`sh` 執行時會因為 `\r` 失敗。
- 本機跑過 `sh installer/build.sh`：複製檔案的步驟正常，停在沒有 makensis；`leihu.nsi` 能不能編譯，要看這個 workflow 第一次跑的結果。

### 3.18 桌面版開啟：找已在跑的練唱台（2026-11-22 決定這版做）
- 上游就有的問題：`desktop.main()` 開伺服器前，依序對 8765～8774 呼叫 `running_instance()`（`/api/status`，逾時 2 秒），最後再查一次第一個埠。Windows 連沒人聽的埠要等到逾時才失敗，沒有練唱台在跑時要 22 秒。
- 改法：`find_instance()` 先同時檢查 10 個埠有沒有程式在聽（TCP 連線，逾時 0.5 秒），只對有在聽的埠依序呼叫 `running_instance()`，取第一個是這份練唱台的埠。判斷哪一份、用哪個埠都和原本一樣。
- 速度修正，依 3.1 本來要延後；開啟太慢影響使用，決定這版做。
- 2026-11-22 量測：沒有練唱台在跑時從 22.01 秒變 0.51 秒；練唱台在 8767 時 0.53 秒找到。測試在 `tests/test_desktop_ports.py`。

### 3.19 E 組進度（2026-11-22）
本文其他章節的 `app.py:`、`pipeline.py:` 行號是 v2.3.2 的；步驟 1 之後 `app.py` 的程式在 `leihu/web/`。`pipeline.py` 從 C 組起行號就和 v2.3.2 不同，步驟 4 搬走函式後又再變，查程式請用函式名稱。這一節的行號是步驟 4 之後的。

- **步驟 1（`app.py` 拆成 `leihu/web` Blueprint）**：完成。路由依功能分成 8 個 Blueprint，共用狀態在 `leihu/web/common.py`，啟動時的背景工作在 `leihu/web/background.py`；程式原封搬移，只把 `@app.X` 改成 `@bp.X`（錯誤處理用 `app_errorhandler`，`before_request` 用 `before_app_request`），`app.static_folder`、`app.response_class` 改用 `current_app`。`app.py` 只建立 app 和 `main()`，`main()` 照舊用 `app` 模組裡的名稱呼叫背景工作（契約沙盒從這裡替換），測試用到的 `app.<名稱>` 由 `app.py` 轉出。
  - 驗證：93 條路由（加 `/static/<path:filename>` 共 94 條規則）的網址、方法、函式名稱和拆檔前相同；`uv run pytest` 通過；契約比對（`compare_upstream.py`，1 次）141 個案例和快照相同、異常「無」，沙盒的 8 個背景工作都有替換到。數值、效能基準沒有重跑：這一步不碰 `pipeline.py`。
  - 拆檔的風險逐項查過：`leihu/web` 沒有 `global` 重新指派（跨檔共用的都是同一個物件）；`current_app` 只在路由裡用；沒有用到 `url_for`、`request.endpoint`（Blueprint 會改 endpoint 名稱）。
  - 同步成本：以後同步上游時，`app.py` 的改動要對應到 `leihu/web/*.py`，看路由在哪個檔案就改哪個（v2.0.0～v2.3.2 上游改了 `app.py` 19 次）。
- **步驟 2（值物件、`update_song`、背景工作佇列）**：不做。
  - 上游 2.3.2 已有：`meta.json` 讀寫共用 `_META_LOCK`、先寫暫存檔再取代、鎖內讀改寫的 `update_meta`；`Jobs` 的 Condition、取消、優先處理、全部暫停。
  - 延後（會改變行為）：舊 commit 的 API 變更（`step2-plan.md` 的 D1–D6，和 `upstream-sync-2.3.2.md` 的 D1–D5 不同；例如 KEY 不合法回 400、刪除處理中的歌回 400）；`step_import`（`pipeline.py:1419`，v2.3.2 是 1342）在鎖外讀出 `steps` 再交給 `update_meta`，是上游既有的並行問題。只有 `steps` 欄位可能被舊值蓋掉（其他欄位在 `update_meta` 的鎖內合併），蓋掉的是處理狀態、不是使用者資料，所以不列為防止資料遺失的修正。
  - 不做：值物件、把 `Jobs` 搬出 `pipeline`。不改行為，但要動 `pipeline.py`，以後同步上游要對應的地方變多（3.4 第 3 點），效益不夠。
- **步驟 3 的非 ml-worker 部分**：這個版本不實作。
  - 已做：`LIANTAI_NO_JOBS` 從 `LIANTAI_NO_BACKGROUND` 拆出（C 組 S28）；數值比對工具（D 組，改成配合 `gpu_worker`，`child_hook.py` 取代舊分支的 `worker_nodither.py`；`measure_r4.py` 不搬）。
  - 延後（效能調整，3.8 第二階段）：伺服器不載入 librosa、scipy、numba（舊分支的 `leihu/dsp.py`、soxr）。伺服器行程的 `read_audio`、`_block_power`、`separate_dsp`、`shift_audio`、`share._resample`、`share.peak_limit` 會載入它們；改用 soxr 重新取樣的結果不同（舊 commit 寫人聲頻段差 < −50 dB），不是行為等價。舊分支的 `test_server_memory` 一起延後。
  - 不做：舊分支的 `LIANTAI_URL_BASE` 與背景工作契約快照（`fake_net.py`、`contract_jobs.json`）。沒設定時行為不變，但要改 `pipeline.py` 的歌詞搜尋網址（LRCLIB、網易雲、酷狗）和 `leihu/web/update.py` 的 GitHub 更新檢查，約 12 處；同步上游的成本大於讓測試離線跑歌詞搜尋、更新檢查的好處（3.4 第 3 點）。
- **步驟 4（拆 `pipeline.py`）**：改用只拆穩定區塊。
  - 依據：`v2.0.0`～`v2.3.2`（10 天）上游有 25 個 commit 改 `pipeline.py`；從 `v1.6` 算起 40 個 commit，沒改過的定義，行數只占 26%。最常改的是 `Jobs`、`USER_AGENT`、`process_song`、`DEFAULT_CONFIG`、`separate_uvr`，舊步驟 4 的 config、dsp、media、lyrics 每一類都有。
  - 搬出去的（`v1.6` 以來沒改過、只依賴標準函式庫和 numpy；`to_simplified` 會選用 `opencc`，沒裝就原樣回傳）：`leihu/pitch.py`（`bridge_gaps`、`octave_low_score`、`fix_octaves`、`trim_glides`、`clean_pitch`、`segment_notes`，連同 `PITCH_SR`、`HOP`、`HOP_S`）；`leihu/lyrics_text.py`（`fmt_lrc_time`、`parse_lrc`、`to_lrc`、`lyric_queries`、`to_simplified`、`clean_lrc`、`_norm`、`similarity`、`srt_to_lrc`、`align_to_recognized`，連同 `_NOISE`、`_CREDIT`）。21 個定義的 AST 和搬移前相同，`pipeline` 只多了轉出這些名稱的 import，`P.<名稱>` 拿到的是同一個物件。
  - 沒搬的：假名（`tw_to_ja` 用 `log_error`，`furigana` 用安裝狀態 `_kana_lock`、`_KANA_TK`）、`align_by_voice`（用上游常改的 `voice_phrases`）、線上歌詞搜尋（呼叫 `P._http_json`，`tests/test_fork_update.py` 會替換它）、`gpu_worker` 用到的名稱（`PITCH_SR` 除外：搬到 `leihu/pitch.py`，`pipeline` 轉出同一個值，`P.PITCH_SR` 不變）。測試或沙盒會替換的 `P.<名稱>` 都留在 `pipeline`：搬走的話，搬過去的模組直接呼叫它，替換就會靜默失效。
  - 安裝檔：`build.sh` 複製 `leihu/*.py` 和 `leihu/web/*.py`（不遞迴，新增子資料夾要在 `build.sh` 加一行）；`leihu.nsi` 修復與解除安裝時清掉 `$INSTDIR\leihu`；`installer.yml` 的路徑篩選加 `leihu/**`。
  - 驗證：`uv run pytest` 通過，ruff 違規數和步驟 1 後相同；契約比對 141 個案例和快照相同、異常「無」。數值基準（CPU）各跑 1 次、和 `f3bf3df` 逐檔核對：只有合成素材時 14／14，加一首真實歌曲時 20／20 個檔案完全相同，異常都是「無」。真實歌曲只和 `f3bf3df` 比；`v2.3.2` 的真實歌曲基準已在開發機用測試計畫錄好（3.9）。
  - **偏離 3.1 第 5 點**：步驟 4 動到 `pipeline.py`，本來要加跑 `tests/perf/baseline.py`，這一步沒跑。理由：搬的是同一份程式碼（AST 相同），只多一層 import，預期不會變慢；之後補量了啟動時間（2026-11-22，只量 import）：`refactor`（98aa307）和 v2.3.4 各交錯跑 15 次，`import pipeline, share` 中位數 0.253 秒對 0.249 秒，`import app` 0.277 秒對 0.276 秒，合計 0.536 秒對 0.526 秒，範圍重疊；`-X importtime` 看 `leihu.pitch`、`leihu.lyrics_text` 本身各約 3 毫秒，步驟 1 的 `leihu/web/` 各模組本身合計約 30 毫秒（程式原本就在 `app.py` 裡）。

### 3.20 待查項目的判定（2026-11-22）
照 3.3 逐項對照 `refactor` 現在的程式，不看 v2.3.2 的行號。**結論：這幾項都不是這個版本要做的安全或防止資料遺失修正。** 每項的判定（已做、不適用、延後）寫在第 2 節備註。

- 搬移歌曲庫（69、70）：上游搬移中擋下寫進歌曲庫的請求（`leihu/web/security.py` 的 `MOVE_BLOCK`），舊的那份不刪，使用者確認後才刪（`move_storage`、`delete_old_storage`）。沒擋到的是背景工作（歌詞搜尋、補量響度、主唱補回檢查）在複製途中寫進舊位置，新位置少了這些；它們都能重新產生，舊位置也還在，所以不算使用者資料遺失。KEY 轉換超過 600 秒就開始搬，複製到的移調版本可能不完整，移調版本是快取，可以重做。
- 分離的退回（22、36）：`separate_uvr` 的主唱與合音分離已有「混合精度失敗改一般精度、高品質失敗改標準模型」兩層退回，遇到 out of memory 不標壞混合精度；`JobDeferred` 是 `JobCancelled` 的子類別，讓位會直接放行。舊 commit 多做的第一段分離退回、auto 模式依顯示卡記憶體選模型（6 GB 門檻當時沒有實測）都會改變行為。
- 下載（38）：取消後還寫入一段 `.partK` 的問題，下載完成時的 SHA-256 核對（C 組 O12）會擋下壞檔重新下載，不會留下壞的模型。
- 舊分支自己造成的問題：36 的錄音開始時間為負、`.server_port` 擁有者，以及 29 的 G1–G3。`refactor` 沒有那些程式，不適用。
- 沒有逐行看 diff 的：36 的「建議與可選」各小項、70 的 `cancel_song`、`on_cancel` 細節，只依 commit 說明判成「會改變行為」。

### 3.21 同步上游的流程（2026-11-22）
`refactor` 要求線性歷史，不能 merge 上游，只能把上游的 commit 逐一套進來。步驟 1、4 之後，上游改 `app.py` 的 commit 直接 cherry-pick 一定會衝突；程式是原封搬的、函式名稱沒變，所以用 `tests/tools/upstream_map.py` 依「hunk 落在上游哪個最上層定義裡 → 那個名稱現在在哪個檔案」改寫路徑。

1. `git fetch upstream --tags`，從 `refactor` 開同步分支。
2. 列出對應並產生 patch（`tests/private/` 不進 git）：
   ```
   python tests/tools/upstream_map.py <上次同步的版本> <新版本> --patch=tests/private/sync/<新版本>
   ```
   報告列出每個 hunk 的目標檔案。名稱相同時，只找來源檔拆出去的範圍（app.py → `app.py`、`leihu/web/`；pipeline.py → `pipeline.py`、`leihu/pitch.py`、`leihu/lyrics_text.py`）。pipeline.py 上游新增的定義自動放在 `pipeline.py`；標成「手動」的（模組層級的 import、常數，橫跨兩個現在分在不同檔案的定義，app.py 上游新增的定義）不在 patch 裡，照報告的名稱手動改。app.py 新增的路由，依網址決定放哪個 Blueprint；啟動時的背景工作放 `leihu/web/background.py`。
3. 依序套用每個 commit 的兩個 patch：
   - `<序號>-<sha>.other.patch`（其他檔案，有 blob index）：`git apply --index --3way`。fork 自己改過的地方（版號、`help.html`、`leihu.nsi`）會變成衝突，手動解。
   - `<序號>-<sha>.split.patch`（app.py、pipeline.py 改好路徑的 hunk，沒有對應的 blob，不能 3-way）：`git apply --index --reject --recount`。工具會去掉頭尾的空白上下文行（函式搬到檔案開頭或結尾時，上游 hunk 邊上的空行不存在），所以要 `--recount`。套不上的 hunk 留在 `.rej` 手動處理，通常是 C 組改過的地方（例如 `main()`、`take_export`、`downloads_dir`）。
   - 版號照 3.15 改成「上游版本＋.1」（`VERSION`、`leihu.nsi`、`help.html`、`index.html`、`leihu/web/common.py`、`pipeline.USER_AGENT`），版號測試（`test_fork_update.py`、`test_installer_identity.py`）和漏移檢查的 `OWN_CHANGES` 一起改。
   - commit 沿用上游的作者和日期（`--author`、`--date` 取自上游 commit），說明保留上游原文，後面補兩段：放進現在的結構、fork 的調整，最後加 `Upstream-commit: <sha>` trailer（trailer 的 key 要用英文）。連續幾個上游 commit 改同一個功能、中間狀態馬上被換掉時，可以合成一個，每個來源各寫一行 trailer。
   - 注意：`git apply --index` 會把改動放進暫存區。要先 commit 別的東西時，先確認暫存區裡只有那些檔案。
4. 套完後檢查：
   - `uv run pytest`。
   - `compare_upstream.py <新版本> --leak`：前端和安裝檔和上游比只能剩 `OWN_CHANGES`，上游新增的名稱都要找得到（它會搜尋包含 `leihu/` 的所有程式）。
   - 契約：上游行為改變時，用 `compare_upstream.py <同步後的 commit> --record --base=<新版本>` 重錄快照（fork 和上游的差異都要符合 `RULES` 才寫入），再跑 `check_snapshot.py`。`compare_upstream.py`、`check_snapshot.py` 要用內嵌的 Python（`%LOCALAPPDATA%\LeihuLiantai\python\python.exe`）執行：快照是用它錄的，`.venv` 沒裝分離、音高、辨識的套件，`/api/status` 的 `modules` 會全部變成差異。上游新增路由時，`test_contract.py` 的 `CASES` 要補案例，路由數（`check_snapshot.py`、`test_contract.py`、`test_smoke.py`）一起改。
   - 數值：動到分離、音高、歌詞、評分時，挑合成素材或一首歌跑 `baseline.py <新版本>` 和 `baseline.py <同步後的 commit> --against=<新版本>`（3.8）。
   - 上游每個最上層定義都和現在的程式比對一次，比對時套用步驟 1 的改寫規則。確認沒有漏掉，fork 原本的修改也沒有被蓋掉。

演練（`v2.2.0..v2.3.2`，逐個 hunk 反向套到現在的程式，只算拆檔前的 `refactor` 也套得上的 hunk）：工具能定位的 app.py 8 個、pipeline.py 30 個全部套得上；app.py 另有 3 個要手動（1 個 import 區、2 個橫跨兩個現在分在不同檔案的定義）。這段期間上游沒有改過步驟 4 搬出去的函式。其餘 hunk 在拆檔前就被上游或 C 組改過，沒有拆檔一樣要手動合併。

**v2.3.3 同步（2026-11-22）**：上游 5 個 commit 變成 3 個同步 commit（佇列保存的 3 個合成 1 個），版號 2.3.3.1。
- 錄音位置改用開口瞬間對齊（`score_take`、`onset_offset`，`TAKE_ALGO` 4 → 5：舊錄音會自動重新評分，錄音的 `offset` 和匯出的音訊會和 2.3.2 不同）。
- 處理佇列跨重開保存：佇列保存的程式放在 `leihu/web/background.py`，和上游逐字相同。fork 的調整：`queue.json` 放 `P.DATA`（S28；沒設 `LIANTAI_DATA` 時和上游一樣是程式資料夾）；`restore_queue`、`queue_saver` 放在 `main()` 的 `if not P.NO_BACKGROUND` 裡；契約沙盒替換這兩個名稱；`conftest` 的使用者資料清單、`.gitignore` 加 `queue.json`。
- 匯出同名自動加編號：`unique_path` 照上游。上游 `take_export` 加的是「同一次錄音匯出過就加編號」，和 C 組 M4 是兩個問題（M4 是歌名有「.」時 `with_suffix` 截斷檔名、不同的歌撞名），兩個都要：編號邏輯改用 `SH.out_path` 檢查和產生檔名，`test_security.py` 加回歸測試（換回上游用 `with_suffix` 檢查的寫法會失敗）。`unique_path` 遇到歌名有「.」時編號位置不理想，但不會蓋掉檔案，照上游。
- 驗證：v2.3.3 `app.py`、`pipeline.py` 的 467 個最上層定義都找得到；同步只改到上游 2.3.3 也改過的 13 個定義，fork 原本的修改沒有被蓋掉。漏移檢查 0 項（補登記步驟 1 的 `leihu.nsi` 清除 `leihu`）。契約快照以 v2.3.3 重錄：141 個案例 140 個相同、1 個是已登記的 F1，`check_snapshot.py` 兩次都和快照相同、92 條路由都有案例。數值（合成素材 1 次）和 v2.3.3 逐檔 14／14 相同、異常「無」。`uv run pytest` 97 passed。
- 沒有驗證：真實歌曲的數值、效能基準、顯示卡路徑。之後重構的比對對象改成 v2.3.3，還沒用測試計畫錄 v2.3.3 的正式基準。

**v2.3.4 同步（2026-11-22）**：上游 2 個 commit 各一個同步 commit，版號 2.3.4.1。
- 歌詞移動不推擠相鄰句、九宮格 1／2／3 一句一句移動：只改前端（`app.js`、`help.html`），版號照 3.15 改。
- 新圖示、開程式時沿用上次的音量：`/api/ui-prefs` 的 GET、POST 放 `leihu/web/system.py`（和 `/api/config` 同一個 Blueprint），程式和上游逐字相同。fork 的調整：`ui_prefs.json` 放 `P.DATA`（S28）；`conftest` 的使用者資料清單、`.gitignore` 加 `ui_prefs.json`；`build.sh` 的 `icon.png` 加在 fork 的那一行。`desktop.py` 的工作列圖示照上游，fork 原本的修改沒變。
- 驗證：v2.3.4 的最上層定義都找得到；和上游不同的定義，同步前後只多了 `UI_PREFS`（放 `P.DATA`）。漏移檢查 0 項。契約加 3 個 `ui-prefs` 案例（路由 92 → 94），快照以 v2.3.4 重錄：144 個案例 143 個相同、1 個是已登記的 F1，原本的 141 個沒有變；`check_snapshot.py` 兩次都和快照相同、94 條路由都有案例。`uv run pytest` 97 passed。沒有改到分離、音高、歌詞、評分，數值沒有跑。
- 沒有驗證：新圖示與工作列圖示要裝起來看（只在 Windows 的桌面視窗有作用）、安裝檔建置交給 CI。

## 4. 要先決定的事
- 舊分支要留著（唯讀）還是封存成 tag，`claude/original-refactor-behavior-comparison-d469de` 也一樣。在搬完之前不要刪。
- 測試歌曲的清單和授權（3.8）。
- 延後到後面版本的項目（會改變行為，3.1）：`pipeline.py` 缺 `import subprocess`（3.11）、步驟 2 的 API 變更（`step2-plan.md` 的 D1–D6）與 `step_import` 補鎖（3.19）、步驟 3 的伺服器不載入 librosa／scipy／numba（效能調整，3.19）、S31、`_set_priority`（3.6）、長音訊旋律分析逾時放寬（3.6）、M3、M7、S8、O21、S19（3.13）、69、70（3.20）、6 的 R2／R3、9 的試聽檔、22 的第一段退回與 6 GB 門檻、36 的建議與可選、38 的 `.partK`／PowerShell 路徑／濾波分段、68 的主唱補回與移調互斥（3.20）；C、F 組其餘逐項確認後補列。
- 上游 `gpu_worker` 的主程式備援（`_local`）要保留還是拿掉：保留則子程式壞了仍能處理歌曲，但伺服器會載入 torch；拿掉則遵守「伺服器不載入 torch」，子程式壞了就不能處理（3.6）。拿掉會改變行為，依 3.1 屬於後面的版本，這個版本先保留。

## 5. 沒有驗證的部分
- 初判只看了 commit 標題、檔案清單，以及 M1、M2、M5、S19 四處抽查，其他 commit 都還沒看 diff。
- 「上游 2.3.2 也有這些問題」只根據 commit 說明，還沒有在 `v2.3.2` 上重現。
- 顯示卡路徑只有筆電的一份基準（3.10）：只錄真實歌曲 2 首的效能（合成歌在顯示卡上失敗）和 `refactor` 計畫的數值各 1 次；CUDA 失敗時的退回、顯示卡記憶體不足沒有被觸發過；顯示卡的相似度基準（`similarity.py --fixed`）沒錄。
- 背景工作（線上歌詞搜尋、更新檢查、下載）沒有契約快照（3.19 決定不做）；#43 的歌詞文字測試在步驟 4 之後才加入（3.1 第 6 點要求在拆分之前）；步驟 4 搬的函式當時靠「AST 相同」和數值基準驗證。`fix_octaves`、`octave_low_score` 的單元測試用合成的音高、RMS 和泛音訊號，涵蓋各個判斷分支、門檻的邊界、加權中位數和第二輪，確認的是判斷邏輯；真實歌曲的修正效果仍要實際錄音驗證。
- 步驟 1、4 對啟動時間的影響只量了 import（3.19），沒有量伺服器開好要多久，也沒有用 `tests/perf/baseline.py` 跑整首歌的效能。
