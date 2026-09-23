# Steam 官方 Followers：每日動態補漏

此資料夾是獨立於正式網站資料的補漏檢查點，不修改 `data/steam_followers_cache.json`、原始搜尋游標或前端資料。

## 排程與速度

由 **GitHub Actions 自己排程**，台灣時間每天 **06:00～23:00 的每個整點**執行同一份 [GitHub 工作流程](../../.github/workflows/steam-official-daily-catchup-250.yml)，cron `0 6-23 * * *`、`timezone: Asia/Taipei`。原先 5 個同功能 ChatGPT 排程已停用；不再依賴 `requests/YYYYMMDD/HH00.txt` 的 push 觸發。GitHub 排程可能延遲或漏跑，工作流程會拒絕在台灣時間 06:00～23:59 以外由 schedule 發出的正式 Steam 查詢。 每批至多 250 款、查詢起點相隔至少 8 秒、最長 3,450 秒，同一時間不並行另一個官方補漏工作流程。成功 10 款即保存 GitHub checkpoint。首個 Steam 429 立即停止，至少冷卻 48 小時；已查過或資料缺失不冒充 0。

## 動態補漏來源

1. 讀取 [原始 1,317 筆來源](../steam_official_nearfirst_20260922/source_queue.json)，扣掉其獨立 [已核實 384 筆 Checkpoint](../steam_official_nearfirst_20260922/checkpoint.json) 和本資料夾更新成功的結果。舊隊列 checkpoint 尚有 933 筆；經正式 cache 和另一個官方驗證 checkpoint 跨來源去重後，2026-09-23 靜態驗證確認尚有 **905 筆實際待查**（未來新增的每日漏網之魚另計）。
2. 如前段每日 Followers 搜尋確實有新寫入 `data/steam_prefilter_state.json`，且其 `updated_at` 為台灣當天，與 `data/steam_candidates_eligible.json` 的具明確日期、正式遊戲、已通過成人排除之作品合併。合格補漏條件：第三方無可用累積 Followers，或第三方 ≥4,000 尚無官方值。
3. 排除已有累積官方數字的 AppID（包含既有 official checkpoint、正式官方 cache 與本資料夾），依台灣上市日期「今天和未來由近到遠」，已過日期的歷史待查放最後。隊列會增減，**不是固定 1,317 筆**。
4. 當日上游搜尋仍停用或未更新時，只接續既有待查清單，在報告中明確標示來源不新鮮。**本 GitHub 自動補漏只會消化已持久化的候選和上游更新後的漏網之魚；上游「每日 Steam 遊戲資料同步」目前停用，尚未改成 GitHub 自動每日蒐集，因此不能宣稱每天新遊戲的發現也已全自動。**

## 進度與來源

- `checkpoint.json`：從每個小時批次持續保存新增的官方 `memberCount`、日期與缺數／限流狀態，不覆蓋原始 384 筆 Checkpoint。
- `requests/`：先前 ChatGPT 調度留下的歷史請求檔，不再用來觸發補漏；GitHub scheduler 配合 concurrency 確保同時只執行一個官方補漏工作。
- GitHub Actions 每輪自帶摘要與 30 天保留的 JSON Artifact。成功 250 款是上限，不是預先承諾每輪必達。
