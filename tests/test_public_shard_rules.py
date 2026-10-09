"""Offline contracts for the shard eligibility and presentation rules."""
from copy import deepcopy
from datetime import date

import pytest

from radar_backend.domain import public_shards as rules


TODAY = "2026-10-09"


def game(**changes):
    return {
        "appid": 7, "followers": 5000, "release_start": TODAY,
        "release_precision": "day", **changes,
    }


def qualified(row):
    return row.get("qualified") is True


def valid(row):
    return rules.valid_record(row, is_twitch_qualified=qualified)


def merge(existing, incoming, **ports):
    identity = lambda previous, current: current
    return rules.merge_game(
        existing, incoming,
        keep_newer_release=ports.pop("keep_newer_release", identity),
        preserve_twitch_admission=ports.pop("preserve_twitch_admission", identity),
        preserve_player_categories=ports.pop("preserve_player_categories", identity),
        add_traditional_display_names=ports.pop("add_traditional_display_names", lambda row: None),
        **ports,
    )


def eligible(row, *, blocked=None, unconfirmed_ids=None, audit_active=False, **ports):
    return rules.publishable(
        row, today_s=TODAY, blocked=blocked if blocked is not None else set(),
        unconfirmed_ids=unconfirmed_ids if unconfirmed_ids is not None else set(),
        audit_active=audit_active,
        valid_record=ports.get("valid_record", valid),
        is_disallowed=ports.get("is_disallowed", lambda row, ids: int(row["appid"]) in ids),
        is_twitch_qualified=ports.get("is_twitch_qualified", qualified),
    )


@pytest.mark.parametrize("row", [None, [], True, "record", 7])
def test_valid_record_requires_dictionary(row):
    assert valid(row) is False


@pytest.mark.parametrize("field,value", [
    ("appid", None), ("appid", "bad"), ("appid", 0), ("appid", -1),
    ("followers", None), ("followers", "bad"), ("followers", []),
    ("release_start", "2026-02-30"), ("release_start", "2026-13-01"),
    ("release_start", "20261009"), ("release_start", "2026-W41-5"),
    ("release_start", "2026-10-09T00:00:00Z"), ("release_start", TODAY + " "),
    ("release_start", None), ("release_start", date(2026, 10, 9)),
    ("release_start", True), ("release_precision", "month"),
    ("release_precision", "quarter"), ("release_precision", None),
])
def test_valid_record_rejects_noncanonical_values(field, value):
    assert valid(game(**{field: value})) is False


@pytest.mark.parametrize("followers,proof,expected", [
    (2999, False, False), (2999, True, True), (3000, False, True),
    (3000, True, True), (4999, False, True), (5000, False, True),
    ("3000", False, True), (3000.9, False, True), (-1, True, True),
    (True, False, False),
])
def test_base_record_threshold_accepts_three_thousand_or_explicit_proof(followers, proof, expected):
    assert valid(game(followers=followers, qualified=proof)) is expected


@pytest.mark.parametrize("appid", ["7", 7.9, True])
def test_legacy_integer_coercion_of_appid_is_preserved(appid):
    assert valid(game(appid=appid)) is True


@pytest.mark.parametrize("field", ["appid", "followers"])
def test_integer_overflow_is_not_silently_converted_to_rejection(field):
    with pytest.raises(OverflowError):
        valid(game(**{field: float("inf")}))


def test_release_date_fallback_and_default_day_precision_remain_accepted():
    row = {"appid": 7, "followers": 3000, "release_date": TODAY}
    assert valid(row) is True
    assert valid({**row, "release_start": ""}) is True


def test_date_port_and_qualification_have_original_short_circuit_order():
    events = []
    row = game(followers=2999, release_precision="month")

    class DatePort:
        @staticmethod
        def fromisoformat(value):
            events.append(("parse", value))
            return date.fromisoformat(value)

    def proof(actual):
        assert actual is row
        events.append("proof")
        return True

    assert rules.valid_record(row, date_type=DatePort, is_twitch_qualified=proof) is False
    assert events == [("parse", TODAY), "proof"]
    events.clear()
    assert rules.valid_record(game(appid=0), date_type=DatePort, is_twitch_qualified=proof) is False
    assert events == [("parse", TODAY)]


def test_adequate_followers_never_consult_twitch_proof():
    def forbidden(row):
        raise AssertionError("Qualification should short circuit")

    assert rules.valid_record(game(followers=3000), is_twitch_qualified=forbidden)


@pytest.mark.parametrize("day,followers,proof,audited,full,expected", [
    ("2026-10-08", 3000, False, True, False, True),
    ("2026-10-08", 2999, False, False, True, False),
    ("2026-10-08", 2999, True, True, False, True),
    (TODAY, 3000, False, False, True, False),
    (TODAY, 4999, False, False, True, False),
    (TODAY, 5000, False, False, False, True),
    (TODAY, 5000, False, True, False, False),
    (TODAY, 5000, False, True, True, True),
    (TODAY, 1, True, False, False, True),
    (TODAY, 1, True, True, False, False),
    (TODAY, 1, True, True, True, True),
    ("2026-10-10", 5000, False, True, True, True),
    ("2026-10-10", 5000, False, True, False, False),
])
def test_publication_gate_uses_today_as_future_and_separate_display_date_proof(
    day, followers, proof, audited, full, expected,
):
    row = game(release_start=day, followers=followers, qualified=proof)
    if full:
        row["release_display_precision"] = "date_full"
    assert eligible(row, audit_active=audited) is expected


@pytest.mark.parametrize("day", ["2026-10-08", TODAY, "2026-10-10"])
def test_unconfirmed_dates_need_full_proof_even_for_released_games(day):
    row = game(release_start=day)
    assert eligible(row, unconfirmed_ids={7}) is False
    assert eligible({**row, "release_display_precision": "date_full"}, unconfirmed_ids={7}) is True


def test_publication_gate_checks_record_then_adult_and_passes_blocked_object():
    events, blocked = [], {99}
    row = game()

    def adult(actual, actual_blocked):
        assert actual is row and actual_blocked is blocked
        events.append("adult")
        return True

    def validation(actual):
        assert actual is row
        events.append("valid")
        return True

    assert eligible(row, blocked=blocked, valid_record=validation, is_disallowed=adult) is False
    assert events == ["valid", "adult"]
    events.clear()
    assert eligible(row, valid_record=lambda row: False, is_disallowed=adult) is False
    assert events == []


def test_unconfirmed_gate_precedes_future_twitch_gate():
    row = game(followers=3000)
    assert eligible(
        row, unconfirmed_ids={7},
        is_twitch_qualified=lambda row: pytest.fail("Unconfirmed date must short circuit"),
    ) is False


def test_legacy_release_date_only_future_gate_still_requires_release_start_key():
    row = {"appid": 7, "followers": 5000, "release_date": TODAY}
    assert valid(row)
    with pytest.raises(KeyError, match="release_start"):
        eligible(row)


@pytest.mark.parametrize("authoritative,day,present,proof,expected", [
    (False, TODAY, False, False, False),
    (True, "2026-10-08", False, False, False),
    (True, TODAY, True, False, False),
    (True, TODAY, False, True, False),
    (True, TODAY, False, False, True),
    (True, "2026-10-10", False, False, True),
    (True, None, False, False, False),
])
def test_stale_future_removes_only_absent_unqualified_authoritative_future(
    authoritative, day, present, proof, expected,
):
    assert rules.stale_future(
        game(release_start=day, qualified=proof), 7,
        authoritative_future=authoritative, today_s=TODAY,
        incoming_ids={7} if present else set(), is_twitch_qualified=qualified,
    ) is expected


def test_stale_future_does_not_borrow_release_date_fallback():
    assert rules.stale_future(
        {"release_date": TODAY}, 7, authoritative_future=True,
        today_s=TODAY, incoming_ids=set(), is_twitch_qualified=qualified,
    ) is False


def test_stale_future_qualification_runs_only_after_all_removal_gates():
    events = []
    proof = lambda row: events.append(row) or False
    row = game()
    for authority, ids in [(False, set()), (True, {7})]:
        assert rules.stale_future(
            row, 7, authoritative_future=authority, today_s=TODAY,
            incoming_ids=ids, is_twitch_qualified=proof,
        ) is False
    assert events == []
    assert rules.stale_future(
        row, 7, authoritative_future=True, today_s=TODAY,
        incoming_ids=set(), is_twitch_qualified=proof,
    ) is True
    assert events == [row]


def test_merge_orders_metadata_ports_and_preserves_their_return_objects():
    prior = game(storage_version=9, tags=["rich"])
    raw = game(appid="7", followers=6000)
    release = {**raw, "release_raw": "new"}
    twitch = {**release, "twitch_admission": {"evidence": "keep"}}
    categories = {**twitch, "categories": [], "categories_source": "observed"}
    events = []

    def port(label, expected, result):
        def apply(actual_prior, actual_incoming):
            assert actual_prior is prior and actual_incoming is expected
            events.append(label)
            return result
        return apply

    def display(row):
        assert row["appid"] == 7
        assert row["storage_version"] == 9
        assert row["categories"] is categories["categories"]
        events.append("display")
        row["name_zh_tw_traditional"] = "顯示"

    merged = merge(
        prior, raw, keep_newer_release=port("release", raw, release),
        preserve_twitch_admission=port("twitch", release, twitch),
        preserve_player_categories=port("categories", twitch, categories),
        add_traditional_display_names=display,
    )
    assert events == ["release", "twitch", "categories", "display"]
    assert merged["storage_version"] == 2
    assert merged["tags"] is prior["tags"]
    assert merged["twitch_admission"] is twitch["twitch_admission"]
    assert "name_zh_tw_traditional" not in prior
    assert raw == game(appid="7", followers=6000)


@pytest.mark.parametrize("key", ["followers", "release_start", "sexual_content_screened", "steam_type"])
def test_backend_core_fields_refresh_existing_values(key):
    assert merge({"appid": 7, key: "old"}, {"appid": 7, key: "new"})[key] == "new"


@pytest.mark.parametrize("value", [None, "", []])
def test_absent_backend_value_does_not_erase_rich_or_core_fields(value):
    prior = {"appid": 7, "followers": 6000, "tags": ["retained"]}
    merged = merge(prior, {"appid": 7, "followers": value, "tags": value})
    assert merged["followers"] == 6000
    assert merged["tags"] is prior["tags"]


@pytest.mark.parametrize("prior_value", [None, "", []])
def test_new_noncore_metadata_can_fill_empty_existing_field(prior_value):
    new = {"nested": []}
    merged = merge({"appid": 7, "detail": prior_value}, {"appid": 7, "detail": new})
    assert merged["detail"] is new


@pytest.mark.parametrize("key", ["name", "name_en", "name_zh_tw", "name_zh_cn", "capsule_image"])
def test_backend_names_and_asset_values_refresh_rich_metadata(key):
    assert merge({"appid": 7, key: "old"}, {"appid": 7, key: "new"})[key] == "new"


def test_empty_name_list_keeps_original_special_name_loop_semantics():
    merged = merge({"appid": 7, "name": "old"}, {"appid": 7, "name": []})
    assert merged["name"] == []


@pytest.mark.parametrize("value", [[], None, ""])
def test_explicit_category_values_bypass_general_empty_value_skips(value):
    merged = merge({"appid": 7, "categories": ["old"]}, {"appid": 7, "categories": value})
    assert merged["categories"] is value


def test_unverified_category_fields_are_removed_when_port_does_not_return_them():
    prior = {"appid": 7, "categories": ["old"], "categories_source": "old", "categories_checked_at": "old"}
    merged = merge(prior, {"appid": 7})
    assert all(field not in merged for field in rules.PLAYER_CATEGORY_FIELDS)
    assert "categories" in prior


def test_core_and_category_field_ports_preserve_legacy_patchability():
    prior = {"appid": 7, "followers": 5000, "custom": "old", "category": "old"}
    merged = merge(
        prior, {"appid": "7", "followers": 6000, "custom": "new", "category": None},
        core_fields={"custom"}, player_category_fields=("category",),
    )
    assert merged["followers"] == 5000
    assert merged["custom"] == "new"
    assert merged["category"] is None
    assert merged["appid"] == 7


@pytest.mark.parametrize("failing_port,expected_events", [
    ("keep_newer_release", []),
    ("preserve_twitch_admission", ["release"]),
    ("preserve_player_categories", ["release", "twitch"]),
    ("add_traditional_display_names", ["release", "twitch", "categories"]),
])
def test_merge_propagates_port_errors_and_stops_later_steps(failing_port, expected_events):
    events = []
    ports = {
        "keep_newer_release": lambda previous, current: events.append("release") or current,
        "preserve_twitch_admission": lambda previous, current: events.append("twitch") or current,
        "preserve_player_categories": lambda previous, current: events.append("categories") or current,
        "add_traditional_display_names": lambda row: events.append("display"),
    }

    def fail(*args):
        raise RuntimeError("port failed")

    ports[failing_port] = fail
    with pytest.raises(RuntimeError, match="port failed"):
        merge(game(), game(), **ports)
    assert events == expected_events


def test_invalid_incoming_appid_fails_before_display_and_missing_appid_uses_prior():
    assert merge({"appid": "7"}, {})["appid"] == 7
    with pytest.raises(TypeError):
        merge(
            {"appid": 7}, {"appid": None},
            add_traditional_display_names=lambda row: pytest.fail("AppID conversion precedes display"),
        )


def test_verified_release_preservation_mutates_only_the_merged_release_keys():
    prior = {
        "release_start": "2026-10-08", "release_end": "2026-10-08",
        "release_raw": "verified raw", "release_display_precision": "date_full",
        "release_date_verified_at": "proof", "release_time_utc": "prior-time",
    }
    row = {"release_start": TODAY, "release_display_precision": "query", "followers": 6000}
    merged = {**row, "release_time_utc": "new-time", "release_date_verified_at": "new-proof"}
    original_prior, original_row = deepcopy(prior), deepcopy(row)
    assert rules.preserve_verified_release(prior, row, merged) is None
    for key in ("release_start", "release_end", "release_raw", "release_display_precision", "release_date_verified_at"):
        assert merged[key] == prior[key]
    assert merged["followers"] == 6000
    assert merged["release_time_utc"] == "new-time"
    assert prior == original_prior and row == original_row


def test_same_release_date_preserves_only_display_proof_not_raw_or_end():
    prior = {"release_start": TODAY, "release_date_verified_at": "old", "release_display_precision": "date_full"}
    row = {"release_start": TODAY, "release_display_precision": "timestamp"}
    merged = {"release_start": TODAY, "release_end": "new-end", "release_raw": "new-raw"}
    rules.preserve_verified_release(prior, row, merged)
    assert merged["release_raw"] == "new-raw" and merged["release_end"] == "new-end"
    assert merged["release_date_verified_at"] == "old"


@pytest.mark.parametrize("proof,precision", [(None, "query"), ("", "query"), (False, "query"), ("old", "date_full")])
def test_missing_prior_proof_or_new_full_date_leaves_merged_unchanged(proof, precision):
    merged = {"release_start": TODAY, "release_date_verified_at": "new"}
    before = dict(merged)
    rules.preserve_verified_release(
        {"release_start": "2026-10-08", "release_date_verified_at": proof},
        {"release_start": TODAY, "release_display_precision": precision}, merged,
    )
    assert merged == before


def test_verified_date_defaults_only_missing_end_and_raw_not_explicit_nulls():
    row = {"release_start": TODAY}
    missing = {"release_start": "2026-10-08", "release_date_verified_at": "proof"}
    merged = {}
    rules.preserve_verified_release(missing, row, merged)
    assert merged["release_end"] == merged["release_raw"] == "2026-10-08"
    assert merged["release_display_precision"] is None
    merged = {}
    rules.preserve_verified_release({**missing, "release_end": None, "release_raw": None}, row, merged)
    assert merged["release_end"] is None and merged["release_raw"] is None


def test_missing_verified_start_propagates_original_key_error():
    with pytest.raises(KeyError, match="release_start"):
        rules.preserve_verified_release({"release_date_verified_at": "proof"}, {"release_start": TODAY}, {})


def test_rows_sort_by_date_then_descending_followers_then_numeric_appid_without_copy():
    rows = {
        9: game(appid="9", followers="6000"),
        2: game(appid=2, followers=6000),
        1: game(appid=1, followers=5000),
        8: game(appid=8, release_start="2026-10-08", followers=3000),
        10: {"appid": 10, "followers": None},
        11: {"appid": 11, "release_date": "2026-10-10", "followers": 9000},
    }
    ordered = rules.sorted_rows(rows)
    assert [row["appid"] for row in ordered] == [8, 2, "9", 1, 11, 10]
    assert ordered[2] is rows[9]
    assert list(rows) == [9, 2, 1, 8, 10, 11]


def test_month_groups_keep_input_order_and_record_references():
    rows = [game(appid=2), game(appid=1), {"appid": 3, "release_date": "2026-11-01"}]
    grouped = rules.rows_by_month(rows)
    assert list(grouped) == ["2026-10", "2026-11"]
    assert grouped["2026-10"] == rows[:2]
    assert grouped["2026-10"][0] is rows[0]
    assert grouped["missing"] == []


@pytest.mark.parametrize("day,followers,proof,expected", [
    ("2026-10-08", 5000, False, []),
    (TODAY, 4999, False, []),
    (TODAY, 5000, False, [7]),
    (TODAY, 1, True, [7]),
    ("2026-10-10", 5000, False, [7]),
    ("2026-10-10", 3000, False, []),
    ("2026-10-10", 3000, True, [7]),
])
def test_upcoming_list_cutoff_and_follower_gate(day, followers, proof, expected):
    assert rules.upcoming_appids(
        [game(release_start=day, followers=followers, qualified=proof)],
        today_s=TODAY, is_twitch_qualified=qualified,
    ) == expected


@pytest.mark.parametrize("day,followers,proof,source,expected", [
    ("2026-09-08", 5000, False, None, []),
    ("2026-09-09", 5000, False, None, [7]),
    ("2026-10-08", 5000, False, None, [7]),
    (TODAY, 5000, False, None, []),
    ("2026-10-10", 5000, False, None, []),
    ("2026-10-08", 3000, False, "tracked_release", []),
    ("2026-10-08", 3000, False, "direct_release", []),
    ("2026-10-08", 3001, False, "tracked_release", [7]),
    ("2026-10-08", 3001, False, "direct_release", [7]),
    ("2026-10-08", 4999, False, "other", []),
    ("2026-10-08", 1, True, None, [7]),
])
def test_released_list_has_inclusive_thirty_day_start_and_strict_darkhorse_threshold(
    day, followers, proof, source, expected,
):
    assert rules.released_appids(
        [game(release_start=day, followers=followers, qualified=proof, recent_source=source)],
        today_s=TODAY, released_from="2026-09-09", is_twitch_qualified=qualified,
    ) == expected


def test_list_selection_preserves_input_order_duplicates_and_integer_output():
    upcoming = [game(appid="9"), game(appid="2"), game(appid="9")]
    assert rules.upcoming_appids(upcoming, today_s=TODAY, is_twitch_qualified=qualified) == [9, 2, 9]
    released = [{**row, "release_start": "2026-10-08"} for row in upcoming]
    assert rules.released_appids(
        released, today_s=TODAY, released_from="2026-09-09", is_twitch_qualified=qualified,
    ) == [9, 2, 9]


def test_list_proof_ports_short_circuit_dates_and_sufficient_followers():
    def forbidden(row):
        raise AssertionError("No Twitch proof lookup is required")

    assert rules.upcoming_appids(
        [game(), game(release_start="2026-10-08", followers=1)],
        today_s=TODAY, is_twitch_qualified=forbidden,
    ) == [7]
    assert rules.released_appids(
        [game(release_start="2026-10-08"), game(followers=1)],
        today_s=TODAY, released_from="2026-09-09", is_twitch_qualified=forbidden,
    ) == [7]
