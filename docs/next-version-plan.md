# 下一版規劃：延後的行為修正與效能調整（2026-11-22，未實作）

`refactor` 目前是 2.3.4.1：只做了搬遷、行為等價的結構調整，以及安全和防止資料遺失的修正（`refactor-port-plan.md` 3.1）。這份文件把當時延後的項目（第 4 節的清單）分批排序。每項都在 v2.3.4 的程式上確認過問題還在（2026-11-22）：`pipeline.py` 模組頂端仍然沒有 `import subprocess`（ruff F821 有 4 處），`gpu_worker._set_priority` 仍然沒有設 `restype`／`argtypes`，`gpu_worker` 的旋律分析（`pitch`）逾時仍然固定 600 秒（`STALL`，不依音訊長度），KEY 不合法仍然回 200，`step_import` 仍然在鎖外讀 `steps`，`safe_name` 仍然有三份。

## 1. 先決定的事

- **版號**：3.15 的第 4 段是 fork 序號（同步時照 3.21 設成「上游版本＋.1」）。這一版會改變行為，要單獨發 2.3.4.2，還是等下一次同步上游時一起發。
- **改上游擁有的檔案會讓同步變難**：`pipeline.py`、`gpu_worker.py`、`score_sheet.py`（M6）、`share.py`（S19）、`static/app.js`、`installer.py` 等，每多一處 fork 的修改，同步上游時就多一處要手動合併（3.21）；`--leak` 不比對後端檔案的內容，只檢查上游新增的名稱找不找得到；後端的修改只靠 3.21 步驟 4 逐一比對。真的是上游 bug 的（例如缺 `import subprocess`、`_set_priority`、M6），可以考慮回報給上游，上游修了之後同步就會帶進來；回報要你決定，也要你送出。
- **前端**：D2（刪除處理中的歌回 400）要改 `static/app.js` 才看得到錯誤。`app.js` 是照上游同步的檔案，改了要登記在漏移檢查的 `OWN_CHANGES`。
- **ruff 的範圍**：目前全 repo 311 個違規（`leihu/` 62 個、`tests/` 35 個，其餘在上游的檔案；2026-11-23 補 `import subprocess` 後，`uvx ruff@0.17.5 check .`）。`leihu/` 是上游程式原封搬過去的（3.19），同步時 hunk 會套到這裡（3.21），清了一樣會和同步衝突；建議只在 `tests/` 啟用。
- **`_local` 備援**（第 4 節）：拿掉才能遵守「伺服器不載入 torch」，但顯示卡子程式壞了就不能處理歌曲。

## 2. 分批

每批各開分支、各發 PR。每項照 3.3：先在目前的程式重現，加回歸測試（測試要在修之前失敗），契約快照有差異就先在 `RULES` 登記。D1–D6 指 `step2-plan.md`（`git show 3d0fd5f:docs/step2-plan.md`）。動到分離、音高、歌詞、評分的，再跑數值基準（3.8）；效能調整用 CPU 基準驗證。

| 批 | 項目 | 改了之後的差別 | 風險 | 驗證 |
|---|---|---|---|---|
| 1 明顯的錯誤 | `import subprocess`（3.11）；`_set_priority` 設 `restype`／`argtypes`（46，3.6）；M6 旋律譜切點無限迴圈 | `tasklist` 備援、`nvidia-smi` 取樣第一次真的會跑：顯示卡被其他程式佔滿（剩不到 3.5 GB，或連續兩次用到 85%）時，不管有沒有開「直播中暫停背景處理」，新的分離、音高都改用 CPU＋標準模型、不自動辨識歌詞，並標記 `live_light`（顯示卡不忙後自動補做高品質）；背景工作暫停，做到一半的放回佇列；CPU 處理程式一律降低優先順序，顯示卡處理程式在暫停判斷成立時降低；同時只分離一首、KEY 轉換只用一半 CPU 核心（按了「直播中也照常處理」的一小時內不判斷）（處理會變慢，這是原本的設計）；旋律譜不會卡住 | 中。顯示卡忙碌的門檻上游沒有實際跑過；`nvidia-smi` 取樣多了週期性的子程式，要量一次 CPU 使用率 | 單元測試；`_set_priority` 用 `GetPriorityClass` 讀回；筆電（3.10）開實際的程式測顯示卡忙碌判斷（契約、數值、效能的沙盒都換掉了 `_running_apps`、`_sample_gpu_load`，基準測不到）；契約快照應該不變 |
| 2 並行與一致性 | `step_import` 補鎖（3.19）；M7 與 9 的試聽檔（錄音刪除後才寫回）；D2、D4（刪除處理中的歌回 400，先照現有的 `{error}` 格式；刪掉的歌的排隊工作改成已取消）；D6（換 KEY 等分軌結束）；68 主唱補回和移調互斥 | 處理中的歌不能刪；換 KEY 會排隊；刪除錄音後不會留下孤兒檔 | 中。D2 要改前端 | 路由測試（舊分支 `test_jobs.py` 的做法）；契約快照有差異才在 `RULES` 登記（舊分支只有 D1、D4 出現差異，D2 沒有快照案例，用路由測試涵蓋）；並行要用可以控制時序的測試 |
| 3 API 輸入驗證與錯誤格式 | D1（KEY 不合法回 400）、D3（錄音的 `key`、`speed` 回 400）、D5（結束超過 600 秒的工作回 404）；S15 錯誤格式統一（2.3.2 之後新增的路由一起）；S19 三份 `safe_name` 合併 | 不合法的輸入不再悄悄存成 0 或回 500；錯誤回應的內容變了；匯出的檔名可能和以前不同 | 中。S15、S19 會改到很多回應和檔名 | 契約快照的差異逐項在 `RULES` 登記（D3、D5 沒有快照案例，用路由測試涵蓋）；S19 要確認舊的匯出檔不會被蓋掉（M4 的測試） |
| 4 下載與安裝 | S8（yt-dlp 取消與逾時）；O21（installer 整體逾時）；M3 與 38 的 PowerShell 路徑（路徑嵌進程式碼字串）；38 取消下載後的 `.partK`（39 修的 `TypeError` 一起：這個錯誤由 38 的修法造成，v2.3.4 沒有） | 取消下載會真的停；安裝卡住會逾時；路徑有特殊字元時安裝不會失敗 | 中。安裝流程不好自動測 | 測試版安裝檔（3.14）實際安裝；假的下載測試（O14 的做法） |
| 5 分離的退回 | 22 的第一段分離兩層退回、auto 模式 6 GB 門檻；36 的重疊處理 OOM；長音訊旋律分析逾時依長度放寬（3.6） | 顯示卡記憶體不夠時改用較小的模型，不是失敗；長歌在 CPU 上不會逾時 | 高。6 GB 門檻沒有實測，要在筆電量 | 用測試強制觸發顯示卡記憶體不足、CUDA 失敗（基準不會觸發，3.10）；筆電顯示卡基準（3.10）；數值基準；長歌 CPU 實測 |
| 6 搬移與分軌一致性 | 69、70（搬移時的寫入者計數與隔離、主唱補回清 KEY 版本、KEY 版本失效、旋律過期、兩軌一起換、補做標記、`live_light`、`read_audio(fallback=False)`） | 搬移歌曲庫時背景工作不會寫到舊位置；換分軌後舊的 KEY 版本和旋律會重做 | 高。會改分軌，3.20 決定沒有數值參考輸出之前不合併 | 先用測試計畫在開始前的 commit 錄數值基準（見表下說明）；每項各自的回歸測試 |
| 7 效能調整（3.8 第二階段） | 6 的 R2（`/api/status` 不 import torch）、R3（`OPENBLAS_NUM_THREADS`）；伺服器不載入 librosa／scipy／numba（3.19）；38 的濾波分段；S31（`SetErrorMode`、`ffmpeg_path()` 從 import 搬到 `init_runtime()`）；`_local` 依第 1 節的決定（S31、`_local` 是行為改變，另外測 `gpu_worker` 子程式的分離、音高、歌詞路徑，3.2） | 伺服器記憶體變少、啟動變快；數值輸出可能有微小差異（濾波、執行緒數） | 中到高。librosa 的替代實作會和上游分歧 | CPU 效能基準（`tests/perf/baseline.py`）和相似度基準（3.8：寫明快了多少、輸出差多少，門檻不比上游自己跑多次的差異嚴）；數值基準逐檔比對，有差異要說明 |

建議順序：1 → 2 → 3 → 4，之後依需要做 5、6、7。第 1 批小，可以先驗證「改上游檔案」對同步流程的影響；第 6 批要等數值基準。第 5、6、7 批開始前，先用測試計畫在開始前的 commit 錄 CPU 數值、效能基準（第 1–4 批都還沒做時就是 2.3.4.1；已經做了的話，先用 `--against=v2.3.4.1` 確認數值輸出沒變）。第 7 批另外錄相似度基準（3.8，評分在 v2.3.3 改過，v2.3.2 的不能沿用）。筆電顯示卡基準是 v2.3.2 錄的，第 5 批要在開始前的 commit 重錄。

## 3. 進度

fork 對上游後端檔案的修改（同步時 3.21 步驟 4 要確認沒被蓋掉，`--leak` 抓不到）：

| 項目 | 檔案、定義 | 改了什麼 | 測試 |
|---|---|---|---|
| M6 | `score_sheet.py` 的 `_chunks` | 字的時間等於段落起點時不當切點（逐字時間早於句首、兩個字同一個時間時，原本會無限迴圈）；原本會正常結束的輸入，結果不變 | `tests/test_sheet_io.py` 的 `test_chunks_*`：會卡住的 3 個情境（包括改用音符切點）舊程式都卡到 30 秒逾時；`test_chunks_still_splits_long_line_between_chars` 確認一般的切法不變 |
| 46 | `gpu_worker.py` 的 `_set_priority` | 用自己的 `WinDLL("kernel32")` 實例設 `GetCurrentProcess.restype`、`SetPriorityClass.argtypes`，64 位元 Windows 上才真的改得了優先順序；CPU 處理程式（`CPU_POOL`）從此一律是「低於標準」，顯示卡處理程式在暫停判斷成立時降低 | `tests/test_gpu_worker.py`（子行程裡讀回優先順序；舊程式兩次都是 NORMAL） |
| `import subprocess` | `pipeline.py` 模組頂端 | 補 `import subprocess`。`_running_apps` 的 `tasklist` 備援（Windows API 查不到程式清單時）和 `_sample_gpu_load` 的 `nvidia-smi` 取樣原本丟 `NameError` 被吞掉，現在真的會跑，效果見第 2 節第 1 批「改了之後的差別」；ruff F821 從 4 個變 0 個 | `tests/test_gpu_busy.py`（替換 `subprocess.run`，舊程式分別回傳 `None`、`GPU_LOAD` 不變）；契約沙盒換掉了這兩個函式，`check_snapshot` 144 個案例兩次相同 |
| `step_import` 補鎖 | `pipeline.py` 的 `step_import` | 讀出 `steps` 到寫回之間拿著 `_META_LOCK`（`integrated_loudness` 移到鎖外先算），不再蓋掉期間別人記下的步驟 | `tests/test_jobs.py` 的 `test_step_import_keeps_steps_written_meanwhile`（另一個執行緒在讀出後寫進 `separate`，舊程式被蓋掉） |
| M7、9 | `pipeline.py` 新增 `_TAKE_LOCK`、`write_take_info`、`write_take_audio`、`delete_take_files`，`score_take` 改用；`leihu/web/takes.py` 的 `_score`、`take_mixprep`、`take_preview`、`delete_take`、`delete_takes`、`claude_coach` | 錄音的寫入和刪除共用一把鎖：處理期間錄音被刪掉時，評分、Claude 建議不寫回 JSON（建議照樣回傳），試聽用的人聲、混音不寫、回 404 | `tests/test_takes.py`：4 個情境舊程式都留下孤兒檔或回 200；批次刪除和單筆刪除刪同樣的檔案 |
| D2、D4（含 28 的 A2、A5） | `leihu/web/library.py` 的 `delete_song`；`pipeline.Jobs` 新增 `song_running`、`cancel_song`；`static/app.js` 的刪除按鈕（登記在 `OWN_CHANGES`） | 這首歌有工作在執行、或正在預先下載時，刪除回 400（`{error}`），排隊中的工作照舊；確定要刪才把兩條佇列裡這首歌排隊中的工作改成已取消，不再列在 `/api/status`。前端刪除失敗時顯示錯誤 | `tests/test_jobs.py` 的 `test_delete_*`；契約快照的差異登記成 `RULES` 的 `D4`（`status_*` 案例少了被刪的兩首歌的工作） |
| D6、68 | `pipeline.Jobs` 新增 `wait_for`、`stems_busy`；`leihu/web/common.py` 的 `vjobs` | 同一首歌在 `jobs` 有會重寫分軌的工作在執行（匯入、分離、主唱補回；只重做旋律、歌詞的不算）時，這首的移調先跳過、狀態寫「等這首歌的分軌處理完」，做完後最多 5 秒內開始；別首歌的移調照常。只擋這個方向：移調已經在跑時開始的分軌不等（移調通常幾十秒，分軌最後才寫檔；分軌後舊 KEY 版本失效是第 6 批） | `tests/test_jobs.py` 的 `test_variant_*`、`test_queued_stem_job_does_not_block_variant` |
| S15 | `leihu/web/security.py` 新增 `http_error`、`unhandled_error` | 404、405、400（壞掉的 JSON）、403 等 HTTP 錯誤回 `{"error": "請求失敗（代碼）"}`，不回 HTML，狀態碼和 `Allow` 等標頭照舊；沒處理到的例外回 500 `{error}`，traceback 照舊經 `app.logger` 寫進 app.log，另記 `log_error`。前端 `api()` 本來就把非 JSON 的錯誤顯示成「請求失敗（狀態碼）」，畫面上的字不變 | `tests/test_errors.py` 的 `test_s15_*`；契約差異登記成 `RULES` 的 `S15`（10 個 404／400 案例） |
| D1 | `leihu/web/library.py` 的 `rename`（`PATCH /api/songs/<sid>`） | 不合法的 KEY（文字、超過 ±6、`1.5`、`true`）回 400「KEY 範圍是 ±6」，不再悄悄存成 0；空字串、`null` 照舊當成清除 | `tests/test_errors.py` 的 `test_d1_*`；契約差異登記成 `D1`（3 個案例）和 `D1-後續`（之後 8 個案例讀到的 KEY 維持 2） |
| D3 | `leihu/web/takes.py` 的 `add_take` | 存錄音檔之前驗證：`start`、`duration` 要是有限且不小於 0 的數字，`key` 是 ±6 的整數，`speed` 在 0.5–1.5；不合法回 400、不留下錄音檔（原本不是數字回 500，超出範圍照存） | `tests/test_errors.py` 的 `test_d3_*`（契約沒有案例） |

第 3 批延後（2026-11-23 決定，等重構穩定再考慮）：

- **S19**：`share.safe_name` 沒有任何地方呼叫（v2.3.4 也一樣）。實際在用的 `leihu/web/common.py` 的 `_safe_name`（歌詞、混音、錄音匯出）和 `score_sheet.safe_name`（旋律譜）規則相同，只差長度上限（80／60）和名稱為空時的預設字（「歌詞」／「旋律譜」），目前沒有會出錯的情況。合併要改 `score_sheet.py`、`share.py` 兩個上游檔，會增加同步成本。
- **D5**：上游已經在送出新工作時清掉結束超過 1 小時的工作（`Jobs.submit`），查不到的工作 `/api/jobs/<id>` 本來就回 404，「狀態從不清除」的問題已經不在；只剩保留時間是 1 小時而不是 `step2-plan.md` 的 600 秒。前端輪詢到 `done`、`error`、`cancelled` 就停。

數值基準（CPU，開發機，2026-11-23）：第 2 批動到評分（`score_take` 寫檔）和匯入（`step_import`），`tests/numeric/run.py` 會跑這兩個。用 `tests/private/plans/refactor.json` 各跑 1 次：`baseline.py v2.3.4.1 --label=desktop-9900x3d-cpu`（1100.6 秒）和 `baseline.py 4262dda --label=desktop-9900x3d-cpu --against=v2.3.4.1`（1097.5 秒），兩邊每一步實際都在 CPU 上，異常都是「無」，逐檔核對 38／38 個檔案完全相同。摘要在 `tests/numeric/baselines/v2.3.4.1-desktop-9900x3d-cpu-refactor.json`、`4262dda-desktop-9900x3d-cpu-refactor.json`；`v2.3.4.1` 這份也是第 5、6、7 批開始前要對照的 CPU 數值基準（第 2 節表下說明）。只跑 1 次，同一版的穩定性沒有判定，靠和 `v2.3.4.1` 逐檔相同確認。

## 4. 還沒確認的

- 28（審查修正 A–F）只拆出 A2、A5（第 2 批已做），其餘沒有逐項拆開；36 的「建議與可選」各小項（佇列、下載、歌單、匯出影片、`LIANTAI_PORT` 等，目前只把重疊處理 OOM 放在第 5 批）、70 的 `cancel_song`、`on_cancel` 只依 commit 說明判斷（3.20）。開始第 4、5、6 批之前要逐行看 diff（第 2 批開始前看過 28、70 的 commit 說明：70 的 `cancel_song`、`on_cancel` 是舊分支 `leihu/jobs.py` 的，目前的 `Jobs` 沒有 `on_cancel`，不適用）；看完 36 的各小項，再分到對應批次。
- `import subprocess` 已補（2026-11-23），顯示卡忙碌判斷開始作用：上游從來沒有真的跑過這段，門檻是否合理、`nvidia-smi` 週期取樣的 CPU 使用率，都還沒在筆電（3.10）開實際的程式驗證。
- 舊分支的測試（`test_jobs.py`、`test_values.py`）能不能直接搬，沒有確認。
