"""Real state composition and historical runtime replacement contracts."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.adapters import published_titles
from radar_backend.adapters import steam_preview_metadata as preview_metadata
from radar_backend.domain import published_titles as title_rules
from scripts import publish_steam_preview as preview
from scripts import refresh_published_chinese_titles as legacy_titles


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


def test_direct_published_refresh_uses_new_layers_with_legacy_helpers_forbidden(tmp_path, monkeypatch):
    data, accepted, excluded = public_files(tmp_path)
    excluded_bytes = (data / "games/9.json").read_bytes()
    for name in ("read", "save_changed", "candidate", "update_title", "localized_batch", "refresh"):
        monkeypatch.setattr(legacy_titles, name, Mock(side_effect=AssertionError("Legacy helper forbidden")))
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


def test_legacy_refresh_keeps_call_time_title_state_projection_and_clock_ports(tmp_path, monkeypatch):
    data, _, _ = public_files(tmp_path)
    events = []
    original_read, original_save = legacy_titles.read, legacy_titles.save_changed
    monkeypatch.setattr(legacy_titles, "read", lambda path: (events.append(("read", path.name)), original_read(path))[1])
    monkeypatch.setattr(legacy_titles, "save_changed", lambda path, doc: (events.append(("save", path.name)), original_save(path, doc))[1])
    monkeypatch.setattr(legacy_titles, "localized_batch", lambda session, ids, lang, interval:
                        (events.append(("lookup", lang)), {7: lang})[1])
    monkeypatch.setattr(legacy_titles, "candidate", lambda value, english: "patched-" + value)
    def update(row, tw, cn):
        events.append(("update", row["appid"]))
        return {**row, "display_name": tw, "name_zh_cn": cn}
    monkeypatch.setattr(legacy_titles, "update_title", update)
    monkeypatch.setattr(legacy_titles, "preserve_twitch_admission", lambda old, full: full)
    monkeypatch.setattr(legacy_titles, "is_twitch_qualified", lambda row: False)
    monkeypatch.setattr(legacy_titles, "write_catalog_projection", lambda path, rows, now:
                        (events.append(("projection", now)), {"catalog_revision": "patched"})[1])
    instants = iter([datetime(2026, 9, 22, tzinfo=timezone.utc), datetime(2026, 9, 23, tzinfo=timezone.utc)])
    monkeypatch.setattr(legacy_titles, "datetime", SimpleNamespace(now=lambda tz: next(instants)))
    sleeps = []
    monkeypatch.setattr(legacy_titles.time, "sleep", sleeps.append)
    result = legacy_titles.refresh(data, session=object(), batch_size=1, interval=0.25)
    assert result["at"] == "2026-09-23T00:00:00+00:00"
    assert sleeps == [0.25, 0.25]
    assert events.index(("lookup", "schinese")) < events.index(("read", "7.json"))
    assert events.count(("update", 7)) == 2
    assert ("projection", "2026-09-22T00:00:00+00:00") in events
    assert json.loads((data / "games/7.json").read_text())["display_name"] == "patched-tchinese"


def test_legacy_candidate_and_converter_are_resolved_after_input_checks(monkeypatch):
    monkeypatch.setattr(legacy_titles, "HAN", re.compile("ASCII"))
    assert legacy_titles.candidate(" ASCII ", "English") == "ASCII"
    monkeypatch.setattr(legacy_titles, "CONVERT", None)
    result = legacy_titles.update_title({"appid": 7, "name_en": "English"}, None, None)
    assert result["display_name"] == "English"
    converter = Mock(side_effect=lambda value: "converted:" + value)
    monkeypatch.setattr(legacy_titles, "CONVERT", SimpleNamespace(convert=converter))
    result = legacy_titles.update_title({"appid": 7}, " 台灣 ", " 中国 ")
    assert converter.call_args_list[0].args == ("台灣",)
    assert converter.call_args_list[1].args == ("中国",)
    assert result["name_zh_tw"] == " 台灣 " and result["display_name"] == "converted:台灣"


def test_legacy_published_transport_uses_current_url_logger_and_wait(monkeypatch):
    replies = iter([SimpleNamespace(status_code=429), SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {"response": {"store_items": [{"appid": 7, "name": " 中文 "}]}},
    )])
    calls = []
    session = SimpleNamespace(get=lambda url, **kw: (calls.append((url, kw)), next(replies))[1])
    waits, logger = [], Mock()
    monkeypatch.setattr(legacy_titles, "STORE", "https://example.invalid/patched")
    monkeypatch.setattr(legacy_titles, "LOG", logger)
    monkeypatch.setattr(legacy_titles.time, "sleep", waits.append)
    assert legacy_titles.localized_batch(session, [7], "custom", 123) == {7: "中文"}
    assert waits == [15] and logger.warning.call_count == 1
    assert all(url == "https://example.invalid/patched" and kw["timeout"] == 45 for url, kw in calls)


def test_direct_preview_metadata_does_not_call_legacy_helpers_and_keeps_locale_policy(monkeypatch):
    for name in ("steam_get", "app_details", "localized_names", "normalized_metadata", "add_traditional_name"):
        monkeypatch.setattr(preview, name, Mock(side_effect=AssertionError("Legacy preview helper forbidden")))
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


def test_legacy_preview_localization_preserves_mutations_before_display_failure(monkeypatch):
    events, game = [], {"name": "Fallback", "release_start": "2026-09-22"}
    monkeypatch.setattr(preview.time, "sleep", lambda value: events.append(("sleep", value)))
    def fetch(session, appid, *, language):
        events.append(("fetch", appid, language))
        return {"name": "ignored"}
    monkeypatch.setattr(preview, "app_details", fetch)
    monkeypatch.setattr(preview, "localized_names", lambda en, tw: ("patched English", "patched TW"))
    def display(row):
        events.append(("display", dict(row)))
        raise ValueError("display failure")
    monkeypatch.setattr(preview, "add_traditional_display_names", display)
    with pytest.raises(ValueError, match="display failure"):
        preview.add_traditional_name(None, 7, game, {"name": "Original"}, delay_seconds=0)
    assert events[:2] == [("sleep", 0), ("fetch", 7, "tchinese")]
    assert game == {"name": "patched English", "name_en": "patched English",
                    "name_zh_tw": "patched TW", "release_start": "2026-09-22"}


def test_legacy_preview_normalization_resolves_current_ports_and_gates_clock(monkeypatch):
    release = {"release_precision": "month", "release_start": "2026-09-01"}
    resolver = Mock(return_value=release)
    now, preserve = Mock(), Mock(side_effect=lambda old, new: new)
    monkeypatch.setattr(preview, "resolved_store_date", resolver)
    monkeypatch.setattr(preview, "preserve_player_categories", preserve)
    monkeypatch.setattr(preview, "datetime", SimpleNamespace(now=now))
    categories = []
    details = {"name": "Original", "type": "game", "steam_appid": 7, "categories": categories}
    assert preview.normalized_metadata(7, details, 6000, None) is None
    now.assert_not_called()
    preserve.assert_not_called()
    release.update(release_precision="day", release_start="2026-09-22")
    now.return_value = SimpleNamespace(isoformat=lambda: "patched clock")
    row = preview.normalized_metadata(7, details, 6000, None)
    assert row["categories"] is categories and row["categories_checked_at"] == "patched clock"
    now.assert_called_once_with(preview.timezone.utc)
    resolver.assert_called_with(7, None, None, fallback_detail={})
    preserve.assert_called_once_with({}, row)


def test_legacy_preview_http_and_app_details_use_runtime_fetch_ports(monkeypatch):
    waits, calls, logger = [], [], Mock()
    replies = iter([SimpleNamespace(status_code=429), SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {"7": {"success": True, "data": {"name": "Store"}}},
    )])
    session = SimpleNamespace(get=lambda url, **kw: (calls.append((url, kw)), next(replies))[1])
    monkeypatch.setattr(preview, "APP_DETAILS", "https://example.invalid/details")
    monkeypatch.setattr(preview, "LOGGER", logger)
    monkeypatch.setattr(preview.time, "sleep", waits.append)
    assert preview.app_details(session, 7, language="tchinese") == {"name": "Store"}
    assert waits == [60] and logger.warning.call_count == 1
    assert calls[0] == ("https://example.invalid/details", {"params": {"appids": 7, "cc": "TW", "l": "tchinese"}, "timeout": 25})
    replacement = Mock(return_value={"7": {"success": True, "data": {"name": "replaced"}}})
    monkeypatch.setattr(preview, "steam_get", replacement)
    assert preview.app_details(None, 7) == {"name": "replaced"}
    replacement.assert_called_once_with(None, "https://example.invalid/details", {"appids": 7, "cc": "TW", "l": "english"})
