"""Twitch metadata collection delegates all unknown Followers to one worker."""
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import Mock

import pytest
import requests

from scripts.import_twitch_steam_discoveries import apply_batch, collect, stamp
from tests.test_twitch_steam_admission import (
    NOW, SHA, extend_frontend_appids, fake_frontend, metadata_responses, proof,
)


def run(root, state=None, *, master=None, session=None, **kwargs):
    return collect(root, SHA, master or {"games": []}, state or {}, now=NOW,
                   session=session or Mock(), sleep=lambda _: None, blocked=set(),
                   community_queue_only=True, **kwargs)


def seed(root, *appids):
    session = Mock()
    session.get.side_effect = [response for aid in appids for response in metadata_responses(aid)]
    batch = run(root, session=session)
    _, state = apply_batch({"games": []}, {}, batch)
    return state


@pytest.mark.parametrize("until", [NOW - timedelta(minutes=1), NOW + timedelta(hours=2)])
def test_unknown_count_is_queued_without_community_even_after_cooldown(tmp_path, until):
    # An unexpired per-game Community receipt must not postpone metadata queuing.
    fake_frontend(tmp_path)
    prior = {"status": "pending", "reason": "RateLimited", "rate_limit_stage": "steam_community",
             "retry_at": stamp(until), "retry_source": "steam_retry_after", "retry_after": "7200",
             "rate_limit_attempts": 2, "updated_at": stamp(NOW - timedelta(minutes=5)),
             "validation_version": 3, "twitch_admission": proof()}
    session = Mock()
    session.get.side_effect = list(metadata_responses(123))

    batch = run(tmp_path, {"games": {"123": prior}}, session=session)

    assert session.get.call_count == 2
    assert all(not call.args[0].startswith("https://steamcommunity.com/") for call in session.get.call_args_list)
    assert batch["records"] == []
    assert batch["active_twitch_appids"] == [123]
    candidate, = batch["follower_candidates"]
    assert candidate["appid"] == 123 and candidate["release_date"] == "2026-10-02"
    assert candidate["queue_source"] == "twitch_steam_discovery"
    assert candidate["group_id64"] is None
    for field in ("followers", "follower_checked_at", "follower_source", "official_ge5000"):
        assert field not in candidate["steam_candidate"]
    pending = batch["state_updates"]["123"]
    assert pending["reason"] == "queued_official_followers"
    assert pending["retry_at"] is None and pending["rate_limit_stage"] is None
    assert pending["retry_source"] is None and pending["retry_after"] is None
    assert pending["rate_limit_attempts"] == 0
    assert pending["follower_candidate"] == candidate


def test_official_checkpoint_cache_publishes_true_count_and_removes_queue(tmp_path):
    fake_frontend(tmp_path)
    prior = seed(tmp_path, 123)
    session = Mock()
    session.get.side_effect = list(metadata_responses(123))
    cache = {"official_results": {"123": {"official_followers": 17,
             "official_checked_at_taipei": "2026-10-02T16:20:00+08:00"}}}

    batch = run(tmp_path, prior, session=session, caches=[cache])

    assert session.get.call_count == 2
    assert batch["follower_candidates"] == []
    assert batch["records"][0]["followers"] == 17
    assert batch["records"][0]["follower_checked_at"] == "2026-10-02T16:20:00+08:00"
    assert batch["state_updates"]["123"]["follower_candidate"] is None
    master, state = apply_batch({"games": []}, prior, batch)
    session.reset_mock()
    session.get.side_effect = AssertionError("Accepted rows need no request")
    again = run(tmp_path, state, master=master, session=session)
    assert again["active_twitch_appids"] == [123]
    assert again["follower_candidates"] == []
    assert not session.get.called


@pytest.mark.parametrize("mutation,reason", [
    ("adult", "adult_content"), ("dlc", "not_a_steam_game"),
    ("date_conflict", "steam_date_conflict"), ("date_uncertain", "uncertain_steam_date"),
])
def test_metadata_disqualification_removes_previously_queued_candidate(tmp_path, mutation, reason):
    fake_frontend(tmp_path)
    state = seed(tmp_path, 123)
    responses = list(metadata_responses(123))
    item = responses[0].json.return_value["response"]["store_items"][0]
    details = responses[1].json.return_value["123"]["data"]
    if mutation == "adult":
        details["content_descriptors"]["ids"] = [3]
    elif mutation == "dlc":
        details["type"] = "dlc"
    elif mutation == "date_conflict":
        details["release_date"]["date"] = "2026-10-01"
    else:
        item["release"].update(is_coming_soon=True, coming_soon_display="date_quarter")
    session = Mock()
    session.get.side_effect = responses

    batch = run(tmp_path, state, session=session)

    assert batch["records"] == [] and batch["follower_candidates"] == []
    assert batch["state_updates"]["123"]["reason"] == reason
    assert batch["state_updates"]["123"]["follower_candidate"] is None


@pytest.mark.parametrize("failure", ["request", "descriptors", "global_cooldown", "per_game_retry", "deadline"])
def test_verified_queue_survives_temporary_metadata_skip_or_failure(tmp_path, failure):
    fake_frontend(tmp_path)
    state = seed(tmp_path, 123)
    expected = deepcopy(state["games"]["123"]["follower_candidate"])
    session = Mock()
    kwargs = {}
    if failure == "request":
        session.get.side_effect = requests.ConnectionError("Temporary metadata failure")
    elif failure == "descriptors":
        responses = list(metadata_responses(123))
        responses[1].json.return_value["123"]["data"].pop("content_descriptors")
        session.get.side_effect = responses
    elif failure == "global_cooldown":
        state["api_cooldowns"] = {"steam_appdetails": {
            "retry_at": stamp(NOW + timedelta(minutes=10)), "updated_at": stamp(NOW),
            "retry_source": "steam_retry_after", "retry_after": "600", "attempts": 1}}
        session.get.side_effect = AssertionError("Metadata cooldown must be respected")
    elif failure == "per_game_retry":
        state["games"]["123"].update(reason="ConnectionError", retry_at=stamp(NOW + timedelta(minutes=5)))
        session.get.side_effect = AssertionError("Metadata retry must be respected")
    else:
        kwargs["monotonic"] = Mock(side_effect=[0, 1000])
        session.get.side_effect = AssertionError("Deadline must be respected")

    batch = run(tmp_path, state, session=session, **kwargs)

    assert batch["active_twitch_appids"] == [123]
    assert batch["follower_candidates"] == [expected]
    if "123" in batch["state_updates"]:
        assert batch["state_updates"]["123"]["follower_candidate"] == expected


@pytest.mark.parametrize("mutation", ["proof", "appid", "date", "adult", "excluded", "prior_conflict"])
def test_invalid_saved_queue_is_not_retained_when_metadata_cannot_run(tmp_path, mutation):
    fake_frontend(tmp_path)
    state = seed(tmp_path, 123)
    row = state["games"]["123"]
    candidate = row["follower_candidate"]
    if mutation == "proof":
        candidate["twitch_admission"]["igdb_id"] = "55"
    elif mutation == "appid":
        candidate["steam_candidate"]["appid"] = 999
    elif mutation == "date":
        candidate["release_date"] = "2026-10-01"
    elif mutation == "adult":
        candidate["steam_candidate"]["content_descriptorids"] = [3]
    elif mutation == "excluded":
        row["status"] = "excluded"
    else:
        row["reason"] = "steam_date_conflict"
    row["retry_at"] = stamp(NOW + timedelta(minutes=5))
    session = Mock()
    session.get.side_effect = AssertionError("Per-game retry remains active")

    batch = run(tmp_path, state, session=session)

    assert batch["follower_candidates"] == []


def test_expired_twitch_source_is_removed_from_active_queue_membership(tmp_path):
    fake_frontend(tmp_path)
    state = seed(tmp_path, 123)
    path = tmp_path / "data/twitch_tracking.json"
    tracking = json.loads(path.read_text())
    tracking["games"]["22"]["tracking_sources"]["twitch_new"]["expires_at"] = stamp(NOW)
    path.write_text(json.dumps(tracking))
    session = Mock()
    session.get.side_effect = AssertionError("Expired admission must not run")

    batch = run(tmp_path, state, session=session)

    assert batch["active_twitch_appids"] == []
    assert batch["follower_candidates"] == []


def test_metadata_limit_on_first_game_keeps_other_unvisited_pending_candidates(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, [123, 124])
    state = seed(tmp_path, 123, 124)
    limited = Mock(status_code=429, headers={"Retry-After": "600"})
    session = Mock()
    session.get.side_effect = [limited]

    batch = run(tmp_path, state, session=session)

    assert session.get.call_count == 1
    assert batch["active_twitch_appids"] == [123, 124]
    assert [candidate["appid"] for candidate in batch["follower_candidates"]] == [123, 124]
    assert batch["stop_reason"] == "steam_rate_limited"
