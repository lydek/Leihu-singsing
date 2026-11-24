# 第 5 批筆電實測步驟（RTX 3060 筆電）

對應 PR：[lydek/Leihu-singsing#17](https://github.com/lydek/Leihu-singsing/pull/17)（draft）。分支 `claude/next-version-batch5`，2026-11-24 rebase 到 2.3.5 之後是 commit `7d2b39e`（第一段的高品質退回改用上游 2.3.5 的寫法）。改了什麼見 `docs/next-version-plan.md` 第 3 節「5：」各列。

rebase 前的 `bd140a0` 已經在筆電錄過數值基準（和 `263833a` 逐檔 38／38 相同），手動實測還沒做。rebase 後要重錄一次，手動實測照第三節做。

測完把兩份 `numeric-report.md`、每項實測的結果和 `app.log` 的相關幾行傳回來，再決定能不能把 PR 轉成正式的。

## 一、準備

### 1. 取得這個分支的程式

筆電上已經有 repo：

```powershell
git fetch origin
```

```powershell
git switch -c claude/next-version-batch5 origin/claude/next-version-batch5
```

上次已經切過這個分支：分支被 rebase 過，要整個換成遠端的版本（筆電上沒有自己的修改才可以這樣做）：

```powershell
git switch claude/next-version-batch5
```

```powershell
git reset --hard origin/claude/next-version-batch5
```

還沒有 repo：

```powershell
git clone https://github.com/lydek/Leihu-singsing.git
```

### 2. 確認 `tests\private\` 齊全

這個資料夾不在 git 裡，沿用 3.10 錄筆電基準時那份（標籤 `laptop-3060-gpu`、計畫 `refactor`、腳本 `tests\private\laptop-gpu.ps1`）。不要換新的歌曲或素材：計畫檔記了每首歌的 sha256，`baseline.py` 也會核對素材和模型，換了會被列為異常、不能比對。

要有：

- `tests\private\plans\refactor.json`
- `tests\private\songs\` 的 4 首 flac
- `tests\private\numeric\inputs\manifest.json`
- `tests\private\models\`
- `tests\private\upstream\263833a\numeric\laptop-3060-gpu-refactor\`（上次錄的 2.3.4.2，第一次比對要用）

沒有的話，從開發機複製 `.claude\worktrees\refactor-branch-setup-377f8d\tests\private\private.zip`（約 1.8 GB），解壓到 `tests\private\`。

### 3. 確認有裝顯示卡版的正式版練唱台

數值基準用它的內嵌 Python：`%LOCALAPPDATA%\LeihuLiantai\python\python.exe`。

## 二、錄數值基準

關掉其他吃顯示卡的程式，插上電源。在 repo 根目錄依序執行（同一時間只能錄一個，第一個跑完再跑第二個）：

```powershell
& "$env:LOCALAPPDATA\LeihuLiantai\python\python.exe" tests\numeric\baseline.py e1da07b --label=laptop-3060-gpu --plan=tests/private/plans/refactor.json --against=263833a
```

```powershell
& "$env:LOCALAPPDATA\LeihuLiantai\python\python.exe" tests\numeric\baseline.py 7d2b39e --label=laptop-3060-gpu --plan=tests/private/plans/refactor.json --against=e1da07b
```

- `263833a`：2.3.4.2（上次已錄）。
- `e1da07b`：第 5 批開始前（2.3.5 同步、分離失敗診斷、顯示卡忙碌判斷、app.log 時間）。和 2.3.4.2 比，2.3.5 改了線上歌詞對時間和跳過前奏，這組素材的歌詞輸出可能不同，有差異要看是不是這兩項造成的。
- `7d2b39e`：第 5 批。

看報告 `tests\private\upstream\<版本>\numeric\laptop-3060-gpu-refactor\numeric-report.md`：

- 「異常」要是「無」。
- 「實際使用的裝置與模型」要是 `cuda`。
- 「逐檔核對」最好是 38／38 相同（3.10 那次筆電上兩版就是 38／38 相同）。

## 三、裝測試版做手動實測

### 1. 下載測試版安裝檔

找 PR #17 的 installer run 編號：

```powershell
gh run list --repo lydek/Leihu-singsing --branch claude/next-version-batch5 --limit 3
```

把 installer 那一列的編號填進 `<run 編號>`：

```powershell
gh run download <run 編號> --repo lydek/Leihu-singsing -n installer-test -D $env:USERPROFILE\Downloads\leihu-batch5-test
```

也可以在 PR #17 的 Checks 頁，從 installer 的 Artifacts 下載 `installer-test`。

### 2. 安裝

測試版裝到 `%LOCALAPPDATA%\LeihuLiantaiTest`，名稱是「雷虎練唱台 測試版」，不影響正式版，也不做程式內更新。安裝時會從網路下載 Python 和套件，要連網。

執行紀錄：`%LOCALAPPDATA%\LeihuLiantaiTest\logs\app.log`。

### 3. 逐項測試

| 項目 | 做法 | 預期 |
|---|---|---|
| 高品質照常使用 | 匯入一首歌 | 進度顯示「（高品質）」；log 沒有「改用標準模型」 |
| 第一段分離退回 | 開著吃顯示卡記憶體的程式（遊戲、VTube Studio）處理一首長歌 | log 出現「高品質人聲分離失敗，改用標準模型」（前面有顯示卡處理程式寫的「[gpu] 分離失敗診斷」那一段）或「顯示卡太忙，這首改用 CPU」；改用標準模型時歌曲說明有「高品質人聲分離模型無法使用，這首改用標準模型」（說明被收尾清掉的修正在 PR #23，已在 `refactor` 裡；要用 rebase 之後重建的測試版，Codex r2-4）；歌曲提示**不會**出現「AI 分離失敗，改用聲像分離」 |
| 停止不改跑聲像分離 | 分離途中按「停止」 | 直接停下；log 沒有「AI 分離失敗，改用聲像分離」 |
| 長歌的旋律分析 | 開直播模式（只用 CPU）處理一首超過 4 分鐘的歌 | 不會出現「顯示卡處理程式 600 秒沒有回應（pitch）」 |
| 第 1 批：顯示卡忙碌判斷 | 讓其他程式把顯示卡佔滿（剩不到 3.5 GB，或使用率 85% 以上）再匯入一首歌 | log 出現「顯示卡記憶體只剩…」或「顯示卡已被其他程式用到…%」，這首改用 CPU 輕量處理 |
| 第 1 批：`nvidia-smi` 的負擔 | 工作管理員觀察 | `nvidia-smi.exe` 大約每 20 秒跑一下，CPU 使用率很低 |
| 顯示卡忙碌不來回切換 | 讓顯示卡使用率在 85% 上下跳（例如開著遊戲、時而暫停）約 10 分鐘 | log 的「[live] …新的分離、音高分析改用 CPU」和「恢復正常處理」不會每 20 秒交替；恢復前至少隔約 1 分鐘 |
| app.log 時間 | 打開 app.log | 每一行開頭有 `月-日 時:分:秒`（含 `[gpu] 子程式 … 啟動` 這些顯示卡處理程式的訊息）；沒有成功的 `POST /api/overlay` 那幾行 |

## 這台筆電測不到的

- **6 GB 門檻**：RTX 3060 筆電版回報約 6.44e9 位元組（6 GiB），比門檻 `6e9` 大，照常用高品質。這正好確認「6 GB 以上照常」；要測門檻本身：關掉測試版，把**測試版安裝資料夾**裡的 `%LOCALAPPDATA%\LeihuLiantaiTest\pipeline.py` 的 `HQ_MIN_VRAM = 6e9` 暫時改成 `7e9`，再開測試版、匯入一首歌，進度不會出現「（高品質）」、歌曲的分離模型是標準模型；測完改回 `6e9`。改 repo 裡的 `pipeline.py` 沒有用：測試版跑的是安裝資料夾裡的程式，數值基準也是從指定的 commit 匯出程式。
- **重疊處理時記憶體不夠**：顯示卡記憶體 10 GB 以上、設定開了「閒置時兩首同時分離」才會兩首同時分離（`parallel_ok`），這台不到 10 GB。
