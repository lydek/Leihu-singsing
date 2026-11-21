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
| 3 | `447489e` | uv、CI、測試骨架 | 搬 | `requirements` 已經跟著上游變了，`uv.lock` 要重新產生 |
| 27 | `407b2a7` | ruff 設定 | 搬 | |
| 31 | `cbb0fbf` | 開發用啟動器（用已安裝的內嵌 Python） | 搬 | |
| 33 | `ebae3ee` | makensis `-INPUTCHARSET UTF8` | 搬 | 上游 `build.sh` 有改過，要確認 |
| 41 | `cf7a96e` | CI 加 ruff、`.git-blame-ignore-revs` | 重做 | 格式化 commit 的 hash 會不一樣 |
| 40 | `c666a91` | ruff format（只改格式） | 重做 | 不要 cherry-pick，在新程式上重跑 `ruff format` |

### C. 步驟 0：對上游程式的修正（先在扁平結構上做）
| # | commit | 內容 | 初判 | 備註 |
|---|---|---|---|---|
| 4 | `a142c98` | 環境變數覆寫 S28、import 副作用 S31 | 重做 | 2.3.2 新增了 `library_dir`、`models_dir`，覆寫順序要重新設計 |
| 5 | `e2168c4` | M1–M7 修正與回歸測試 | 待查 | M1/M2 上游已修（只差 port 比對），M5 還在；其他逐項查 |
| 6 | `7ee7791` | R1–R3 資源浪費 | 待查 | 上游 2.2.0 之後有 GPU worker，R1/R2 可能已經改變 |
| 7 | `da59e07` | 錯誤格式統一 S15 | 重做 | 2.3.2 新增了路由，要一起統一 |
| 9 | `dee8f6b` | 步驟 0 審查修正 | 待查 | 跟 4–7 一起看 |
| 10 | `dfcb0b9` | yt-dlp 取消／逾時、檢查碼、pip 逾時、`safe_name` 合併 | 重做 | S19 確定還在；其他逐項查 |

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
| 11 | `90d0357` | 步驟 1：`app.py` 拆成 `leihu/web` Blueprint | 重做 | 2.3.2 的 `app.py` 和 2.0.0 差很多，沒辦法 cherry-pick |
| 12 | `3d0fd5f` | 步驟 2：值物件、`update_song`、背景工作佇列 | 重做 | 上游 2.2.0 之後已經有自己的佇列和優先處理 |
| 13 | `c84046a` | 步驟 3：ml-worker、外部網址、伺服器記憶體 | 重做 | 沿用上游 `gpu_worker.py`，不做 ml-worker（3.6）；只評估非 ml-worker 部分 |
| 21 | `7247929` | 步驟 4：拆出 config、dsp、media、lyrics、llm、render | 重做 | |
| 22 | `34b292d` | BS-Roformer 補強 | 待查 | HF 網址上游 2.2.0 `d2e7e44` 已修；失敗退回上游 `pipeline.py:2063` 起已有兩層；剩 auto 模式 6 GB 門檻要查 |
| 45 | `07c4be0` | 拆 `read_sheet`、`sheet.render` | 併入 | 跟步驟 4 一起 |

### F. 寫在 `leihu/` 上的審查修正
都要等 E 組完成後才有地方放；如果修的是上游本來就有的問題，可以提早在扁平結構上先修。

| # | commit | 內容 | 初判 | 備註 |
|---|---|---|---|---|
| 28 | `70d30ce` | 審查修正 A–F：並行、輸入驗證、ext_url、rms 等 | 併入 | |
| 29 | `1e9f23c` | 審查修正 G：桌面啟動、打包、CI | 待查 | `desktop.py`、`leihu.nsi` 部分可以提早 |
| 36 | `891b78e` | 第二次審查：錄音負的開始時間、混合精度 OOM、`.server_port` 擁有者 | 待查 | 多數是重構或移植造成的問題（混合精度 OOM 是「上游 2.2.0 漏移植」，上游 `pipeline.py:2069` 已有）；`.server_port` 擁有者等要逐項查上游有沒有 |
| 38 | `73590fd` | `.partK`、PowerShell 路徑、pip 檢查碼、濾波分段 | 待查 | PowerShell 路徑和 pip 檢查碼可以提早 |
| 39 | `e38525f` | ruff 違規清到 0、拆 `process_song`、修取消下載的 TypeError | 併入 | TypeError 可以提早 |
| 46 | `1b5074b` | ml-worker 設定優先順序（錯誤 6） | 重做 | 上游 `gpu_worker._set_priority` 有同一個 bug，提早在 C 組修（3.6） |
| 68 | `dcd5ce4` | Codex：三個移植問題 | 待查 | WorkerPool、`cuda_ok` 兩項是 ml-worker 才有，不做（3.6）；leadfix 和移調互斥要查上游有沒有 |
| 69 | `340914c` | Codex：五個上游既有問題（搬移、主唱補回、補做標記） | 重做 | **優先**：上游 2.3.2 也有這些問題，可以直接修在扁平結構上 |
| 70 | `0fcc37f` | Codex 第二次：搬移隔離、KEY 版本失效、旋律過期、兩軌一致 | 重做 | 同 69 |

### G. 測試、量測、紀錄文件
| # | commit | 內容 | 初判 | 備註 |
|---|---|---|---|---|
| 32 | `bfc3f0d` | 量測真實歌曲的速度和記憶體 | 搬 | 腳本搬；數字要重量 |
| 43 | `2f3d2f2` | 補測試：歌詞文字、Claude 價格、歌單解析、旋律譜匯出 | 搬 | import 路徑要配合當下的結構 |
| 47 | `6705dd4` | 效能分析、量測腳本 | 重做 | `tests/perf/` 的 `profile_song.py`、`load_profile.py`、`worker_capped.py` 依賴 ml-worker（3.6 不做）；改成量 `gpu_worker` 的父子程式，舊數字只當舊架構的紀錄 |
| 37、44 | `c9c1411`、`f063ed7` | 修正紀錄、ruff 文件 | 重做 | 跟著修正一起寫，不單獨搬 |

### H. 移植上游與同步紀錄：不搬
`refactor` 已經是原生的 2.3.2，所以下面這些不搬。

- 移植上游功能：14–19、34、49–66（`93e6ac8` 到 `7d913d9`）。
- 同步紀錄：35、48、67（`30b882d`、`2929da8`、`85db775`）。
- merge commit：`964e33b`、`24379a9`、`1731902`。

不搬但要留下的東西：

- 舊分支移植時做過的**決定**（`docs/upstream-sync-2.3.2.md` 第 2 節的 D3–D5）在 E 組重做時還是要照做。D1、D2 和「`gpu_worker.py` 不保留」已由 3.6 取代，「伺服器不載入 torch」見第 4 節。重做時拿這些 commit 當參考答案。
- 移植時另外補的**測試**（`tests/test_upstream_220.py`、`tests/test_upstream_232.py`、`tests/test_live.py` 等）測的是上游行為，但 import 的是 `leihu.jobs`、`leihu.inference`（`WorkerPool`、`WorkerClient`）、`leihu.web.*`，也依賴 S28 的環境變數（`tests/conftest.py`），**不能直接在 2.3.2 上跑**。當成測試案例清單：隨 E 組對應步驟改寫成測 `jobs`、`gpu_worker`，`gpu_worker` 用 3.6 的 monkeypatch；只用 `pipeline` 的案例可以在 S28 完成後提早搬。

## 3. 建議做法

### 3.1 順序
每一組各開工作分支、各發 PR、用 rebase 合併進 `refactor`。

1. **B 組**：開發環境、pytest CI、ruff 設定。只新增檔案，衝突最少；`tests/test_smoke.py` 的路由數要從 78 改成 93。完整的 ruff 檢查（`cf7a96e` 的 `ruff check .`、`ruff format --check .`）不在這一步啟用：2.3.2 有既有違規（例如 `pipeline.py:118`、`pipeline.py:308` 的 `has = lambda` 會觸發 E731），清違規的 `e38525f` 排在 E 組。先記錄固定 ruff 版本下的違規數，清完再啟用；不要為了讓 B 組通過混入大批程式清理。
2. **A 組**：文件，同時把基準改成 2.3.2。
3. **D 組（安全網，先於任何程式修改）**：搬比對工具，在**原版 `v2.3.2`** 上錄功能基準、效能基準和相似度基準（做法見 3.7、3.8）。H 組的測試不在這一步搬（見 H 組）。
   - 數值工具要先改成配合 `gpu_worker`，不能只重新產生輸出。舊的 `tests/numeric/run.py` 只改父行程的 `P.MODELS` 和 `torchcrepe.convert.dither`，只有 `hasattr(P, "INFER")` 時才換 worker；上游 `gpu_worker` 會開新的 Python、重新 import pipeline，父行程的修改不會帶過去。
   - 兩邊都要在獨立的暫存程式碼和資料夾執行（2.3.2 不讀 `LIANTAI_DATA`，目前只有上游那一側會複製程式碼），模型路徑在父、子程式都要生效，dither 要在真正跑推論的子程式裡關掉。
   - 先證明原版 `v2.3.2` 自己跑兩次結果相同，再錄參考輸出。
4. **C 組加上 F 組可以提早的部分**：在扁平結構上修上游既有的問題。每個問題都要先確認 2.3.2 還有，才修並加回歸測試；快照出現的差異逐一登記。69、70 優先，但它們會改分軌，沒有數值參考輸出之前不合併。S28 環境變數覆寫要在這一步完成（3.7）。
5. **E 組加上 F 組其餘的部分**：重構。每做完一步都要跑契約快照和比對測試，只能出現已登記的差異。
6. **G 組**：量測、更新紀錄文件。#43 裡測既有函式的測試（例如 `read_sheet`、旋律譜匯出）改好 import 後，提前到 E 組對應的拆分之前加入。

### 3.2 E 組做法：照步驟 1–4 重做（2026-11-21 決定）
- 舊分支最後的檔案（`sync/upstream-2.3.2` 的 `leihu/`、`ml_worker/`）拿來當參考答案，但程式要從 2.3.2 的 `app.py`、`pipeline.py` 重新拆。
- 每一步都是可以獨立發布的 PR，合併前契約快照和比對測試都要過。
- 不採用的做法：直接搬舊分支最後的 `leihu/` 樹。原因是中間的 commit 可能跑不起來，也看不出跟 2.3.2 的差別。

F 組的修正跟著程式落點放進對應的步驟。下表是依 commit 標題和檔案清單做的初步對應，review 時再修正：

| 步驟 | 舊 commit | 一起處理的 F 組修正 | 要對照的上游 2.3.2 差異 |
|---|---|---|---|
| 1 web Blueprint | `90d0357` | 28 的輸入驗證、42 | 2.3.2 新增的路由（歌單、搬移、搜尋範圍、直播歌單）、`_local_only` 已有的 Host／Origin 檢查 |
| 2 值物件、背景工作佇列 | `3d0fd5f` | 28 的並行、68 的 leadfix 互斥 | 上游 2.2.0 之後的重疊處理、全部暫停、優先處理 |
| 3 推論子程式 | `c84046a` 的非 ml-worker 部分 | 46 的優先順序修正（改修在 `gpu_worker._set_priority`） | **沿用上游 `gpu_worker.py`，不做 ml-worker**（評估見 3.6） |
| 4 拆 config、dsp、media、lyrics、llm、render | `7247929` | 22、45、39 的拆 `process_song`、28 的 rms／ext_url、38 的濾波分段 | 2.3.2 的 `library_dir`、`models_dir`、搬移流程（`docs/upstream-sync-2.3.2.md` 的 D3） |

步驟 4 拆 `pipeline.py` 時，要保留 `gpu_worker.py` 用到的名稱：`P._sep_progress`、`P._outputs`、`P._track_pitch_local`、`P._whisper_model`、`P.pick_language`、`P._collect_words`、`P.cuda_ok`、`P.MODELS`、`P.SR`、`P.PITCH_SR` 等（`git show v2.3.2:gpu_worker.py | grep -oE 'P\.[A-Za-z_]+'`）。優先在 `pipeline` 保留這些名稱；如果改了 worker 的 import 或呼叫，要另外測 TASKS 的分離、音高、歌詞路徑。契約測試會把 worker 換成假的，不能證明這些依賴還能用。

### 3.3 每個 commit 的 review 步驟
1. 看 `git show <commit>`，確認它想解決什麼問題。
2. 在 `refactor` 上確認問題還在：用 `git grep`，或寫一個會失敗的測試。
3. 評估有沒有比舊 commit 更好的做法，判斷依據見 3.4。
4. 判定是搬、重做、併入、不搬或待查，把結果填回本文件第 2 節。如果改用別的做法，在備註寫「改用：……，原因：……」。
5. 判定要搬或重做的：開分支，實作加測試，`ruff`、`pytest` 都過了才發 PR。
6. commit message 用繁體中文，結尾註明 `來源：sync/upstream-2.3.2 <hash>`，方便日後追查。

### 3.4 評估更好的做法
舊 commit 是在 2.0.0 上、還不知道後來會發生什麼的情況下寫的。現在多了兩樣東西可以參考：上游 2.1–2.3.2 的做法，以及後來的審查修正。review 每個 commit 時，依序問下面四個問題：

1. **上游有沒有已經解決了？** 有的話優先沿用上游的寫法，只補不足的地方。這樣跟上游的差異小，以後同步比較容易。
2. **後來有沒有修正這個 commit？** 舊分支有不少審查修正是在修重構時產生的問題，例如 28 的並行和輸入驗證、36、46。重做時要一開始就寫對，不要先重現問題再修。
3. **以後同步上游要花多少成本？** 搬動或改名越多，每次上游更新時要對應的地方就越多。如果效益不夠大，就不要搬動。
4. **有沒有更小的改法？** 優先用現成的程式、標準函式庫、已安裝的套件，不加沒人要求的抽象。

評估結果有三種，寫在第 2 節的備註：
- **照舊**：舊做法仍然最好。
- **改用**：寫明新做法和原因。
- **不做**：目的已經不成立，寫明原因。

如果評估結果改變了某一步的範圍（例如步驟 3 不做了），先更新 3.2 的表格，再開始實作。

### 3.5 目前已知要評估的替代做法
| 範圍 | 舊做法 | 候選做法 | 要比較的地方 |
|---|---|---|---|
| 步驟 3 ml-worker | 自己寫 `ml_worker/` 和 `leihu/inference.py`，上游的 `gpu_worker.py`（670 行）整個不保留 | 沿用上游的 `gpu_worker.py` | **已評估，改用候選做法**，見 3.6 |
| 步驟 0 M1、M2 | 自己加 `before_request` | 沿用上游 `_local_only`（`app.py:131`），只補不足的地方（例如比對 port） | 回歸測試照樣加 |
| 步驟 1 Blueprint | `app.py`（2.3.2 是 2164 行）整個拆成 `leihu/web/*` | 先只拆出新功能需要的部分，或依路由群組分批拆 | 以後同步上游時，`app.py` 的改動要對應到哪個檔案 |
| 步驟 4 拆模組 | `pipeline.py`（2.3.2 是 4801 行）拆成 config、dsp、media、lyrics、llm、render | 依上游最常改的區塊決定先拆哪裡，或保留 `pipeline.py` 當相容層 | 同上 |
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
| 子程式不 import pipeline，啟動比較輕 | 子程式的時間和記憶體主要花在 torch 和模型上，差距沒有量過；需要時用 `tests/numeric/measure_r4.py` 量了再決定 |
| 伺服器永遠不載入 torch | 上游只有在子程式開不起來時才在主程式裡做。**要決定**：保留上游的備援（不用改，子程式壞了還能處理歌曲），還是拿掉（遵守舊分支 `docs/upstream-sync-2.2.0.md` 第 2 節「伺服器不載入 torch」，但子程式壞了就完全不能處理） |
| 長音訊的旋律分析逾時依音訊長度放寬（`leihu/inference.py` 的 `max(STALL["crepe"], 3 * 音訊秒數)`） | 上游 GPU、CPU worker 都固定 600 秒（`gpu_worker.py:524` 的 `STALL.get(name, 600.0)`），長歌在 CPU 上可能逾時失敗。另外評估在 `gpu_worker` 保留這項保護，加逾時邊界測試，不用搬回 ml-worker |
| 歌詞辨識拆成三個呼叫、同一個 worker 做完 | 上游是一個工作做完三步，結果一樣，不搬 |
| JSON 協定（不用 pickle） | 兩邊都只在本機溝通，上游有隨機 authkey，不搬 |

**在上游 `gpu_worker.py` 上要補的修正：**
- **`_set_priority` 的優先順序從來沒生效**（舊分支 `1b5074b`）。上游 `gpu_worker.py:42` 一樣沒有設 `GetCurrentProcess` 的 `restype` 和 `SetPriorityClass` 的 `argtypes`，64 位元 Windows 上 `SetPriorityClass` 會失敗（回傳 0，不丟例外），上游沒有檢查回傳值，所以完全沒有訊息。直播中的低優先順序在上游也沒有生效。修法照 `1b5074b` 搬，放在 C 組（扁平結構上的修正）。回歸測試也照搬，但上游沒有失敗訊息，只保留「子行程讀回 `GetPriorityClass` 是 `0x4000`、`0x20`」的斷言，載入的檔案改成 `gpu_worker.py`、呼叫 `_set_priority`。
- `dcd5ce4` 的 GpuStalled 放鎖順序，commit 說明寫「和上游 `gpu_worker.call` 一致」，所以上游不用修。`cuda_ok` 交握卡住是 ml-worker 才有的問題，上游 `track_pitch` 判斷用 CPU 時不會呼叫 `cuda_ok`，不用處理。
- 36 的混合精度 OOM：commit 說明寫「上游 2.2.0 漏移植」，上游 `pipeline.py:2069` 遇到 out of memory 不標壞、交給外層退回標準模型，預計不搬，review 時確認。

**連帶影響：**
- F 組：46 改修在 `gpu_worker._set_priority`；68 的 WorkerPool 和 `cuda_ok` 兩項不做；68 的 leadfix 互斥照原計畫查。
- 舊步驟 3 的其他部分照原計畫逐項評估：數值比對與量測工具（`tests/numeric/`、`tests/test_numeric.py`，D 組要用；`measure_r4.py`、`worker_nodither.py` 可能依賴 ml-worker，要改）、`LIANTAI_URL_BASE`、背景工作契約快照、伺服器不載入 librosa／scipy／numba（`leihu/dsp.py`、soxr）。其中 dsp 改寫會跟上游 pipeline 的改動衝突，要用 3.4 第 3 點判斷。
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
   - 之後 C 組、E 組每次改變行為：先在 `RULES` 登記，再更新快照。除了第一次建立基準，更新都要先寫出候選結果，確認案例集合一致，並和現場跑的 `v2.3.2` 比對；所有差異都符合 `RULES` 才覆寫 `contract.json`，否則 exit 1、保留原快照。
3. 步驟 1 之前，`refactor` 這邊用 `compare_upstream.py <已 commit 的參照> --record`，以 `serve_upstream.py` 執行匯出的程式，兩邊只要一個啟動器。步驟 1 之後改用 `serve.py` 加 pytest 更新目前工作樹的快照（`LIANTAI_SNAPSHOT_UPDATE=1 uv run pytest tests/contract`，要 S28），不再用 `--record`；`serve.py` 的 `ROOT` 固定是 repo 本身，如果要錄重構後的任意參照，要先讓它接受程式碼資料夾參數。上一點「先比對、全部登記才覆寫」的規則兩種錄法都要做到。`test_contract.py` 同時是工具庫和唯一的 pytest 測試，所以 D 組要讓 CI 不收集 `test_contract_snapshot`（例如在 `pyproject.toml` 的 `addopts` 加 `--deselect tests/contract/test_contract.py::test_contract_snapshot`），等 C 組完成 S28 後再拿掉。
4. 步驟 1 會把 `prefetcher`、`lead_fix_sweep` 等背景函式從 `app` 搬走，`serve_upstream.py` 的 `hasattr(app, name)` 找不到就靜默略過，背景執行緒會真的啟動。所以 S28 要在步驟 1 之前完成，之後 `refactor` 這邊改用 `serve.py`。

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

- **第一階段（行為等價）**：B、A、D、C、E 組。只允許 `RULES` 登記過的差異，不做任何會改變輸出數值的效能調整。
- **第二階段（效能調整）**：第一階段完成後才開始。每項調整都要對照效能基準和相似度基準，寫明快了多少、輸出差多少。

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
- 上游 2.3.2 只支援 NVIDIA（CUDA）。這台開發機是 AMD Radeon RX 9070 XT，所有推論都會走 CPU，**顯示卡基準在有 RTX 3060 的筆電上錄**。

**錄基準的兩台電腦：**

| 用途 | CPU | 顯示卡 | 記憶體 |
|---|---|---|---|
| CPU 基準（開發機） | AMD Ryzen 9 9900X3D（12 核） | AMD Radeon RX 9070 XT（上游不支援，推論走 CPU） | 31 GB |
| 顯示卡基準（筆電） | Intel Core i7-11370H（4 核 8 緒） | NVIDIA GeForce RTX 3060 Laptop GPU（筆電版 6 GB） | 待記錄 |

兩台的 CPU 差很多，CPU 數字不能跨電腦比；筆電也要錄一份自己的 CPU 基準，顯示卡和 CPU 的差異才比得出來。筆電版 3060 的功耗上限依機型不同（大約 60–130 W），要記下機型和功耗設定。

**RTX 3060 筆電要注意的（以下是讀程式碼的判斷，沒有實測）：**
- 架構（Ampere）在 CUDA 12.6／12.8 版 PyTorch 的支援範圍內，驅動要支援 CUDA 12.6 以上。安裝程式會跑顯示卡運算測試，失敗會依序退到 CUDA 12.6、CPU 版，結果寫在 `logs/install.log`。
- 筆電版 3060 多半是 6 GB 顯示卡記憶體。上游沒有最低記憶體門檻（舊分支的 6 GB 門檻是 fork 自己加的，2.3.2 沒有）；不到 10 GB 時不會兩首同時分離（`pipeline.py:3405` 的 `PARALLEL_MIN_VRAM`），一次只跑一個顯示卡工作。
- 高品質模型（BS-Roformer）在 6 GB 放不放得下不確定。放不下時上游會先改一般精度、再退回標準模型（`pipeline.py:2063` 起），歌曲仍會處理完，但**用的模型不同**。所以每首歌都要記錄實際用的分離模型（看歌曲的 meta 和 `logs/app.log`），不然效能數字沒辦法比。
- 筆電的效能會受電源和散熱影響：要插電、Windows 電源模式設「最佳效能」，並在 Windows 顯示設定把內嵌 Python（`%LOCALAPPDATA%\LeihuLiantai\python\python.exe`）指定用獨立顯示卡；量測時記下這些設定，每項多跑幾次看變異。
- 先確認跑得起來再錄基準：裝好原版 2.3.2 後，`/api/status` 的 `modules.cuda` 是 `true`，處理一首歌時 `logs/app.log` 有 `[gpu]` 子程式啟動的訊息，而且沒有「改在主程式裡執行」或「改用 CPU」。

**每份基準都要記錄：** 程式的 commit、測試工具的 commit、電腦規格（CPU、記憶體、顯示卡、驅動）、Python 和主要套件版本（torch、onnxruntime、faster-whisper、audio-separator）、模型檔的 sha256、歌曲清單和 sha256、執行次數。基準結果和本機素材放 `tests/private/`，不進 git；git 裡只放工具、歌曲清單和錄製方法。

**記錄規則：執行條件和平常不同的地方，一律寫進輸出紀錄。** 例如顯示卡探測失敗、改用 CPU、改在主程式裡做、退回標準模型、記憶體不足、逾時、沙盒替換了什麼、哪些替換目標找不到、對外連線、兩次結果不同。沒有就寫「無」，不能省略，否則數字會被誤解。

**測試音檔分三層：**

| 層 | 來源 | 放哪裡 | 用在 |
|---|---|---|---|
| 1 合成 | `tests/numeric/make_inputs.py`（從舊分支搬來） 用程式產生：已知旋律的合成人聲加和弦伴奏、偏高 30 音分且晚 0.2 秒的錄音、Windows 語音合成唸的中文句子 | git 只放產生程式，產生的檔案放 `tests/private/` | 功能基準的數值比對（逐位元組）；音高有標準答案 |
| 2 真實歌曲 | 自己擁有的音檔：自己錄的歌、自己買的音樂轉檔 | 只放 `tests/private/songs/`，**不進 git** | 效能基準、相似度基準（真實長度、合音、對唱） |
| 3 開放授權（可選） | 允許再散布的有人聲歌曲（CC0、CC BY；CC BY-NC 只限非商業） | 不放音檔，git 放下載網址、授權、sha256 | 讓別人也能重現第 2 層的結果 |

- git 裡放一份歌曲清單（`tests/perf/songs.json` 之類）：編號、長度、sha256、特徵（有沒有合音、對唱、語言）、來源類型與授權。不放歌名以外的版權內容；商業歌曲的檔案本身不進 git。
- 每次錄基準都核對 sha256，兩台電腦必須用同一批檔案；不同就寫進紀錄。
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
- **還沒做**：筆電的顯示卡與 CPU 數值基準（複製 `tests/private/numeric/inputs` 和 `tests/private/models` 過去，`baseline.py --label=laptop-3060-gpu`；顯示卡路徑兩次不一定逐位元組相同，看兩次差距表決定門檻）、效能基準（正常執行緒數、真實歌曲、多次取中位數、記憶體）、相似度基準（真實歌曲）、歌曲清單。`test_contract_snapshot` 需要 `conftest` 和 S28，目前不能用 pytest 跑。

## 4. 要先決定的事
- 舊分支要留著（唯讀）還是封存成 tag，`claude/original-refactor-behavior-comparison-d469de` 也一樣。在搬完之前不要刪。
- 測試歌曲的清單和授權（3.8）。
- 上游 `gpu_worker` 的主程式備援（`_local`）要保留還是拿掉：保留則子程式壞了仍能處理歌曲，但伺服器會載入 torch；拿掉則遵守「伺服器不載入 torch」，子程式壞了就不能處理（3.6）。

## 5. 沒有驗證的部分
- 初判只看了 commit 標題、檔案清單，以及 M1、M2、M5、S19 四處抽查，其他 commit 都還沒看 diff。
- 「上游 2.3.2 也有這些問題」只根據 commit 說明，還沒有在 `v2.3.2` 上重現。
