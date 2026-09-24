from __future__ import annotations

import json
from pathlib import Path

from scripts.refresh_published_chinese_titles import candidate, refresh, update_title


def test_chinese_title_priority_and_preserves_source() -> None:
    row = {"appid": 4019220, "name": "Dressmaker", "name_en": "Dressmaker",
           "release_start": "2026-09-22", "followers": 12629}
    updated = update_title(row, "繁體名稱", "针影裁梦")
    assert updated["display_name"] == "繁體名稱"
    assert updated["display_name_source"] == "tchinese"
    assert updated["name_zh_cn"] == "针影裁梦"
    assert updated["name_zh_cn_traditional"] == "針影裁夢"
    assert updated["followers"] == row["followers"]
    assert updated["release_start"] == row["release_start"]

    cn = update_title(row, None, "针影裁梦")
    assert cn["display_name"] == "針影裁夢"
    assert cn["display_name_source"] == "schinese_converted"
    assert candidate("Dressmaker", "Dressmaker") is None
    assert candidate("针影裁梦", "Dressmaker") == "针影裁梦"


def test_refresh_only_published_qualified_and_keeps_history(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "games").mkdir(parents=True)
    (data / "calendar").mkdir()
    a = {"appid": 4019220, "name": "Dressmaker",
         "release_start": "2026-09-22", "release_end": "2026-09-22",
         "followers": 12629, "release_precision": "day"}
    b = {"appid": 999, "name": "Below threshold",
         "release_start": "2026-09-23", "followers": 3001}
    (data / "index.json").write_text(json.dumps({"version": 2, "months": ["2026-09"]}),encoding="utf-8")
    (data / "calendar" / "2026-09.json").write_text(
        json.dumps({"version": 2, "month": "2026-09", "count": 2, "games": [a,b]}),encoding="utf-8"
    )
    (data / "games" / "4019220.json").write_text(json.dumps(a),encoding="utf-8")
    (data / "games" / "999.json").write_text(json.dumps(b),encoding="utf-8")

    class FakeResponse:
        status_code = 200
        def __init__(self, items): self.items = items
        def raise_for_status(self): pass
        def json(self): return {"response": {"store_items": self.items}}

    class FakeSession:
        def get(self, url, *, params, timeout):
            payload = json.loads(params["input_json"])
            assert [item["appid"] for item in payload["ids"]] == [4019220]
            language = payload["context"]["language"]
            return FakeResponse([{
                "appid": 4019220,
                "name": "Dressmaker" if language == "tchinese" else "针影裁梦",
            }])

    stats = refresh(data, session=FakeSession(), interval=0)
    assert stats["published_qualified"] == 1
    assert stats["changed_games"] == 1
    assert stats["changed_months"] == ["2026-09"]
    doc = json.loads((data / "calendar" / "2026-09.json").read_text(encoding="utf-8"))
    assert len(doc["games"]) == 2
    assert doc["games"][0]["release_start"] == "2026-09-22"
    assert doc["games"][0]["display_name"] == "針影裁夢"
    assert doc["games"][1] == b
    assert json.loads((data / "games" / "999.json").read_text(encoding="utf-8")) == b
