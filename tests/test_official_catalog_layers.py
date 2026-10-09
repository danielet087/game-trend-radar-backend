"""Offline Store verification and ordinary official master promotion."""
from copy import deepcopy
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

from radar_backend.adapters import steam_store as adapter
from radar_backend.application import official_catalog as application
from radar_backend.domain import official_catalog as domain
from radar_backend.domain.official_queue import TAIPEI


NOW = datetime(2026, 10, 8, 16, 10, tzinfo=timezone.utc)
TODAY = date(2026, 10, 9)


def result(appid=10, **fields):
    return {
        "appid": appid, "name": "Verified title", "official_followers": 5000,
        "release_date": "2026-10-23", "store_date_exact": True,
        "release_display_precision": "date_full",
        "official_checked_at_taipei": "2026-10-09T00:00:00+08:00",
        "official_source": "Steam Community XML memberCount",
        **fields,
    }


def exact_detail(day="2026-10-24"):
    return {
        "exact": True, "status": "date_full", "release_start": day,
        "release_timestamp_taipei_date": day,
        "release_display_provider": "Steam IStoreBrowseService/GetItems",
        "release_date_basis": "steam_store_browse_verified_full_date",
        "release_date_timezone": "Asia/Taipei",
        "release_time_utc": "2026-10-23T16:00:00Z",
    }


@pytest.mark.parametrize("fields", [
    {"official_followers": True}, {"official_followers": 5000.0},
    {"official_followers": "5000"}, {"official_followers": -1},
    {"official_followers": 4999}, {"official_followers": None},
    {"store_date_exact": "true"}, {"store_date_exact": False},
    {"release_display_precision": "date_month"},
    {"release_date": "2026-02-29"}, {"release_date": "2026-10"},
    {"release_date": "2026-10-23T00:00:00"},
])
def test_ineligible_promotion_leaves_master_and_adult_port_untouched(fields):
    master = {"games": [{"appid": 88}], "updated_at": "old"}
    before = deepcopy(master)
    adult = Mock(side_effect=AssertionError("No adult read for ineligible evidence"))
    observed = result(**fields)

    assert domain.qualifies_for_master(observed) is False
    assert domain.upsert_qualified_master(
        master, observed, now=None, blocked=set(), is_disallowed=adult,
    ) is False
    assert master == before
    adult.assert_not_called()


def test_threshold_promotion_preserves_prior_enrichment_and_freezes_one_utc_clock():
    master = {
        "schema_note": "Keep the established envelope",
        "games": [
            {"appid": 10, "description": "Existing Chinese content", "release_time_utc": "prior-time"},
            {"appid": 30, "release_start": "2026-10-23", "followers": 7000},
            {"appid": 20, "release_start": "2026-10-20", "followers": 1},
            {"appid": 15, "release_start": "2026-10-23", "followers": 5000},
        ],
    }
    evidence = result(queue_source="ordinary", custom_observation="Keep in checkpoint")
    before_evidence = deepcopy(evidence)
    adult = Mock(return_value=False)

    assert domain.upsert_qualified_master(
        master, evidence, now=NOW.astimezone(TAIPEI), blocked={99}, is_disallowed=adult,
    ) is True

    assert [row["appid"] for row in master["games"]] == [20, 30, 10, 15]
    promoted = next(row for row in master["games"] if row["appid"] == 10)
    assert promoted["description"] == "Existing Chinese content"
    assert promoted["release_time_utc"] == "prior-time"
    assert promoted["release_start"] == promoted["release_end"] == "2026-10-23"
    assert promoted["followers"] == 5000
    assert promoted["official_ge5000"] is True
    assert promoted["post_followers_store_verified"] is True
    assert promoted["follower_checked_at"] == evidence["official_checked_at_taipei"]
    assert promoted["store_url"] == "https://store.steampowered.com/app/10/"
    assert promoted["release_date_verified_at"] == promoted["post_followers_store_verified_at"] == NOW.isoformat()
    assert master["updated_at"] == master["post_followers_store_gate_checked_at"] == NOW.isoformat()
    assert master["post_followers_store_gate_version"] == 1
    assert master["schema_note"] == "Keep the established envelope"
    assert "count" not in master
    assert evidence == before_evidence
    adult.assert_called_once()
    assert adult.call_args.args[1] == {99}


def test_blocked_adult_promotion_does_not_change_master():
    master = {"games": [], "updated_at": "old"}
    before = deepcopy(master)
    assert domain.upsert_qualified_master(
        master, result(), now=NOW, blocked={10},
        is_disallowed=lambda row, blocked: row["appid"] in blocked,
    ) is False
    assert master == before


def test_same_frozen_promotion_is_idempotent_and_does_not_touch_envelope_metadata():
    master = {"games": []}
    ports = {"now": NOW, "blocked": set(), "is_disallowed": lambda *_: False}
    assert domain.upsert_qualified_master(master, result(), **ports) is True
    master["updated_at"] = "another-producer-update"
    master["post_followers_store_gate_checked_at"] = "another-producer-check"
    before = deepcopy(master)
    assert domain.upsert_qualified_master(master, result(), **ports) is False
    assert master == before


def test_missing_games_array_is_replaced_only_by_qualified_evidence():
    master = {"version": 1, "games": None}
    assert domain.upsert_qualified_master(
        master, result(name="", official_source=None), now=NOW,
        blocked=set(), is_disallowed=lambda *_: False,
    ) is True
    promoted = master["games"][0]
    assert promoted["name"] == promoted["name_en"] == "Steam App 10"
    assert promoted["follower_source"] == "Steam Community XML memberCount"


@pytest.mark.parametrize("announced,timestamp,conflict", [
    ("2026-10-23", "2026-10-24", True),
    ("2026-10-25", "2026-10-24", True),
    ("2026-10-24", "2026-10-24", False),
])
def test_store_recheck_preserves_announced_taiwan_day_in_both_midnight_directions(announced, timestamp, conflict):
    observed = result(release_date=announced, custom_proof={"source": "candidate"})
    assert domain.apply_store_detail_to_result(
        observed, exact_detail(timestamp), checked_at=NOW,
    ) is True
    assert observed["release_date"] == announced
    assert observed["release_timestamp_taipei_date"] == timestamp
    assert observed["release_date_conflict"] is conflict
    assert observed["store_date_checked_at_taipei"] == "2026-10-09T00:10:00+08:00"
    assert observed["custom_proof"] == {"source": "candidate"}
    assert observed["official_followers"] == 5000


@pytest.mark.parametrize("announced", [None, "2026-10", "2026-02-29"])
def test_invalid_announced_day_uses_exact_store_timestamp_day(announced):
    observed = result(release_date=announced)
    assert domain.apply_store_detail_to_result(
        observed, exact_detail(), checked_at=NOW,
    ) is True
    assert observed["release_date"] == "2026-10-24"
    assert observed["release_date_conflict"] is False


def test_store_unavailable_does_not_invent_a_new_date_or_erase_old_diagnostics():
    observed = result(release_time_utc="prior-time", release_date_conflict=True)
    assert domain.apply_store_detail_to_result(
        observed, {"exact": False, "status": "unavailable"}, checked_at=NOW,
    ) is False
    assert observed["store_date_exact"] is False
    assert observed["store_date_status"] == "unavailable"
    assert observed["release_date"] == "2026-10-23"
    assert observed["release_time_utc"] == "prior-time"
    assert observed["release_date_conflict"] is True


def test_recheck_selection_skips_twitch_same_day_exact_and_nonofficial_counts():
    candidates = {
        "1": result(1, store_date_exact=False, release_date="2026-10-21"),
        "2": result(2, store_date_exact=False, release_date="2026-10-20"),
        "3": result(3, store_date_exact=False, release_date="2026-10-20"),
        "4": result(4, store_date_exact=False, queue_source="twitch_steam_discovery"),
        "5": result(5, store_date_exact=False, store_date_checked_at_taipei="2026-10-09T00:00:00+08:00"),
        "6": result(6),
        "7": result(7, store_date_exact=False, official_followers=4999),
        "8": result(8, store_date_exact=False, official_followers=True),
        "9": result(9, store_date_exact=False, official_followers=6000.0),
    }
    checkpoint = {"official_results": candidates}
    before = deepcopy(checkpoint)
    selected = domain.select_pending_store_results(checkpoint, today=TODAY, limit=2)
    assert [row["appid"] for row in selected] == [2, 3]
    assert selected[0] is candidates["2"]
    assert checkpoint == before


def test_recheck_defaults_to_twenty_five_and_preserves_saved_result_identity():
    candidates = {str(aid): result(aid, store_date_exact=False) for aid in range(1, 31)}
    selected = domain.select_pending_store_results({"official_results": candidates}, today=TODAY)
    assert [row["appid"] for row in selected] == list(range(1, 26))
    assert selected[-1] is candidates["25"]


def test_single_result_use_case_uses_store_only_with_the_injected_taiwan_day():
    session = object()
    fetch = Mock(return_value={10: exact_detail()})
    observed = result(store_date_exact=False)
    assert application.verify_store_date_for_result(
        observed, session, clock=lambda: NOW, fetch_store_release_details=fetch,
    ) is True
    fetch.assert_called_once_with(session, [10], today=TODAY, interval=0.0)
    assert observed["release_date"] == "2026-10-23"
    assert observed["release_date_conflict"] is True


def test_single_missing_store_response_is_unavailable_and_never_claims_exact():
    observed = result(store_date_exact=False)
    assert application.verify_store_date_for_result(
        observed, object(), clock=lambda: NOW,
        fetch_store_release_details=lambda *args, **kwargs: {},
    ) is False
    assert observed["store_date_status"] == "unavailable"


def test_store_read_failure_propagates_without_marking_any_result_verified():
    observed = result(store_date_exact=False)
    before = deepcopy(observed)
    fetch = Mock(side_effect=RuntimeError("Store read rejected"))
    with pytest.raises(RuntimeError, match="Store read rejected"):
        application.verify_store_date_for_result(
            observed, object(), clock=lambda: NOW, fetch_store_release_details=fetch,
        )
    assert observed == before


def test_no_pending_recheck_creates_no_session_and_does_not_dispatch_twitch():
    checkpoint = {"official_results": {"10": result(10, store_date_exact=False, queue_source="twitch_steam_discovery")}}
    before = deepcopy(checkpoint)
    network = Mock(side_effect=AssertionError("No Store query for Twitch"))
    assert application.reverify_pending_store_dates(
        checkpoint, {"games": []}, clock=lambda: NOW, session_factory=network,
        fetch_store_release_details=network, upsert_qualified_master=network,
        dispatch_content_event=network,
    ) == 0
    assert checkpoint == before
    network.assert_not_called()


def test_batch_recheck_returns_checked_count_and_promotes_then_dispatches_only_exact_rows():
    checkpoint = {"official_results": {
        "20": result(20, store_date_exact=False, release_date="2026-10-25"),
        "10": result(10, store_date_exact=False),
    }}
    master = {"games": []}
    session = object()
    fetch = Mock(return_value={10: exact_detail()})
    events = []
    upsert = lambda passed_master, row: events.append(("upsert", passed_master is master, row["appid"]))
    dispatch = lambda passed_checkpoint, row: events.append(("dispatch", passed_checkpoint is checkpoint, row["appid"]))

    checked = application.reverify_pending_store_dates(
        checkpoint, master, clock=lambda: NOW, session_factory=lambda: session,
        fetch_store_release_details=fetch, upsert_qualified_master=upsert,
        dispatch_content_event=dispatch,
    )

    assert checked == 2
    fetch.assert_called_once_with(session, [10, 20], today=TODAY, interval=0.5)
    assert events == [("upsert", True, 10), ("dispatch", True, 10)]
    assert checkpoint["official_results"]["10"]["store_date_exact"] is True
    assert checkpoint["official_results"]["20"]["store_date_exact"] is False
    assert checkpoint["official_results"]["20"]["store_date_status"] == "unavailable"
    assert application.reverify_pending_store_dates(
        checkpoint, master, clock=lambda: NOW, session_factory=Mock(side_effect=AssertionError("Once today")),
        fetch_store_release_details=fetch, upsert_qualified_master=upsert,
        dispatch_content_event=dispatch,
    ) == 0


def test_recheck_read_failure_preserves_batch_and_master():
    checkpoint = {"official_results": {"10": result(store_date_exact=False)}}
    master = {"games": []}
    before = deepcopy((checkpoint, master))
    followup = Mock(side_effect=AssertionError("No promotion before Store read"))
    with pytest.raises(RuntimeError, match="incomplete Store batch"):
        application.reverify_pending_store_dates(
            checkpoint, master, clock=lambda: NOW, session_factory=object,
            fetch_store_release_details=Mock(side_effect=RuntimeError("incomplete Store batch")),
            upsert_qualified_master=followup, dispatch_content_event=followup,
        )
    assert (checkpoint, master) == before
    followup.assert_not_called()


@pytest.mark.parametrize("use_case", ["verify", "reverify", "promotion"])
def test_naive_verification_clock_fails_before_network_or_master_mutation(use_case):
    naive = NOW.replace(tzinfo=None)
    observed = result(store_date_exact=use_case == "promotion")
    master = {"games": []}
    before = deepcopy((observed, master))
    network = Mock(side_effect=AssertionError("No network with unknown Taiwan day"))
    with pytest.raises(ValueError, match="clock must include an offset"):
        if use_case == "verify":
            application.verify_store_date_for_result(
                observed, object(), clock=lambda: naive, fetch_store_release_details=network,
            )
        elif use_case == "reverify":
            application.reverify_pending_store_dates(
                {"official_results": {"10": observed}}, master, clock=lambda: naive,
                session_factory=network, fetch_store_release_details=network,
                upsert_qualified_master=network, dispatch_content_event=network,
            )
        else:
            domain.upsert_qualified_master(
                master, observed, now=naive, blocked=set(), is_disallowed=network,
            )
    assert (observed, master) == before
    network.assert_not_called()


def test_store_transport_adapter_forwards_explicit_interval_and_read_errors(monkeypatch):
    from scripts import steam_master_date_gate as shared
    session = object()
    fetch = Mock(return_value={10: exact_detail()})
    monkeypatch.setattr(shared, "fetch_store_release_details", fetch)
    assert adapter.fetch_store_release_details(session, [10], today=TODAY, interval=0.5) == {10: exact_detail()}
    fetch.assert_called_once_with(session, [10], today=TODAY, interval=0.5)
    fetch.side_effect = RuntimeError("Shared Store source failed")
    with pytest.raises(RuntimeError, match="Shared Store source failed"):
        adapter.fetch_store_release_details(session, [10], today=TODAY)


def test_adult_ledger_adapter_preserves_missing_ledger_failure_and_blocked_ids(monkeypatch):
    from scripts import steam_adult_exclusions as shared
    read = Mock(return_value={10})
    monkeypatch.setattr(shared, "excluded_appids", read)
    assert adapter.excluded_appids() == {10}
    assert adapter.is_disallowed({"appid": 10}, {10}) is True
    assert adapter.is_disallowed({"appid": 11, "content_descriptors": {"ids": [4]}}, set()) is True
    assert adapter.is_disallowed({"appid": 11, "content_descriptorids": [2]}, set()) is False
    read.side_effect = RuntimeError("Steam adult exclusion ledger missing")
    with pytest.raises(RuntimeError, match="ledger missing"):
        adapter.excluded_appids()
