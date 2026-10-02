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


def test_refresh_low_followers_verified_twitch_source_preserves_admission_and_details(tmp_path: Path) -> None:
    from scripts.twitch_steam_admission import is_twitch_qualified

    data = tmp_path / "data"
    (data / "games").mkdir(parents=True)
    (data / "calendar").mkdir()
    proof = {
        "schema_version": 1, "method": "twitch_igdb_external_steam_v1", "appid": 200,
        "twitch_game_id": "300", "igdb_id": "400", "checked_at": "2026-10-02T09:00:00Z",
        "source_frontend_commit": "a" * 40,
        "source_enrollment": {"source": "igdb_first_release_date", "viewer_count": 7200,
                              "min_viewers": 7000, "observed_at": "2026-09-20T01:00:00Z"},
    }
    accepted = {
        "appid": 200, "name": "Twitch discovery", "name_en": "Twitch discovery", "followers": 812,
        "follower_checked_at": "2026-10-02T09:00:00Z", "release_start": "2026-09-20",
        "release_end": "2026-09-20", "release_precision": "day",
        "release_display_precision": "date_full", "release_date_timezone": "Asia/Taipei",
        "release_time_utc": "2026-09-19T17:00:00Z", "release_timestamp_taipei_date": "2026-09-20",
        "release_date_conflict": False, "steam_type": "game", "sexual_content_screened": True,
        "twitch_admission": proof,
    }
    unverified = {**accepted, "appid": 201, "name": "Unverified manual entry"}
    full = {key: value for key, value in accepted.items()
            if key not in {"twitch_admission", "steam_type", "sexual_content_screened", "release_time_utc"}}
    full["followers"] = 9000  # A stale detail count must not replace the public count.
    full["short_description"] = "已有繁中介紹"
    (data / "index.json").write_text(json.dumps({"version": 2, "months": ["2026-09"]}), encoding="utf-8")
    (data / "calendar" / "2026-09.json").write_text(json.dumps({
        "version": 2, "month": "2026-09", "count": 2, "games": [accepted, unverified],
    }), encoding="utf-8")
    (data / "games" / "200.json").write_text(json.dumps(full), encoding="utf-8")
    (data / "games" / "201.json").write_text(json.dumps(unverified), encoding="utf-8")

    class Response:
        status_code = 200
        def __init__(self, title): self.title = title
        def raise_for_status(self): pass
        def json(self): return {"response": {"store_items": [{"appid": 200, "name": self.title}]}}

    class Session:
        def get(self, url, *, params, timeout):
            payload = json.loads(params["input_json"])
            assert [item["appid"] for item in payload["ids"]] == [200]
            return Response("新作繁中名稱" if payload["context"]["language"] == "tchinese" else "新作简中名称")

    result = refresh(data, session=Session(), interval=0)
    assert result["published_qualified"] == 1
    assert result["changed_appids"] == [200]
    detail = json.loads((data / "games" / "200.json").read_text(encoding="utf-8"))
    assert detail["short_description"] == "已有繁中介紹"
    for path in [data / "catalog.json", data / "steam_upcoming.json", data / "calendar" / "2026-09.json"]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        row = next(row for row in payload["games"] if row["appid"] == 200)
        assert row["display_name"] == "新作繁中名稱"
        assert row["followers"] == 812
        assert row["twitch_admission"] == proof
        assert is_twitch_qualified(row)
    assert is_twitch_qualified(detail)
    assert detail["twitch_admission"] == proof
    assert json.loads((data / "games" / "201.json").read_text(encoding="utf-8")) == unverified
    assert refresh(data, session=Session(), interval=0)["changed_games"] == 0
    # Even when no title changes, an older detail shard missing the proof must
    # be repaired instead of silently skipping the source preservation write.
    detail.pop("twitch_admission")
    (data / "games" / "200.json").write_text(json.dumps(detail), encoding="utf-8")
    assert refresh(data, session=Session(), interval=0)["changed_appids"] == [200]
    assert json.loads((data / "games" / "200.json").read_text(encoding="utf-8"))["twitch_admission"] == proof
