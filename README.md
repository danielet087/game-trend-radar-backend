# game-trend-radar-backend

遊戲熱度追蹤與分析的私人資料蒐集端。此 Repository 保持 **Private**；公開網站放在 `game-trend-radar`。

## 目前正式 Steam 初始化流程（2026-09-19）

以下為現行流程；下方的「6 個兩月區段」及 `update-steam.yml` 是舊版手動復原流程，不可用於接續目前的 Followers 游標。

1. `steam-two-phase.yml` 已掃描完台灣日期未來 365 天，接續按候選順序每批最多發送 50 次新的 Followers 查詢；已查詢的資料沿用私人 cache 與 checkpoint。
2. `steam-candidate-supervisor.yml` 可手動啟動，且會在每次 `Steam candidates then Followers` 工作完成後由 `workflow_run` 自動接力；工作執行中或上批失敗時不會重複派送。**目前沒有 cron 自動開機首次派送**。
3. `publish-steam-preview.yml` 的近期上市 Followers 查詢也已改到同一個專用 runner，避免額外消耗私人專案的 GitHub-hosted 分鐘。
4. 三個工作流程都要求 `runs-on: [self-hosted, linux, steam-followers]`。這是執行機器的要求，單純修改 YAML **不會提供一台免費主機**。自架 runner 不計入 GitHub-hosted Actions 分鐘，但機器與網路須自行提供。

### 一次性啟動專用 runner

- 在本私人 Repository 的 `Settings → Actions → Runners → New self-hosted runner` 選擇 Linux，於自己管理、可信任的常駐 Linux 主機按畫面中的即時指令下載與註冊（註冊 token 有時效，不要存進原始碼）。
- 註冊時設定自訂標籤 `steam-followers`；GitHub 自動的 `self-hosted` 與 `linux` 標籤也必須存在。確認 runner 在 GitHub 頁面顯示 **Online**。
- 確認主機有 Git、Python 3.12 的安裝能力、可連 GitHub/Steam 的網路，以及供資料和 pip 使用的空間。於 Linux 可依 GitHub 提示用 `sudo ./svc.sh install`、`sudo ./svc.sh start` 安裝常駐服務。
- Runner Online 後，到 `Actions → Steam candidate supervisor → Run workflow` 手動啟動一次；之後 Followers 批次完成會觸發下一次 supervisor。若先前已有排隊中的 workflow，先檢查狀態，避免重複手動派送。
- 追蹤進度看 `data/steam_candidate_state.json` 的 `next_follower_index` 和公開站 `data/steam_upcoming.json` 的 `initialization`。**不要重設游標或清空 cache**。


## 第一階段：Steam 未上市遊戲

目標：每天整理 Steam 未上市遊戲，保留「未來一年內可能上市」且 Followers >= 5,000 的項目。

資料來源直接使用 Steam：

- Steam Store `comingsoon` 搜尋：取得 AppID、名稱、圖片、上市時間。
- Steam Community 遊戲群組 `memberCount`：作為 Steam Followers 數量。
- 不自動爬 SteamDB；SteamDB 僅作人工交叉確認。

沒有精確日期的 `Coming Soon / TBA` 第一版不納入「未來一年」清單，避免把無法確認日期的遊戲硬塞進結果。

### 本機執行

```bash
python -m pip install -r requirements.txt
python -m scripts.update_steam
```

預設輸出：`output/steam_upcoming.json`

可調整條件：

```bash
python -m scripts.update_steam --country TW --days 365 --min-followers 5000
```

### 測試

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

## 下一階段

1. GitHub Actions 每天自動執行 Steam 更新。
2. 將整理後的 `steam_upcoming.json` 發佈到公開前端 Repository。
3. 加入 Twitch 每小時資料。
4. 加入 YouTube Live 每小時資料。
5. 計算台灣／亞洲／全球與新上榜遊戲趨勢。


## GitHub Actions 自動更新

`.github/workflows/update-steam.yml` 會：

- 每天 11:23（台灣時間）自動執行一次。
- 也可以從 GitHub Actions 頁面手動按 `Run workflow`。
- 先跑測試，再產生 `output/steam_upcoming.json`。
- JSON 目前只保存為 Private Repository 的 Artifact，保留 7 天。
- 下一步才會設定跨 Repository 發佈，只把整理後的 JSON 寫入公開前端 `game-trend-radar`。


## Twitch 每小時資料

`.github/workflows/update-twitch.yml` 每小時第 37 分執行一次。

目前收集：

- Twitch 高觀看直播樣本，彙整成遊戲層級的直播主數與觀看人數。
- Steam 關注清單中的遊戲，對應 Twitch 遊戲分類後另外追蹤。
- 語言分布會保存；Twitch Helix API 不提供直播主實體所在地，因此不把語言直接當成台灣／亞洲所在地。

需要在 Private Repository 的 GitHub Actions Secrets 設定：

- `TWITCH_CLIENT_ID`
- `TWITCH_CLIENT_SECRET`

Twitch App Access Token 由程式使用 Client Credentials Flow 自動取得，不需要把 Access Token 手動存進 GitHub Secrets。

如果上述兩個 Secret 尚未設定，Twitch workflow 會安全跳過，不會讓排程顯示失敗。

公開輸出預計為：

```
game-trend-radar/data/twitch_live.json
```


## Steam 初始建立與後續更新策略

為避免第一次對大量 Steam 遊戲逐筆查 Followers 時觸發 HTTP 429，初始資料分成 6 個區段建立：

- 第 1 次：今天起 0～2 個月
- 第 2 次：2～4 個月
- 第 3 次：4～6 個月
- 第 4 次：6～8 個月
- 第 5 次：8～10 個月
- 第 6 次：10～12 個月

每個區段只有在 Followers 查詢完整成功後才會前進到下一段。若有任何 Followers 查詢失敗，隔天會重試同一段；已成功查到的資料保留於 Private cache，不必全部重抓。

一年初始化完成後：

- Followers >= 5,000：每天重新確認
- Followers 3,000～4,999：每 3 天重新確認
- Followers < 3,000：每 30 天重新確認
- 新出現遊戲：第一次看到時立即確認
- 已正式上市的遊戲：移出 upcoming 公開清單，不再作為未上市遊戲追蹤

## YouTube 每小時資料

`.github/workflows/update-youtube.yml` 每小時第 52 分執行。

- 使用 `YOUTUBE_API_KEY`
- 以 YouTube Search 做候選直播發現，再用 `videos.list/liveStreamingDetails` 確認是否仍在直播
- 保存直播主數、同時觀看人數與直播明細
- 頻道若有設定 `snippet.country`，用該國家作為台灣／亞洲／其他地區的代理分類；不視為實際 GPS 所在地
- 公開輸出：`game-trend-radar/data/youtube_live.json`
