# 拆 `pipeline.py` 的計畫（2026-11-24，第 6 節已決定）

## 1. 目的與範圍

`pipeline.py` 現在 5133 行、327 個最上層定義。3.19 的步驟 4 只搬了 `v1.6` 以來沒改過的音高後處理、歌詞文字（`leihu/pitch.py`、`leihu/lyrics_text.py`）。原因是同步上游以檔案和行號為單位，上游常改的定義一搬就套不上 patch（3.4 第 3 點），3.19 因此明寫「不做：把 `Jobs` 搬出 `pipeline`」。

PR #26 合併後，`tests/tools/defmerge.py` 以定義為單位三方合併，fork 的定義放在哪個檔案都找得到（`docs/upstream-sync-research.md`）。這份計畫說明怎麼利用它放寬原則、把 `pipeline.py` 依功能拆開。

- **只拆 `pipeline.py`**。`app.py` 已經拆成 `leihu/web/`（步驟 1）。`gpu_worker.py`、`static/app.js`、`installer.py` 不拆：`defmerge.py` 對這些檔案只能整個檔案三方合併。
- **只搬，不改行為**。行為修正照 `next-version-plan.md` 的批次做，不和搬移放在同一個 PR。
- **名稱不變**。`pipeline` 照樣提供每一個名稱，`P.<名稱>` 拿到的是同一個物件：`leihu/web/`、`gpu_worker.py`（子程式裡 `import pipeline`）、測試、契約沙盒都用 `P.<名稱>`。

## 2. 現況（2026-11-24，`refactor` 的 `80aed5b`）

### 2.1 各區塊的大小與上游改動

依 `pipeline.py` 裡的區塊標題分組。「上游改動」是 v2.3.2→2.3.3→2.3.4→2.3.5 三次之間，上游改過或新增的定義數。

| 區塊 | 定義 | 行數 | 上游改動 |
|---|---|---|---|
| 開頭、設定、檔案 | 42 | 362 | 1（`unique_path`） |
| 歌曲庫搜尋、ffmpeg 與 YouTube、批次匯入 | 20 | 358 | 0 |
| 曲名／歌手／預設 KEY | 34 | 450 | 0 |
| 音量標準化 | 11 | 133 | 0 |
| 分離 | 78 | 897 | 8（2.3.5 的高品質退回：`separate_uvr`、`vocal_model`、`karaoke_model` 等） |
| 音高、歌詞、移調／變速、錄音評分 | 30 | 460 | 0 |
| 日文假名標音 | 28 | 305 | 4（2.3.3 的評分：`score_take`、`onset_offset` 等，放在這個區塊裡） |
| 背景工作、直播中暫停背景處理（含 `Jobs`） | 38 | 717 | 0 |
| 線上歌詞搜尋 | 21 | 293 | 3（每一版的 `USER_AGENT`） |
| 依主唱聲音對時間 | 8 | 232 | 1（`voice_fit`） |
| 分類、歌曲庫／模型搬移 | 17 | 223 | 0 |

最近三版的改動集中在「分離」和版號。3.19 統計的 v2.0.0～2.3.2 則是 `Jobs`、`process_song`、`DEFAULT_CONFIG`、`separate_uvr` 改得最多。上游改到哪裡隨功能而定，沒有一定穩定的區塊。

### 2.2 搬到別的模組會出問題的三件事

1. **測試、沙盒對 `P.<名稱>` 的替換**：
   - 測試替換 79 個不同的名稱，共 173 處、17 個檔案（`monkeypatch.setattr(P, ...)` 和 `P.x = ...`）。
   - 契約沙盒替換 `_GPU_INFO`、`_running_apps`、`_sample_gpu_load`（`tests/contract/serve.py`）。

   定義搬到別的模組後，在那個模組裡直接呼叫的是那個模組自己的名稱，替換 `P.<名稱>` 會**靜默失效**。測試照樣通過，卻沒有測到該測的東西（3.19 已經提過）。
2. **執行中重新指派的全域變數**：10 個名稱用 `global` 在執行中換掉。
   - `LIB`、`MODELS`：搬移歌曲庫時換。讀取的定義分別有 14、11 個。
   - `_GPU_INFO`、`CUDA_NOTE`、`_CUDA_OK`。
   - 5 個延遲建立的物件：`_T2S`、`_TW2JA`、`_KANA_TK`、`_EARLY_POOL`、`_LYRICS_POOL`。

   別的模組用 `from pipeline import LIB` 拿到的是當時的值，搬移歌曲庫後還是舊位置。
3. **相依關係**：`Jobs` 直接用到 13 個 `pipeline` 的名稱：
   - 其中 4 個（`free_gpu`、`live_status`、`load_config`、`log_error`）測試會替換。
   - 往下追的完整相依有 43 個定義、314 行，包括顯示卡狀態（`gpu_info`，它讀 `MODELS`）和直播判斷。

   要把 `Jobs` 搬成真正獨立的模組，得先搬這 43 個定義，否則就要互相 import。

## 3. 兩種拆法

### 方式一：分檔、共用同一個命名空間

定義寫在 `leihu/core/*.py`，`pipeline.py` 依序把它們載入自己的命名空間（`exec(compile(原始碼, 檔名, "exec"), globals())`），效果等於把那段程式放回原處：

- 所有定義仍然在 `pipeline` 的命名空間裡，`P.<名稱>` 是同一個物件，`__module__` 仍是 `pipeline`。測試、沙盒的替換和 `global` 重新指派都照常有效，不用改任何測試。
- 每個檔案的內容和搬移前逐字相同，`defmerge.py` 在 `leihu/core/` 裡照樣找得到定義（`GROUPS` 已包含），上游沒改到 fork 的地方就是「直接換」。
- traceback 和函式的 `inspect.getsource` 都用 `compile` 給的檔名，會指到 `leihu/core/*.py` 的正確行號（2026-11-24 用小原型確認過；替換、`global` 重新指派也確認照常有效）。類別的 `inspect.getsource` 會找 `pipeline.py` 而失敗，這個 repo 沒有用到。

代價：
- 這不是真正的模組，沒有相依邊界，只是把一個大檔案分成幾個檔案。
- `leihu/core/*.py` 不能單獨 import。ruff、IDE 看不到 `pipeline` 的其他名稱，會報未定義名稱，要在 ruff 設定裡排除這幾個檔案的 F821。排除後，函式裡打錯名稱（例如 `reslut`）ruff 也抓不到，所以改用 `symtable` 逐層檢查：每個檔案裡、每個作用域的全域或未綁定名稱，都要在 `pipeline.__dict__` 或 builtins 裡找得到。這樣跨檔漏掉的名稱和函式裡的筆誤都抓得到。
- 每次 `import pipeline` 都要重新編譯這些檔案，不經過 `.pyc` 快取。顯示卡子程式每次開也會 import。第 7 節量啟動時間，明顯變慢再補快取。

載入的規格（步驟 0 定下來，第 1 輪計畫審查補）：
- **位置**：每個檔案的載入呼叫，放在 `pipeline.py` 原本那段程式的位置，不集中到檔尾。模組層級有在載入時就執行的敘述。例如 `pipeline.py` 檔尾附近的 `try: ffmpeg_path() except Exception: traceback.print_exc()` 會呼叫第 655 行的 `ffmpeg_path`。如果 `ffmpeg_path` 搬走、載入卻排在這段後面，就會丟 `NameError` 又被 `except` 吞掉，ffmpeg 不會加進 `PATH`。
- **`from __future__ import annotations`**：`pipeline.py` 有這行。`compile()` 明確傳 `flags=__future__.annotations.compiler_flag, dont_inherit=True`，不靠「從呼叫的地方繼承」。這樣分出去的檔案和原本一樣，型別註記不會在載入時執行。
- **同步工具**：`defmerge.py` 目前把 `leihu/core/*.py` 當成各自獨立的模組，和方式一衝突：
  - 上游新增的 import 只被搬走的定義用到時，它不肯加進 `pipeline.py`，改報「用到它的定義在 `leihu/core/...`」。
  - 上游在分出去的檔案新增定義、被 `pipeline.py` 或另一個分出去的檔案用到時，它會報「要加 import」，結束代碼 1。

  在共用命名空間裡，這兩種情況本來就該加在 `pipeline.py`、不需要互相 import。步驟 0 要讓 `defmerge.py` 知道哪些檔案和 `pipeline.py` 共用命名空間（例如 `SHARED_NS = {"pipeline.py": ("leihu/core/",)}`）：同一個命名空間裡不做跨檔 import 檢查；`import_plan` 把它們當成同一個目標，`merged_uses_before` 依載入順序一起看。方式二的模組照舊當成獨立模組。

### 方式二：真正的模組，由下往上搬

和步驟 4 的 `leihu/pitch.py` 相同：新模組 import 它用到的下層模組，`pipeline` 轉出名稱。

- 好處：有真正的相依邊界，可以單獨 import、單獨測試，之後才能做「伺服器不載入 librosa」這類依模組延遲載入的事（第 7 批）。
- 代價：
  - **替換**：測試改用一個共用的輔助函式，替換 `pipeline` 和所有持有同一個物件的 `leihu` 模組；再加一個守門測試，抓直接 `setattr(P, 名稱)`、而那個名稱在別的模組也有綁定的寫法。173 處要逐一改，契約沙盒也要改。
  - **重新指派的全域變數**：讀 `LIB`、`MODELS` 等名稱的定義，要嘛和負責重新指派的函式放在同一個模組，要嘛改成 `模組.LIB` 的寫法。後者改到上游定義的內容，同步時就是三方合併，上游改同一行會衝突。
  - **順序**：`Jobs` 要等它的 43 個相依定義先搬完，至少 3 個 PR。

### 建議

**上游常改的區塊（`Jobs`、分離、處理流程、線上歌詞搜尋）用方式一，相依少、不讀重新指派變數的穩定區塊用方式二。**

- 方式一不改定義內容、不改命名空間，又能馬上把最大、最常改的區塊分出去。剩下的風險在載入機制本身（第 3 節的規格），由步驟 0 的檢查守住。
- 方式二的成本主要花在測試替換和全域變數上，只值得用在之後真的要單獨載入的地方。
- 兩種可以並存。方式一分出去的檔案，之後要改成方式二時，再個別處理替換和相依。

## 4. 要改的原則

第 6 節決定後（2026-11-24）已照下面改了這些文件：

- **`refactor-port-plan.md` 3.4 第 3 點、3.19 步驟 4**：「上游常改的程式不搬、不拆」改成「可以搬」，條件如下：
  1. 名稱不變，`pipeline` 照樣提供，`P.<名稱>` 是同一個物件。改名要登記在 `defmerge.RENAMES`。
  2. 定義內容逐字不變。方式二不得不改的（例如 `模組.LIB`）要登記在 fork 修改表。
  3. 不為了排版或包條件去縮排上游的定義。要加條件時抽成新函式（研究文件第 6 節，2.3.3 的 `main` 就是反例）。
  4. 上游新增定義的放置位置補進 `defmerge.PLACE`。
- **`next-version-plan.md` 第 1 節**：「改上游擁有的檔案會讓同步變難」改成：只有兩邊改到同一段才需要手動合併。修上游的 bug 不用再迴避上游的檔案。
- **3.21 同步流程**：改用 `defmerge.py`，`upstream_map.py` 在下一次同步時一起跑、比較結果。拆檔之後 `upstream_map.py` 只找得到原本的三個檔案，搬到 `leihu/core/` 的定義它找不到，所以拆檔和改用 `defmerge.py` 要一起決定。`defmerge.py` 要先支援共用命名空間（第 3 節「同步工具」），不然一拆就會誤報手動。同步後多一步檢查：上游在搬走的定義裡用到新名稱時，`defmerge.py` 不會發現，要靠 `symtable` 的名稱檢查和 pytest。

## 5. 步驟（每一步一個 PR）

| 步驟 | 內容 | 方式 |
|---|---|---|
| 0 | 載入機制與檢查，搬移本身寫成可以重跑的腳本（`tests/tools/`），之後能套到舊 commit 上做模擬同步：<br>・`pipeline.py` 的載入函式，照第 3 節的規格（位置、`__future__` 旗標）<br>・`symtable` 名稱檢查的測試<br>・搬移前後的 AST 比對腳本<br>・「`pipeline` 的名稱齊全、是同一個物件」的測試<br>・ruff 設定排除 F821<br>・`defmerge.py` 支援共用命名空間（含單元測試）<br>・`installer/build.sh` 複製 `leihu/core/*.py`。目前寫死只複製 `leihu/*.py`、`leihu/web/*.py`，不改的話測試版啟動就會失敗；`leihu.nsi` 用 `File /r "payload\*"`，不用改 | 只加工具，不搬程式 |
| 1 | `Jobs` 和直播中暫停背景處理（`JobCancelled`、`JobDeferred`、`live_status`、`LIVE_BACKGROUND` 等，約 700 行）→ `leihu/core/jobs.py` | 方式一 |
| 2 | 分離（78 個定義、約 900 行）→ `leihu/core/separation.py` | 方式一 |
| 3 | `process_song` 等處理流程、YouTube 與批次匯入 → `leihu/core/process.py`、`leihu/core/youtube.py` | 方式一 |
| 4 | 線上歌詞搜尋、依主唱聲音對時間 → `leihu/core/lyrics_search.py`、`leihu/core/voice_align.py` | 方式一 |
| 之後 | 曲名／歌手／預設 KEY、音量標準化等穩定區塊：先看相依和有沒有讀重新指派的變數，適合的才用方式二 | 個別評估 |

每一步搬完，`pipeline.py` 留下設定、檔案、共用的小函式，以及依序載入各檔案的那幾行。

### 和其他工作的先後

- **第 7 批**：和拆檔各自排程，不互相等。
  - `_local` 改交給 `CPU_POOL` 主要改 `gpu_worker.py`。
  - `pipeline.py` 頂端本來就只 import `numpy`、`soundfile`，librosa、scipy、torch 都在函式裡延遲 import；方式一分出去的檔案也不會改變載入時機。效能調整不用等拆檔。
  - 兩件事改到同一段程式時，先合併的那邊先做，另一邊 rebase。
- **同步上游**：拆檔期間上游發新版時，先同步再繼續拆。同步就用 `defmerge.py`，拆到一半的狀態正好是它的實際驗證。

## 6. 決定（2026-11-24）

1. **同意第 4 節放寬原則**，已改 `refactor-port-plan.md` 3.4、3.19、3.21 和 `next-version-plan.md` 第 1 節。
2. **拆法照第 3 節的建議**：上游常改的區塊用方式一，相依少、不讀重新指派變數的穩定區塊用方式二。
3. **3.21 現在就改用 `defmerge.py`**。步驟 0 要先讓它支援共用命名空間。下一次同步上游時 `upstream_map.py` 照樣跑一次比較結果。

## 7. 每一步的驗證

- **AST 相同**：每個搬走的定義，AST 和搬移前相同（步驟 4 的做法）。方式一另外檢查檔案內容逐字相同。
- **名稱齊全**：搬移前 `pipeline` 的每一個最上層名稱，搬移後仍然在，而且是同一個物件。方式一另外檢查 `__module__` 不變。
- **pytest 全過**；ruff 違規數不增加（`80aed5b` 是 311，排除的 F821 另外記）。
- **契約比對**（`compare_upstream.py`）：和快照相同，異常「無」。
- **CPU 數值基準** `--against=<搬移前>`：逐檔相同。方式一理論上必然相同，照樣跑一次，確認載入機制在顯示卡子程式裡也正常。
- **啟動時間**：`import pipeline, share`、`import app`，交錯各跑 15 次比中位數（3.1 第 5 點、3.19 的做法）。
- **模擬同步**：把搬移腳本套到 `263833a`（2.3.5 同步前）和 `43ddbb0`（手動同步後），用 `defmerge.py` 同步 2.3.5，`--expect` 和搬移後的 `43ddbb0` 比較，要沒有差異。研究文件 5.2 的壓力測試只搬了文字，這一步在能執行的程式上做，合併結果也要跑一次 pytest。
- **安裝檔**：測試版安裝檔（3.14）實際安裝、開一首歌，確認 `leihu/core/` 有被打包（`installer/build.sh`）、子程式能載入。

## 8. 不做、限制

- 不拆 `gpu_worker.py`、`static/app.js`、`installer.py`。
- 不改名、不改函式內容；行為修正照批次做。
- `defmerge.py` 把類別整個當成一個定義合併：`Jobs` 裡任何一處被上游改到，整個類別都做三方合併，不受拆檔影響。
- 方式一的檔案不能單獨 import，也不會因此多出相依邊界。真正的邊界要等個別改成方式二。
