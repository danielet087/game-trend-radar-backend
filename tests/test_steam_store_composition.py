"""Shared Store ports are composed directly and retain failure boundaries."""
from copy import deepcopy
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

from radar_backend.adapters import official_catalog, steam_metadata, steam_store
from radar_backend.adapters.candidate_sources import CandidateSources
from radar_backend.domain.store_release import parse_store_release_detail
from radar_backend.state import adult_exclusions
from scripts import screen_steam_candidates_before_followers as legacy_screen
from scripts import steam_adult_exclusions as legacy_adult
from scripts import steam_master_date_gate as legacy_release


NOW = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 9)


def candidate(appid=101):
    return {"appid": appid, "name": "Verified game", "release_start": "2026-10-20",
            "release_end": "2026-10-20", "release_precision": "day", "followers": 6000}


def store_item():
    return {"appid": 101, "release": {"coming_soon_display": "date_full",
                                       "steam_release_date": 1792537200},
            "content_descriptorids": [1], "basic_info": {"short_description": "Adventure"}}


def forbid_legacy(monkeypatch):
    for module, names in (
        (legacy_screen, ("fetch_metadata", "build_snapshot", "classify", "is_explicit_sex_game")),
        (legacy_release, ("parse_store_release_detail", "fetch_store_release_details",
                          "apply_store_release_detail", "filter_confirmed_master_games")),
        (legacy_adult, ("excluded_appids", "is_disallowed")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, Mock(side_effect=AssertionError("No legacy Store port")))


def test_candidate_default_ports_use_shared_layers_when_legacy_helpers_are_forbidden(monkeypatch):
    sources = CandidateSources()
    forbid_legacy(monkeypatch)
    items = {101: store_item()}
    fetch = Mock(return_value=items)
    monkeypatch.setattr(steam_metadata, "fetch_metadata", fetch)
    monkeypatch.setattr(adult_exclusions, "excluded_appids", lambda: {202})
    monkeypatch.setattr(steam_store, "_utc_now", lambda: NOW)
    session = object()
    assert sources.fetch_metadata(session, [101]) is items
    catalog = {"games": [candidate(), candidate(202)]}
    original = deepcopy(catalog)
    snapshot = sources.build_snapshot(catalog, items)
    assert snapshot["screened_at"] == NOW.isoformat()
    assert snapshot["reasons"] == {"audited_adult_exclusion": 1, "eligible": 1}
    assert snapshot["count"] == 1 and catalog == original
    details = sources.fetch_store_release_details(session, [101], today=TODAY, interval=0.5)
    assert details == {101: parse_store_release_detail(items[101], today=TODAY)}
    assert fetch.call_args.kwargs == {"batch_size": 35, "interval": 0.5}
    row = sources.apply_store_release_detail(snapshot["games"][0], details[101])
    assert row["release_start"] == "2026-10-20"
    assert row["post_followers_store_verified_at"] == NOW.isoformat()
    assert row["post_followers_store_verified"] is True
    assert sources.filter_confirmed_master_games([row], snapshot["games"], today=TODAY) == [row]
    assert sources.excluded_appids() == {202}
    assert sources.is_disallowed(candidate(202), {202}) is True


def test_official_ports_share_direct_metadata_and_ledger_owners(monkeypatch):
    forbid_legacy(monkeypatch)
    fetch = Mock(return_value={101: store_item()})
    ledger = Mock(return_value=set())
    monkeypatch.setattr(steam_metadata, "fetch_metadata", fetch)
    monkeypatch.setattr(adult_exclusions, "excluded_appids", ledger)
    row = {"appid": 101, "name": "Verified game", "official_followers": 6000,
           "release_date": "2026-10-20"}
    master = {"games": []}
    assert official_catalog.verify_store_date_for_result(row, object(), clock=lambda: NOW) is True
    assert row["release_date"] == "2026-10-20"
    assert official_catalog.upsert_qualified_master(master, row, clock=lambda: NOW) is True
    assert master["games"][0]["appid"] == 101
    ledger.assert_called_once_with()


@pytest.mark.parametrize("operation", ["snapshot", "master_filter", "official_promotion"])
def test_direct_composition_propagates_ledger_failure_without_mutating_input(monkeypatch, operation):
    sources = CandidateSources()
    ledger = Mock(side_effect=RuntimeError("Steam adult exclusion ledger missing"))
    monkeypatch.setattr(adult_exclusions, "excluded_appids", ledger)
    catalog, master = {"games": [candidate()]}, {"games": []}
    before = deepcopy((catalog, master))
    with pytest.raises(RuntimeError, match="ledger missing"):
        if operation == "snapshot":
            sources.build_snapshot(catalog, {101: store_item()})
        elif operation == "master_filter":
            sources.filter_confirmed_master_games([], [], today=TODAY)
        else:
            official_catalog.upsert_qualified_master(master, {
                "appid": 101, "official_followers": 6000, "release_date": "2026-10-20",
                "store_date_exact": True, "release_display_precision": "date_full",
            }, clock=lambda: NOW)
    assert (catalog, master) == before
    ledger.assert_called_once_with()


def test_canonical_store_metadata_error_propagates_without_a_successful_snapshot(monkeypatch):
    sources = CandidateSources()
    fetch = Mock(side_effect=RuntimeError("Steam Browse batch failed"))
    monkeypatch.setattr(steam_metadata, "fetch_metadata", fetch)
    with pytest.raises(RuntimeError, match="batch failed"):
        sources.fetch_store_release_details(object(), [101], today=TODAY)
    assert fetch.call_count == 1
