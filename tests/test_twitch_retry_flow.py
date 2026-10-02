"""Offline integration checks for Twitch import retries and legacy receipts."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests

from scripts.import_twitch_steam_discoveries import (
    FOLLOWERS, RateLimited, apply_batch, collect, request,
)
from tests.test_twitch_steam_admission import (
    NOW, SHA, extend_frontend_appids, fake_frontend, metadata_responses, proof,
)


def stamp(instant):
    return instant.isoformat().replace("+00:00", "Z")


def xml_response(count=42):
    response = Mock(status_code=200, headers={})
    response.text = f"<memberList><groupDetails><memberCount>{count}</memberCount></groupDetails></memberList>"
    return response


def cache(*appids):
    return {"games": {str(appid): {"followers": 40 + index,
            "checked_at": stamp(NOW - timedelta(minutes=10))}
            for index, appid in enumerate(appids)}}


def legacy_receipt(appid, failure_time, *, reason="RateLimited", until=None):
    enrollment_proof = proof()
    enrollment_proof["appid"] = appid
    return {"status": "pending", "reason": reason,
            "updated_at": stamp(failure_time),
            "retry_at": stamp(until or failure_time + timedelta(hours=6)),
            "validation_version": 1, "twitch_admission": enrollment_proof}


def community_calls(session):
    return [call for call in session.get.call_args_list
            if call.args[0].startswith("https://steamcommunity.com/")]


def run_collect(root, master, state, session, *, now=NOW, caches=None, **kwargs):
    return collect(root, SHA, master, state, session=session, now=now,
                   caches=caches, blocked=set(), sleep=lambda _: None, **kwargs)


def test_legacy_six_hour_receipts_migrate_then_real_xml_imports_once(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, [123, 892970])
    failed_at = NOW - timedelta(hours=2)
    old_until = failed_at + timedelta(hours=6)
    derivative = legacy_receipt(123, NOW - timedelta(minutes=5),
                                reason="steam_community_cooldown", until=old_until)
    derivative["rate_limit_stage"] = "steam_community"
    state = {"games": {
        "892970": legacy_receipt(892970, failed_at),
        "123": derivative,
        # A receipt outside the current frontend snapshot must migrate too;
        # otherwise it can resurrect the old global deadline next hour.
        "999": legacy_receipt(999, NOW - timedelta(minutes=7),
                               reason="steam_community_cooldown", until=old_until),
    }, "api_cooldowns": {"steam_community": {
        "retry_at": stamp(old_until), "updated_at": stamp(failed_at)}}}
    original = deepcopy(state)
    session = Mock()
    session.get.side_effect = [*metadata_responses(123), xml_response(14),
                               *metadata_responses(892970), xml_response(15)]

    output = run_collect(tmp_path, {"games": []}, state, session)

    assert [(record["appid"], record["followers"]) for record in output["records"]] == [(123, 14), (892970, 15)]
    assert len(community_calls(session)) == 2
    assert state == original
    assert output["retry_migrations"]["cooldowns"]["steam_community"]["before"] == original["api_cooldowns"]["steam_community"]
    expected_until = stamp(failed_at + timedelta(hours=1))
    assert output["retry_migrations"]["cooldowns"]["steam_community"]["after"]["retry_at"] == expected_until
    assert output["retry_migrations"]["games"]["999"]["after"]["retry_at"] == expected_until

    master, saved = apply_batch({"games": []}, state, output)
    assert saved["api_cooldowns"]["steam_community"]["retry_at"] == expected_until
    assert saved["games"]["999"]["retry_at"] == expected_until
    session.reset_mock()
    session.get.side_effect = AssertionError("Already qualified games must not make another Steam request")
    again = run_collect(tmp_path, master, saved, session, now=NOW + timedelta(minutes=5))
    assert again["records"] == []
    assert session.get.call_count == 0
    _, resaved = apply_batch(master, saved, again)
    assert resaved["api_cooldowns"]["steam_community"] == saved["api_cooldowns"]["steam_community"]
    assert resaved["games"]["999"]["retry_at"] == expected_until


def test_migrated_first_hour_blocks_only_community_and_keeps_cached_imports(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, [123, 124, 125])
    failed_at = NOW - timedelta(minutes=30)
    state = {"games": {"123": legacy_receipt(123, failed_at)},
             "api_cooldowns": {"steam_community": {
                 "retry_at": stamp(failed_at + timedelta(hours=6)),
                 "updated_at": stamp(failed_at)}}}
    session = Mock()
    session.get.side_effect = [*metadata_responses(124), *metadata_responses(125)]

    output = run_collect(tmp_path, {"games": []}, state, session, caches=[cache(124)])

    assert community_calls(session) == []
    assert session.get.call_count == 4
    assert [(record["appid"], record["followers"]) for record in output["records"]] == [(124, 40)]
    assert output["state_updates"]["125"]["reason"] == "steam_community_cooldown"
    assert output["state_updates"]["125"]["retry_at"] == stamp(failed_at + timedelta(hours=1))


def test_new_official_cache_bypasses_per_appid_community_retry(tmp_path):
    fake_frontend(tmp_path)
    pending = legacy_receipt(123, NOW - timedelta(minutes=20))
    pending.update(rate_limit_stage="steam_community", retry_source="steam_retry_after",
                   retry_after="10800", retry_at=stamp(NOW + timedelta(hours=3)),
                   rate_limit_attempts=1)
    state = {"games": {"123": pending}}
    session = Mock()
    session.get.side_effect = list(metadata_responses(123))

    output = run_collect(tmp_path, {"games": []}, state, session, caches=[cache(123)])

    assert len(output["records"]) == 1
    assert output["records"][0]["followers"] == 40
    assert session.get.call_count == 2
    assert community_calls(session) == []
    _, saved = apply_batch({"games": []}, state, output)
    accepted = saved["games"]["123"]
    assert accepted["retry_at"] is None
    assert accepted["rate_limit_stage"] is None
    assert accepted["retry_source"] is None
    assert accepted["rate_limit_attempts"] == 0


@pytest.mark.parametrize("stage", ["steam_store_browse", "steam_appdetails"])
def test_metadata_server_retry_is_not_bypassed_by_followers_cache(tmp_path, stage):
    fake_frontend(tmp_path)
    until = NOW + timedelta(minutes=10)
    state = {"api_cooldowns": {stage: {
        "retry_at": stamp(until), "updated_at": stamp(NOW - timedelta(minutes=5)),
        "observed_at": stamp(NOW - timedelta(minutes=5)),
        "retry_source": "steam_retry_after", "retry_after": "900", "attempts": 1,
    }}}
    session = Mock()
    session.get.side_effect = AssertionError("Followers cache cannot bypass metadata service cooldown")

    output = run_collect(tmp_path, {"games": []}, state, session, caches=[cache(123)])

    assert not session.get.called
    assert output["records"] == []
    assert output["stop_reason"] == "steam_metadata_cooldown"
    assert output["state_updates"]["123"]["retry_at"] == stamp(until)


def test_transient_request_errors_backoff_and_success_clears_retry_receipts(tmp_path):
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

    # Waiting runs preserve the original deadline instead of starting a fresh
    # ten-minute timer on every hourly frontend update.
    session.reset_mock()
    session.get.side_effect = AssertionError("Retry is not due yet")
    waiting = run_collect(tmp_path, master, saved, session, now=second_at + timedelta(minutes=9))
    assert not session.get.called
    master, saved = apply_batch(master, saved, waiting)
    assert saved["games"]["123"]["retry_at"] == stamp(second_at + timedelta(minutes=10))

    success_at = second_at + timedelta(minutes=10)
    session.reset_mock()
    session.get.side_effect = [*metadata_responses(123), xml_response(19)]
    successful = run_collect(tmp_path, master, saved, session, now=success_at)
    assert [(record["appid"], record["followers"]) for record in successful["records"]] == [(123, 19)]
    master, saved = apply_batch(master, saved, successful)
    accepted = saved["games"]["123"]
    assert accepted["retry_attempts"] == 0
    assert accepted["rate_limit_attempts"] == 0
    for key in ("retry_at", "rate_limit_stage", "retry_source", "retry_after"):
        assert accepted[key] is None
    assert {record["appid"] for record in master["games"]} == {123, 124}


def test_delayed_429_uses_response_clock_and_full_retry_after_seconds():
    observed_at = NOW + timedelta(minutes=10)
    session = Mock()
    session.get.return_value = Mock(status_code=429, headers={"Retry-After": "900"})

    with pytest.raises(RateLimited) as caught:
        request(session, FOLLOWERS.format(appid=123), params={"xml": 1},
                now=NOW, clock=lambda: observed_at)

    error = caught.value
    assert error.stage == "steam_community"
    assert error.retry_seconds == 900
    assert error.policy["observed_at"] == stamp(observed_at)
    assert error.policy["retry_at"] == stamp(NOW + timedelta(minutes=25))
    assert error.policy["retry_source"] == "steam_retry_after"
    assert error.policy["retry_after"] == "900"


def test_delayed_429_collect_saves_observed_time_not_batch_start(tmp_path):
    fake_frontend(tmp_path)
    observed_at = NOW + timedelta(minutes=10)
    limited = Mock(status_code=429, headers={"Retry-After": "900"})
    session = Mock()
    session.get.side_effect = [*metadata_responses(123), limited]

    output = run_collect(tmp_path, {"games": []}, {}, session,
                         clock=lambda: observed_at)

    assert output["state_updates"]["123"]["retry_at"] == stamp(NOW + timedelta(minutes=25))
    cooldown = output["cooldown_updates"]["steam_community"]
    assert cooldown["observed_at"] == cooldown["updated_at"] == stamp(observed_at)
    assert cooldown["retry_at"] == stamp(NOW + timedelta(minutes=25))


def test_missing_retry_after_creates_one_global_default_and_accepts_cached_game(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, [123, 124, 125])
    limited = Mock(status_code=429, headers={})
    session = Mock()
    session.get.side_effect = [*metadata_responses(123), limited,
                               *metadata_responses(124), *metadata_responses(125)]

    output = run_collect(tmp_path, {"games": []}, {}, session, caches=[cache(125)])

    assert len(community_calls(session)) == 1
    assert [(record["appid"], record["followers"]) for record in output["records"]] == [(125, 40)]
    assert output["state_updates"]["124"]["reason"] == "steam_community_cooldown"
    cooldown = output["cooldown_updates"]["steam_community"]
    assert cooldown["retry_source"] == "default_backoff"
    assert cooldown["retry_after"] is None
    assert cooldown["retry_at"] == stamp(NOW + timedelta(minutes=15))
    assert output["state_updates"]["123"]["retry_at"] == cooldown["retry_at"]
    assert output["state_updates"]["124"]["retry_at"] == cooldown["retry_at"]


def test_current_version_waiter_inherits_and_migrates_original_legacy_failure(tmp_path):
    fake_frontend(tmp_path)
    # Halloween's date parser was already upgraded while it was waiting on an
    # older game's Community cooldown; the waiting receipt itself is v3.
    appid = 3219630
    extend_frontend_appids(tmp_path, [appid])
    failed_at = NOW - timedelta(hours=2)
    old_until = failed_at + timedelta(hours=6)
    waiting = legacy_receipt(appid, NOW - timedelta(minutes=10),
                             reason="steam_community_cooldown", until=old_until)
    waiting.update(validation_version=3, rate_limit_stage="steam_community")
    state = {"games": {
        "892970": legacy_receipt(892970, failed_at),
        str(appid): waiting,
    }, "api_cooldowns": {"steam_community": {
        "retry_at": stamp(old_until), "updated_at": stamp(failed_at)}}}
    browse, details_response = metadata_responses(appid)
    item = browse.json.return_value["response"]["store_items"][0]
    item["name"] = "Halloween: The Game"
    item["release"]["steam_release_date"] = int(NOW.replace(month=9, day=8, hour=16).timestamp())
    details = details_response.json.return_value[str(appid)]["data"]
    details["name"] = "Halloween: The Game"
    details["release_date"] = {"date": "2026-09-08", "coming_soon": False}
    session = Mock()
    session.get.side_effect = [browse, details_response, xml_response(7620)]

    output = run_collect(tmp_path, {"games": []}, state, session)

    assert len(community_calls(session)) == 1
    assert len(output["records"]) == 1
    accepted = output["records"][0]
    assert accepted["appid"] == appid
    assert accepted["followers"] == 7620
    assert accepted["release_start"] == "2026-09-09"
    migration = output["retry_migrations"]["games"][str(appid)]
    assert migration["before"]["validation_version"] == 3
    assert migration["after"]["retry_at"] == stamp(failed_at + timedelta(hours=1))
    master, saved = apply_batch({"games": []}, state, output)
    assert master["games"][0]["appid"] == appid
    assert saved["games"][str(appid)]["status"] == "accepted"
    assert saved["games"][str(appid)]["retry_at"] is None
    assert saved["api_cooldowns"]["steam_community"]["retry_at"] == stamp(failed_at + timedelta(hours=1))


def test_empty_followup_batch_preserves_master_and_entire_import_state(tmp_path):
    fake_frontend(tmp_path)
    session = Mock()
    session.get.side_effect = list(metadata_responses(123))
    first = run_collect(tmp_path, {"games": []}, {}, session, caches=[cache(123)])
    master, saved = apply_batch({"games": []}, {}, first)
    original_master, original_state = deepcopy(master), deepcopy(saved)
    # A new frontend commit and the next 15-minute schedule tick must not
    # rewrite state timestamps or generate a commit for an idle import.
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
    assert not any(followup.get("retry_migrations", {}).values())
    assert not session.get.called

    resaved_master, resaved_state = apply_batch(master, saved, followup)

    assert resaved_master == original_master
    assert resaved_state == original_state
    assert master == original_master and saved == original_state
