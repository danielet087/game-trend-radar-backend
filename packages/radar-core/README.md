# radar-core

遊戲雷達後端共用的純規則與工作結果契約。版本 `0.1.0`，支援 Python 3.12 以上，沒有執行期第三方套件依賴，也不會自行查詢 API、寫入資料或啟動收集工作。

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
