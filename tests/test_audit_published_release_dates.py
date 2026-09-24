from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.audit_published_release_dates import audit


def test_audit_removes_uncertain_future_but_keeps_exact_and_history(tmp_path: Path):
    data = tmp_path / "data"
    (data / "calendar").mkdir(parents=True)
    (data / "games").mkdir()
    (data / "lists").mkdir()

    historical = {
        "appid": 1, "name": "History", "release_start": "2026-09-20",
        "release_end": "2026-09-20", "release_precision": "day", "followers": 6000,
    }
    exact = {
        "appid": 2, "name": "Exact", "release_start": "2026-12-31",
        "release_end": "2026-12-31", "release_precision": "day", "followers": 7000,
    }
    uncertain = {
        "appid": 3956640, "name": "Hero's Adventure: Another Tale",
        "release_start": "2026-12-31", "release_end": "2026-12-31",
        "release_precision": "day", "followers": 13431,
    }
    rows = [historical, exact, uncertain]
    (data / "index.json").write_text(json.dumps({
        "version": 2, "game_count": 3, "months": ["2026-09", "2026-12"]
    }), encoding="utf-8")
    (data / "calendar" / "2026-09.json").write_text(json.dumps({
        "version": 2, "month": "2026-09", "count": 1, "games": [historical]
    }), encoding="utf-8")
    (data / "calendar" / "2026-12.json").write_text(json.dumps({
        "version": 2, "month": "2026-12", "count": 2, "games": [exact, uncertain]
    }), encoding="utf-8")
    for row in rows:
        (data / "games" / f"{row['appid']}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )

    taipei = ZoneInfo("Asia/Taipei")
    exact_stamp = int(datetime(2026, 10, 20, 12, 0, tzinfo=taipei).timestamp())

    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"response": {"store_items": [
                {"appid": 2, "release": {
                    "coming_soon_display": "date_full",
                    "steam_release_date": exact_stamp,
                }},
                {"appid": 3956640, "release": {
                    "coming_soon_display": "date_year",
                    "steam_release_date": int(datetime(2026, 12, 31, 16, 0, tzinfo=taipei).timestamp()),
                }},
            ]}}

    class Session:
        def get(self, *args, **kwargs): return Response()

    result = audit(
        data, session=Session(), interval=0, today=date(2026, 9, 25)
    )
    assert result["historical_retained"] == 1
    assert result["future_exact_kept"] == 1
    assert result["future_uncertain_removed"] == 1
    assert result["published_after"] == 2
    assert result["date_corrections"] == [{"appid": 2, "from": "2026-12-31", "to": "2026-10-20"}]

    index = json.loads((data / "index.json").read_text(encoding="utf-8"))
    assert index["release_date_audited"] is True
    assert index["game_count"] == 2

    excluded = json.loads((data / "excluded_date_appids.json").read_text(encoding="utf-8"))
    assert excluded["appids"] == [3956640]
    assert not (data / "games" / "3956640.json").exists()

    october = json.loads((data / "calendar" / "2026-10.json").read_text(encoding="utf-8"))
    assert [row["appid"] for row in october["games"]] == [2]
    assert october["games"][0]["release_display_precision"] == "date_full"
    assert october["games"][0]["release_start"] == "2026-10-20"

    september = json.loads((data / "calendar" / "2026-09.json").read_text(encoding="utf-8"))
    assert [row["appid"] for row in september["games"]] == [1]

    # Re-running an already-clean public catalogue must NOT erase the pending
    # exclusion ledger for games that are no longer published.
    second = audit(
        data, session=Session(), interval=0, today=date(2026, 9, 25)
    )
    assert second["future_uncertain_removed"] == 0
    assert second["date_exclusions_tracked"] == 1
    excluded_again = json.loads(
        (data / "excluded_date_appids.json").read_text(encoding="utf-8")
    )
    assert excluded_again["appids"] == [3956640]
