from copy import deepcopy

import pytest

from scripts.twitch_official_queue import (
    is_twitch_queue_candidate, sync_community_cooldown, sync_twitch_queue,
)
from tests.test_twitch_steam_admission import NOW, proof, row


def queued():
    metadata = row()
    for key in ("followers", "follower_checked_at", "follower_source", "official_ge5000"):
        metadata.pop(key, None)
    return {"appid": 123, "name": "Test game", "release_date": metadata["release_start"],
            "steam_url": metadata["store_url"], "group_id64": None,
            "queue_source": "twitch_steam_discovery", "twitch_admission": proof(),
            "steam_candidate": metadata}


def batch(candidate=None, *, active=None, updates=None):
    return {"follower_candidates": [queued() if candidate is None else candidate],
            "active_twitch_appids": [123] if active is None else active,
            "state_updates": {} if updates is None else updates}


def checkpoint():
    return {"pending_candidates": {}, "official_results": {
        "456": {"official_followers": 0, "official_checked_at_taipei": "2026-10-02T10:00:00+08:00"}},
        "next_request_after_taipei": "2026-10-03T01:00:00+08:00",
        "cursor": 47, "attempt_events": [{"appid": 456, "http": 200}],
        "steam_followers_cache": {"games": {"456": {"followers": 0}}}}


def test_merge_deduplicates_and_preserves_group_and_normal_shadow():
    cp = checkpoint()
    normal = {"appid": 123, "name": "Normal title", "release_date": "2026-10-10",
              "group_id64": 103582791429999999, "queue_source": "fresh_daily_prefilter_unresolved",
              "daily_source_updated_at": "keep"}
    cp["pending_candidates"]["123"] = deepcopy(normal)
    original = deepcopy(cp)
    incoming = batch()
    incoming["follower_candidates"] *= 2
    result = sync_twitch_queue(cp, incoming, NOW)
    assert cp == original
    assert len(result["pending_candidates"]) == 1
    actual = result["pending_candidates"]["123"]
    assert actual["priority"] == 100
    assert actual["group_id64"] == normal["group_id64"]
    assert actual["normal_candidate"] == normal
    assert actual["release_date"] == "2026-10-02"
    assert actual["steam_candidate"]["release_time_utc"] == queued()["steam_candidate"]["release_time_utc"]
    assert sync_twitch_queue(result, incoming, NOW) == result


@pytest.mark.parametrize("updates,active", [
    ({}, []), ({"123": {"status": "excluded", "reason": "adult_content"}}, [123]),
    ({"123": {"status": "pending", "reason": "steam_date_conflict"}}, [123]),
    ({"123": {"status": "pending", "reason": "uncertain_taiwan_store_date"}}, [123]),
    ({"123": {"status": "accepted", "reason": "steam_verified"}}, [123]),
])
def test_withdrawal_restores_normal_candidate_and_preserves_results(updates, active):
    cp = checkpoint()
    normal = {"appid": 123, "queue_source": "normal", "group_id64": 5,
              "release_date": "2026-10-10"}
    cp["pending_candidates"]["123"] = deepcopy(normal)
    result = sync_twitch_queue(cp, batch(), NOW)
    # Retain even a legitimate low result and its original observation time.
    result["official_results"]["123"] = {"official_followers": 4, "checked_at": "old"}
    incoming = {"active_twitch_appids": active, "follower_candidates": [], "state_updates": updates}
    final = sync_twitch_queue(result, incoming, NOW)
    assert final["pending_candidates"]["123"] == normal
    assert final["official_results"] == result["official_results"]
    assert final["cursor"] == cp["cursor"]
    assert final["attempt_events"] == cp["attempt_events"]
    assert final["next_request_after_taipei"] == cp["next_request_after_taipei"]
    assert final["steam_followers_cache"] == cp["steam_followers_cache"]


def test_expired_twitch_only_row_is_removed_without_losing_zero_result():
    cp = sync_twitch_queue(checkpoint(), batch(), NOW)
    cp["official_results"]["123"] = {"official_followers": 0, "checked_at": "original"}
    result = sync_twitch_queue(cp, {"active_twitch_appids": [], "follower_candidates": []}, NOW)
    assert "123" not in result["pending_candidates"]
    assert result["official_results"] == cp["official_results"]


def test_transient_metadata_failure_keeps_prior_valid_queue_candidate():
    cp = sync_twitch_queue(checkpoint(), batch(), NOW)
    incoming = {"active_twitch_appids": [123], "follower_candidates": [queued()],
                "state_updates": {"123": {"status": "pending", "reason": "steam_type_unavailable"}}}
    assert sync_twitch_queue(cp, incoming, NOW) == cp


def test_authoritative_queue_removes_active_candidate_missing_from_batch():
    cp = sync_twitch_queue(checkpoint(), batch(), NOW)
    result = sync_twitch_queue(cp, {"active_twitch_appids": [123], "follower_candidates": []}, NOW)
    assert "123" not in result["pending_candidates"]
    assert result["official_results"] == cp["official_results"]


def test_queue_does_not_persist_placeholder_or_incoming_followers():
    incoming = queued()
    incoming["steam_candidate"].update(followers=0, follower_checked_at="2026-10-02T08:00:00Z",
                                        follower_source="not a verified query", official_ge5000=False)
    incoming["official_followers"] = 0
    result = sync_twitch_queue(checkpoint(), batch(incoming), NOW)
    actual = result["pending_candidates"]["123"]
    assert "official_followers" not in actual
    for key in ("followers", "follower_checked_at", "follower_source", "official_ge5000"):
        assert key not in actual["steam_candidate"]


@pytest.mark.parametrize("change", [
    "wrong_appid", "wrong_metadata_appid", "changed_proof", "bad_proof", "bool_proof",
    "adult", "audited_adult", "malformed_descriptors", "bool_descriptors", "missing_descriptors",
    "alternate_adult", "wrong_type", "unscreened", "uncertain_date", "timestamp_conflict",
    "wrong_timezone", "wrong_store_day", "future_checked", "future_verified", "outside_window",
    "wrong_url", "strong_rule", "malformed_basic_info", "malformed_tags",
])
def test_candidate_rejects_malformed_or_ineligible_metadata(change):
    candidate = queued()
    metadata = candidate["steam_candidate"]
    if change == "wrong_appid": candidate["appid"] = 999
    elif change == "wrong_metadata_appid": metadata["appid"] = 999
    elif change == "changed_proof": metadata["twitch_admission"]["igdb_id"] = "444"
    elif change == "bad_proof": candidate["twitch_admission"]["method"] = "search"
    elif change == "bool_proof": candidate["twitch_admission"]["appid"] = True
    elif change == "adult": metadata["content_descriptorids"] = [3]
    elif change == "audited_adult":
        aid = 4005870
        candidate["appid"] = metadata["appid"] = aid
        candidate["twitch_admission"]["appid"] = metadata["twitch_admission"]["appid"] = aid
        candidate["steam_url"] = metadata["store_url"] = f"https://store.steampowered.com/app/{aid}/"
    elif change == "malformed_descriptors": metadata["content_descriptorids"] = ["3"]
    elif change == "bool_descriptors": metadata["content_descriptorids"] = [True]
    elif change == "missing_descriptors": metadata.pop("content_descriptorids")
    elif change == "alternate_adult": metadata["content_descriptors"] = {"ids": [4]}
    elif change == "wrong_type": metadata["steam_type"] = "dlc"
    elif change == "unscreened": metadata["sexual_content_screened"] = False
    elif change == "uncertain_date": candidate["release_date"] = "2026-10"
    elif change == "timestamp_conflict": metadata["release_time_utc"] = "2026-10-03T01:00:00Z"
    elif change == "wrong_timezone": metadata["release_date_timezone"] = "UTC"
    elif change == "wrong_store_day": metadata["release_store_date"] = "2026-10-01"
    elif change == "future_checked": candidate["twitch_admission"]["checked_at"] = "2026-10-03T00:00:00Z"
    elif change == "future_verified": metadata["release_date_verified_at"] = "2026-10-03T00:00:00Z"
    elif change == "outside_window":
        candidate["release_date"] = "2025-10-02"
        metadata.update(release_start="2025-10-02", release_end="2025-10-02", release_store_date="2025-10-02",
                        release_timestamp_taipei_date="2025-10-02", release_time_utc="2025-10-02T01:00:00Z")
    elif change == "wrong_url": candidate["steam_url"] = "https://store.steampowered.com/app/999/"
    elif change == "strong_rule":
        metadata["tags"] = [{"tagid": 12095}, {"tagid": 9130}]
        metadata["basic_info"] = {"short_description": "An explicit sexual game."}
    elif change == "malformed_basic_info": metadata["basic_info"] = "invalid"
    elif change == "malformed_tags": metadata["tags"] = {"tagid": 12095}
    assert not is_twitch_queue_candidate(candidate, NOW)
    assert sync_twitch_queue(checkpoint(), batch(candidate), NOW)["pending_candidates"] == {}


def test_taiwan_next_day_normalization_is_valid():
    candidate = queued()
    candidate["steam_candidate"].update(release_time_utc="2026-10-01T16:02:05Z",
                                        release_store_date="2026-10-01",
                                        release_date_normalization="steam_utc_date_normalized_to_taipei")
    assert is_twitch_queue_candidate(candidate, NOW)


def test_invalid_current_candidate_restores_normal_before_remerge():
    cp = checkpoint()
    normal = {"appid": 123, "queue_source": "normal", "release_date": "2026-10-20"}
    cp["pending_candidates"]["123"] = deepcopy(normal)
    cp = sync_twitch_queue(cp, batch(), NOW)
    cp["pending_candidates"]["123"]["steam_candidate"]["release_date_conflict"] = True
    result = sync_twitch_queue(cp, {"active_twitch_appids": [123], "follower_candidates": []}, NOW)
    assert result["pending_candidates"]["123"] == normal


def test_cooldown_transfers_complete_provenance_and_taiwan_deadline():
    cp = checkpoint()
    cooldown = {"retry_at": "2026-10-02T18:00:00Z", "retry_source": "steam_retry_after",
                "retry_after": "32400", "observed_at": "2026-10-02T09:00:00Z", "attempts": 3,
                "retry_seconds": 32400, "custom_provenance": "retain"}
    original = deepcopy(cp)
    result = sync_community_cooldown(cp, {"api_cooldowns": {"steam_community": cooldown}}, NOW)
    assert cp == original
    assert result["community_cooldown"] == cooldown
    assert result["next_request_after_taipei"] == "2026-10-03T02:00:00+08:00"
    assert result["official_results"] == original["official_results"]


@pytest.mark.parametrize("existing_kind", ["next_request", "community_cooldown", "equal"])
def test_cooldown_never_shortens_existing_deadline(existing_kind):
    cp = checkpoint()
    cp["next_request_after_taipei"] = None
    if existing_kind == "next_request": cp["next_request_after_taipei"] = "2026-10-03T02:00:00+08:00"
    else: cp["community_cooldown"] = {"retry_at": "2026-10-02T18:00:00Z", "retry_source": "steam_retry_after"}
    deadline = "2026-10-02T18:00:00Z" if existing_kind == "equal" else "2026-10-02T17:00:00Z"
    incoming = {"api_cooldowns": {"steam_community": {"retry_at": deadline, "retry_source": "default_backoff"}}}
    assert sync_community_cooldown(cp, incoming, NOW) == cp


@pytest.mark.parametrize("deadline", [None, "broken", "2026-10-03T01:00:00", "2026-10-02T08:00:00Z"])
def test_cooldown_ignores_invalid_naive_or_expired_input(deadline):
    cp = checkpoint()
    incoming = {"api_cooldowns": {"steam_community": {"retry_at": deadline}}}
    assert sync_community_cooldown(cp, incoming, NOW) == cp


def test_malformed_active_list_cannot_silently_withdraw_valid_queue():
    cp = sync_twitch_queue(checkpoint(), batch(), NOW)
    incoming = {"active_twitch_appids": [True], "follower_candidates": []}
    with pytest.raises(ValueError):
        sync_twitch_queue(cp, incoming, NOW)
    assert "123" in cp["pending_candidates"]


def test_naive_sync_time_is_rejected():
    with pytest.raises(ValueError):
        sync_twitch_queue(checkpoint(), batch(), NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        sync_community_cooldown(checkpoint(), {}, NOW.replace(tzinfo=None))
