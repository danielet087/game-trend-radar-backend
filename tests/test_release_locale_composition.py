"""Direct date/title composition and historical call-time compatibility."""
from copy import deepcopy
from datetime import date
import re
from types import SimpleNamespace
from unittest.mock import Mock

from collectors import steam_upcoming as collector
from radar_backend.adapters import public_release_dates as dates, steam_store
from radar_backend.adapters import steam_localized_titles as titles
from radar_backend.adapters import steam_release_timestamps as timestamps
from radar_backend.adapters.candidate_sources import CandidateSources
from radar_backend.domain.release_window import ReleaseWindow
from scripts import steam_localized_titles as legacy_titles
from scripts import steam_release_dates as legacy_dates


STAMP = 1790096100


class Session:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(
            status_code=200, raise_for_status=lambda: None,
            json=lambda: {"response": {"store_items": self.items}},
        )


def test_canonical_scheduled_dates_work_when_legacy_helpers_are_forbidden(monkeypatch):
    for name in ("fetch_store_browse_releases", "resolve_release_date", "resolved_store_date", "corrected_games"):
        monkeypatch.setattr(legacy_dates, name, Mock(side_effect=AssertionError("No legacy date helper")))
    monkeypatch.setattr(collector, "parse_release_window", Mock(side_effect=AssertionError("No collector parser")))
    session = Session([{"appid": 123, "release": {"steam_release_date": STAMP}}])
    monkeypatch.setattr(timestamps.time, "sleep", lambda value: None)
    releases = dates.fetch_store_browse_releases(session, [123], request_interval=0)
    nested = {"preserved": True}
    original = [{"appid": 123, "release_raw": "22 Sep, 2026", "followers": 6000, "unknown": nested}]
    before = deepcopy(original)
    result = dates.corrected_games(original, releases)
    assert original == before
    assert result[0]["release_start"] == "2026-09-23"
    assert result[0]["release_time_source"] == dates.STORE_BROWSE_URL
    assert result[0]["followers"] == 6000 and result[0]["unknown"] is nested
    assert session.calls[0][1]["timeout"] == 25


def test_official_announcement_authority_and_scheduled_time_policy_remain_separate():
    item = {"release": {"coming_soon_display": "date_full", "steam_release_date": STAMP}}
    from radar_backend.domain.store_release import parse_store_release_detail
    detail = parse_store_release_detail(item, today=date(2026, 9, 20))
    official = steam_store.apply_store_release_detail({
        "appid": 123, "release_start": "2026-09-22", "release_display_precision": "date_full",
    }, detail)
    scheduled = dates.resolved_store_date(123, "22 Sep, 2026", {"steam_release_date": STAMP})
    assert official["release_start"] == "2026-09-22"
    assert official["release_timestamp_taipei_date"] == "2026-09-23"
    assert scheduled["release_start"] == "2026-09-23"


def test_candidate_default_title_ports_use_direct_layers_and_preserve_raw_evidence(monkeypatch):
    sources = CandidateSources()
    for name in ("display_in_traditional", "add_traditional_display_names", "actual_zh_tw_title",
                 "fetch_store_tw_names", "enrich_tw_names"):
        monkeypatch.setattr(legacy_titles, name, Mock(side_effect=AssertionError("No legacy title helper")))
    monkeypatch.setattr(titles.time, "sleep", lambda value: None)
    session = Session([{"appid": 123, "name": "针影裁梦"}])
    names = sources.fetch_store_tw_names(session, [123], interval=0)
    row = {"appid": 123, "name": "Dressmaker", "followers": 6000,
           "language_support": {"tchinese": False, "schinese": True}}
    summary = sources.enrich_tw_names([row], names)
    assert row["name"] == row["name_en"] == "Dressmaker"
    assert row["name_zh_tw"] == "针影裁梦"
    assert row["display_name"] == "針影裁夢"
    assert row["language_support"] == {"tchinese": False, "schinese": True}
    assert summary == {"total": 1, "official_zh_tw": 1, "english_fallback": 0, "store_not_returned": 0}
    assert session.calls[0][1]["timeout"] == 30


def test_collector_parser_preserves_late_month_and_recursive_callback_ports(monkeypatch):
    monkeypatch.setattr(collector, "_month_number", lambda value: 2)
    assert collector.parse_release_window("Jan 2026").start == date(2026, 2, 1)
    assert collector.ReleaseWindow is ReleaseWindow
    original_parser = collector.parse_release_window
    replacement = Mock(return_value=ReleaseWindow("custom", date(2026, 10, 1), date(2026, 10, 1), "day"))
    monkeypatch.setattr(collector, "parse_release_window", replacement)
    result = original_parser("1790096100")
    replacement.assert_called_once_with(1790096100)
    assert result.raw == "1790096100" and result.start == date(2026, 10, 1)


def test_legacy_date_wrappers_resolve_current_parser_map_and_bulk_callback(monkeypatch):
    parsed = ReleaseWindow("source", date(2026, 9, 21), date(2026, 9, 21), "day")
    parser = Mock(return_value=parsed)
    monkeypatch.setattr(legacy_dates, "parse_release_window", parser)
    monkeypatch.setattr(legacy_dates, "TAIWAN_STOREFRONT_DATES", {
        7: {"store_date": "2026-09-21", "taiwan_date": "2026-09-22", "basis": "custom_review"},
    })
    resolved = legacy_dates.resolve_release_date(7, "source")
    assert resolved["release_start"] == "2026-09-22" and resolved["release_time_utc"] is None
    parser.assert_called_once_with("source")
    resolve = Mock(return_value={"release_start": "2026-10-01"})
    monkeypatch.setattr(legacy_dates, "resolved_store_date", resolve)
    source = {"appid": 7, "release_raw": "source", "followers": 7000}
    assert legacy_dates.corrected_games([source])[0]["followers"] == 7000
    resolve.assert_called_once_with(7, "source", None, fallback_detail=None)


def test_legacy_title_wrappers_resolve_current_converter_han_and_enrichment_callbacks(monkeypatch):
    convert = Mock(return_value="converted")
    monkeypatch.setattr(legacy_titles, "_CONVERT_TO_TRADITIONAL", SimpleNamespace(convert=convert))
    assert legacy_titles.display_in_traditional(" raw ") == "converted"
    convert.assert_called_once_with("raw")
    monkeypatch.setattr(legacy_titles, "HAN", re.compile("A"))
    assert legacy_titles.actual_zh_tw_title("ASCII") == "ASCII"
    select, display = Mock(return_value="custom title"), Mock()
    monkeypatch.setattr(legacy_titles, "actual_zh_tw_title", select)
    monkeypatch.setattr(legacy_titles, "add_traditional_display_names", display)
    row = {"appid": 123, "name": "English"}
    assert legacy_titles.enrich_tw_names([row], {123: "raw"})["official_zh_tw"] == 1
    select.assert_called_once_with("raw")
    display.assert_called_once_with(row)


def test_legacy_title_transport_passes_current_runtime_ports(monkeypatch):
    transport = Mock(return_value={123: "name"})
    sleep, monotonic, logger = Mock(), Mock(), Mock()
    monkeypatch.setattr(titles, "fetch_store_tw_names", transport)
    monkeypatch.setattr(legacy_titles.time, "sleep", sleep)
    monkeypatch.setattr(legacy_titles.time, "monotonic", monotonic)
    monkeypatch.setattr(legacy_titles, "LOG", logger)
    monkeypatch.setattr(legacy_titles, "STORE_URL", "https://offline.example/titles")
    session = object()
    assert legacy_titles.fetch_store_tw_names(session, [123], batch_size=2, interval=0.3) == {123: "name"}
    transport.assert_called_once_with(
        session, [123], batch_size=2, interval=0.3, sleep=sleep, monotonic=monotonic,
        logger=logger, requests_module=legacy_titles.requests, url="https://offline.example/titles",
    )
