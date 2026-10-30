# Game Trend Radar — 主後端

本 Repo 負責 Steam 候選、資格與官方 Followers；內容補充由 `game-trend-radar-content-backend` 負責，網站位於 `game-trend-radar`。直播收集器另拆為獨立 Repo。

## 現行流程

1. 台灣時間每日 00:00：`steam-two-phase.yml` 更新未來 365 天候選，檢查商店確切日期與成人內容，再做第三方初篩。
2. 台灣時間 03:00–23:00 的每小時排程：`steam-official-daily-catchup-250.yml` 從持久化佇列查官方 Followers，最多 90 次、至少間隔 30 秒，收集限時 35 分鐘、整個工作上限 60 分鐘以預留收尾時間；遇限流停止並保存冷卻與斷點。Cloudflare 觸發與 GitHub runner 開始執行仍可能延遲。
3. 官方 Followers 達 5,000 後，再確認 Steam 商店的確切日期，寫入 `data/steam_upcoming_master.json` 並送內容事件。
4. 內容後端取得正式大圖／2x 圖、語言、中文名稱、TAG，合併 AppID 檔與公開索引；事件送達不等於完成發布。
5. 內容後端每日 07:30／19:30 對帳：檢查已接受的主清單是否已發布，並補齊缺漏內容。每輪最多 60 款，保留未完成與失敗清單。
6. 公開網站從精簡 `data/catalog.json` 讀清單；詳細頁先讀單款 `data/games/{appid}.json`，相似遊戲另外載入。

### Twitch 新作補入 Steam

Twitch 正式收錄的新作是獨立入口：Twitch ID → Helix 的 IGDB ID → IGDB 官方 Steam 外部 ID，並驗證原始收錄觀眾至少 7,000 人。Steam 身分、正式遊戲類型、台灣完整上市日與成人內容規則仍須通過。可用的真實官方 Followers 直接沿用；缺少 GroupID 或官方 Followers 不再阻擋收錄，`followers`、`follower_checked_at`、`follower_source` 保存為 `null`，前端顯示「未取得／收錄依據 Twitch」。不以 0 代替未知，也不修改一般 Steam 來源的 5,000 Followers 門檻。IGDB 沒有 Steam 連結仍保留待確認，每日重查。

每輪既有 hourly 工作先以不可變 Git／資料庫快照驗證 Twitch 身分鏈，再查 Steam 商店 metadata。收錄後同步保存主清單、匯入狀態與 checkpoint 的 `twitch_admitted_candidates` 回條，移除待查群組及其一般來源副本。重建 frozen／每日佇列會讀取這份獨立收錄證據，避免同一款因缺少 GroupID 被重新加入；官方數值、請求歷史與 Community 冷卻均保留。舊版因 metadata 暫時失敗而未完成收錄的候選仍可續跑。`reconcile-twitch-admissions.yml` 提供人工補跑入口，不增加定時排程。

若 Steam 台灣商店的完整上市日與 Store Browse 時間戳日期不同，已驗證 Twitch 身分的遊戲可採用同 AppID 的 `appdetails cc=TW l=tchinese` 完整日期進月曆。保存日期來源、查證時間與 `steam_taiwan_store_date_authoritative` 政策，同時保留真實 UTC、換算後台灣日期及衝突診斷，不改寫原始時間戳。

主清單及 `data/twitch_steam_import_state.json` 持久化後，送 `steam_game_twitch_discovered` 內容事件；未知 Followers 隨事件保留未知，內容後端重新核對不可變 Twitch 證據與 Steam metadata 後發布。事件失敗保留重試。來源證據 `twitch_admission` 隨清單、內容、索引與成長紀錄保存，正常更新不能移除已接受的來源；後來取得的真實官方數值不能被未知值覆蓋。沿用既有 `CONTENT_BACKEND_TOKEN`，不新增 Secrets。

既有官方 worker 的 Community 429 以 15 分鐘開始逐次退避，最長 24 小時，並遵守更長的 `Retry-After` 秒數或 HTTP 日期；唯一的 Community 冷卻保存在官方 worker checkpoint。Twitch importer 只查商店 metadata、讀官方快取及建立候選，不能自行查 Community，也不另存 Community 冷卻副本。商店 metadata 按 5／10／20／30 分鐘退避；一般 metadata 網路或解析失敗按 5 分鐘開始、最長 1 小時逐次退避。期限只決定何時可再查，實際執行仍依原排程與共用鎖。Community 冷卻期間仍可用真實官方 Followers 快取完成核對。日期衝突與資格排除仍每日核對；來源失效或不再合格的未收錄候選撤回 Twitch 優先資格，原一般候選恢復，不重設已查進度與官方結果。

手動測試可在 Actions → `Steam official Followers - hourly bounded queue` → Run workflow，選 `main`、`trigger_source=manual`、`target_slot` 留空，勾選 `skip_cooldown`。此選項預設關閉，只略過這次 Followers 的冷卻檢查，依目前輪替順序測試最多 1 款；群組 API 與商店 metadata 的重試不受影響。CLI 同時要求 `GITHUB_EVENT_NAME=workflow_dispatch` 與 `SCHEDULE_TRIGGER_SOURCE=manual`，Cloudflare／其他事件不能啟用。測試前不清除持久化冷卻；有效且群組吻合的 XML 成功才依原規則清除現行冷卻。若再次 429 或查詢失敗，立即停止，保存結果與退避，且不縮短原現行／舊佇列尚未到期的期限。這個選項只對本輪有效，不改自動排程或後續政策。

官方 Followers 工作現在先準備群組 ID：沿用合法的既有 `group_id64`，缺少者透過 Steam 官方 `ResolveVanityURL`（`vanityurl=AppID`、`url_type=3`）解析。沿用既有 `STEAM_WEB_API_KEY` Secret，以 `x-webapi-key` header 傳送，不把 key 放進 URL、紀錄或狀態 JSON；禁止轉址。一般與 Twitch 共用既有工作，Twitch 優先，每輪至多 20 次、每次至少隔 1 秒、準備時間至多 120 秒，每個 AppID 不在同輪重試，不新增 Cron。API 回應 `success=42` 才代表本次無符合群組；Twitch 優先候選隔 3 小時再確認，一般候選隔 24 小時；缺 key、429、403、網路錯誤與格式錯誤各自記錄，不能宣稱遊戲沒有群組。群組 API 有獨立冷卻，準備可在 Community 冷卻期間執行，但不清除原 Community 冷卻、不取得或捏造 Followers。更新群組對應後，官方 XML 只查已解析的合法群組 ID；未知者保留待解析，不能改查 `/games/{appid}/memberslistxml` 迴避限制。apply 每次以最新正式候選來源重新核對，保留並行完成數值、已解析 ID 與較新解析紀錄，不從舊批次重建已撤回的 Twitch 候選。

2026-10-02 已確認七筆舊六小時冷卻紀錄全部完成遷移，清除其失效欄位並移除持續執行的遷移程式及舊查詢模式。原始紀錄與遷移證據保留在 Git 歷史；現行測試只驗證仍使用的商店重試、官方佇列及收錄／發布行為。

### Cloudflare 定時觸發

2026-10-03 起，以下三項定時工作由既有 [Cloudflare 排程控制器](https://github.com/danielet087/game-trend-radar-twitch-backend/tree/main/scheduler/cloudflare) 透過 `workflow_dispatch` 觸發。原 GitHub `schedule` 已移除，保留手動與必要的 push/CI 入口；執行時間、共用鎖、冷卻和查詢流程維持原樣。Twitch 新作繼續進入既有官方 Followers 佇列，優先處理，不另設查詢排程。

| Workflow | 台灣定時工作 | 外部 `workflow_dispatch` 輸入 |
|---|---|---|
| `steam-two-phase.yml` | 每日 00:00 | `trigger_source=cloudflare`、`target_slot`、`refresh_today=true` |
| `steam-official-daily-catchup-250.yml` | 03:00–23:00 每小時 | `trigger_source=cloudflare`、`target_slot` |
| `steam-public-growth.yml` | 每日 01:15 | `trigger_source=cloudflare`、`target_slot` |

`target_slot` 使用原訂分鐘的 UTC ISO timestamp，例如台灣 2026-10-03 03:00 是 `2026-10-02T19:00:00Z`。run-name 包含原樣的 `slot=`，讓觸發器查證同一時段是否已送出。Followers 定時工作若開始時已跨入下一個台灣小時，就跳過該舊時段；準備 Twitch 候選後、查詢 Community 前再檢查一次。daily discovery 與 growth 的外部時段必須仍屬本日，錯誤時段或缺少 timezone 會停止。

每日重建在 collection 前持久化 `daily_refresh_slot` 與 `last_reset_date_taipei`，且核對候選 `anchor_date`。相同定時 slot 重送會沿用候選與初篩斷點；遷移當天已存在同日候選、但尚無 marker 時，只補 marker 並續跑，不清空斷點。人工 `refresh_today=true` 仍可強制重建。已落地的 pipeline 進度也在後續步驟失敗時保存。原 Followers 共用鎖、冷卻、Twitch 優先佇列與內容事件保持相同流程。

### 排程與佇列狀態快照

`python -m scripts.export_scheduler_queue_status` 只讀正式來源並產生 `data/scheduler_queue_status.json`，不發任何查詢、不消耗佇列、不改 checkpoint 或快取。剩餘數量與排序沿用官方 collector 的 `make_queue()`：已取得數值者剔除，Twitch 保留優先輪替，缺少群組 ID 且 XML 回傳 HTML 者另列暫停。JSON 同時保留官方請求時間軸、來源更新時間、今日官方佇列成功／429 次數、冷卻與下一個可嘗試的排程時段；時段只代表機會，不保證觸發或查詢成功。昨日初篩不能標成今日完成。

狀態隨既有 hourly 工作的 Twitch 準備／收錄、每 10 筆成功或中斷 checkpoint，以及每日 discovery 重建／結束儲存；沒有新增 Cron 或 Steam 查詢。新的 batch／attempt 會記錄執行 ID，舊資料沒有 ID 時不臆造連結。`generated_at` 是狀態快照產生時間，來源各自的時間列於 `source`。前端須搭配 GitHub Actions 真實狀態判斷被取消或強制中斷的執行，不能將舊 `active` 快照當成仍在查詢。若每日與 hourly 同時寫快照，僅快照衝突可從合併後來源重新產生；checkpoint／主清單衝突仍停下，不能為了顯示狀態覆蓋進度。

## 資料責任

| 資料 | 負責方 | 用途 |
|---|---|---|
| `steam_candidates*.json`、`steam_prefilter_state.json` | 主後端 | 候選、日期／內容篩選與第三方初篩進度 |
| `steam_upcoming_master.json` | 主後端 | 已接受的官方 Followers 與日期 |
| `twitch_steam_import_state.json` | 主後端 | Twitch 反查的收錄、待查與內容事件重試 |
| `experiments/steam_official_daily_catchup/checkpoint.json` | 主後端 | 動態補漏、官方結果、事件送出與冷卻 |
| `scheduler_queue_status.json` | 主後端唯讀匯出器 | 官方佇列排序、暫停原因、時間軸與來源新鮮度 |
| 公開 `data/games/{appid}.json` | 發布器＋內容後端 | 可公開的單款完整紀錄，保留已上市歷史 |
| 公開 `catalog.json`、月份檔、清單檔、舊格式檔 | 同次發布產生 | 同一份資料的各種讀取格式 |
| 公開 `content_refresh_status.json` | 內容後端 | 補齊完成數、待補欄位、失敗與重試時間 |

主後端的 `scripts/public_catalog.py` 與內容後端的同名模組定義相同前端投影。新增前端欄位時需同步兩份投影契約，避免其中一個發布器漏欄位。

## 發布保護

- 來源 JSON 格式錯誤或權威清單空白時停止，避免批次刪除既有遊戲。
- 發布衝突時重新讀取最新前端、合併單款內容並重建索引；不直接把舊版整份 JSON rebase 回去。
- 資料筆數沒變但 Followers／TAG／圖片變更，仍會更新內容版本指紋。
- 已上市遊戲保留；明確的日期／成人排除仍由既有資格規則處理。
- 不將「已送內容事件」「工作成功結束」當成「全年候選完整覆蓋」。

## 已知覆蓋限制

第三方初篩可能低估關注人數；未查得資料與官方限流仍可能造成延遲。現行動態補漏佇列主要處理尚未取得官方數值者，**不等於已對所有既有遊戲實施每日／每三日／每月 Followers 更新**。既有官方值的定期重查應使用獨立 due queue 與同一限流鎖，避免與初次補漏搶額度；本次不重設原有 Followers 斷點或偽造查詢時間。

即將上市頁只顯示未來 45 天；完整公開待上市資料可由月曆與 TAG 探索瀏覽。這個顯示範圍差異不是發布缺漏。

## 驗證

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

早期 CM、第三方來源與初次補漏實驗保留於 [歷史研究](docs/steam-research-history.md)，其中舊排程／私人 Repo 說明不代表現況。

## 獨立直播後端

- [YouTube 後端](https://github.com/danielet087/game-trend-radar-youtube-backend)：自己的程式、測試與 Secrets，只更新前端 `data/youtube_live.json`。
- [Twitch 後端](https://github.com/danielet087/game-trend-radar-twitch-backend)：自己的程式、測試與 Secrets，每小時更新 Twitch 快照、追蹤、歷史、Steam 對照與反查佇列。

本 Repo 已移除直播收集器與對應 workflows，避免重複執行；既有 Git 歷史、Actions 紀錄及前端直播 JSON 保留。Steam 流程及 Secrets 維持原有設定。Twitch 每小時收集由獨立後端與 Cloudflare 排程負責。

## 後端架構

共用核心、工作結果契約與分批遷移範圍，見 [後端架構文件](docs/backend-architecture.md)。執行 Python 工作前請安裝 `requirements.txt`；執行離線測試請安裝 `requirements-dev.txt`。
