"""Offline behavior checks for group discovery ahead of official XML lookup."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sys
from unittest.mock import Mock

import pytest
import requests

from scripts import resolve_official_group_ids as resolver


NOW = datetime(2026, 10, 3, 10, 40, tzinfo=timezone.utc)
GROUP_BASE = 103582791429521408
FAKE_KEY = "group-resolution-test-key-do-not-persist"


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def row(appid=123, *, priority=False, group=None):
    return {
        "appid": appid, "name": f"Game {appid}", "release_date": "2026-10-03",
        "group_id64": group,
        "queue_source": ("twitch_steam_discovery" if priority
                         else "fresh_daily_prefilter_ge4000_pending_official"),
        "steam_url": f"https://store.steampowered.com/app/{appid}/",
        "eligibility_evidence": {"kept": True},
    }


def checkpoint(*rows):
    return {
        "pending_candidates": {str(item["appid"]): deepcopy(item) for item in rows},
        "official_results": {}, "attempt_events": [{"appid": 999, "http": 429}],
        "next_request_after_taipei": "2026-10-03T22:00:00+08:00",
        "community_cooldown": {"status": "rate_limited", "attempts": 5},
        "rate_limit_count": 5,
        "scheduler_batch": {"status": "finished", "stop_reason": "first_http_429"},
    }


def response(payload=None, *, code=200, headers=None):
    result = Mock(status_code=code, headers=headers or {})
    result.json.return_value = payload
    return result


def found(appid=123):
    return response({"response": {"success": 1, "steamid": str(GROUP_BASE + appid)}})


def collect(cp, candidates=None, *, session=None, now=NOW, **kwargs):
    return resolver.collect(
        cp, candidates if candidates is not None else list(cp["pending_candidates"].values()),
        api_key=FAKE_KEY, now=now, session=session or Mock(),
        sleep=kwargs.pop("sleep", lambda _: None), **kwargs,
    )


def test_cached_group_ids_require_no_http_and_source_inputs_are_unchanged():
    candidate = row(group=str(GROUP_BASE + 123))
    cp = checkpoint(candidate)
    original = deepcopy(cp)
    session = Mock()
    session.get.side_effect = AssertionError("A known group must not be looked up again")

    batch = collect(cp, session=session)

    assert batch["requests_this_run"] == 0
    assert cp == original
    session.get.assert_not_called()


def test_latest_checkpoint_group_wins_over_a_stale_missing_group_candidate():
    stale = row()
    cp = checkpoint({**stale, "group_id64": str(GROUP_BASE + 123)})
    session = Mock()
    session.get.side_effect = AssertionError("Checkpoint already has the group")

    batch = collect(cp, [stale], session=session)

    assert batch["requests_this_run"] == 0
    session.get.assert_not_called()


def test_priority_order_request_budget_and_request_start_pacing():
    # Late Twitch entries precede the ordinary queue without a separate worker.
    candidates = [row(aid) for aid in range(200, 223)] + [row(123, priority=True)]
    cp = checkpoint(*candidates)
    elapsed = [0.0]
    starts = []

    def sleep(seconds):
        elapsed[0] += seconds

    def get(url, **kwargs):
        starts.append((elapsed[0], url, kwargs["params"]))
        elapsed[0] += 0.25
        return found(int(kwargs["params"]["vanityurl"]))

    session = Mock()
    session.get.side_effect = get
    batch = collect(cp, session=session, monotonic=lambda: elapsed[0], sleep=sleep)

    assert batch["requests_this_run"] == session.get.call_count == 20
    appids = [int(params["vanityurl"]) for _, _, params in starts]
    assert appids[0] == 123
    assert appids[1:] == list(range(200, 219))
    assert len(set(appids)) == 20
    assert all(second[0] - first[0] >= 1 for first, second in zip(starts, starts[1:]))
    assert all(url.startswith("https://api.steampowered.com/ISteamUser/ResolveVanityURL/")
               and params["url_type"] == 3 for _, url, params in starts)
    assert not any("steamcommunity.com" in url for _, url, _ in starts)
    assert all("key" not in call.kwargs["params"]
               and call.kwargs["headers"]["x-webapi-key"] == FAKE_KEY
               and call.kwargs["allow_redirects"] is False
               for call in session.get.call_args_list)


def test_elapsed_budget_stops_before_starting_another_request():
    cp = checkpoint(row(123), row(124))
    elapsed = [0.0]
    session = Mock()

    def get(*args, **kwargs):
        elapsed[0] = 121
        return found(123)

    session.get.side_effect = get
    batch = collect(cp, session=session, monotonic=lambda: elapsed[0])

    assert batch["requests_this_run"] == session.get.call_count == 1
    assert "124" not in batch["results"]


@pytest.mark.parametrize("unsafe", [
    {"max_requests": 21}, {"max_seconds": 121}, {"interval": 0.9},
])
def test_unsafe_limits_are_rejected_before_any_http(unsafe):
    session = Mock()

    with pytest.raises(ValueError):
        collect(checkpoint(row()), session=session, **unsafe)

    session.get.assert_not_called()


@pytest.mark.parametrize("value,expected", [
    (str(GROUP_BASE + 1), str(GROUP_BASE + 1)),
    (GROUP_BASE + 4294967295, str(GROUP_BASE + 4294967295)),
    (True, None), (GROUP_BASE, None), (GROUP_BASE + 4294967296, None),
    ("76561198000000000", None), ("103582791429521531.0", None),
    ("１０３５８２７９１４２９５２１５３１", None),
])
def test_only_real_clan_steamids_are_accepted(value, expected):
    assert resolver.valid_group_id(value) == expected


def test_group_success_enriches_only_group_fields_and_never_resets_community_cooldown():
    cp = checkpoint(row())
    original = deepcopy(cp)
    session = Mock()
    session.get.return_value = found()

    batch = collect(cp, session=session)
    merged = resolver.apply_batch(cp, batch)

    assert cp == original
    assert session.get.call_count == 1  # Community cooldown does not block Group API.
    updated = merged["pending_candidates"]["123"]
    assert updated["group_id64"] == str(GROUP_BASE + 123)
    assert updated["group_resolution"]["status"] == "resolved"
    assert updated["group_resolution"]["checked_at"] == stamp(NOW)
    assert {key: value for key, value in updated.items()
            if key not in {"group_id64", "group_resolution"}} == {
        key: value for key, value in original["pending_candidates"]["123"].items()
        if key not in {"group_id64", "group_resolution"}
    }
    for key in ("official_results", "attempt_events", "next_request_after_taipei",
                "community_cooldown", "rate_limit_count", "scheduler_batch"):
        assert merged[key] == original[key]
    assert "official_followers" not in updated


def test_missing_api_key_is_explicit_without_attempting_community_fallback():
    cp = checkpoint(row())
    session = Mock()
    session.get.side_effect = AssertionError("No credential means no HTTP")

    batch = resolver.collect(cp, list(cp["pending_candidates"].values()),
                             api_key="", now=NOW, session=session)

    assert batch["requests_this_run"] == 0
    assert batch["results"]["123"]["group_resolution"]["status"] == "missing_api_key"
    assert batch["results"]["123"]["group_resolution"]["attempted"] is False
    assert not batch.get("api_cooldown_update")
    session.get.assert_not_called()


def test_configuring_a_key_retries_missing_key_rows_without_waiting_an_hour():
    cp = checkpoint(row())
    missing = resolver.collect(cp, list(cp["pending_candidates"].values()),
                               api_key="", now=NOW, session=Mock())
    cp = resolver.apply_batch(cp, missing)
    session = Mock()
    session.get.return_value = found()

    batch = collect(cp, session=session, now=NOW + timedelta(seconds=1))
    merged = resolver.apply_batch(cp, batch)

    assert batch["requests_this_run"] == 1
    assert merged["pending_candidates"]["123"]["group_resolution"]["status"] == "resolved"
    assert merged["pending_candidates"]["123"]["group_id64"] == str(GROUP_BASE + 123)


@pytest.mark.parametrize("failure,expected", [
    ("not_found", "not_found"), ("invalid_json", "invalid_response"),
    ("wrong_id_type", "invalid_response"), ("malformed_success", "invalid_response"),
    ("unexpected_api_code", "api_error"),
])
def test_not_found_is_distinct_from_invalid_or_failed_responses(failure, expected):
    cp = checkpoint(row(123), row(124))
    first = response({"response": {"success": 42}})
    if failure == "invalid_json":
        first.json.side_effect = ValueError("not json")
    elif failure == "wrong_id_type":
        first = response({"response": {"success": 1, "steamid": "76561198000000000"}})
    elif failure == "malformed_success":
        first = response({"response": {"success": 1}})
    elif failure == "unexpected_api_code":
        first = response({"response": {"success": 2}})
    session = Mock()
    session.get.side_effect = [first, found(124)]

    batch = collect(cp, session=session)

    receipt = batch["results"]["123"]
    assert receipt["group_resolution"]["status"] == expected
    assert receipt["group_resolution"]["attempted"] is True
    assert "group_id64" not in receipt
    assert "official_followers" not in receipt
    if expected in {"not_found", "invalid_response"}:
        assert receipt["group_resolution"]["retry_at"] == stamp(NOW + timedelta(days=1))
    assert batch["results"]["124"]["group_resolution"]["status"] == "resolved"
    assert session.get.call_count == 2


@pytest.mark.parametrize("header,seconds", [
    ("600", 900), ("Sat, 03 Oct 2026 11:40:00 GMT", 3600),
])
def test_first_429_stops_batch_and_respects_retry_after(header, seconds):
    cp = checkpoint(row(123), row(124))
    session = Mock()
    session.get.return_value = response(code=429, headers={"Retry-After": header})

    batch = collect(cp, session=session)
    merged = resolver.apply_batch(cp, batch)

    assert batch["requests_this_run"] == session.get.call_count == 1
    assert batch["results"]["123"]["group_resolution"]["status"] == "api_rate_limited"
    assert "124" not in batch["results"]
    until = datetime.fromisoformat(batch["api_cooldown_update"]["retry_at"].replace("Z", "+00:00"))
    assert until == NOW + timedelta(seconds=seconds)
    assert merged["group_resolution_api_cooldown"]["retry_at"] == stamp(until)
    assert merged["community_cooldown"] == cp["community_cooldown"]
    assert merged["next_request_after_taipei"] == cp["next_request_after_taipei"]


@pytest.mark.parametrize("api_result,status,minutes", [
    (84, "api_rate_limited", 15), (15, "api_forbidden", 1440),
])
def test_http_200_api_throttle_or_access_denial_stops_the_whole_batch(api_result, status, minutes):
    cp = checkpoint(row(123), row(124))
    session = Mock()
    session.get.return_value = response({"response": {"success": api_result}})

    batch = collect(cp, session=session)

    assert batch["requests_this_run"] == session.get.call_count == 1
    assert batch["stop_reason"] == status
    assert batch["results"]["123"]["group_resolution"]["status"] == status
    assert batch["api_cooldown_update"]["retry_at"] == stamp(NOW + timedelta(minutes=minutes))
    assert "124" not in batch["results"]


def test_existing_group_api_cooldown_makes_no_request():
    cp = checkpoint(row())
    cp["group_resolution_api_cooldown"] = {
        "status": "api_rate_limited", "observed_at": stamp(NOW - timedelta(minutes=1)),
        "retry_at": stamp(NOW + timedelta(minutes=5)), "attempts": 2,
    }
    original = deepcopy(cp)
    session = Mock()
    session.get.side_effect = AssertionError("Group API cooldown must be respected")

    batch = collect(cp, session=session)

    assert batch["requests_this_run"] == 0
    assert batch["stop_reason"] == "api_cooldown"
    assert batch["results"] == {}
    assert cp == original
    session.get.assert_not_called()


@pytest.mark.parametrize("failure,status", [
    ("forbidden", "api_forbidden"), ("network", "network_error"),
])
def test_transport_or_forbidden_failure_stops_without_leaking_key(failure, status, capsys):
    cp = checkpoint(row(123), row(124))
    session = Mock()
    if failure == "forbidden":
        # A server-controlled error body is not safe log text.
        session.get.return_value = response({"message": f"invalid key={FAKE_KEY}"}, code=403)
    else:
        session.get.side_effect = requests.ConnectionError(
            f"GET https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/?key={FAKE_KEY}")

    batch = collect(cp, session=session)

    assert session.get.call_count == batch["requests_this_run"] == 1
    assert batch["results"]["123"]["group_resolution"]["status"] == status
    assert "124" not in batch["results"]
    assert batch.get("api_cooldown_update")
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out + captured.err + json.dumps(batch)


def test_per_game_retry_defers_only_that_game():
    delayed = row(123, priority=True)
    delayed["group_resolution"] = {
        "status": "not_found", "checked_at": stamp(NOW - timedelta(hours=1)),
        "retry_at": stamp(NOW + timedelta(hours=23)), "attempted": True,
    }
    cp = checkpoint(delayed, row(124))
    session = Mock()
    session.get.return_value = found(124)

    batch = collect(cp, session=session)

    assert session.get.call_count == 1
    assert session.get.call_args.kwargs["params"]["vanityurl"] == "124"
    assert "123" not in batch["results"]
    assert batch["results"]["124"]["group_resolution"]["status"] == "resolved"


def test_twitch_not_found_retries_earlier_than_ordinary_candidates():
    cp = checkpoint(row(123, priority=True), row(124))
    session = Mock()
    session.get.return_value = response({"response": {"success": 42}})

    batch = collect(cp, session=session)

    assert batch["results"]["123"]["group_resolution"]["retry_at"] == stamp(NOW + timedelta(hours=3))
    assert batch["results"]["124"]["group_resolution"]["retry_at"] == stamp(NOW + timedelta(hours=24))


@pytest.mark.parametrize("change", ["new_group", "newer_status", "completed", "withdrawn"])
def test_fresh_apply_preserves_concurrent_queue_progress(change):
    cp = checkpoint(row())
    session = Mock()
    session.get.return_value = found()
    batch = collect(cp, session=session)
    fresh = deepcopy(cp)
    if change == "new_group":
        fresh["pending_candidates"]["123"].update(
            group_id64=str(GROUP_BASE + 999), group_resolution={
                "status": "resolved", "checked_at": stamp(NOW + timedelta(seconds=10)),
                "source": "Steam ISteamUser/ResolveVanityURL", "attempted": True})
    elif change == "newer_status":
        fresh["pending_candidates"]["123"]["group_resolution"] = {
            "status": "not_found", "checked_at": stamp(NOW + timedelta(seconds=10)),
            "retry_at": stamp(NOW + timedelta(days=1)), "attempted": True}
    elif change == "completed":
        fresh["official_results"]["123"] = {
            "official_followers": 0, "official_checked_at_taipei": stamp(NOW)}
    else:
        fresh["pending_candidates"].pop("123")

    merged = resolver.apply_batch(fresh, batch)

    assert merged["pending_candidates"] == fresh["pending_candidates"]
    assert merged["official_results"] == fresh["official_results"]
    assert merged["attempt_events"] == fresh["attempt_events"]


def test_failed_result_cannot_clear_an_already_resolved_group_even_if_receipt_is_newer():
    cp = checkpoint(row())
    session = Mock()
    session.get.return_value = response({"response": {"success": 42}})
    batch = collect(cp, session=session, now=NOW + timedelta(hours=1))
    fresh = deepcopy(cp)
    fresh["pending_candidates"]["123"].update(
        group_id64=str(GROUP_BASE + 123), group_resolution={
            "status": "resolved", "checked_at": stamp(NOW), "attempted": True})

    merged = resolver.apply_batch(fresh, batch)

    assert merged["pending_candidates"]["123"] == fresh["pending_candidates"]["123"]


def test_fresh_apply_does_not_attach_a_result_to_a_replaced_candidate():
    cp = checkpoint(row())
    session = Mock()
    session.get.return_value = found()
    batch = collect(cp, session=session)
    fresh = deepcopy(cp)
    fresh["pending_candidates"]["123"]["release_date"] = "2026-10-04"
    fresh["pending_candidates"]["123"]["queue_source"] = "replacement_discovery"

    merged = resolver.apply_batch(fresh, batch)

    assert merged["pending_candidates"] == fresh["pending_candidates"]


def test_fresh_eligibility_filter_prevents_reactivating_an_expired_candidate():
    cp = checkpoint(row())
    session = Mock()
    session.get.return_value = found()
    batch = collect(cp, session=session)

    merged = resolver.apply_batch(cp, batch, eligible_appids=[])

    assert merged["pending_candidates"] == cp["pending_candidates"]


def test_older_batch_cannot_shorten_a_newer_global_api_cooldown():
    cp = checkpoint(row())
    session = Mock()
    session.get.return_value = response(code=429, headers={"Retry-After": "600"})
    batch = collect(cp, session=session)
    fresh = deepcopy(cp)
    fresh["group_resolution_api_cooldown"] = {
        "status": "api_rate_limited", "observed_at": stamp(NOW + timedelta(minutes=1)),
        "retry_at": stamp(NOW + timedelta(hours=2)), "attempts": 9,
    }

    merged = resolver.apply_batch(fresh, batch)

    assert merged["group_resolution_api_cooldown"] == fresh["group_resolution_api_cooldown"]


def test_malformed_old_api_cooldown_does_not_prevent_a_valid_new_receipt():
    cp = checkpoint(row())
    cp["group_resolution_api_cooldown"] = "old invalid data"
    session = Mock()
    session.get.return_value = response(code=429)

    batch = collect(cp, session=session)
    merged = resolver.apply_batch(cp, batch)

    assert merged["group_resolution_api_cooldown"]["status"] == "api_rate_limited"
    assert merged["group_resolution_api_cooldown"]["retry_at"] == stamp(NOW + timedelta(minutes=15))


def test_apply_cli_keeps_new_daily_rows_from_fresh_projection(monkeypatch, tmp_path):
    from tests.test_unified_twitch_followers import (
        checkpoint as formal_checkpoint, current_daily, queue_inputs,
    )
    worker = resolver.worker
    cp = formal_checkpoint()
    frozen, legacy, groups = queue_inputs()
    eligible, prefilter = current_daily(567)
    eligible["screened_at"] = prefilter["updated_at"] = stamp(NOW)
    prefilter["games"]["567"]["group_short_id"] = None
    documents = {
        worker.CHECKPOINT: cp,
        worker.FROZEN / "source_queue.json": frozen,
        worker.FROZEN / "checkpoint.json": legacy,
        worker.FROZEN / "source_unresolved.json": groups,
        worker.ELIGIBLE: eligible, worker.PREFILTER: prefilter,
        worker.OFFICIAL_CACHE: {"games": {}}, worker.ORIGINAL_OFFICIAL: {"verified": {}},
    }
    monkeypatch.setattr(worker, "clock", lambda: NOW.astimezone(worker.TZ))
    monkeypatch.setattr(worker, "read", lambda path: deepcopy(documents[path]))
    forbidden = Mock(side_effect=AssertionError("CLI apply must never contact Steam"))
    monkeypatch.setattr(resolver.requests, "Session", forbidden)
    projected, candidates = resolver.current_queue(cp)
    session = Mock()
    session.get.return_value = found(567)
    batch = collect(projected, candidates, session=session)
    assert "567" not in cp["pending_candidates"]
    batch_path = tmp_path / "collected.json"
    documents[batch_path] = batch
    written = {}
    monkeypatch.setattr(worker, "save", lambda path, value: written.update({path: deepcopy(value)}))
    monkeypatch.setattr(sys, "argv", ["resolve", "--phase", "apply", "--batch", str(batch_path)])

    resolver.main()

    assert set(written) == {worker.CHECKPOINT}
    merged = written[worker.CHECKPOINT]
    assert merged["pending_candidates"]["567"]["group_id64"] == str(GROUP_BASE + 567)
    assert merged["pending_candidates"]["567"]["queue_source"] == "fresh_daily_prefilter_ge4000_pending_official"
    assert merged["official_results"] == cp["official_results"]
    assert merged["attempt_events"] == cp["attempt_events"]
    forbidden.assert_not_called()
