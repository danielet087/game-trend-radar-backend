"""Public timestamp-calendar rules remain distinct from official Store gates."""
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone

import pytest

from radar_backend.application.public_release_dates import corrected_games
from radar_backend.domain.public_release_dates import (
    STORE_BROWSE_RELEASE_SOURCE,
    TAIWAN_STOREFRONT_DATES,
    resolve_release_date,
    resolved_store_date,
)
from radar_backend.domain.release_window import ReleaseWindow, parse_release_window


@pytest.mark.parametrize("raw", [
    "18 Sep, 2026", "18 September 2026", "September 18, 2026",
    "2026-09-18", "20260918", "  18\n Sep,   2026  ",
])
def test_announced_days_keep_their_calendar_date(raw):
    release = parse_release_window(raw)
    assert (release.start, release.end, release.precision) == (
        date(2026, 9, 18), date(2026, 9, 18), "day",
    )
    assert release.raw == " ".join(raw.split())


@pytest.mark.parametrize("raw,first,last,precision", [
    ("Feb 2028", date(2028, 2, 1), date(2028, 2, 29), "month"),
    ("Feb 2027", date(2027, 2, 1), date(2027, 2, 28), "month"),
    ("q1,2027", date(2027, 1, 1), date(2027, 3, 31), "quarter"),
    ("Q4 2026", date(2026, 10, 1), date(2026, 12, 31), "quarter"),
    ("2027", date(2027, 1, 1), date(2027, 12, 31), "year"),
])
def test_broad_announcements_preserve_their_precision(raw, first, last, precision):
    release = parse_release_window(raw)
    assert (release.start, release.end, release.precision) == (first, last, precision)


@pytest.mark.parametrize("raw", [
    None, "", "   ", True, False, "Coming Soon", "T.B.A.", "TBD",
    "to be announced", "date to be announced", "announced later", "Soon",
    "31 Feb 2026", "2026-02-30", "unknown", "Q5 2026",
    "2026-09-18T17:00:00", "-1790096400", "1790096400.0",
    float("nan"), float("inf"), -float("inf"),
])
def test_unusable_announcements_remain_unknown(raw):
    release = parse_release_window(raw)
    assert (release.start, release.end, release.precision) == (None, None, "unknown")


@pytest.mark.parametrize("raw", [
    1790096400, 1790096400.75, 1790096400000, "1790096400", "1790096400000",
    "2026-09-22T17:00:00Z", "2026-09-22T09:00:00-08:00",
    "2026-09-23T01:00:00+0800",
])
def test_supplied_instants_convert_to_taiwan_day(raw):
    release = parse_release_window(raw)
    assert (release.start, release.end, release.precision) == (
        date(2026, 9, 23), date(2026, 9, 23), "day",
    )


@pytest.mark.parametrize("raw", ["0000", "Jan 0000", "Q1 0000"])
def test_invalid_broad_date_year_keeps_original_error(raw):
    with pytest.raises(ValueError):
        parse_release_window(raw)


def test_window_overlap_is_inclusive_and_value_is_frozen():
    release = parse_release_window("Feb 2028")
    assert release.overlaps(date(2028, 1, 31), date(2028, 2, 1))
    assert release.overlaps(date(2028, 2, 29), date(2028, 3, 1))
    assert not release.overlaps(date(2028, 3, 1), date(2028, 3, 2))
    assert not parse_release_window(None).overlaps(date.min, date.max)
    with pytest.raises(FrozenInstanceError):
        release.precision = "day"


def test_parser_does_not_inherit_transport_year_restriction():
    assert parse_release_window(0).start == date(1970, 1, 1)
    assert parse_release_window(-86400).start == date(1969, 12, 31)
    assert parse_release_window("2101-01-01").start == date(2101, 1, 1)


def test_parser_digit_recursion_is_a_call_time_port():
    seen = []
    marker = ReleaseWindow("patched", date(2027, 1, 2), date(2027, 1, 3), "month")
    result = parse_release_window(
        "1790096400", parse_window=lambda value: seen.append(value) or marker,
    )
    assert seen == [1790096400]
    assert result == ReleaseWindow("1790096400", marker.start, marker.end, "month")


def test_parser_preserves_datetime_timezone_month_and_window_ports():
    calls = []

    class PatchedDatetime:
        @staticmethod
        def fromtimestamp(value, *, tz):
            calls.append((value, tz))
            return datetime(2027, 2, 3, tzinfo=tz)

    custom_tz = timezone(timedelta(hours=9))
    value = parse_release_window(1790096400000, datetime_type=PatchedDatetime, taiwan_tz=custom_tz)
    assert value.start == date(2027, 2, 3)
    assert calls == [(1790096400.0, custom_tz)]

    def window(raw, first, last, precision):
        calls.append((raw, first, last, precision))
        return ReleaseWindow(raw, first, last, precision)

    value = parse_release_window("18 Sep 2026", month_number=lambda value: 10, window_type=window)
    assert value.start == date(2026, 10, 18)
    assert calls[-1] == ("18 Sep 2026", date(2026, 10, 18), date(2026, 10, 18), "day")


def test_reported_storefront_day_is_date_only_and_requires_matching_store_day():
    corrected = resolve_release_date(4019220, "21 Sep, 2026")
    assert corrected["release_start"] == "2026-09-22"
    assert corrected["release_time_utc"] is None
    assert corrected["release_raw"] == "21 Sep, 2026"
    assert corrected["release_date_basis"] == "steam_tw_storefront_date_user_reported"
    assert resolve_release_date(4019220, "25 Sep, 2026")["release_start"] == "2026-09-25"
    assert resolve_release_date(123, "21 Sep, 2026")["release_start"] == "2026-09-21"
    assert resolve_release_date(4019220, "21 Sep, 2026", storefront_dates={})["release_start"] == "2026-09-21"
    assert TAIWAN_STOREFRONT_DATES[4019220]["store_date"] == "2026-09-21"


@pytest.mark.parametrize("days,accepted", [(-3, False), (-2, True), (-1, True), (0, True), (1, True), (2, True), (3, False)])
def test_timestamp_must_be_within_two_days_of_announcement(days, accepted):
    local = datetime(2026, 9, 22, 12, tzinfo=timezone(timedelta(hours=8))) + timedelta(days=days)
    release = resolve_release_date(123, "22 Sep, 2026", detail={"timestamp": local.isoformat()})
    assert release["release_start"] == (local.date().isoformat() if accepted else "2026-09-22")
    assert release["release_date_basis"] == ("steam_structured_release_time" if accepted else "steam_store_announced_date")
    assert (release["release_time_utc"] is not None) is accepted


@pytest.mark.parametrize("key", ["steam_release_date", "timestamp", "release_timestamp", "release_time_utc"])
def test_structured_candidate_fields_keep_provenance(key):
    detail = {key: "2026-09-22T17:00:00.123456Z", "release_time_source": "custom-source"}
    release = resolve_release_date(123, "22 Sep, 2026", detail=detail)
    assert release["release_start"] == "2026-09-23"
    assert release["release_time_utc"] == "2026-09-22T17:00:00Z"
    assert release["release_time_source"] == ("custom-source" if key == "steam_release_date" else None)
    assert release["release_date_basis"] == ("steam_store_browse_release_time" if key == "steam_release_date" else "steam_structured_release_time")
    assert set(release) == {
        "release_raw", "release_start", "release_end", "release_precision",
        "release_date_timezone", "release_date_basis", "release_time_utc", "release_time_source",
    }
    assert release["release_date_timezone"] == "Asia/Taipei"


@pytest.mark.parametrize("candidate", ["unknown", "2026-09-22", "2026-09-22T17:00:00", 999, "999"])
def test_present_invalid_candidate_does_not_fall_through_to_other_sources(candidate):
    release = resolve_release_date(4019220, "21 Sep, 2026", detail={
        "steam_release_date": candidate, "timestamp": "2026-09-21T17:00:00Z",
    })
    assert release["release_start"] == "2026-09-21"
    assert release["release_date_basis"] == "steam_store_announced_date"
    assert release["release_time_utc"] is None
    assert release["release_time_source"] is None


@pytest.mark.parametrize("first", [None, True, False])
def test_none_and_bool_candidates_are_skipped_without_changing_basis_contract(first):
    release = resolve_release_date(123, "22 Sep, 2026", detail={
        "steam_release_date": first, "timestamp": "2026-09-22T17:00:00Z",
    })
    assert release["release_start"] == "2026-09-23"
    assert release["release_date_basis"] == ("steam_structured_release_time" if first is None else "steam_store_browse_release_time")


def test_missing_announcement_can_use_timestamp_without_http_year_gate():
    release = resolve_release_date(123, None, detail={"timestamp": 0})
    assert release["release_start"] == "1970-01-01"
    assert release["release_time_utc"] == "1970-01-01T00:00:00Z"


def test_release_resolver_uses_injected_parser_and_datetime():
    calls = []

    def parse(raw):
        calls.append(raw)
        return ReleaseWindow(str(raw), date(2026, 9, 22), date(2026, 9, 22), "day")

    class PatchedDatetime:
        @staticmethod
        def fromtimestamp(value, *, tz):
            calls.append((value, tz))
            return datetime(2026, 9, 22, 4, tzinfo=timezone.utc)

    release = resolve_release_date(123, "patched", detail={"timestamp": 1790096400}, parse_window=parse, datetime_type=PatchedDatetime)
    assert calls == ["patched", 1790096400, (1790096400.0, timezone.utc)]
    assert release["release_time_utc"] == "2026-09-22T04:00:00Z"


@pytest.mark.parametrize("browse", [None, {}, {"steam_release_date": None}])
def test_absent_browse_timestamp_uses_cached_structured_fallback(browse):
    release = resolved_store_date(123, "22 Sep, 2026", browse, fallback_detail={"timestamp": "2026-09-22T17:00:00Z"})
    assert release["release_start"] == "2026-09-23"
    assert release["release_date_basis"] == "steam_structured_release_time"


@pytest.mark.parametrize("source,expected", [("absent", STORE_BROWSE_RELEASE_SOURCE), (None, None), ("custom", "custom")])
def test_browse_source_defaults_only_when_the_key_is_absent(source, expected):
    browse = {"steam_release_date": 1790096400}
    if source != "absent":
        browse["release_time_source"] = source
    release = resolved_store_date(123, "22 Sep, 2026", browse)
    assert release["release_time_source"] == expected
    assert release["release_date_basis"] == "steam_store_browse_release_time"


@pytest.mark.parametrize("candidate", [False, 999, "invalid"])
def test_present_browse_value_shadows_valid_cached_fallback(candidate):
    release = resolved_store_date(123, "22 Sep, 2026", {"steam_release_date": candidate}, fallback_detail={"timestamp": "2026-09-22T17:00:00Z"})
    assert release["release_start"] == "2026-09-22"
    assert release["release_time_utc"] is None


def test_resolved_store_date_uses_original_callback_signature_and_source_port():
    seen = []
    marker = {"patched": True}

    def resolve(appid, announced, *, detail):
        seen.append((appid, announced, detail))
        return marker

    result = resolved_store_date(123, "announced", {"steam_release_date": 456}, resolve=resolve, browse_source="patched-source")
    assert result is marker
    assert seen == [(123, "announced", {"steam_release_date": 456, "release_time_source": "patched-source"})]


@pytest.mark.parametrize("basis,key", [
    ("steam_structured_release_time", "release_time_utc"),
    ("steam_store_browse_release_time", "steam_release_date"),
    ("steam_store_announced_date", None),
    ("steam_tw_storefront_date_user_reported", None),
])
def test_bulk_correction_preserves_shallow_rows_and_cached_provenance(basis, key):
    nested = {"tags": ["action"]}
    record = {
        "appid": "123", "release_raw": "22 Sep, 2026", "followers": 7000,
        "unknown": nested, "release_time_utc": "2026-09-22T17:00:00Z",
        "release_time_source": "cached-source", "release_date_basis": basis,
    }
    seen = []

    def resolve(appid, announced, browse, *, fallback_detail):
        seen.append((appid, announced, browse, fallback_detail))
        return {"release_start": "2026-09-23", "release_time_utc": None}

    corrected = corrected_games([record], resolve=resolve)
    fallback = {key: record["release_time_utc"], "release_time_source": "cached-source"} if key else None
    assert seen == [(123, "22 Sep, 2026", None, fallback)]
    assert corrected[0] is not record
    assert corrected[0]["unknown"] is nested
    assert corrected[0]["followers"] == 7000
    assert record["release_time_utc"] == "2026-09-22T17:00:00Z"
    assert corrected[0]["release_time_utc"] is None
    assert corrected[0]["appid"] == "123"


def test_bulk_correction_keeps_cached_browse_source_and_updated_metadata():
    original = [{"appid": 123, "release_raw": "22 Sep, 2026", "followers": 7000,
                 "release_date_basis": "steam_store_browse_release_time",
                 "release_time_utc": "2026-09-22T17:00:00Z", "release_time_source": "cached-source"}]
    corrected = corrected_games(original)
    assert corrected[0]["release_start"] == "2026-09-23"
    assert corrected[0]["release_time_source"] == "cached-source"
    assert corrected[0]["release_date_basis"] == "steam_store_browse_release_time"
    refreshed = corrected_games(original, {123: {"steam_release_date": "2026-09-22T04:00:00Z", "release_time_source": "fresh-source"}})
    assert refreshed[0]["release_start"] == "2026-09-22"
    assert refreshed[0]["release_time_source"] == "fresh-source"
    assert original[0]["release_time_source"] == "cached-source"


@pytest.mark.parametrize("record,error", [({}, KeyError), ({"appid": "bad"}, ValueError), ({"appid": None}, TypeError)])
def test_bad_appid_raises_before_resolver_and_does_not_modify_input(record, error):
    before = dict(record)

    def forbidden(*args, **kwargs):
        raise AssertionError("resolver must not run before appid validation")

    with pytest.raises(error):
        corrected_games([record], resolve=forbidden)
    assert record == before


def test_bulk_correction_preserves_input_order_and_fails_at_the_original_record():
    records = [{"appid": 9}, {"appid": "bad"}, {"appid": 8}]
    seen = []

    def resolve(appid, announced, browse, *, fallback_detail):
        seen.append(appid)
        return {}

    with pytest.raises(ValueError):
        corrected_games(records, resolve=resolve)
    assert seen == [9]
    assert records == [{"appid": 9}, {"appid": "bad"}, {"appid": 8}]
