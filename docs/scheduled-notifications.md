# 雷達排程通知門檻

各排程從套用本規則後分別累計連續失敗；未滿 100 次不觸發 GitHub Actions 失敗通知。
第 100 次通知一次，同一段連續失敗的第 101 次以後保持安靜；成功完成後歸零並重新啟用下一次門檻通知。
同一 run 的重試失敗不重複計數，重試成功可以歸零。跳過、取消、冷卻而跳過完成步驟的工作不算成功，也不清除計數。

適用工作：Steam 每日候選、官方 Followers、成長追蹤、Turso／D1 同步、內容對帳、介紹更新、Twitch、IGDB 主機目錄、前端統計。
這些工作中的手動執行也共用同一計數。獨立 CI 測試及一次性維護工作維持原通知方式。

收集 job 使用 job 層級 `continue-on-error`，內部步驟失敗後的停止、保存與發布條件完全保留。
尾端的 `notification` job 直接讀取原 job／step 的實際結果；只有首次達到 100 次門檻時才使整個 workflow 失敗。
因此 workflow 顯示成功只代表本次未達通知門檻，不可作為資料已完整收集的證據。
Cloudflare 每日補跑仍檢查原收集 job 與成長覆蓋步驟，不會因通知抑制而提前跳過。

計數保存在各 repo 的 `radar-notification-state` 分支 `workflows/<workflow>.json`，與 main、收集資料庫及部署分離。
透過既有 `GITHUB_TOKEN`（通知 job 的 `actions: read`、`contents: write`）讀寫，不需新增 Secrets。
更新採 blob SHA 比對重試，已保存的 run 不重複計數；晚到的舊 run 不覆蓋新狀態。
無法讀寫通知計數時保留既有計數並顯示 warning，不以零重建，也不額外觸發通知。
在第 100 次先保存「已通知」標記，再令通知步驟失敗；若 runner 在兩者之間中斷，通知可能漏發，但不會因重試而連續寄信。

驗證：`node --test .github/actions/scheduled-failure-notice/index.test.cjs`。
