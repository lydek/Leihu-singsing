# 2.3.5.1 發版前的筆電實測（RTX 3060 筆電）

`refactor` 從 2.3.4.2 之後多了 84 個 commit：2.3.5 同步、下一版第 5 批、多輪審查修正、拆檔（`pipeline.py` 分成 `leihu/core/` 的 7 個檔案）、第 7 批 7a（顯示卡處理程式開不起來時改交給 CPU 處理程式）。拆檔改了程式的載入方式，7a 改了顯示卡處理程式的啟動方式。這兩項到目前只有 CI 建置和開發機（只用 CPU）的數值基準驗證過，還沒有實際安裝、在顯示卡上跑過。這份文件列出發版前要在筆電上做的事。

第 5 批的實測步驟在 `docs/laptop-batch5-test.md`，那裡的數值基準已經做完（`e4e93d5`、`cbfd6a1`），手動實測還有 4 項沒測，這次一起做（第三節標「第 5 批」的幾列）。

測完把第二節的 `numeric-report.md`、第三節每項的結果、`app.log` 傳回來，再決定能不能發版。

## 一、準備

### 1. 取得程式

筆電上沒有自己的修改時，把分支整個換成遠端的 `refactor`：

```powershell
git fetch origin
```

```powershell
git switch refactor
```

```powershell
git reset --hard origin/refactor
```

確認是 `7a97899` 或更新的 commit：

```powershell
git log --oneline -1
```

`tests\private\` 沿用第 5 批那份（`docs/laptop-batch5-test.md` 第一節第 2 點）。

### 2. 下載測試版安裝檔

GitHub 的 Actions → `installer` workflow → `refactor` 分支最新一次（`7a97899`，run 45659213877）→ Artifacts 的 `installer-test`，裡面是 `LeihuLiantai-Test-2.3.5.1.exe`。

測試版裝到 `%LOCALAPPDATA%\LeihuLiantaiTest`，不影響正式版。之前裝過舊的測試版的話，直接覆蓋安裝。執行紀錄在 `%LOCALAPPDATA%\LeihuLiantaiTest\logs\app.log`。

## 二、顯示卡數值基準

關掉其他吃顯示卡的程式、插上電源，在 repo 根目錄執行：

```powershell
& "$env:LOCALAPPDATA\LeihuLiantai\python\python.exe" tests\numeric\baseline.py 7a97899 --label=laptop-3060-gpu --plan=tests/private/plans/refactor.json --against=cbfd6a1
```

`cbfd6a1` 是第 5 批在筆電錄過的那次。之後只有拆檔、7a 和審查修正，對這組素材的輸出應該沒有影響。

看 `tests\private\upstream\7a97899\numeric\laptop-3060-gpu-refactor\numeric-report.md`：

- 「異常」是「無」。7a 新增的「顯示卡處理程式開不起來，改交給 CPU 處理程式」如果出現，會列在這裡。
- 「實際使用的裝置與模型」是 `cuda`。
- 「和 cbfd6a1 逐檔核對」最好是 38／38 相同。不同的話，看差距表和是哪些檔案。

## 三、手動實測

| 項目 | 做法 | 要看到的 |
|---|---|---|
| 安裝與啟動 | 裝測試版、開程式 | 畫面標題是 2.3.5.1；`app.log` 開頭沒有 Traceback |
| 匯入（拆檔後的主要流程） | 匯入一首歌，等它做完 | 進度有「（高品質）」；log 有「separate 交給處理程式」「顯示卡處理程式 … 已連上」；分離、旋律、歌詞都完成，能播放。這一步會走到 `jobs`、`separation`、`process`、`lyrics_search`、`voice_align` 五個分出去的檔案 |
| YouTube 與批次匯入 | 貼一個 YouTube 網址；再用「批次匯入」貼兩個網址（或一份 Excel） | 下載、處理都完成。走到 `youtube.py`、`batch_import.py` |
| 練唱與匯出 | 換 KEY、錄一段、看評分、匯出 | 和以前一樣 |
| 重做全部歌曲（2.3.5） | 設定裡按「重做全部歌曲」 | 沒處理完、分離失敗的歌也排進去；直播模式開著時這些工作暫停 |
| 刪除歌曲 | 刪一首歌，連按兩次刪除 | 第二次回「找不到這首歌」；歌曲庫資料夾裡沒有留下 `.trash` |
| 第 5 批：直播中處理、`live_light` 補做 | 開直播模式匯入一首；結束直播模式、等約 2 分鐘 | 直播中 log 有「（直播中，低優先順序）」；這首的 `library\<歌曲編號>\meta.json` 有 `"live_light": true`；結束後佇列自動用高品質重做，做完變成 `false` |
| 第 5 批：長歌的旋律分析 | 直播模式下處理一首超過 4 分鐘的歌 | 旋律分析做完，log 沒有「沒有回應」「卡住」 |
| 第 5 批：第一段分離退回 | 開吃顯示卡記憶體、但使用率不到 85% 的程式（例如暫停中的遊戲），匯入一首長歌 | 記憶體不夠時 log 有「高品質人聲分離失敗，改用標準模型」，歌曲說明有原因；不會出現「AI 分離失敗，改用聲像分離」。沒有觸發退回也記下來 |
| 第 5 批：`nvidia-smi` 的負擔 | 程式閒置、直播模式開著時看工作管理員 | `nvidia-smi` 不會一直佔 CPU（約每 20 秒跑一次） |
| 7a：平常不會走到 CPU 備援 | 上面幾項做完後搜尋 `app.log` | 沒有「交給 CPU 處理程式（顯示卡處理程式無法啟動」；`/api/status` 沒有 `gpu_fallback`。顯示卡處理程式開不起來的情況在筆電上不好製造，靠單元測試 |
| app.log | 打開 `app.log` | 每一行開頭有時間，時間不會出現在一行中間；`[gpu]` 的訊息沒有被別的行插在中間 |

## 四、發版（測完之後）

1. 我整理 release 說明（`docs/release-notes-2.3.5.1.md`）。
2. 你同意後，我在 `refactor` 上打 `v2.3.5.1` tag 並推送。CI 會建 draft release，附正式版安裝檔和 `.sha256`，並檢查 tag 等於 `v` 加 `VERSION`。
3. 你在 GitHub 檢查 draft、貼上說明，再按發佈。2.3.4.2 的 draft 一直沒有發佈過，要刪掉還是留著由你決定。
