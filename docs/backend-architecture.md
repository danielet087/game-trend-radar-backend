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

`external_schedule.py` 的成長完整性判斷與發布回條改由 domain/publication 單一實作，保留原公開函式。日候選 CLI 回報收集進度，第四批以 daily publication 用例確認最終 Git 保存。

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

## 第四批：工作中的 checkpoint 與私有狀態

| 入口 | 用例與狀態 | 工作流程 |
| --- | --- | --- |
| 官方中途及結尾保存 | `publication/official_checkpoint.py`、`state/official_merge.py`，由 official application／jobs 組裝 | hourly 與直接使用同一 worker 的手動 backlog |
| 官方中斷備援 | `jobs/persist_official_checkpoint.py` 重播待保存批次 | 原 `always()` final save |
| daily reset／私有保存 | `publication/daily_state.py`、`jobs/persist_daily_state.py` 的 reset／capture／publish | `steam-two-phase.yml` |

worker 在首次修改 checkpoint／master 前捕捉 baseline，保存時先凍結觀測與時鐘，再三方合併最新遠端。成功後更新 baseline，並 reload 原有 dict，讓握有其參考的快取與冷卻 adapter 讀到併發保存的狀態。權威主清單中的其他 AppID、growth 觀測、Twitch 佇列、群組解析與較新冷卻不會被整份舊 checkpoint 覆蓋；無法判定的相同欄位衝突會中止。

每次保存的待重試批次先寫在 `output/`。push 失敗後仍保留 baseline、原始觀測與固定時間，final 備援從這份批次重播，不能把 reset 後的遠端內容當成原始觀測而產生錯誤成功。如果第 10 筆已保存、第 11 筆寫到本機後中斷，final 會以先前已發布的提交為 baseline，另行保存新增觀測，不沿用舊回條。待重試檔案是恢復輸入，成功回條只在 Git 確認後產生；兩者皆保留為短期 runner artifact。保存本身不重新查 Steam 或派發內容事件。

daily 在 reset 後、收集前 capture 私有狀態。reset 每次對最新狀態判定同一天的重設或恢復，沿用同一台灣日期與 slot；保留 Cloudflare 恢復與明確手動重設的差異，並拒絕覆蓋已前進到更新日期的狀態。私有 final 即使收集失敗，仍保存合法的凍結進度；baseline 未取得則停止保存。官方 worker 的本輪成功數按本輪 attempt AppID 計算，避免把併發 producer 新增的結果誤算為本輪成果。

來源門檻、請求間隔、上限與 worker 時限保留。本日成功後跳過、每六小時失敗恢復的排程控制器未改。手動 backlog 的 sparse checkout 加入 dashboard 與其來源檔，使用同一保存契約；舊 `rebase_checkpoint()` 僅保留相容呼叫，正式保存由 Core 執行。

第四批 PR 接在 Steam 第三批分支之後；前批合併後才調整 base。共用 Core 固定版本維持 0.2.0，不新增其他 consumer PR。

## 第五批：steam-state 狀態傳輸

| 責任 | 位置 |
| --- | --- |
| Followers 觀測的時間戳合併 | `domain/follower_checkpoint.py` |
| 本機 checkpoint JSON 與暫存檔保存 | `state/follower_checkpoint.py` |
| GitHub Contents GET／條件式 PUT 與回條驗證 | `adapters/github_contents_checkpoint.py` |
| 凍結輸入、合併遠端與有界競爭重試 | `application/follower_checkpoint.py` |

上表路徑皆相對於 `radar_backend/`。`collectors/steam_upcoming.py` 保留原建構參數、快取合併函式及 checkpoint methods，負責組裝依賴；各 method 在呼叫時提供原 HTTP、時鐘與 payload helper，維持既有呼叫端及 monkeypatch 介面。這個傳輸使用獨立 `steam-state` 分支的 Contents API，不經 runner 的 main Git reset，也不需要調整 Core 版本。

原保存流程只讀遠端 blob SHA，再 PUT 整份本機快取，可能刪除其他工作剛保存的 AppID。新用例先凍結本機觀測與時間，再讀取遠端完整內容並合併；有效時間優先，兩側皆有效時取較新量測，相同或兩側皆未知時保留先讀到的遠端整列及其未知欄位。遇到 409 只在有限次數內重新讀取遠端、重播同一輸入，不重新查 Followers。其他 HTTP 錯誤、毀損／空內容或不合法回條會停止交付，保留本機觀測與 dirty 計數，不能把讀取失敗當成空狀態後覆寫。

成功回條同時驗證提交 SHA 與本次 JSON 位元組的 blob SHA，確認後才 reload 原快取 dict 並清除 dirty 計數。本機 checkpoint 與其暫存檔在寫入前驗證路徑；錯誤日誌只回報類型或狀態，不包含 token 或原始 HTTP 例外文字。無 token 時仍保存本機；預設每 5 次新查詢、429 與 segment-end 的保存時機、Followers TTL、請求預算及 Steam 來源規則不變。

第五批 PR 接在第四批分支之後。正式及歷史共用 collector 的入口一併使用新的狀態邊界；既有 sparse checkout 加入新傳輸測試，歷史工具的觸發條件維持。

## 第六批：Store 複驗、主清單提升與內容派送

| 責任 | 純規則 | 用例 | 外部來源與組裝 |
| --- | --- | --- | --- |
| Store 日期複驗與主清單提升 | `domain/official_catalog.py` | `application/official_catalog.py` | `adapters/steam_store.py`、`adapters/official_catalog.py` |
| 內容事件資格、signature 與重試 | `domain/content_dispatch.py` | `application/content_dispatch.py` | `adapters/github_content_dispatch.py`、`adapters/official_catalog.py` |

上表路徑皆相對於 `radar_backend/`。正式 `jobs/official_followers.py` 直接組裝新用例與 adapter，不再反向匯入 historical hourly worker。舊 worker 的六個公開函式保留為薄相容入口，在呼叫時提供原時鐘、Store helper、成人名單、HTTP 及派送 callback，維持既有呼叫與 monkeypatch 行為。純規則不讀環境變數、檔案、HTTP 或 Git；用例透過明確依賴協調。

官方 Followers ≥ 5,000、精確 Store 日期與 `date_full` 門檻保留。主清單提升先判定資格，符合後才讀成人排除名單；名單缺失或毀損會中止，不能當成空名單。合併保留既有列的未知欄位，候選的 `None` 不覆蓋既有值，沿用原排序與 metadata。一次提升共用同一個 UTC 時間戳。

合法的台灣商店公告日仍是上市日期權威；timestamp 換算的台灣日期另存為診斷與衝突資訊。Store 不可用時留下複驗時間與失敗狀態，不把舊 metadata 當成新的精確證據。時鐘必須帶時區，Store 判斷使用台灣日期；批次選取與查詢共用同一天，避免跨午夜改變查詢範圍。單筆間隔 0 秒、批次間隔 0.5 秒、預設上限 25、同日略過、已精確略過與 Twitch 佇列排除都保留。複驗回傳選取筆數，並在精確結果上依序執行主清單提升與派送，維持原有計數及 callback 契約。

派送保留 `steam_game_qualified`、七個 payload 欄位、`appid:followers:release_date:store-v2` signature、環境變數優先順序、HTTP headers 與 20 秒 timeout。只有 HTTP 204 才記錄 `dispatched`，相同 signature 的已確認派送才去重；這個確認表示 GitHub 接受事件，不能代表 Content 或前端已發布。非 204 與網路例外保留失敗紀錄，例外日誌只包含類型。重試沿用原排序、Twitch 排除及預設上限 25；符合 Followers 門檻但日期未驗證或未配置傳輸的項目仍計入嘗試筆數。

第六批 PR 接在第五批分支之後，共用 Core 0.2.0 固定 SHA 不變。hourly 請求預算、排程、Secrets、公開 JSON、checkpoint 格式與 Git 發布邊界沒有調整；歷史手動測試 workflow 只補入新測試的 sparse checkout 依賴，不重新啟用自動觸發。

## 第七批：共用 Store 與成人排除邊界

| 責任 | 純規則 | 用例、來源與狀態 |
| --- | --- | --- |
| Store 日期解析、公告日保留與主清單過濾 | `domain/store_release.py` | `application/store_release.py` |
| 候選日期／成人內容分類與完整快照 | `domain/candidate_screening.py` | `application/candidate_screening.py` |
| Store Browse 分批 HTTP、節流與重試 | 無資格判斷 | `adapters/steam_metadata.py` |
| 成人排除名單與 descriptor 判斷 | `domain/adult_exclusions.py` | `state/adult_exclusions.py` |

上表路徑皆相對於 `radar_backend/`。`adapters/steam_store.py` 組裝共用邊界，正式 `CandidateSources` 與官方 worker 直接使用新規則、來源與狀態。`scripts/steam_master_date_gate.py`、`screen_steam_candidates_before_followers.py` 與 `steam_adult_exclusions.py` 保留公開函式、常數、預設路徑及既有命令列介面，以薄 wrapper 在呼叫時提供原 metadata、parser、分類、ledger、HTTP 等待、logger、時鐘及成人／Twitch predicate。正式 Store ports 不再回呼這三個相容 helper。

Store 日期解析仍拒絕 bool timestamp，保留原數字／字串轉換、UTC instant 到 `Asia/Taipei` 的日曆日期，以及 `date_full` 或已上市精確日期的判定。套用 Store 證據先完成純規則才讀時鐘；已公告的 full date 保留，TW 權威來源另外保留原 provider 與原日期驗證時間。主清單過濾維持先讀成人名單、首筆去重、原列身分與順序、未來候選資格及已上市歷史／Twitch 例外。

候選快照依序驗證 games array、讀成人名單、完整分類，再產生 UTC `screened_at`。既有 descriptor 3／4、tag 位置與文字規則、source／rule／reasons／count 及來源不修改的契約保留。成人名單讀取維持原 criteria 與 JSON 驗證及錯誤傳遞，不以空集合備援；既有型別轉換、descriptor 優先順序、容錯與 symlink 行為保持。

metadata transport 保留 GetItems v1、TW／english／realm 1、release／basic info／20 tags、35 款 batch、1.5 秒預設間隔、30 秒 timeout 與四次嘗試。429 冷卻依序 20／40／60／80 秒，其他既有可重試錯誤的等待為 5／10／15 秒；耗盡後中止，不回傳部分結果。成功但缺少單款 metadata 仍交由日期規則判定 unavailable，不新增來源覆蓋率政策。所有等待、monotonic、logger 與 HTTP 邊界都可替換，離線測試不需要正式查詢。

第七批 PR 接在第六批分支之後。固定 Core 版本、正式排程、來源資格、HTTP 預算、Secrets、data／experiments 與 Git 發布流程不變；手動測試 workflow 補入四個新測試檔的 sparse 依賴。

## 第八批：上市日期更新與台灣名稱本地化

| 責任 | 純規則 | 用例與來源 |
| --- | --- | --- |
| 公告日期、年月季視窗與帶 offset 的時間解析 | `domain/release_window.py` | collector 保留 parser wrapper 與 `ReleaseWindow` 匯出 |
| scheduled 日期權威、人工日期訂正與快取來源 | `domain/public_release_dates.py` | `application/public_release_dates.py`、`adapters/public_release_dates.py` |
| scheduled Browse timestamp 查詢 | 無收錄資格判斷 | `adapters/steam_release_timestamps.py` |
| 台灣中文名稱資格、繁體顯示與補名統計 | `domain/localized_titles.py` | `application/localized_titles.py`、`adapters/steam_localized_titles.py` |

上表路徑皆相對於 `radar_backend/`。`ReleaseWindow` 與日期 parser 只有一份純實作，collector 保留原匯出、函式參數及呼叫時可替換的 parser ports。日期更新、預覽、候選本地化及公開 shard builder 直接匯入新日期／名稱 adapter；舊 `scripts/steam_release_dates.py` 與 `steam_localized_titles.py` 保留薄相容 wrapper，原 parser、日期訂正 map、converter、HAN、等待、logger 與 HTTP patch 在呼叫時生效。

此處 scheduled 日期更新沿用有效 timestamp 優先的原契約，與正式主清單 Store gate 的台灣公告日優先分開。date-only 不猜解鎖時刻、不統一平移；帶時區時間與秒／毫秒 timestamp 換算台灣日曆日。訂正 map 只在沒有 timestamp 候選且公告日仍相符時使用，不製造 UTC 證據。候選 timestamp 只取原優先序中的第一個非 `None`／bool 值；無效時不改取後面的值。與公告日期相差超過兩天則保留公告；精確時間的 provenance、快取 fallback 與時間消失時的欄位替換保持。批次修正淺拷貝原列，保留 Followers、未知欄位與嵌套引用。

兩個來源保留不同的傳輸政策：scheduled timestamp 查詢預設 35 款、2 秒間隔、25 秒 timeout、三次嘗試，429 等待 15／30／45 秒，其他既有錯誤等待 5／10 秒，耗盡後回傳已取得的部分資料。台灣名稱查詢使用 TW／tchinese、35 款、1.5 秒間隔、30 秒 timeout、四次嘗試，429 等待 20／40／60／80 秒，其他既有錯誤等待 5／10／15 秒，耗盡則中止。timestamp response 的 2010～2100 年檢查不擴大成 parser 或 resolver 的新門檻；缺單款名稱的 HTTP 成功處理與候選用例原有 95% 覆蓋率 gate 保持。

名稱只來自 Steam 回傳；HAN 範圍與 240 字限制只用於中文名稱資格，不能改成 HTTP 回傳清洗政策。中文來源值保留 Steam 字形，繁體顯示值另存；`name_en` 沿用既有原名選取與 strip，遊戲 `name` 及語言支援旗標不被繁體轉換改寫。OpenCC 在 adapter 組裝；新合格中文名可補入，沒有新合格名稱時保留原已確認中文，stale 顯示欄位清理、顯示優先序及原地補名／例外次序都保持。domain 與 application 不匯入 OpenCC、requests 或舊 collector。

第八批 PR 接在第七批分支之後，固定 Core 版本與 requirements、data／experiments、正式排程、Secrets、請求預算及 Git 發布流程不變。consumer 只更換共用日期／名稱 import；其他函式、命令列與 Git 保存內容維持。手動測試 workflow 補入四個新測試檔的 sparse 依賴。

## 第九批：預覽 metadata 與已發布名稱更新

| 責任 | 純規則 | 用例、來源與狀態 |
| --- | --- | --- |
| 預覽 appdetails response、兩語名稱與 metadata 欄位 | `domain/preview_metadata.py` | `application/preview_metadata.py`、`adapters/steam_preview_metadata.py` |
| 已發布 TW／CN 名稱資格、繁體顯示與 Twitch 既有事實 | `domain/published_titles.py` | `application/published_titles.py`、`adapters/published_titles.py` |
| 已發布名稱單批 Store 查詢 | 無收錄資格判斷 | `adapters/steam_published_titles.py` |
| 已發布 JSON 讀取與內容變更保存 | 無來源查詢 | `state/published_titles.py` |

上表路徑皆相對於 `radar_backend/`。預覽的五個 metadata／名稱 helper 及已發布名稱更新的六個公開函式保留為薄相容入口；CLI、預覽 run／搜尋／Followers／近期上市與 Git 保存函式維持原樣。新 canonical adapter 不回呼這兩個舊入口。舊函式在呼叫時傳入原 fetch、parser、名稱判定、converter、HAN、狀態、Core Twitch predicate、projection、clock、等待、logger 與 HTTP，維持原參數、常數及 monkeypatch 介面。

預覽 appdetails 保留 TW／english 日期解析與 tchinese 名稱查詢，三次嘗試、25 秒 timeout、429 的 60／120／180 秒等待及其他既有錯誤的 5／10 秒等待，最後失敗回傳 `None`。名稱仍只比較 strip 後的非空原字串與英文是否不同，不加已發布名稱的 casefold／HAN／240 字門檻。名稱更新依序等待（包含零秒）、查詢、寫入 name／name_en／name_zh_tw，再加入繁體顯示，例外前完成的 mutation 保持。metadata 先解日期，非精確日即返回；之後建構欄位，只有合法 game／AppID／categories list 才讀驗證 clock，最後交給既有 categories 保存 callback。categories list 引用與 release 展開覆蓋順序保留。

已發布名稱更新只處理公開 calendar 中 Followers 至少 5,000 或已驗證 Twitch 收錄的 AppID，不掃候選、不查 Followers、不新增收錄。來源按排序後的 ID 分批，先 TW 再 CN，所有查詢成功後才開始讀寫 detail。傳輸維持五次嘗試、45 秒 timeout、429 的 15／30／45／60／75 秒等待及其他既有錯誤的 5／10／15／20 秒等待；耗盡則中止，不寫入查詢不完整的新名稱。每次成功語言查詢後的 interval 等待（包括最後一批）由 application 協調，HTTP adapter 不自行加入 interval。回傳 ID 必須是 exact int，原 list membership、重複列與 raw name strip 規則保持；AttributeError 不增加到重試範圍。

名稱判定拒絕英文 casefold fallback，保留 Steam 原 TW／CN 字形與另存的繁體顯示，不改語言旗標。已有名稱、舊顯示欄位及 storage_version=2 的處理保持。更新按 calendar 原順序執行，保留較完整 detail 的未知欄位與 Core Twitch 證據，已驗證 Twitch 的十六個 Followers／日期／Store 事實依原公開列保留或移除。只更新有變更的 AppID 與月份，月份 count／日期保持；即使名稱無變更仍執行原 catalog projection、aggregate 與 index 流程，兩個 clock 的讀取位置維持。

state 保留 UTF-8 JSON object 驗證、原 pretty JSON 與結尾換行；檔案位元組相同時不重寫。讀寫錯誤及已完成寫入的順序照原流程傳出。domain／application 不讀寫檔案、不匯入 requests、OpenCC 或舊 scripts；catalog projection 與 categories 保存仍透過明確 bridge 連接既有 owner，另批遷移。

第九批 PR 接在第八批分支之後。固定 Core／requirements、正式及手動發布 workflows、請求預算、Secrets、data／experiments、來源資格與 Git 發布邊界維持；唯一 workflow 變更是暫停中的手動測試補入四個 sparse 測試檔及命令，不重新啟用退役入口。

## 尚未遷移

正式候選、官方、日期／名稱及已接線的 preview metadata／已發布名稱更新已使用共用來源、規則與用例層。其他 Steam 內容詳細資料流程、Twitch 來源協調、公開目錄 projection 與 categories 保存仍有舊 helper；共用 Twitch 收錄純規則已固定在 Core，不再複製實作。

歷史 prescreen、shortlist、stress 與公開 maintenance 工具的 Git/rebase 仍各自執行，資料所有權及格式不同，尚未全面遷移。Stage 2 prescreen、Stage 3 shortlist 與 preview 是手動／暫停的恢復入口，沒有正式自動排程；舊 `update-steam` 的 collector 入口已硬停止。後續整理需各自定義 cursor、公開資格或 recent-release 的保存契約，不因架構重構重新啟用退役入口。
