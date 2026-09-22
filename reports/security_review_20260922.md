# Game Trend Radar — 公開前安全檢查結果（2026-09-22）

此報告不包含任何金鑰值。Repository 仍維持 Private；未修改 Steam 候選、Followers Cache、同步游標及公開前端資料。

## 已實際完成

- 正式 `steam-two-phase.yml`、`steam-candidate-supervisor.yml`、`publish-steam-preview.yml` 改用 GitHub-hosted `ubuntu-latest`；另外再次逐一核對 **全部 33 個 Workflow**，均使用 `ubuntu-latest`，不再有 `runs-on: ... self-hosted` 或將前端 PAT 放進 `git remote set-url` URL 的寫法。
- Twitch OAuth 不再把 Client Secret 放進請求 URL，並以 HTTP 狀態或錯誤類型回報 API 失敗；Steam Query／YouTube API 的例外也移除含金鑰的 URL。
- 前端跨 Repository 寫入改採暫時性 AskPass，避免把 `FRONTEND_REPO_TOKEN` 寫入 Git Remote URL；Token 的帳號設定由使用者另行處理並已表示完成。
- 已加入 `tests/test_security_redaction.py`。
- 已執行全部可達 Git 歷史掃描：**3,015 個可達 Git 物件、795 個 Blob、148,036,026 Bytes**；掃描到的憑證格式、私鑰、敏感檔名等 finding_counts 為空，`incomplete_object_count=0`。
- 最新成功驗證：**93 passed**。執行紀錄：[Security audit - historical Git objects and tests #35681537114](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35681537114)。
- 曾掃描 `main` 的大型候選與初篩 JSON（約 8.3 / 2.8 / 2.4 MB），未發現常見憑證、個人電子郵件、內部 IP 格式。
- 檢查 GitHub API 可列出的 149 次歷史 Actions run，以及有提供的 Job 日誌；可讀取的日誌沒有命中所檢查的未遮罩 GitHub/Google/Steam/Twitch 憑證格式。抽查連線暫時失敗的 3 筆已重試並讀取成功。

## 限制與未宣稱完成的帳號設定

- 已取消的歷史 preview run `35447303379`，Job `105908348249` 的日誌下載會回傳 `404 BlobNotFound`；**無法審核該 Job 的原始日誌**。它是 cancelled run，不得當作已確認安全。
- 掃描器屬規則式偵測，不保證識別自訂、加密、被切割或未命中特徵的機密內容；歷史掃描限定可達的 Git 物件，不含不可達／已被清除的物件。
- GitHub 連接器對 Branch Protection API 回覆 `403 Resource not accessible by integration`；私人 Repository Ruleset API 另回覆方案／可見度限制，因此**未由助手設定 main 分支保護**，也未更改 Repository visibility。
- GitHub Secrets 的有效值、舊憑證撤銷、Token 權限及 Runner 註冊均屬帳號層級設定；依使用者最新回覆，Token 已處理且 Runner 清單為空，無須重複要求。
- 歷史 scan/test 的第一輪曾失敗，第二輪修正後成功；以最新 `#35681537114` 為準。

結論：本次能以現有 GitHub 連接器修改的程式碼、憑證處理與歷史掃描已完成；尚不能把「沒有命中規則」說成零資安風險，也不能把未授權的 Branch Protection 修改宣稱已完成。
