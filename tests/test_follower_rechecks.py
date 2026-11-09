"""Dated official rechecks, threshold recovery and honest hourly completion."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.adapters import official_groups, queue_inputs as canonical_queue
from radar_backend.domain.official_queue import latest_official_observations
from tests.test_unified_twitch_followers import (
    NOW, checkpoint, current_daily, known_group, ordinary_candidate,
    queue_inputs, run_worker, worker, xml_response,
)


def observation(appid=124, followers=2500, age=31):
    return {**known_group(ordinary_candidate(appid)),
            "official_followers": followers,
            "official_checked_at_taipei": (NOW - timedelta(days=age)).isoformat()}


def projected(*, result=None, prescreen=1000, sources=None, amend=None):
    cp = checkpoint()
    cp["official_results"]["124"] = deepcopy(result or observation())
    frozen, legacy, groups = queue_inputs()
    eligible, prefilter = current_daily(124)
    prefilter["games"]["124"]["third_party_followers"] = prescreen
    cache, original = {"games": {}}, {"verified": {}}
    if sources:
        sources(cp, legacy, cache, original)
    if amend:
        amend(cp, eligible, prefilter)
    before = deepcopy(cp["official_results"])
    queue, status = canonical_queue.make_queue(cp, frozen, legacy, groups, eligible,
        prefilter, cache, original, now=NOW)
    assert cp["official_results"] == before  # queueing cannot erase old evidence
    return queue, status, cp


@pytest.mark.parametrize("followers,days", [(0, 30), (2999, 30), (3000, 3), (4999, 3), (5000, 1), (9000, 1)])
def test_official_count_expires_at_its_1_3_30_day_boundary(followers, days):
    result = observation(followers=followers, age=days)
    result["official_checked_at_taipei"] = (NOW - timedelta(days=days) + timedelta(seconds=1)).isoformat()
    queue, _, _ = projected(result=result)
    assert queue == []
    result["official_checked_at_taipei"] = (NOW - timedelta(days=days)).isoformat()
    queue, status, cp = projected(result=result)
    assert [row["appid"] for row in queue] == [124]
    assert status["official_rechecks_pending"] == 1
    assert queue[0]["follower_recheck_after"] == result["official_checked_at_taipei"]
    assert cp["pending_candidates"]["124"]["follower_recheck"] is True


@pytest.mark.parametrize("followers", [0, 2999, 3000, 4999])
def test_fresh_ge4000_prescreen_accelerates_previously_low_official_counts(followers):
    queue, _, _ = projected(result=observation(followers=followers, age=1), prescreen=4000)
    assert [row["appid"] for row in queue] == [124]
    queue, _, _ = projected(result=observation(followers=followers, age=0.5), prescreen=6000)
    assert queue == []


@pytest.mark.parametrize("signal", [None, 0, 1000, 3999])
def test_expired_official_count_is_rechecked_even_below_first_verification_gate(signal):
    queue, _, _ = projected(prescreen=signal)
    assert [row["appid"] for row in queue] == [124]


@pytest.mark.parametrize("field,count_field,time_field", [
    ("official_results", "official_followers", "official_checked_at_taipei"),
    ("official_growth_observations", "official_followers", "official_checked_at_taipei"),
    ("games", "followers", "checked_at"),
    ("verified", "official_followers", "checked_at"),
])
def test_newest_actual_observation_across_all_producers_prevents_duplicate_query(field, count_field, time_field):
    def newer(cp, legacy, cache, original):
        cache.setdefault(field, {})["124"] = {
            "appid": 124, count_field: 4999, time_field: NOW.isoformat(),
        }
    queue, _, _ = projected(sources=newer)
    assert queue == []


@pytest.mark.parametrize("checked", [None, "bad", "2026-10-01T00:00:00", (NOW + timedelta(days=1)).isoformat()])
def test_missing_invalid_or_future_timestamp_cannot_make_old_count_permanent(checked):
    result = observation()
    result["official_checked_at_taipei"] = checked
    queue, _, _ = projected(result=result)
    assert [row["appid"] for row in queue] == [124]


def test_future_or_mismatched_identity_is_not_a_fresh_official_observation():
    rows = {"games": {
        "124": {"appid": 125, "followers": 6000, "checked_at": NOW.isoformat()},
        "125": {"followers": 6000, "checked_at": (NOW + timedelta(seconds=1)).isoformat()},
    }}
    assert latest_official_observations(rows, now=NOW) == {}


@pytest.mark.parametrize("change", [
    lambda cp, eligible, pre: eligible.update(games=[]),
    lambda cp, eligible, pre: eligible["games"][0].update(sexual_content_screened=False),
    lambda cp, eligible, pre: eligible["games"][0].update(release_display_precision="month"),
    lambda cp, eligible, pre: eligible["games"][0].update(release_precision="month"),
    lambda cp, eligible, pre: eligible["games"][0].update(release_start="2026-10-01"),
    lambda cp, eligible, pre: eligible["games"][0].update(release_start="2027-10-04"),
    lambda cp, eligible, pre: pre.update(updated_at=(NOW - timedelta(days=1)).isoformat()),
])
def test_rechecks_require_current_exact_date_content_screen_and_rolling_year(change):
    queue, _, _ = projected(amend=change)
    assert queue == []


def test_first_verification_keeps_priority_over_periodic_rechecks():
    def first_check(cp, eligible, pre):
        fresh, counts = current_daily(125)
        eligible["games"] += fresh["games"]
        pre["games"].update(counts["games"])
    queue, _, _ = projected(amend=first_check)
    assert [row["appid"] for row in queue] == [125, 124]


def test_known_official_group_survives_missing_current_prefilter_mapping():
    queue, _, _ = projected(amend=lambda cp, eligible, pre: pre["games"]["124"].update(group_short_id=None))
    assert queue[0]["group_id64"] == worker.group_to_gid(124)


def test_new_group_identity_does_not_reuse_recent_count_for_old_group():
    queue, _, _ = projected(result=observation(age=0),
        amend=lambda cp, eligible, pre: pre["games"]["124"].update(group_short_id=999))
    assert [row["appid"] for row in queue] == [124]
    assert queue[0]["group_id64"] == worker.group_to_gid(999)


def test_due_recheck_with_missing_group_can_still_resolve_despite_old_count():
    result = observation()
    result["group_id64"] = None
    queue, _, cp = projected(result=result,
        amend=lambda cp, eligible, pre: pre["games"]["124"].update(group_short_id=None))
    assert official_groups.completed(cp, "124") is False
    client = SimpleNamespace(headers={}, get=Mock(return_value=SimpleNamespace(
        status_code=200, json=lambda: {"response": {"success": 1, "steamid": worker.group_to_gid(124)}})))
    batch = official_groups.collect(cp, queue, api_key="test-key", now=NOW,
        session=client, monotonic=lambda: 100, sleep=lambda seconds: None)
    assert batch["requests_this_run"] == 1
    merged = official_groups.apply_batch(cp, batch, eligible_appids=[124])
    assert merged["pending_candidates"]["124"]["group_id64"] == worker.group_to_gid(124)
    assert merged["official_results"] == cp["official_results"]


def test_successful_recheck_crosses_threshold_and_runs_existing_publication_gates(monkeypatch, tmp_path):
    queue, _, cp = projected()
    saved, report, client, verify, upsert, dispatch, _ = run_worker(
        monkeypatch, tmp_path, queue, [xml_response(5200, worker.group_to_gid(124))],
        initial_checkpoint=cp)
    assert client.get.call_count == 1
    assert report["remaining_queue"] == 0
    assert saved["official_results"]["124"]["official_followers"] == 5200
    verify.assert_called_once()
    upsert.assert_called_once()
    dispatch.assert_called_once()
    # The saved new observation, including a threshold crossing, suppresses
    # the next hourly run without pretending the old checkpoint is new.
    queue, _, _ = projected(result=saved["official_results"]["124"])
    assert queue == []


@pytest.mark.parametrize("cooldown", [None, (NOW + timedelta(hours=2)).isoformat()])
def test_limited_or_cooling_recheck_retains_old_count_and_stays_pending(monkeypatch, tmp_path, cooldown):
    queue, _, cp = projected()
    old = deepcopy(cp["official_results"]["124"])
    limited = SimpleNamespace(status_code=429, headers={"Retry-After": "7200"})
    saved, report, client, verify, upsert, dispatch, _ = run_worker(
        monkeypatch, tmp_path, queue, [limited], cooldown=cooldown,
        initial_checkpoint=cp)
    assert client.get.call_count == (0 if cooldown else 1)
    assert saved["official_results"]["124"] == old
    assert report["remaining_queue"] == 1
    assert report["job_result"]["collection_complete"] is False
    verify.assert_not_called()
    upsert.assert_not_called()
    dispatch.assert_not_called()
