# 後端架構與分批遷移

採用 Python 模組化批次資料管線。Cloudflare 負責觸發，GitHub Actions 執行有時限的工作；Git checkpoint 保存進度，公開 JSON 提供前端資料。既有儲存庫邊界與排程保留。

## 目標分層

| 層 | 職責 | 目前位置 |
| --- | --- | --- |
| domain | 收錄資格、日期權威及佇列、成長視窗的純規則 | 共用 `radar_core.domain` 與本專案 `radar_backend/domain/` |
| application | 候選各階段、官方批次與成長觀測的協調 | `radar_backend/application/` |
| adapters | HTTP、來源身分驗證與既有來源橋接 | `radar_backend/adapters/` |
| state | checkpoint、快取、冷卻、原子寫入及合併 | `radar_backend/state/` |
| publication | 凍結輸入、合併最新資料及發布回條 | `radar_backend/publication/`；Git 發布由 `radar_core.publication` 確認 |
| jobs | 命令列、依賴組裝及工作入口 | `radar_backend/jobs/`；結果契約仍為 `radar_core.jobs` |

## 共用核心版本

`radar-core` 0.2.0 無第三方執行依賴，支援 Python 3.12。四個專案透過 `requirements-core.txt` 安裝同一個完整提交版本：

```
bf1d4bc64b361ec35cd4041d78c5016396d5d785
```

原有四個 `twitch_steam_admission.py` 保留為相容入口，匯出同一組 15 個公開符號。規則實作只有一份，沒有本地備援副本。版本更新先跑四個專案的離線測試，再一起更新 SHA。

開發環境：`python -m pip install -r requirements-dev.txt`。單獨測試核心：`python -m unittest discover -s packages/radar-core/tests -v`。

先合併核心 PR，使用保留提交歷史的 merge，確保固定版本一直可達；接著合併 Steam、Twitch、Content 與前端 consumer PR。前端 consumer 調整的是 Python 資料腳本。

## 工作完成契約

工作結果區分 `complete`、`partial`、`cooling_down`、`skipped`、`failed`。`complete` 必須同時有完整收集與狀態持久化；需要發布的工作另須確認公開資料已交付，才有 `successful=true`。

官方 Followers worker 只有 checkpoint 實際 push 成功、當日前置篩選完整且沒有未解項目，才宣告完成。Growth 收集器先產出部分／冷卻狀態；workflow 確認 backend checkpoint 與 frontend JSON 保存成功後，才寫入發布回條。部分有效觀測仍可發布，但不會阻止當日後續恢復執行。

## Followers 來源邊界

每次查詢必須有已解析的官方 Group ID，並比對 XML 的群組身分。未知群組留待解析；超時、429、無效 XML 不轉成零值。當日已驗證的官方觀測可跨 hourly 與 growth 工作重用，保留真正量測時間。

兩個工作共用既有官方 checkpoint 的冷卻期限與官方觀測；growth 的觀測保存在 `official_growth_observations`。提交時只合併自己的觀測與冷卻欄位，保留最新 main 的佇列及其他狀態。毀損 JSON 應中止，不能當成空狀態覆蓋。

既有每次請求間隔、每批上限、工作時限、Secrets、資格條件及 checkpoint 資料保留。歷史 backlog workflow 改為明確手動觸發，避免修改架構檔案時意外啟動長時間收集。

## 第二批遷移

| 工作 | 純規則 | 用例 | 外部來源與狀態 | 命令列 |
| --- | --- | --- | --- | --- |
| 候選管線 | `domain/candidates.py` | `application/candidates.py` | `adapters/candidate_sources.py`、`state/candidate_store.py` | `jobs/candidates.py` |
| 官方 Followers | `domain/official_queue.py` | `application/official_followers.py` | `adapters/official_followers.py`、`state/official_followers.py`、`state/official_checkpoint.py` | `jobs/official_followers.py` |
| 成長觀測 | `domain/growth.py` | `application/growth.py` | `state/growth.py` | `jobs/growth.py` |
| 成長保存及發布回條 | `domain/growth.py` | workflow 依真實 push 回條協調 | `state/growth_checkpoint.py`、`publication/growth.py` | `jobs/growth_checkpoint.py` |

上表路徑皆相對於 `radar_backend/`。純規則不讀檔、不查 HTTP、不觸發 Git。用例接受來源、狀態、時鐘與等待的依賴，離線測試可以使用記憶體狀態及假來源。

既有 `scripts/steam_candidate_pipeline.py`、`experiment_official_daily_catchup_250.py`、`collect_public_growth.py`、`persist_growth_checkpoint.py` 保留原匯入及命令列介面。相容函式在呼叫時把舊公開依賴交給用例，維持既有呼叫端與測試的 monkeypatch 行為；沒有模組身分替換或第二份規則實作。可使用原 `python -m scripts.<入口>`，或新的 `python -m radar_backend.jobs.<工作>`。

`external_schedule.py` 的成長完整性判斷與發布回條改由 domain/publication 單一實作，保留原公開函式。日候選 CLI 回報收集進度，最終 Git 保存仍由既有 workflow gate 確認。

歷史 sparse checkout 與離線 CI 已加入 `radar_backend/`。跨 repo 的 Content 靜態驗證在其自身目錄執行，以隔離各專案的 Python package 與工作路徑。資料路徑、JSON 欄位、請求上限、資格條件、正常排程及固定核心版本不變。第二批 PR 接在第一批分支之後；先合併第一批，再將第二批 base 調整為 main。

## 第三批：快照與發布

共用核心的 `snapshot_revision` 以排序後的 UTF-8 JSON 計算 SHA-256；無效 JSON、非有限數值及不合法的路徑應中止發布。`publish_with_retry` 每次先取得遠端最新版本，再用同一份凍結輸入重新合併、只提交指定的 JSON 路徑，最後等待 Git push 成功。重試不重新收集來源，也不更新觀測時鐘。即使資料沒有改變，仍須確認遠端接受目前提交，才有成功回條。

| 版本 | 意義 | 保存位置 |
| --- | --- | --- |
| `input_revision` | 收集工作使用的輸入版本 | 發布 metadata 與回條 |
| `payload_revision` | 凍結批次的 JSON 雜湊 | 發布 metadata 與回條 |
| `dataset_revision` | 公開資料的雜湊，排除包含此欄位的 manifest | 公開 metadata |
| `target_snapshot_revision` | 提交內指定 JSON 路徑的實際快照雜湊 | 回條 |
| `published_revision` | push 確認成功的 Git 提交 SHA | runner 的回條 artifact |

Git 提交不能在自己的內容中寫入自身 SHA。因此 `published_revision` 在 push 成功後才寫到工作輸出，與同一提交內的資料版本分開。失敗或跳過的工作不沿用先前成功回條；workflow 只有取得本次回條才設定 `published`／`state_persisted`。完整性 gate 仍分別檢查收集、checkpoint 保存及公開發布。

Steam 的目錄、growth 公開資料、growth checkpoint 及 hourly 佇列批次透過 `jobs/publish_steam.py` 進入發布層。Twitch 與 Content 使用各自的 publication 用例，保留來源驗證、合併規則與 JSON 契約，共用 Git 重試機制。發布前凍結時間與輸入；併發更新時保留最新狀態，無法安全合併的毀損或過期資料會中止。

核心版本先以獨立 PR 發布，再以該完整 SHA 固定四個 consumer。第三批 consumer PR 接在第二批分支後，避免夾帶前兩批 diff；合併依序為核心、前兩批 consumer、第三批 consumer。Steam 的 growth 發布使用前端 builder 的 `--observed-at`，因此前端第三批須先於 Steam 第三批合併。調整 PR base 時保留提交歷史，確保固定 SHA 可達。

## 尚未遷移

候選來源 adapter 仍橋接既有 Steam Store 日期、成人篩選與公開目錄 helper；官方 worker 的上市日期複驗、master upsert 與 content dispatch 仍保留在相容入口，由 `adapters/official_catalog.py` 提供明確的舊流程橋接。這些原本已服務正式資料流程，需和後續發布邊界一起遷移。

官方 worker 執行中的中途 checkpoint、daily 私有狀態保存，以及歷史一次性工具的 Git/rebase 流程仍保留。第三批遷移公開發布和批次狀態合併，尚未統一所有工作中的 checkpoint；content dispatch 的 HTTP 呼叫也仍由原流程執行。
