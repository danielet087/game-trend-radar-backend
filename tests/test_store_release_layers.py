"""Store date authority, failure ordering, and compatible late-bound ports."""
from copy import deepcopy
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

from radar_backend.application import store_release as application
from radar_backend.domain import store_release as domain
from scripts import steam_master_date_gate as legacy

TODAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 1, 2, 3, tzinfo=timezone.utc)


def store(stamp="2026-10-10T16:30:00+00:00", *, label="date_full", coming=True):
    if isinstance(stamp, str) and "T" in stamp:
        stamp = int(datetime.fromisoformat(stamp).timestamp())
    return {"release": {
        "steam_release_date": stamp, "coming_soon_display": label,
        "is_coming_soon": coming,
    }}


def exact_detail():
    return domain.parse_store_release_detail(store(), today=TODAY)


def game(appid=10, *, day="2026-10-11", followers=5000, **fields):
    return {
        "appid": appid, "release_start": day, "followers": followers,
        "release_display_precision": "date_full",
        "post_followers_store_verified": True, **fields,
    }


def eligible(appid=10, **fields):
    return {
        "appid": appid, "release_display_precision": "date_full",
        "sexual_content_screened": True, **fields,
    }


def filter_games(games, candidates, **ports):
    return domain.filter_confirmed_master_games(
        games, candidates, today=TODAY, blocked=ports.pop("blocked", set()),
        is_disallowed=ports.pop("is_disallowed", lambda row, blocked: int(row.get("appid", 0)) in blocked),
        is_twitch_qualified=ports.pop("is_twitch_qualified", lambda row: False),
        **ports,
    )


@pytest.mark.parametrize("item", [None, [], {}, {"release": []}])
def test_absent_release_is_unavailable(item):
    assert domain.parse_store_release_detail(item, today=TODAY) == {
        "exact": False, "status": "unavailable",
    }


@pytest.mark.parametrize("stamp", [None, True, False, [], {}, object()])
def test_unsupported_timestamp_keeps_the_store_label(stamp):
    assert domain.parse_store_release_detail(store(stamp, label="date_year"), today=TODAY) == {
        "exact": False, "status": "date_year",
    }
    assert domain.parse_store_release_detail(store(stamp, label=None), today=TODAY) == {
        "exact": False, "status": "missing_release_time",
    }


@pytest.mark.parametrize("stamp", ["invalid", float("nan"), float("inf"), 10**50])
def test_invalid_timestamp_is_not_exact(stamp):
    assert domain.parse_store_release_detail(store(stamp, label=None), today=TODAY) == {
        "exact": False, "status": "invalid_release_time",
    }


@pytest.mark.parametrize("coerce", [int, float, str])
def test_numeric_timestamp_types_keep_the_real_utc_instant(coerce):
    stamp = int(datetime(2026, 10, 10, 16, 30, tzinfo=timezone.utc).timestamp())
    detail = domain.parse_store_release_detail(store(coerce(stamp)), today=TODAY)
    assert detail == {
        "exact": True, "status": "date_full", "release_start": "2026-10-11",
        "release_timestamp_taipei_date": "2026-10-11",
        "release_time_utc": "2026-10-10T16:30:00Z",
        "release_display_precision": "date_full",
        "release_display_provider": domain.STORE_DATE_PROVIDER,
        "release_date_basis": "steam_store_browse_verified_full_date",
        "release_date_timezone": "Asia/Taipei",
    }


@pytest.mark.parametrize("label", [None, "date_month", "date_quarter", "date_year", "coming_soon"])
def test_full_date_is_required_until_the_taiwan_release_day(label):
    detail = domain.parse_store_release_detail(
        store("2026-10-09T16:00:00+00:00", label=label, coming=False), today=TODAY,
    )
    assert detail["exact"] is False
    assert detail["status"] == (label or "unknown")
    assert all(value is None for key, value in detail.items() if key.startswith("release_"))
    released = domain.parse_store_release_detail(
        store("2026-10-09T15:59:59+00:00", label=label, coming=False), today=TODAY,
    )
    assert released["exact"] is True
    assert released["status"] == "released_exact"
    assert released["release_start"] == TODAY.isoformat()


@pytest.mark.parametrize("coming", [None, True, 0, "false"])
def test_actual_release_requires_the_boolean_false_marker(coming):
    detail = domain.parse_store_release_detail(
        store("2026-10-08T00:00:00+00:00", label=None, coming=coming), today=TODAY,
    )
    assert detail["exact"] is False


@pytest.mark.parametrize("detail,error", [
    ({"exact": False}, RuntimeError), ({"exact": 1}, RuntimeError),
    ({"exact": True}, KeyError),
])
def test_invalid_store_update_never_reads_the_clock_or_mutates_source(detail, error):
    clock = Mock(side_effect=AssertionError("must not stamp invalid evidence"))
    authority = Mock(return_value=False)
    original = game()
    before = deepcopy(original)
    with pytest.raises(error):
        application.apply_store_release_detail(
            original, detail, clock=clock,
            has_taiwan_store_date_authority=authority,
        )
    assert original == before
    clock.assert_not_called()
    assert authority.call_count == (1 if detail.get("exact") is True else 0)


@pytest.mark.parametrize("authority", [False, True])
def test_announced_day_is_preserved_and_store_provider_depends_on_authority(authority):
    original = game(
        day="2026-10-10", release_display_provider="Taiwan visible announcement",
        release_date_verified_at="2026-10-01T00:00:00Z", unknown={"retained": 1},
    )
    before = deepcopy(original)
    clock = Mock(return_value=NOW)
    updated = application.apply_store_release_detail(
        original, exact_detail(), clock=clock,
        has_taiwan_store_date_authority=lambda row: authority,
    )
    assert original == before
    assert updated["release_raw"] == updated["release_start"] == updated["release_end"] == "2026-10-10"
    assert updated["release_precision"] == "day"
    assert updated["release_timestamp_taipei_date"] == "2026-10-11"
    assert updated["release_time_utc"] == "2026-10-10T16:30:00Z"
    assert updated["release_date_conflict"] is True
    assert updated["unknown"] == {"retained": 1}
    assert updated["release_display_provider"] == (
        "Taiwan visible announcement" if authority else domain.STORE_DATE_PROVIDER
    )
    assert updated["release_date_verified_at"] == (
        original["release_date_verified_at"] if authority else NOW.isoformat()
    )
    assert updated["post_followers_store_verified_at"] == NOW.isoformat()
    assert updated["post_followers_store_verified"] is True
    clock.assert_called_once_with()


@pytest.mark.parametrize("day,precision,expected", [
    (None, "date_full", "2026-10-11"),
    ("2026-02-30", "date_full", "2026-10-11"),
    ("2026-10-10", None, "2026-10-11"),
    ("20261010", "date_full", "20261010"),
])
def test_only_a_valid_prior_full_date_overrides_the_timestamp(day, precision, expected):
    prepared, authority = domain.prepare_store_release_detail(
        game(day=day, release_display_precision=precision), exact_detail(),
        has_taiwan_store_date_authority=lambda row: False,
    )
    assert prepared["release_start"] == expected
    assert authority is False
    assert "post_followers_store_verified_at" not in prepared
    assert "release_date_verified_at" not in prepared


@pytest.mark.parametrize("authority", [False, True])
def test_matching_day_clears_prior_conflict_without_erasing_authority(authority):
    prepared, found = domain.prepare_store_release_detail(
        game(release_date_conflict=True, release_date_conflict_note="stale"), exact_detail(),
        has_taiwan_store_date_authority=lambda row: authority,
    )
    assert found is authority
    assert "release_date_conflict_note" not in prepared
    if authority:
        assert prepared["release_date_conflict"] is False
    else:
        assert "release_date_conflict" not in prepared


@pytest.mark.parametrize("fields,candidates", [
    ({"followers": 4999}, [eligible()]),
    ({"release_display_precision": "date_month"}, [eligible()]),
    ({"post_followers_store_verified": 1}, [eligible()]),
    ({}, []), ({}, [eligible(sexual_content_screened=1)]),
])
def test_future_master_requires_the_full_current_post_follower_proof(fields, candidates):
    assert filter_games([game(**fields)], candidates) == []


def test_master_retains_released_history_and_original_objects_in_source_order():
    historical = game(20, day=TODAY.isoformat(), release_display_precision=None,
                      post_followers_store_verified=False)
    future = game(10)
    duplicate = game(20, day="2026-10-01")
    blocked = game(30, day="2026-10-01")
    source = [future, historical, duplicate, blocked]
    before = deepcopy(source)
    retained = filter_games(source, [eligible()], blocked={30})
    assert retained == [future, historical]
    assert retained[0] is future and retained[1] is historical
    assert source == before


def test_twitch_proof_only_bypasses_follower_threshold_and_eligible_universe():
    verified = game(10, followers=0)
    missing_store = game(20, followers=0, post_followers_store_verified=False)
    coarse = game(30, followers=0, release_display_precision="date_year")
    blocked = game(40, followers=0)
    assert filter_games(
        [verified, missing_store, coarse, blocked], [], blocked={40},
        is_twitch_qualified=lambda row: True,
    ) == [verified]


def test_filter_keeps_the_original_numeric_coercion_and_rejects_malformed_rows():
    accepted = game("10", followers="5000")
    source = [None, {}, game(20, day="invalid"), game(30, followers="invalid"), accepted]
    assert filter_games(source, [eligible("10")], is_disallowed=lambda row, blocked: False) == [accepted]


def test_filter_reads_ledger_before_parsing_an_eligible_appid():
    read = Mock(return_value=set())
    with pytest.raises(ValueError):
        application.filter_confirmed_master_games(
            [], [eligible("invalid")], today=TODAY, excluded_appids=read,
            is_disallowed=Mock(), is_twitch_qualified=Mock(),
        )
    read.assert_called_once_with()


def test_ledger_failure_prevents_master_predicates_and_keeps_source():
    ledger = Mock(side_effect=RuntimeError("missing adult ledger"))
    adult, twitch = Mock(), Mock()
    source = [game()]
    with pytest.raises(RuntimeError, match="missing adult ledger"):
        application.filter_confirmed_master_games(
            source, [eligible("invalid")], today=TODAY,
            excluded_appids=ledger, is_disallowed=adult, is_twitch_qualified=twitch,
        )
    adult.assert_not_called()
    twitch.assert_not_called()
    assert source == [game()]


def test_date_fetch_sorts_deduplicates_positive_ids_and_reports_missing_items():
    session = object()
    fetch = Mock(return_value={10: store()})
    results = application.fetch_store_release_details(
        session, [20, "10", 0, -1, 10], today=TODAY, interval=0.5,
        fetch_metadata=fetch,
    )
    fetch.assert_called_once_with(session, [10, 20], interval=0.5)
    assert list(results) == [10, 20]
    assert results[10]["exact"] is True
    assert results[20] == {"exact": False, "status": "unavailable"}


@pytest.mark.parametrize("ids", [[], [0, -1]])
def test_empty_date_fetch_never_calls_metadata_or_parser(ids):
    fetch, parser = Mock(), Mock()
    assert application.fetch_store_release_details(
        object(), ids, today=TODAY, fetch_metadata=fetch,
        parse_store_release_detail=parser,
    ) == {}
    fetch.assert_not_called()
    parser.assert_not_called()


def test_metadata_failure_propagates_without_parsing_partial_results():
    parser = Mock()
    with pytest.raises(RuntimeError, match="incomplete Store batch"):
        application.fetch_store_release_details(
            object(), [10], today=TODAY,
            fetch_metadata=Mock(side_effect=RuntimeError("incomplete Store batch")),
            parse_store_release_detail=parser,
        )
    parser.assert_not_called()


def test_legacy_fetch_resolves_metadata_and_parser_when_called(monkeypatch):
    session = object()
    fetch = Mock(return_value={10: {"raw": "Store row"}})
    parser = Mock(return_value={"exact": True})
    monkeypatch.setattr(legacy, "fetch_metadata", fetch)
    monkeypatch.setattr(legacy, "parse_store_release_detail", parser)
    assert legacy.fetch_store_release_details(session, [10, "10"], today=TODAY, interval=0) == {
        10: {"exact": True},
    }
    fetch.assert_called_once_with(session, [10], interval=0)
    parser.assert_called_once_with({"raw": "Store row"}, today=TODAY)


def test_legacy_date_constants_and_datetime_ports_remain_late_bound(monkeypatch):
    instant = datetime(2026, 10, 10, 16, 30, tzinfo=timezone.utc)
    conversion = Mock(return_value=instant)
    now = Mock(return_value=NOW)
    fake_datetime = type("Clock", (), {"fromtimestamp": conversion, "now": now})
    monkeypatch.setattr(legacy, "datetime", fake_datetime)
    monkeypatch.setattr(legacy, "TAIPEI", timezone.utc)
    monkeypatch.setattr(legacy, "STORE_DATE_PROVIDER", "Patched Store provider")
    detail = legacy.parse_store_release_detail(store(), today=TODAY)
    assert detail["release_start"] == "2026-10-10"
    assert detail["release_display_provider"] == "Patched Store provider"
    conversion.assert_called_once_with(int(instant.timestamp()), tz=timezone.utc)
    authority = Mock(return_value=True)
    monkeypatch.setattr(legacy, "has_taiwan_store_date_authority", authority)
    updated = legacy.apply_store_release_detail(game(day="2026-10-10"), detail)
    authority.assert_called_once()
    now.assert_called_once_with(timezone.utc)
    assert updated["post_followers_store_verified_at"] == NOW.isoformat()


def test_legacy_filter_resolves_ledger_adult_and_twitch_ports_when_called(monkeypatch):
    row = game(followers=0)
    ledger = Mock(return_value={20})
    adult = Mock(return_value=False)
    twitch = Mock(return_value=True)
    monkeypatch.setattr(legacy, "excluded_appids", ledger)
    monkeypatch.setattr(legacy, "is_disallowed", adult)
    monkeypatch.setattr(legacy, "is_twitch_qualified", twitch)
    assert legacy.filter_confirmed_master_games([row], [], today=TODAY) == [row]
    ledger.assert_called_once_with()
    adult.assert_called_once_with(row, {20})
    twitch.assert_called_once_with(row)
