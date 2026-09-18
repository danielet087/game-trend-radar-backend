# game-trend-radar-backend

遊戲熱度追蹤與分析的私人資料蒐集端。此 Repository 保持 **Private**；公開網站放在 `game-trend-radar`。

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
