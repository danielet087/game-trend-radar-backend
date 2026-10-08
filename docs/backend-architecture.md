# 後端架構與第一批遷移

採用 Python 模組化批次資料管線。Cloudflare 負責觸發，GitHub Actions 執行有時限的工作；Git checkpoint 保存進度，公開 JSON 提供前端資料。既有儲存庫邊界與排程保留。

## 目標分層

| 層 | 職責 | 第一批位置 |
| --- | --- | --- |
| domain | 收錄資格、日期權威等純規則 | `packages/radar-core/src/radar_core/domain/` |
| application | 收集、恢復、驗證與發布用例 | 本批保留既有 scripts；後續逐步拆出 |
| adapters | HTTP、來源身分驗證、快取 | `scripts/steam_official_followers.py` |
| state | checkpoint、冷卻、原子寫入及合併 | Followers 的 CooldownStore、growth checkpoint 合併器 |
| publication | 公開資料發布與成功回條 | Growth workflow 的 checkpoint / frontend 發布確認 |
| jobs | 排程入口與工作結果 | `radar_core.jobs`、`scripts/external_schedule.py` |

## 共用核心版本

`radar-core` 0.1.0 無第三方執行依賴，支援 Python 3.12。四個專案透過 `requirements-core.txt` 安裝同一個完整提交版本：

```
67fc611b448be08808956637fcfc9342a509ce9b
```

原有四個 `twitch_steam_admission.py` 保留為相容入口，匯出同一組 15 個公開符號。規則實作只有一份，沒有本地備援副本。版本更新先跑四個專案的離線測試，再一起更新 SHA。

開發環境：`python -m pip install -r requirements-dev.txt`。單獨測試核心：`python -m unittest discover -s packages/radar-core/tests -v`。

先合併核心 PR，使用保留提交歷史的 merge，確保固定版本一直可達；接著合併 Steam、Twitch、Content 與前端 consumer PR。前端 consumer 調整的是 Python 資料腳本。

## 工作完成契約

工作结果區分 `complete`、`partial`、`cooling_down`、`skipped`、`failed`。`complete` 必須同時有完整收集與狀態持久化；需要發布的工作另須確認公開資料已交付，才有 `successful=true`。

官方 Followers worker 只有 checkpoint 實際 push 成功、當日前置篩選完整且沒有未解項目，才宣告完成。Growth 收集器先產出部分／冷卻狀態；workflow 確認 backend checkpoint 與 frontend JSON 保存成功後，才寫入發布回條。部分有效觀測仍可發布，但不會阻止當日後續恢復執行。

## Followers 來源邊界

每次查詢必須有已解析的官方 Group ID，並比對 XML 的群組身分。未知群組留待解析；超時、429、無效 XML 不轉成零值。當日已驗證的官方觀測可跨 hourly 與 growth 工作重用，保留真正量測時間。

兩個工作共用既有官方 checkpoint 的冷卻期限與官方觀測；growth 的觀測保存在 `official_growth_observations`。提交時只合併自己的觀測與冷卻欄位，保留最新 main 的佇列及其他狀態。毀損 JSON 應中止，不能當成空狀態覆蓋。

既有每次請求間隔、每批上限、工作時限、Secrets、資格條件及 checkpoint 資料保留。歷史 backlog workflow 改為明確手動觸發，避免修改架構檔案時意外啟動長時間收集。

## 後續批次

第二批拆分來源收集、候選驗證、checkpoint 存取與用例協調，逐步縮小大型 scripts。第三批整理公開 snapshot 的 revision、發布回條及 workflow 共用步驟。第一批提供共用邊界與工作結果契約，尚未完成所有 scripts 的分層。
