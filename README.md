# game-trend-radar-backend

遊戲熱度追蹤與分析的私人資料蒐集端。此 Repository 保持 **Private**；公開網站放在 `game-trend-radar`。

## 已部署：第三方 4,000 優先初篩＋Steam XML 5,000 正式驗證

- 4,000 是第三方資料的**優先安排官方驗證**門檻，**不是**公開網站的上榜門檻。第三方回傳 >= 4,000 或沒有任何人數／沒有群組 ID 的遊戲，優先用 Steam Community XML 的 `memberCount` 確認；正式輸出仍只保留官方確認 >= 5,000 的遊戲。第三方數字寫在獨立私人 `data/steam_prefilter_state.json`，不污染 `data/steam_followers_cache.json` 或公開站的 `followers`。
- `steam-two-phase.yml` 每批先使用現有 `STEAM_WEB_API_KEY` 對最多 **200 款未經初篩候選**解析 Steam 群組，再向 `api.steam-groups.com/api/groups/bulk` 一次取得其第三方暫定人數。每一批必須所有解析和 bulk 回應完整成功才保存該批初篩游標；遇到 HTTP 429／授權／第三方錯誤時不把失敗遊戲分類為低關注。
- 各批隨即用原本 XML 收集器，優先查第三方 >=4,000 和第三方缺資料的遊戲，每次最多 **50 次新的官方 XML**，保留現有 30 秒節流、官方 cache 與 checkpoint。初篩整份候選表且優先佇列清空後，原本從 **685** 開始的 `next_follower_index` 繼續循序複查**所有低於 4,000 的剩餘遊戲**；僅官方逐一驗證完成後才標記整年初始化完成。
- 進度有兩個不同意義的游標：`prefilter_next_index` 是第三方初篩到哪一款；`next_follower_index` 是原有**完整官方 XML**順序複查游標。前者快速前進時，後者可以維持在 685，並不代表初篩停擺。公開 JSON `initialization` 會附上初篩進度欄位，但不公開第三方人數。既有 11,467 款候選、官方 cache、已公開的 48 款、舊版游標都不重設。
- 新增 `scripts/steam_follower_prefilter.py`、`tests/test_steam_follower_prefilter.py`；[完整測試 70 passed](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35454016394)。目前 GitHub 正式工作仍指定 `runs-on: [self-hosted, linux, steam-followers]`，**程式部署與單元測試通過不代表已開始實際批次收集**。Runner 上線後先確認舊工作是否 Queued，再按原本 supervisor 啟動方式接續，不要同時發多個 collector。

## Steam 批次群組查詢：2026-09-19 實測紀錄

以下是獨立、只讀的探針結果，**不代表已替換正式 Followers 收集器**。使用者的 Steam Web API 金鑰僅由 GitHub Actions secrets 注入；測試不輸出金鑰。

- [群組批次探針](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35451413393)：既有快取的 12 個 AppID，Steam XML `groupID64` 和 `memberCount` 都成功（12/12）；對其中 6 個執行 `ISteamUser/ResolveVanityURL`、`url_type=3`，6/6 成功且回傳 GroupID64 與 XML 完全相同。
- 同一次探針：匿名 Steam CM 登入成功（eresult=1），送出含 10 個群組的 `CMsgClientGetClanActivityCounts`，但未收到 `ClientGetClanActivityCountsResponse` 或任何對應的 `ClientClanState.user_counts.members`（0/10）。所以**尚未證明能透過匿名 CM 批次查總會員數**；Steam Web API Key 不是 Steam 使用者登入憑證。
- [XML 間隔探針](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35451566114)：每秒開始一個舊 AppID 的 XML 查詢，前 6 個 HTTP 200，第 7 個 HTTP 429 後立即停止。6 筆成功請求平均回應 0.371 秒；第一次群組探針的 12 筆 XML 約每 2 秒間隔全部成功。但兩輪請求相近，不能據此推論 2 秒間隔可長期安全運行。
- 暫不降低正式的 30 秒 Followers 查詢間隔，也不修改 `data/steam_candidate_state.json`、cache 或公開前端。若持續追查 CM，必須先解決會員總數回傳及 XML 比對；不要將線上／遊戲中／聊天中人數誤當 Followers。
- [第三輪 CM 診斷與 Metadata 探針](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35452231242)：使用既有 API Key，3/3 遊戲 GroupID64 解析成功；`ICommunityService/GetClanMetadata` 回傳 HTTP 401（因此未取得會員總數）；匿名 CM 登入結果 1，3 群組查詢後觀察約 34 秒，沒有 `ClientGetClanActivityCountsResponse`、`ClientClanState` 或偵測到連線中斷。不能推論帳號登入一定會改善，也不能以 Web API Key 當成 Steam 帳號憑證。若要測登入帳號，應使用使用者自行管理的專用測試帳號於受信任的本機互動登入，**不要**將密碼、Steam Guard 碼、登入金鑰或 Cookie 提交 GitHub、Actions 日誌或交給聊天助手。


## Steam Groups 第三方批次 API：第四至第六輪實測（2026-09-19）

來源：[steam-groups.com API 文件](https://steam-groups.com/docs)，為**非 Valve** 的第三方公開資料庫。其 `POST /api/groups/bulk` 文件列出最多 10,000 個短群組 ID；實測只測到最多 205 個。傳入 Steam 群組的完整 SteamID64 會回 HTTP 500，使用 `group_id64 - 103582791429521408` 得到的短 ID 才有成功回應。

| 實驗 | 群組 ID 解析 | 第三方人數有資料 | 耗時 | 準確性風險 |
|---|---:|---:|---|---|
| [12 款已知群組](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35452722689) | 已知 ID 12/12 | 9/12；單次 bulk 約 0.679 秒 | 整次測試約數秒 | 9/9 比先前 XML 少 |
| [60 款分層快取樣本](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35452832692) | 60/60 | 56/60；單次 bulk 約 0.461 秒 | 30.44 秒 | XML ≥5,000 的 20 款中 2 款低於第三方門檻 |
| [205 款分層快取樣本](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35452968478) | 205/205 | 188/205；單次 bulk 約 0.943 秒 | 100.81 秒 | XML ≥5,000 的 45 款中，第三方低估至 <5,000 有 7 款、找不到 1 款 |

205 款成功回傳的第三方計數**全部低於**既有 Steam XML 計數；這不證明計數來自同一時間／同一定義。API 未提供可供校驗的會員計數更新時間，`isLastSeen=false` 也不等於人數剛更新。這些是**分層抽樣**，不是全部 11,467 款的真實覆蓋率預測。現有私人 cache 的 1,063 筆中，Steam XML ≥5,000 有 48 筆；樣本取了較多高關注遊戲以檢查漏判。

**當時的研發方向（現已依上方 4,000 優先流程部署程式，仍待 runner 實際運行）：** 先用既有 Steam Web API Key 的 `ISteamUser/ResolveVanityURL`、`url_type=3` 取得 GroupID64，再轉短 ID 批次查第三方。結果只能標記成 `provisional/third_party` 供優先排序；正式前端的 `followers` 與 ≥5,000 篩選仍須使用近期的 Steam 官方 XML 已驗證計數。第三方找不到、接近門檻或與既有官方 cache 衝突者優先以 XML 補查；低關注者繼續背景驗證，不能直接視為已完整掃描。未經長時間測試不可把 205 款吞吐率外推成 Steam Web API 的保證速率。任何 429 都停止並退避。

[SteamDB FAQ](https://steamdb.info/faq/) 明確禁止自動抓取／爬取其網站，也未提供一般公開 API；本專案不能改成 SteamDB 自動爬取來規避官方查詢成本。

## 目前正式 Steam 初始化流程（2026-09-19）

以下為現行流程；下方的「6 個兩月區段」及 `update-steam.yml` 是舊版手動復原流程，不可用於接續目前的 Followers 游標。

1. `steam-two-phase.yml` 已掃描完台灣日期未來 365 天，先以第三方 >=4,000 人初篩每批最多 200 款、優先官方 XML 確認每批最多 50 次新的 Followers 查詢；初篩完會回到原本完整順序補查。已查詢的資料沿用私人 cache 與 checkpoint。
2. `steam-candidate-supervisor.yml` 可手動啟動，且會在每次 `Steam candidates then Followers` 工作完成後由 `workflow_run` 自動接力；工作執行中或上批失敗時不會重複派送。**目前沒有 cron 自動開機首次派送**。
3. `publish-steam-preview.yml` 的近期上市 Followers 查詢也已改到同一個專用 runner，避免額外消耗私人專案的 GitHub-hosted 分鐘。
4. 三個工作流程都要求 `runs-on: [self-hosted, linux, steam-followers]`。這是執行機器的要求，單純修改 YAML **不會提供一台免費主機**。自架 runner 不計入 GitHub-hosted Actions 分鐘，但機器與網路須自行提供。

### 一次性啟動專用 runner

- 在本私人 Repository 的 `Settings → Actions → Runners → New self-hosted runner` 選擇 Linux，於自己管理、可信任的常駐 Linux 主機按畫面中的即時指令下載與註冊（註冊 token 有時效，不要存進原始碼）。
- 註冊時設定自訂標籤 `steam-followers`；GitHub 自動的 `self-hosted` 與 `linux` 標籤也必須存在。確認 runner 在 GitHub 頁面顯示 **Online**。
- 確認主機有 Git、Python 3.12 的安裝能力、可連 GitHub/Steam 的網路，以及供資料和 pip 使用的空間。於 Linux 可依 GitHub 提示用 `sudo ./svc.sh install`、`sudo ./svc.sh start` 安裝常駐服務。
- Runner Online 後，到 `Actions → Steam candidate supervisor → Run workflow` 手動啟動一次；之後 Followers 批次完成會觸發下一次 supervisor。若先前已有排隊中的 workflow，先檢查狀態，避免重複手動派送。
- 追蹤進度看 `data/steam_candidate_state.json` 的 `prefilter_next_index`（第三方初篩）和 `next_follower_index`（官方完整複查），以及公開站 `data/steam_upcoming.json` 的 `initialization`。**不要重設游標或清空 cache**。


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
