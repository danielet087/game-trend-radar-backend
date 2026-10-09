# 後端分層總驗收

驗收日期：2026-10-10（Asia/Taipei）。原規劃的四個 consumer 已完成分層及離線整體驗收；第十七批包含驗收實際發現的修正。PR 尚未合併，不代表正式排程已切換至新版本。

## 固定來源與完整測試

各 consumer 以原 Git revision 建立 exact archive，再套用該 consumer 凍結的修正檔案；每個 consumer 在獨立程序與正確工作目錄執行，沒有以另一個 repo 的同名 `radar_backend` 套件或本地 Core 副本替代。下表 revision 是修正前基線，收尾 PR 分別接續該分支。

| Consumer | 基線 Git revision | 原前置 PR | 修後完整 Python 測試 | 本批新增 |
| --- | --- | --- | ---: | ---: |
| Steam | `606a8af69320ce0fe6ffa8f636af4f16c2a259d6` | [Steam #15](https://github.com/danielet087/game-trend-radar-backend/pull/15) | 3,063 | 20 |
| Twitch | `0dbf6d0e2655094aee68c87511bcfb7d03b7ead3` | [Twitch #5](https://github.com/danielet087/game-trend-radar-twitch-backend/pull/5) | 1,247 | 17 |
| Content | `f157c1b2e6601fa18e5e54e88b12672f1d6365b7` | [Content #4](https://github.com/danielet087/game-trend-radar-content-backend/pull/4) | 348 | 7 |
| Frontend Python／Core | `ca719d4fe5966f8a807b5fc6cea82a305e25aae3` | [Frontend #5](https://github.com/danielet087/game-trend-radar/pull/5) | 78 | 4 |
| 合計 | | | **4,736** | **48** |

完整 suite 均通過；Steam 最終 38.03 秒、Twitch 16.59 秒、Content 6.431 秒、Frontend 2.793 秒。Twitch 另有 77 項 scheduler Node 測試、2 項 workerd 測試及 shell 語法檢查通過。本機 Node 24；既有 Twitch CI 使用 Node 22，並在遠端重新執行同一份 lock 與原 CI 命令。Workerd 本機使用核對全部 31 個 lock 版本的既有安裝依賴，未下載新套件。

Core 為 Python 3.12 的非 editable `radar-core` 0.2.0；四份 `requirements-core.txt`、VCS 安裝來源與五個 installed Python source files 均對應完整 immutable revision：

```
bf1d4bc64b361ec35cd4041d78c5016396d5d785
```

四個舊 admission 入口匯出同一組十五個 Core 公開物件，沒有 fallback 規則副本。Core 對應 [Core #4](https://github.com/danielet087/game-trend-radar-backend/pull/4)，遠端原始 Core 與四份 consumer 基線 CI 均已成功。

## 驗收發現與修正

| 真實觸發 | 原行為 | 修後行為 |
| --- | --- | --- |
| Steam intake 沒有顯式傳入時間 | application 自行讀 wall clock | adapter／舊公開入口先按原順序捕捉所有依賴，再提供 clock port；application 不讀實際時鐘 |
| Twitch 正式 workflow 呼叫原 frontend loader | 動態匯入 legacy mapping collector | 原 CLI 直接接 canonical mapping owner，保留公開 API、四份固定版本輸入與驗證順序 |
| Frontend insights 的 Git push 重試跨台灣午夜 | 每次 builder 重新取時間 | 重試迴圈外取一次 UTC 時刻，每次傳入相同 `--observed-at` |
| Steam 更新量測時間後，Content 收到普通 no-force 事件 | 相同完整 rows hash 被視為相同 browser payload，缺少欄位仍跳過修復，freeze gate 失敗 | 比較真 version、count、games 與 JSON 型別；同步檢查包含 index revision／path，原 repair 分支可恢復一致投影 |
| 原 prefilter 測試只替換 XML collector | 既有快取列仍觸發實際 Store HTTP | 注入 Store source fake，原 XML／cache／cursor 斷言保留；測試阻擋外部 HTTP、DNS 與 socket，吞掉例外也會失敗 |

兩個 producer 的 FIELDS 不同是既有契約；未把全部 producer 的 projection bytes 當成必須相同。Content 發布時從實際 accepted rows 恢復自己的完整 browser projection。完整 rows SHA-256 前二十碼、v3 schema、原始 detail bytes、資格證據與觀測時間不變；合法 same-revision no-op 保留 bytes／mtime。沒有放寬嚴格 JSON 或發布一致性 gate。

## 獨立驗證

- 最終四份凍結修正 source 的跨 consumer 驗收共 350 個斷言，18 項 AST／API／bytes 保真證明。真 Steam builder → Content 普通事件修復 → strict freeze → 本機 bare Git push → 重複 no-op 通過；Twitch 使用同一 immutable commit 的四份輸入，前端真 Python insights 與 TypeScript storage／boundary 讀取同一套產出。
- 涵蓋有效空集合、真實 Followers 零值與 Twitch 資格、成人排除、bool／重複／無效 ID、naive／未來時間、台灣午夜與 date-only、分類證據、metadata 描述變更、移除的 catalog 成員、revision 衝突、缺月份 fallback、來源不可變及 partial／成功回條界線。
- Steam 284 個獨立原 source 語意情境比對一致；兩個公開 collect 的 signatures、34 個依賴的原求值次序、clock truthiness、callbacks／callee 綁定、例外與變更順序保持。Application 僅內部 clock port 改為必填。
- 真正 non-cone sparse checkout：Steam 基線 364 tracked／218 materialized、Content 65 tracked／62 materialized。正式 package 與 CLI 依原 workflow patterns 載入；Twitch 與前端正式 workflows 原本使用完整 checkout。新測試由各完整 offline CI 執行，不更改正式 sparse patterns。
- 阻擋舊 helper owners 後，canonical 入口與實際 callbacks 仍可執行。Twitch 原 loader 在阻擋整個 collectors namespace 下成功；已保留的具體 HTTP collectors 位於外層來源邊界。
- 真 Git 競爭、凍結輸入重播、no-op push、較新結果保留、strict JSON、partial／superseded、失敗清除舊成功收據的測試通過。前端 insights 自身既有 no-diff 提前退出語意保持，本批只補固定時間，不將它冒稱為新的 Core push 回條。
- 16 個修正 source 檔案及全部隔離 checkout source 的 bytes／modes 在獨立驗收前後一致。Steam／Twitch 完整 suite 另封鎖子程序的外部 network，實際外部請求為零。

最終跨 consumer 報告 SHA-256：`c1e8ee784fac2ad82bbfd4347bbe364e6692d3da9df0028a19aa6bc2055c6401`；來源審查：`571a42b6afa65d02532a3995ef195ffd107796bae02e5936ae18c35c7a2d1374`。Steam 獨立語意報告：`757c69ece1a30100417d598b0c211145abb0d742737750067877e4dce18fbfec`。數量分別記錄，不將負向案例或 smoke assertions 重算成完整 suite tests。

## 可重跑的離線命令

先在各 repo 正確工作目錄安裝其固定依賴，以非 editable Core 執行：

```sh
# Steam／Twitch：requirements-dev.txt
python -m pip install -r requirements-dev.txt
python -m pytest -q

# Content
python -m pip install -r content_backend/requirements.txt
PYTHONPATH=content_backend python -m unittest discover -s content_backend -p 'test_*.py' -q

# Frontend Python consumer
python -m pip install -r requirements-core.txt 'requests>=2.32,<3' 'opencc-python-reimplemented>=0.1.7,<1'
python -m unittest discover -s tests -p 'test_*.py' -q

# Twitch scheduler
node --test scheduler/cloudflare/test/*.test.mjs
bash -n scripts/git_frontend_auth.sh scripts/publish_frontend.sh
npm ci --prefix scheduler/cloudflare/test/runtime --no-audit --no-fund
npm test --prefix scheduler/cloudflare/test/runtime
```

## 合併順序與完成界線

1. Steam repository 的 Core [#1](https://github.com/danielet087/game-trend-radar-backend/pull/1) → [#4](https://github.com/danielet087/game-trend-radar-backend/pull/4)。保留提交歷史，確保 consumers 固定的 `bf1d4bc…` 一直可達，不能將它 squash 成另一個 SHA 後刪掉唯一來源。
2. Frontend Python [#4](https://github.com/danielet087/game-trend-radar/pull/4) → [#5](https://github.com/danielet087/game-trend-radar/pull/5) → 第十七批收尾。先提供 `--observed-at`，再合併呼叫它的 Steam 發布用例。
3. Steam consumer [#2](https://github.com/danielet087/game-trend-radar-backend/pull/2) → [#3](https://github.com/danielet087/game-trend-radar-backend/pull/3) → #5 至 [#15](https://github.com/danielet087/game-trend-radar-backend/pull/15) → 第十七批收尾。Core #4 是前述獨立核心 PR，不能混入 consumer 的編號順序。
4. Twitch [#1](https://github.com/danielet087/game-trend-radar-twitch-backend/pull/1) 至 [#5](https://github.com/danielet087/game-trend-radar-twitch-backend/pull/5) → 第十七批收尾；Content [#1](https://github.com/danielet087/game-trend-radar-content-backend/pull/1) 至 [#4](https://github.com/danielet087/game-trend-radar-content-backend/pull/4) → 第十七批收尾。兩條 consumer 分支保持各自前置關係。

每次前置 PR 合併後才 retarget 下一個 PR，保留各批提交；base 或 source 更新後，依實際變更再跑相應 CI。這份總驗收沒有替使用者合併、執行正式 API 收集、發布 production JSON 或部署。

完成範圍是原四 consumer 的模組化批次管線、共用 Core、來源／state ports、正式 jobs 與發布契約。先前已完成的前端 UI 不重建；IGDB console 獨立後端、全面重寫退役／手動實驗工具、引入常駐服務或通用 filesystem repository 另列工作。既有 collection guard 時段檢查、來源 adapter、publication 外層 shard 掃描及三個 Content 文字報告輸出保留。
