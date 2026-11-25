"""Real state composition contracts."""
import json
from pathlib import Path
from types import SimpleNamespace

from radar_backend.adapters import published_titles
from radar_backend.adapters import steam_preview_metadata as preview_metadata
from radar_backend.domain import published_titles as title_rules


class TitleSession:
    def __init__(self):
        self.calls = []

    def get(self, url, *, params, timeout):
        payload = json.loads(params["input_json"])
        self.calls.append((url, payload, timeout))
        language = payload["context"]["language"]
        return SimpleNamespace(
            status_code=200, raise_for_status=lambda: None,
            json=lambda: {"response": {"store_items": [
                {"appid": item["appid"], "name": "针影裁梦" if language == "schinese" else "Dressmaker"}
                for item in payload["ids"]
            ]}},
        )


def public_files(tmp_path: Path):
    data = tmp_path / "data"
    (data / "games").mkdir(parents=True)
    (data / "calendar").mkdir()
    accepted = {"appid": 7, "name": "Dressmaker", "name_en": "Dressmaker",
                "release_start": "2026-09-22", "release_end": "2026-09-22",
                "release_precision": "day", "followers": 6000,
                "language_support": {"tchinese": False}, "unknown": {"retained": True}}
    excluded = {"appid": 9, "name": "Unqualified", "followers": 4999,
                "release_start": "2026-09-23"}
    documents = {
        "index.json": {"version": 2, "months": ["2026-09"], "extra": "keep"},
        "calendar/2026-09.json": {"version": 2, "month": "2026-09", "count": 2,
                                  "games": [accepted, excluded]},
        "games/7.json": {**accepted, "short_description": "既有介紹"},
        "games/9.json": excluded,
    }
    for name, doc in documents.items():
        (data / name).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return data, accepted, excluded


def test_direct_published_refresh_uses_new_layers(tmp_path):
    data, accepted, excluded = public_files(tmp_path)
    excluded_bytes = (data / "games/9.json").read_bytes()
    session = TitleSession()
    result = published_titles.refresh(data, session=session, interval=0)
    assert result["published_qualified"] == 1
    assert result["changed_appids"] == [7] and result["changed_months"] == ["2026-09"]
    assert result["store_tw_titles"] == 0 and result["store_cn_titles"] == 1
    assert len(session.calls) == 2
    assert [call[1]["context"]["language"] for call in session.calls] == ["tchinese", "schinese"]
    assert all(call[2] == 45 and call[1]["ids"] == [{"appid": 7}] for call in session.calls)
    assert (data / "games/9.json").read_bytes() == excluded_bytes
    detail = json.loads((data / "games/7.json").read_text())
    assert detail["short_description"] == "既有介紹"
    assert detail["name"] == accepted["name"]
    assert detail["name_zh_cn"] == "针影裁梦" and detail["display_name"] == "針影裁夢"
    assert detail["language_support"] == accepted["language_support"]
    assert detail["unknown"] == accepted["unknown"] and detail["storage_version"] == 2
    for name in ("calendar/2026-09.json", "steam_upcoming.json", "catalog.json"):
        rows = json.loads((data / name).read_text())["games"]
        assert next(row for row in rows if row["appid"] == 7)["display_name"] == "針影裁夢"
        assert next(row for row in rows if row["appid"] == 9)["name"] == excluded["name"]
    assert json.loads((data / "index.json").read_text())["extra"] == "keep"
    assert published_titles.refresh(data, session=session, interval=0)["changed_games"] == 0


def test_direct_preview_metadata_keeps_locale_policy():
    categories = [{"id": 1, "description": "Multi-player"}]
    details = {"name": "English", "release_date": {"date": "22 Sep, 2026"},
               "type": "game", "steam_appid": 7, "categories": categories}
    session = SimpleNamespace(get=lambda url, **kw: SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {"7": {"success": True, "data": details}},
    ))
    assert preview_metadata.app_details(session, 7) is details
    row = preview_metadata.normalized_metadata(
        7, details, 6000, "checked", preserve_player_categories=lambda old, new: new,
        clock=lambda: "2026-09-22T00:00:00+00:00",
    )
    assert row["release_start"] == "2026-09-22" and row["categories"] is categories
    # Preview accepts a nonblank different Store string without the published gate.
    assert preview_metadata.localized_names({"name": "English"}, {"name": "english"}) == ("English", "english")
    assert title_rules.candidate("english", "English") is None
