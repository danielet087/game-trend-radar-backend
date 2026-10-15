"""Queue fairness after real Followers failures, without network or state reset."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from radar_backend.domain.official_queue import (
    GROUP_BASE, last_failed_request_times, make_queue,
)


NOW = datetime(2026, 10, 15, 7, 30, tzinfo=timezone.utc)
TWITCH = "twitch_steam_discovery"
DAILY = "fresh_daily_prefilter_unresolved"


def candidate(appid, *, source=DAILY, release="2026-10-16"):
    return {"appid": appid, "name": f"Game {appid}", "release_date": release,
            "group_id64": str(GROUP_BASE + appid), "queue_source": source}


def event(appid, *, age=1, status="rate_limited", http=429, source=DAILY, **extra):
    return {"appid": appid, "queue_source": source, "status": status, "http": http,
            "when_taipei": (NOW - timedelta(hours=age)).isoformat(), **extra}


def build_queue(rows, events):
    frozen = [candidate(appid) for appid in range(10000, 11317)]
    groups = [{"appid": appid, "group_short_id": appid}
              for appid in range(10000, 11358)]
    legacy = {"cohort": "steam_fresh_20260922_post_adult_1317_near_release",
              "official_results": {str(row["appid"]): {} for row in frozen}}
    checkpoint = {"pending_candidates": {str(row["appid"]): deepcopy(row) for row in rows},
                  "official_results": {}, "attempt_events": deepcopy(events),
                  "next_request_after_taipei": (NOW + timedelta(hours=8)).isoformat(),
                  "rate_limit_count": 6}
    before = deepcopy(checkpoint)
    queue, _ = make_queue(
        checkpoint, frozen, legacy, groups, {"games": []}, {}, {"games": {}},
        {"verified": {}}, now=NOW, is_twitch_queue_candidate=lambda *args, **kwargs: True,
        cached_follower=lambda *args: None,
    )
    # Rotation never clears rate-limit protection or invents measurements.
    assert checkpoint["next_request_after_taipei"] == before["next_request_after_taipei"]
    assert checkpoint["rate_limit_count"] == before["rate_limit_count"]
    assert checkpoint["official_results"] == before["official_results"]
    assert checkpoint["attempt_events"] == before["attempt_events"]
    for row in rows:
        assert checkpoint["pending_candidates"][str(row["appid"])] == row
    return [row["appid"] for row in queue]


def test_failed_twitch_candidate_cannot_block_untried_ordinary_candidates():
    rows = [candidate(4218620, source=TWITCH, release="2026-10-13"),
            candidate(4944320, release="2026-10-15"), candidate(5224520)]
    events = [event(4218620, source=TWITCH), event(4944320, age=24)]
    assert build_queue(rows, events) == [5224520, 4944320, 4218620]


def test_repeated_ordinary_failure_rotates_behind_untried_work():
    rows = [candidate(1, release="2026-10-15"), candidate(2), candidate(3)]
    events = [event(1, age=3), event(1, age=2), event(1, age=1)]
    assert build_queue(rows, events) == [2, 3, 1]


def test_failed_candidates_rotate_by_latest_real_failure_across_sources():
    rows = [candidate(1, source=TWITCH), candidate(2), candidate(3)]
    events = [event(1, age=5, source=TWITCH), event(2, age=3),
              event(3, age=2), event(1, age=1, source=TWITCH)]
    assert build_queue(rows, events) == [2, 3, 1]


def test_unfailed_candidates_keep_twitch_and_near_release_priority():
    rows = [candidate(1, release="2026-10-20"),
            candidate(2, release="2026-10-15"),
            candidate(3, source=TWITCH, release="2026-10-21")]
    assert build_queue(rows, []) == [3, 2, 1]


@pytest.mark.parametrize("extra", [
    {"status": "cooldown_no_request", "http": None},
    {"status": "cooldown_no_request", "http": 429},
    {"status": "awaiting_group_resolution", "http": None},
    {"status": "request_started", "http": None},
    {"status": "rate_limited", "http": 429, "cache_reused": True},
    {"status": "transport_or_xml_error", "http": None, "error_type": None},
])
def test_no_request_or_cache_reuse_does_not_introduce_a_failure(extra):
    assert last_failed_request_times([event(1, **extra)], now=NOW) == {}


def test_transport_exception_counts_as_real_failure_without_http_response():
    failure = event(1, status="transport_or_xml_error", http=None, error_type="Timeout")
    assert "1" in last_failed_request_times([failure], now=NOW)


def test_legacy_http_error_without_status_is_still_a_real_failure():
    failure = event(1)
    failure.pop("status")
    assert "1" in last_failed_request_times([failure], now=NOW)


@pytest.mark.parametrize("cache_reused", [False, True])
def test_later_success_clears_failure_even_if_merged_events_arrive_out_of_order(cache_reused):
    events = [event(1, age=1, status="ok", http=200, cache_reused=cache_reused),
              event(1, age=2)]
    assert last_failed_request_times(events, now=NOW) == {}
    assert build_queue([candidate(1, source=TWITCH), candidate(2)], events) == [1, 2]


def test_no_request_does_not_clear_or_change_an_earlier_real_failure():
    failure = event(1, age=2)
    events = [failure, event(1, age=1, status="cooldown_no_request", http=None)]
    assert last_failed_request_times(events, now=NOW) == {
        "1": (NOW - timedelta(hours=2)).timestamp(),
    }


def test_invalid_or_future_events_do_not_change_rotation():
    events = [None, {}, event(1, age=-1), event(1, when_taipei="bad"),
              event(1, status={"invalid": True})]
    assert last_failed_request_times(events, now=NOW) == {}


def test_success_wins_equal_timestamp_after_checkpoint_event_merge():
    events = [event(1, status="ok", http=200), event(1)]
    assert last_failed_request_times(events, now=NOW) == {}
