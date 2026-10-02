# Game Trend Radar — 主後端

本 Repo 負責 Steam 候選、資格與官方 Followers；內容補充由 `game-trend-radar-content-backend` 負責，網站位於 `game-trend-radar`。直播收集器另拆為獨立 Repo。

## 現行流程

1. 台灣時間每日 00:00：`steam-two-phase.yml` 更新未來 365 天候選，檢查商店確切日期與成人內容，再做第三方初篩。
2. 台灣時間 03:00–23:00 的每小時排程：`steam-official-daily-catchup-250.yml` 從持久化佇列查官方 Followers，最多 250 次、至少間隔 8 秒；遇限流停止並保存冷卻與斷點。GitHub 排程可能延遲。
3. 官方 Followers 達 5,000 後，再確認 Steam 商店的確切日期，寫入 `data/steam_upcoming_master.json` 並送內容事件。
4. 內容後端取得正式大圖／2x 圖、語言、中文名稱、TAG，合併 AppID 檔與公開索引；事件送達不等於完成發布。
5. 內容後端每日 07:30／19:30 對帳：檢查已接受的主清單是否已發布，並補齊缺漏內容。每輪最多 60 款，保留未完成與失敗清單。
6. 公開網站從精簡 `data/catalog.json` 讀清單；詳細頁先讀單款 `data/games/{appid}.json`，相似遊戲另外載入。

### Twitch 新作補入 Steam

Twitch 正式收錄的新作是獨立入口：Twitch ID → Helix 的 IGDB ID → IGDB 官方 Steam 外部 ID。找到原清單沒有的 AppID 後，不套用 5,000 Followers 門檻，仍查詢真實官方 Followers、Steam 台灣確切日期、正式遊戲類型與既有成人內容排除規則。資料不足保留待重試；IGDB 沒有 Steam 連結也只是尚未確認，每日重查。

`steam-import-twitch-discoveries.yml` 每小時第 37 分消費前端的 `data/twitch_steam_discovery.json`，與既有官方 Followers 工作共用限流鎖。先持久化主清單與 `data/twitch_steam_import_state.json`，再送 `steam_game_twitch_discovered` 內容事件；事件失敗保留重試。來源證據 `twitch_admission` 隨清單、內容、索引與成長紀錄保存，正常更新不能移除已接受的來源。Twitch 觀測持續沿用 Twitch ID 與圖片，Steam 入口不會反過來冒充 Twitch 新作。沿用既有 `CONTENT_BACKEND_TOKEN`，不新增 Secrets。

Steam HTTP 429 優先遵守 `Retry-After` 的秒數或 HTTP 日期，不縮短伺服器指定的期限。缺少有效標頭時，Community 按 15／30／60 分鐘退避，商店 metadata 按 5／10／20／30 分鐘退避；一般網路或解析失敗按 5 分鐘開始、最長 1 小時逐次退避。期限只決定何時可再查，實際執行仍依排程與共用鎖。Community 冷卻期間可使用已取得的真實官方 Followers 快取完成其他核對，metadata 冷卻仍須等待。收據保存原因、標頭、次數與期限；成功收錄清除各款重試狀態。已證明是舊版固定六小時預設的紀錄及其衍生等待，安全遷移為原失敗起算一小時；無法辨識來源或更新的限流紀錄保留。日期衝突與資格排除仍每日核對。

## 資料責任

| 資料 | 負責方 | 用途 |
|---|---|---|
| `steam_candidates*.json`、`steam_prefilter_state.json` | 主後端 | 候選、日期／內容篩選與第三方初篩進度 |
| `steam_upcoming_master.json` | 主後端 | 已接受的官方 Followers 與日期 |
| `twitch_steam_import_state.json` | 主後端 | Twitch 反查的收錄、待查與內容事件重試 |
| `experiments/steam_official_daily_catchup/checkpoint.json` | 主後端 | 動態補漏、官方結果、事件送出與冷卻 |
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
