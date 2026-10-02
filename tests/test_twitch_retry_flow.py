"""Offline checks for current metadata retries, cached results and safe apply."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests

from scripts.import_twitch_steam_discoveries import (
    APPDETAILS, STORE_BROWSE, RateLimited, apply_batch, collect, request,
)
from tests.test_twitch_steam_admission import (
    NOW, SHA, extend_frontend_appids, fake_frontend, metadata_responses,
)


def stamp(instant):
    return instant.isoformat().replace("+00:00", "Z")


def cache(*appids):
    return {"games": {str(appid): {"followers": 40 + index,
            "checked_at": stamp(NOW - timedelta(minutes=10))}
            for index, appid in enumerate(appids)}}


def run_collect(root, master, state, session, *, now=NOW, caches=None, **kwargs):
    return collect(root, SHA, master, state, session=session, now=now,
                   caches=caches, blocked=set(), sleep=lambda _: None, **kwargs)


@pytest.mark.parametrize("stage", ["steam_store_browse", "steam_appdetails"])
def test_metadata_server_retry_is_not_bypassed_by_followers_cache(tmp_path, stage):
    fake_frontend(tmp_path)
    until = NOW + timedelta(minutes=10)
    state = {"api_cooldowns": {stage: {
        "retry_at": stamp(until), "updated_at": stamp(NOW - timedelta(minutes=5)),
        "observed_at": stamp(NOW - timedelta(minutes=5)),
        "retry_source": "steam_retry_after", "retry_after": "900", "attempts": 1,
    }}}
    original = deepcopy(state)
    session = Mock()
    session.get.side_effect = AssertionError("Followers cache cannot bypass metadata service cooldown")

    output = run_collect(tmp_path, {"games": []}, state, session, caches=[cache(123)])

    assert state == original
    assert not session.get.called
    assert output["records"] == []
    assert output["stop_reason"] == "steam_metadata_cooldown"
    assert output["state_updates"]["123"]["retry_at"] == stamp(until)
    assert output["state_updates"]["123"]["rate_limit_stage"] == stage


def test_transient_errors_backoff_then_true_cache_acceptance_clears_receipts(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, [123, 124])
    session = Mock()
    session.get.side_effect = [requests.RequestException("network unavailable"), *metadata_responses(124)]

    first = run_collect(tmp_path, {"games": []}, {}, session, caches=[cache(124)])

    assert [record["appid"] for record in first["records"]] == [124]
    assert first["state_updates"]["123"]["retry_attempts"] == 1
    assert first["state_updates"]["123"]["retry_at"] == stamp(NOW + timedelta(minutes=5))
    assert first["cooldown_updates"] == {}
    master, saved = apply_batch({"games": []}, {}, first)
    second_at = NOW + timedelta(minutes=5)
    session.reset_mock()
    session.get.side_effect = [requests.RequestException("still unavailable")]
    second = run_collect(tmp_path, master, saved, session, now=second_at)
    assert second["state_updates"]["123"]["retry_attempts"] == 2
    assert second["state_updates"]["123"]["retry_at"] == stamp(second_at + timedelta(minutes=10))
    assert session.get.call_count == 1
    assert second["cooldown_updates"] == {}
    master, saved = apply_batch(master, saved, second)

    # An intervening run cannot restart the timer or create another request.
    session.reset_mock()
    session.get.side_effect = AssertionError("Retry is not due yet")
    waiting = run_collect(tmp_path, master, saved, session, now=second_at + timedelta(minutes=9))
    assert not session.get.called
    master, saved = apply_batch(master, saved, waiting)
    assert saved["games"]["123"]["retry_at"] == stamp(second_at + timedelta(minutes=10))

    success_at = second_at + timedelta(minutes=10)
    session.reset_mock()
    session.get.side_effect = list(metadata_responses(123))
    verified = {"official_results": {"123": {
        "official_followers": 19, "official_checked_at_taipei": stamp(success_at),
    }}}
    successful = run_collect(tmp_path, master, saved, session, now=success_at, caches=[verified])
    assert [(record["appid"], record["followers"]) for record in successful["records"]] == [(123, 19)]
    assert session.get.call_count == 2
    assert successful["follower_candidates"] == []
    master, saved = apply_batch(master, saved, successful)
    accepted = saved["games"]["123"]
    assert accepted["retry_attempts"] == 0
    assert accepted["rate_limit_attempts"] == 0
    for key in ("retry_at", "rate_limit_stage", "retry_source", "retry_after", "follower_candidate"):
        assert accepted[key] is None
    assert {record["appid"] for record in master["games"]} == {123, 124}


@pytest.mark.parametrize("url,stage", [(STORE_BROWSE, "steam_store_browse"), (APPDETAILS, "steam_appdetails")])
def test_delayed_metadata_429_uses_response_clock_and_full_retry_after(url, stage):
    observed_at = NOW + timedelta(minutes=10)
    session = Mock()
    session.get.return_value = Mock(status_code=429, headers={"Retry-After": "900"})

    with pytest.raises(RateLimited) as caught:
        request(session, url, params={"appids": 123}, now=NOW, clock=lambda: observed_at)

    error = caught.value
    assert error.stage == stage
    assert error.retry_seconds == 900
    assert error.policy["observed_at"] == stamp(observed_at)
    assert error.policy["retry_at"] == stamp(NOW + timedelta(minutes=25))
    assert error.policy["retry_source"] == "steam_retry_after"
    assert error.policy["retry_after"] == "900"


def test_delayed_metadata_429_batch_saves_observed_time(tmp_path):
    fake_frontend(tmp_path)
    observed_at = NOW + timedelta(minutes=10)
    session = Mock()
    session.get.side_effect = [Mock(status_code=429, headers={"Retry-After": "900"})]

    output = run_collect(tmp_path, {"games": []}, {}, session, clock=lambda: observed_at)

    assert output["generated_at"] == stamp(NOW)
    assert output["state_updates"]["123"]["retry_at"] == stamp(NOW + timedelta(minutes=25))
    cooldown = output["cooldown_updates"]["steam_store_browse"]
    assert cooldown["observed_at"] == cooldown["updated_at"] == stamp(observed_at)
    assert cooldown["retry_at"] == stamp(NOW + timedelta(minutes=25))
    assert session.get.call_count == 1


def test_missing_header_metadata_429_stops_before_later_cached_game(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, [123, 124])
    session = Mock()
    session.get.side_effect = [Mock(status_code=429, headers={})]

    output = run_collect(tmp_path, {"games": []}, {}, session, caches=[cache(124)])

    assert session.get.call_count == 1
    assert output["records"] == []
    assert output["stop_reason"] == "steam_rate_limited"
    cooldown = output["cooldown_updates"]["steam_store_browse"]
    assert cooldown["retry_source"] == "default_backoff"
    assert cooldown["retry_after"] is None
    assert cooldown["retry_at"] == stamp(NOW + timedelta(minutes=5))
    assert output["state_updates"]["123"]["retry_at"] == cooldown["retry_at"]
    assert "124" not in output["state_updates"]


def test_older_metadata_cooldown_batch_cannot_overwrite_newer_server_receipt():
    latest = {"games": {}, "api_cooldowns": {"steam_appdetails": {
        "retry_at": stamp(NOW + timedelta(hours=4)), "updated_at": stamp(NOW + timedelta(seconds=1)),
        "observed_at": stamp(NOW + timedelta(seconds=1)), "retry_source": "steam_retry_after",
        "retry_after": "14400", "attempts": 3,
    }}}
    batch = {"schema_version": 1, "generated_at": stamp(NOW), "records": [], "state_updates": {},
             "cooldown_updates": {"steam_appdetails": {
                 "retry_at": stamp(NOW + timedelta(minutes=5)), "updated_at": stamp(NOW),
                 "observed_at": stamp(NOW), "retry_source": "default_backoff", "retry_after": None,
                 "attempts": 1,
             }}}
    original = deepcopy(latest)
    _, saved = apply_batch({"games": []}, latest, batch)
    assert latest == original
    assert saved["api_cooldowns"] == original["api_cooldowns"]


def test_empty_followup_batch_preserves_master_and_entire_import_state(tmp_path):
    fake_frontend(tmp_path)
    session = Mock()
    session.get.side_effect = list(metadata_responses(123))
    first = run_collect(tmp_path, {"games": []}, {}, session, caches=[cache(123)])
    master, saved = apply_batch({"games": []}, {}, first)
    original_master, original_state = deepcopy(master), deepcopy(saved)
    # Updating the frontend snapshot alone does not require a new metadata read.
    session.reset_mock()
    session.get.side_effect = AssertionError("Idle imports must not make Steam requests")
    next_at = NOW + timedelta(minutes=15)
    followup = collect(tmp_path, "b" * 40, master, saved, session=session,
                       now=next_at, blocked=set(), sleep=lambda _: None)
    assert followup["generated_at"] == stamp(next_at)
    assert followup["generated_at"] != saved["updated_at"]
    assert followup["records"] == []
    assert followup["state_updates"] == {}
    assert followup["cooldown_updates"] == {}
    assert followup["follower_candidates"] == []
    assert not session.get.called

    resaved_master, resaved_state = apply_batch(master, saved, followup)

    assert resaved_master == original_master
    assert resaved_state == original_state
    assert master == original_master and saved == original_state
