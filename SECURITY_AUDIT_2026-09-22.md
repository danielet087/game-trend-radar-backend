# 公開前安全稽核（2026-09-22，台灣時間）

本檔不儲存任何 API Key、Token 或憑證原文。本報告僅紀錄本次可驗證的修正、範圍和限制。Repository 在稽核完成時仍是 Private；本次未修改 Steam 候選清單、Followers Cache、既有進度或正式篩選門檻。

## 已提交安全修正

- `.github/workflows/steam-two-phase.yml`、`steam-candidate-supervisor.yml`、`publish-steam-preview.yml` 改用 `ubuntu-latest`；不再依賴 Self-hosted Runner。
- Twitch OAuth 將 client credentials 放入 POST body，HTTP 錯誤只回報安全的狀態碼／例外類型。
- Steam Query／YouTube Data API 的 HTTP 錯誤不再將含有 key 的 URL 原文輸出。
- 新增 `scripts/git_frontend_auth.sh`，跨 Repo Git 寫入採用一次性 AskPass，不再把前端 PAT 寫進 Remote URL。
- `tests/test_security_redaction.py` 驗證 HTTP 錯誤不洩露測試金鑰；`scripts/security_audit_history.py` 用於掃描 Git 歷史。
- 正式蒐集與稽核 Workflow 均維持手動觸發，沒有為了此次稽核永久啟用排程或 push trigger。

## GitHub Actions 實際稽核結果

[2026-09-22 第二輪最終稽核](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35681537114)（GitHub-hosted `ubuntu-latest`，唯讀，不注入其他 Secrets）。

- 可達 Git 物件：3,015；成功掃描獨立 Blob：795。
- 總掃描量：148,036,026 Bytes；不可讀取 Blob：0。
- 兩個遠端分支 `main` 與 `steam-state` 均已納入。
- 常見 GitHub PAT、Google API Key、AWS Key、私鑰、URL 參數憑證、密碼格式、一般 Email、內部 IP 及敏感檔名掃描未命中。
- `pytest`：93 passed。
- 靜態比對不能證明不存在自訂格式或已失效但仍敏感的字串。

### 舊 Actions 日誌

稽核前列出歷史 146 次 Workflow run：143 份工作日誌可讀且未命中本次憑證掃描規則；2 次已取消的 Run 沒有 Job；1 份日誌（Run `35447303379`，Job `105908348249`）GitHub 回覆 BlobNotFound，無法驗證。稽核 Job 本身的日誌也已查閱，沒有輸出機密值。

## 帳號層級尚未完成、需 Repo 擁有者操作

這些步驟無法僅靠目前的 GitHub 程式碼讀寫連線完成，不能聲稱已處理。

1. `Settings → Actions → Runners`：如果舊 Self-hosted Runner 仍註冊，將它 Remove／Force remove；停止機器服務並檢查舊工作目錄。改 Workflow 不會自動解除註冊。
2. GitHub Developer settings → Personal access tokens：新建 **Fine-grained PAT**，限制 `Only select repositories: danielet087/game-trend-radar` 且僅需 `Contents: Read and write`；更新後端 `Settings → Secrets and variables → Actions` 內的 `FRONTEND_REPO_TOKEN`，驗證新憑證可發布後，撤銷舊憑證。若既有值其實是 GitHub App Token，則應在該 App 授權中做對應限權。
3. Repository 公開後在 `Settings → Rules → Rulesets` 為 `main` 設定保護（如拒絕 force push／刪除），對自動寫入機器人保留最少必要的寫入途徑。GitHub API 對目前 Private/方案的 Rulesets 讀取回覆需要升級或改 Public，且現有連線未提供 Admin 寫入操作。
4. 確認 Steam Web API Key、YouTube API Key、Twitch Client Secret 是否曾在其他地方外洩。因一份舊日誌不可讀、原始憑證狀態無法由 Repository 直接取得，公開前如需最大程度降低歷史外洩風險，請重新核發並更新 Secrets，再撤銷舊值。
5. 完成上述帳號步驟之後，再決定是否切換 `Private → Public`；本次未替使用者公開 Repository。

公開不會解除 Steam HTTP 429 節流；正式收集仍維持既有 `>=5000`、候選日期／內容篩選以及持久化游標。
