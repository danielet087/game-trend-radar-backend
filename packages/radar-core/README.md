# radar-core

遊戲雷達後端共用的純規則、工作結果契約及 Git 發布流程。版本 `0.2.0`，支援 Python 3.12 以上，沒有執行期第三方套件依賴，也不會自行查詢 API 或啟動收集工作。新版保留 `0.1.0` 的收錄規則及 `JobResult` 行為。

## 安裝

在主後端 repository 中開發：

```sh
python -m pip install -e packages/radar-core --no-build-isolation
python -m unittest discover -s packages/radar-core/tests -v
```

其他 repository 應在 requirements 固定不可變的完整 commit SHA，並指向套件子目錄：

```text
radar-core @ git+https://github.com/danielet087/game-trend-radar-backend.git@<40-character-commit-sha>#subdirectory=packages/radar-core
```

升級核心時，在同一個 PR 中更新固定 SHA 並跑 consumer 的規則測試；請勿依賴會移動的 `main`。既有 `scripts/twitch_steam_admission.py` 入口由各 consumer 保留作為相容轉接，以免需要一次修改全部 importer。收錄門檻與日期政策保持既有行為。

## 純收錄規則

`radar_core.domain.twitch_admission` 提供原有 Twitch 入選證據驗證、Steam 台灣日期判斷及既有證據保留函式；網路請求與資料保存由各後端負責。

## 工作結果

```python
from radar_core.jobs import JobResult, JobStatus

result = JobResult(
    job="steam_growth",
    status=JobStatus.COMPLETE,
    collection_complete=True,
    state_persisted=True,
    requires_publication=True,
    published=True,
    target_slot="2026-10-08",
    input_revision="source-revision",
)
assert result.successful
receipt = result.to_dict()
```

只有確認收集涵蓋範圍完整且狀態已保存，才能標記 `COMPLETE`。需要公開資料的工作另外要求 `published=True` 才算成功。發布失敗可以保留已完成的收集證據，但 `successful` 必須是 `False`。

`PARTIAL`、`COOLING_DOWN`、`SKIPPED`、`FAILED` 分別表示部分完成、等待冷卻、這次略過與執行失敗；皆不等於本次成功。已成功過而略過的工作應沿用外部保存的成功回條，不能把略過結果製造成新的成功紀錄。

所有布林欄位只接受真正的 `bool`，不接受 `0`、`1` 或文字。`to_dict()` 包含 `schema_version: 1`，供 workflow、排程控制器與測試使用相同契約。`JobResult.from_dict()` 會檢查版本、必填證據與型別，重新計算成功狀態；如回條中的 `successful` 與證據不一致，會拒絕讀取。已存在但格式損壞的新版回條不能回退舊版成功判斷。

## 凍結輸入與 Git 發布

`radar_core.publication` 提供三個版本識別及同一個有界發布流程。收集器先把資料與觀測時間凍結到 checkout 外的暫存目錄，再進入發布；重試不重新查詢 API，也不把重試時間改成新的觀測時間。

| 欄位 | 意義 |
| --- | --- |
| `input_revision` | 呼叫者提供的來源 commit、來源 bundle 或其他非空版本識別 |
| `payload_revision` | `snapshot_revision(frozen_payload)`，識別這次固定的輸入資料 |
| `target_snapshot_revision` | 實際提交中，允許發布的 JSON 檔案之內容雜湊，包括保留的較新資料 |
| `published_revision` | 遠端 push 已確認的不可變 Git commit SHA |

`snapshot_revision()` 使用 UTF-8、物件 key 排序及緊縮 JSON 計算 SHA-256；陣列順序保留。非 JSON 型別、非字串 key、NaN 及 Infinity 都會被拒絕。實際公開檔案再以 `{path: {"present": true, "payload": ...}}` 計算 `target_snapshot_revision`；明確指定且已刪除的檔案使用 `{"present": false}`。資料集合本身另有不包含 manifest 的雜湊時，請命名為 `dataset_revision`，避免混淆。

```python
from pathlib import Path
from radar_core.publication import (
    SubprocessGitRepository, publish_with_retry, snapshot_revision,
)

# frozen_payload 已從收集結果深複製，觀測時間不再變動。
def apply(latest_checkout: Path) -> None:
    # 重新讀取最新版 JSON，保留較新的紀錄，再套用 frozen_payload。
    # 只可寫入 paths 宣告的檔案；不可在這裡重新收集。
    ...

repository = SubprocessGitRepository(
    frontend_checkout, disposable_checkout=True,
    push_command_prefix=("bash", "/absolute/scripts/git_frontend_auth.sh"),
)
receipt = publish_with_retry(
    repository, apply,
    paths=("data/twitch_live.json", "data/twitch_history/"),
    message="Publish frozen Twitch observation",
    input_revision=source_commit,
    payload_revision=snapshot_revision(frozen_payload),
    max_attempts=5,
)
# 此時才能寫 runner artifact 或更新既有工作報告。
publication_report = receipt.to_dict()
```

每次嘗試都先 fetch 最新遠端版本、reset disposable checkout，再呼叫同一個 `apply`。Git 只 stage 明確允許的 JSON 檔案，commit 後以不可變 SHA 推送；不 force push。push 失敗最多重試 `max_attempts` 次（1 至 20），fetch、內容驗證、callback、scope 或 commit 錯誤則立即停止。沒有檔案差異也必須真正 push 並獲得確認；如果遠端已前進，會重新取得最新版再嘗試，不能只因空 diff 宣稱發布完成。

允許的範圍是明確 `data/<file>.json`、`data/<specified-directory>/`，以及明確 `experiments/<...>/checkpoint.json`。scope 內的更動仍限定 JSON；根目錄、絕對路徑、路徑穿越、`.git`、symlink、未授權的 tracked/index 更動都會被拒絕。收集階段已更動的 owned JSON 可以在輸入凍結後 reset；其他已存在的 untracked/ignored 輸出會保留，而且遠端更新不得覆蓋它們。callback 不能寫入 scope 外的輸出，連 ignored 檔案也會檢查。callback 需要啟動 Python 時，請使用 `-B` 及 `PYTHONDONTWRITEBYTECODE=1`，避免產生 checkout 內的 bytecode cache。

`push_command_prefix` 取代 push 時的 `git` 執行檔；範例中的腳本本身會執行 Git，後面直接接 `push` 參數。token 由既有 askpass 環境提供，不放入 URL、命令列或錯誤訊息。可用 `environment` 傳入環境覆寫；預設使用 GitHub Actions bot 身分建立 commit，並透過單次命令的 `-c` 指定，無須更改 checkout 設定。

`PublicationReceipt.from_dict()` 嚴格檢查 schema、欄位、型別與 hash；未知欄位或缺少證據都會拒絕。只有 `publish_with_retry()` 真正完成 push 後才產生回條，所有失敗均丟出例外且沒有成功回條。實際 `published_revision` 只能在成功後寫入 runner artifact／既有工作報告，不能寫入正在發布的 commit 形成自身 SHA 引用。Git 內的 manifest 可以包含 `input_revision`、`payload_revision`、`dataset_revision`，但不能預先宣稱 `published=true`。收集是否完整仍由既有 `JobResult` 判定，發布成功不會把 partial 收集變成 complete。

本套件測試使用本機暫存 bare Git repository，涵蓋競爭更新、拒絕 push、空 diff、固定輸入重放及 scope 防護，不會收集 live API。
