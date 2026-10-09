"""Production composition and historical call-time ports, with offline sources."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.adapters import official_catalog as catalog, steam_store
from radar_backend.jobs import official_followers as job
from scripts import experiment_official_daily_catchup_250 as legacy

NOW = datetime(2026, 10, 9, 11, tzinfo=timezone(timedelta(hours=8)))


def result(appid=123, **changes):
    row = {"appid": appid, "name": "Verified game", "official_followers": 6000,
           "official_checked_at_taipei": NOW.isoformat(), "release_date": "2026-10-20",
           "store_date_exact": True, "release_display_precision": "date_full",
           "official_source": "Steam Community XML memberCount"}
    row.update(changes)
    return row


def detail():
    return {"exact": True, "status": "date_full", "release_start": "2026-10-21",
            "release_display_provider": "Steam IStoreBrowseService/GetItems",
            "release_date_timezone": "Asia/Taipei", "release_time_utc": "2026-10-20T23:00:00Z"}


def test_canonical_services_do_not_call_historical_worker_ports(monkeypatch):
    for name in ("verify_store_date_for_result", "upsert_qualified_master", "reverify_pending_store_dates",
                 "dispatch_content_event", "retry_pending_content_dispatches", "content_dispatch_signature"):
        monkeypatch.setattr(legacy, name, Mock(side_effect=AssertionError("No historical worker port")))
    fetch = Mock(return_value={123: detail(), 456: detail()})
    monkeypatch.setattr(steam_store, "fetch_store_release_details", fetch)
    monkeypatch.setattr(steam_store, "excluded_appids", lambda: set())
    monkeypatch.setattr(steam_store, "is_disallowed", lambda row, blocked: False)
    monkeypatch.setattr(job, "clock", lambda: NOW)
    session = SimpleNamespace(headers={})
    monkeypatch.setattr(job.requests, "Session", lambda: session)
    services = job.default_services(job.default_paths())
    post = Mock(return_value=SimpleNamespace(status_code=204))
    monkeypatch.setattr(job.requests, "post", post)  # Resolve after factory creation.
    monkeypatch.setenv("CONTENT_BACKEND_REPOSITORY", "example/content")
    monkeypatch.setenv("CONTENT_BACKEND_TOKEN", "offline-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/steam")
    row, master = result(), {"games": []}
    assert services.verify_store_date_for_result(row, session) is True
    assert row["release_date"] == "2026-10-20" and row["release_date_conflict"] is True
    assert services.upsert_qualified_master(master, row) is True
    checkpoint = {"official_results": {"123": row}}
    assert services.dispatch_content_event(checkpoint, row) == "dispatched"
    assert services.retry_pending_content_dispatches(checkpoint) == 0
    checkpoint["official_results"]["456"] = result(456, store_date_exact=False)
    assert services.reverify_pending_store_dates(checkpoint, master) == 1
    assert [row["appid"] for row in master["games"]] == [123, 456]
    assert post.call_count == 2
    assert fetch.call_args.kwargs == {"today": NOW.date(), "interval": 0.5}
    payload = post.call_args.kwargs["json"]
    assert payload["event_type"] == "steam_game_qualified"
    assert payload["client_payload"]["source_repository"] == "example/steam"


def test_legacy_store_wrappers_resolve_runtime_callbacks(monkeypatch):
    monkeypatch.setattr(legacy, "clock", lambda: NOW)
    fetch = Mock(return_value={123: detail()})
    monkeypatch.setattr(legacy, "fetch_store_release_details", fetch)
    row = result(store_date_exact=False)
    assert legacy.verify_store_date_for_result(row, object()) is True
    assert fetch.call_args.kwargs["interval"] == 0.0
    assert row["store_date_checked_at_taipei"] == NOW.isoformat()
    row, session = result(store_date_exact=False), object()
    monkeypatch.setattr(legacy.requests, "Session", lambda: session)
    promote, dispatch = Mock(return_value=False), Mock(return_value="not_configured")
    monkeypatch.setattr(legacy, "upsert_qualified_master", promote)
    monkeypatch.setattr(legacy, "dispatch_content_event", dispatch)
    checkpoint, master = {"official_results": {"123": row}}, {"games": []}
    assert legacy.reverify_pending_store_dates(checkpoint, master, limit=1) == 1
    assert fetch.call_args.args[0] is session
    promote.assert_called_once_with(master, row)
    dispatch.assert_called_once_with(checkpoint, row)


def test_legacy_dispatch_and_retry_resolve_runtime_signature_and_http(monkeypatch):
    monkeypatch.setattr(legacy, "clock", lambda: NOW)
    monkeypatch.setattr(legacy, "content_dispatch_signature", lambda row: "custom-signature")
    monkeypatch.setenv("CONTENT_BACKEND_REPOSITORY", "example/content")
    monkeypatch.setenv("CONTENT_BACKEND_TOKEN", "offline-token")
    post = Mock(return_value=SimpleNamespace(status_code=204))
    monkeypatch.setattr(legacy.requests, "post", post)
    row, checkpoint = result(), {}
    assert legacy.dispatch_content_event(checkpoint, row) == "dispatched"
    assert checkpoint["content_dispatches"]["123"]["signature"] == "custom-signature"
    assert checkpoint["content_dispatches"]["123"]["dispatched_at_taipei"] == NOW.isoformat()
    assert post.call_args.kwargs["json"]["client_payload"]["signature"] == "custom-signature"
    retry = Mock(return_value="failed")
    monkeypatch.setattr(legacy, "dispatch_content_event", retry)
    checkpoint = {"official_results": {"123": row}}
    assert legacy.retry_pending_content_dispatches(checkpoint, limit=1) == 1
    retry.assert_called_once_with(checkpoint, row)


@pytest.mark.parametrize("entry", [legacy, catalog])
@pytest.mark.parametrize("followers", [True, 4999])
def test_ineligible_promotion_does_not_read_ledger_or_clock(monkeypatch, entry, followers):
    ledger = Mock(side_effect=AssertionError("No ledger before qualification"))
    monkeypatch.setattr(legacy if entry is legacy else steam_store, "excluded_appids", ledger)
    if entry is catalog:
        monkeypatch.setattr(catalog, "_clock", Mock(side_effect=AssertionError("No clock")))
    master = {"games": [{"appid": 9, "unknown": "preserved"}]}
    before = deepcopy(master)
    assert entry.upsert_qualified_master(master, result(official_followers=followers)) is False
    assert master == before
    ledger.assert_not_called()


@pytest.mark.parametrize("entry", [legacy, catalog])
def test_malformed_adult_ledger_stops_promotion_without_mutation(monkeypatch, entry):
    ledger = Mock(side_effect=ValueError("Malformed exclusion ledger"))
    monkeypatch.setattr(legacy if entry is legacy else steam_store, "excluded_appids", ledger)
    master = {"games": []}
    with pytest.raises(ValueError, match="Malformed exclusion ledger"):
        entry.upsert_qualified_master(master, result())
    assert master == {"games": []}
