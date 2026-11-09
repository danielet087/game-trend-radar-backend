"""A daily workload receipt must not confuse planning with collection."""
from copy import deepcopy
from datetime import timedelta

import pytest

from radar_backend.adapters.scheduler_status import build_status
from radar_backend.adapters import queue_inputs
from tests.test_follower_rechecks import observation
from tests.test_unified_twitch_followers import NOW, checkpoint, current_daily, queue_inputs as frozen_inputs


def project(*, age=31, followers=1000, transform=None, now=NOW):
    cp = checkpoint()
    cp["official_results"]["124"] = observation(followers=followers, age=age)
    eligible, prefilter = current_daily(124)
    prefilter["games"]["124"]["third_party_followers"] = 1000
    frozen, legacy, groups = frozen_inputs()
    args = [cp, frozen, legacy, groups, eligible, prefilter, {"games": {}}, {"verified": {}}]
    if transform:
        transform(*args)
    before = deepcopy(args)
    status = build_status(*args, now=now)
    assert args == before  # planning never changes source state or observations
    return status


def test_completed_plan_exposes_today_count_without_claiming_queries_completed():
    status = project()
    plan = status["follower_recheck_plan"]
    assert plan["status"] == "completed"
    assert plan["needed"] is True
    assert plan["planned_count"] == plan["remaining_count"] == plan["due_now_count"] == 1
    assert plan["completed_count"] == 0
    assert status["summary"]["ready_pending"] == 1


def test_daily_plan_includes_expiry_later_today_without_making_live_queue_query_early():
    # 29.9 days old is not due yet, but the 30-day deadline is later today.
    status = project(age=29.9)
    assert status["summary"]["ready_pending"] == 0
    plan = status["follower_recheck_plan"]
    assert plan["planned_count"] == plan["remaining_count"] == 1
    assert plan["due_now_count"] == 0


def test_confirmed_zero_only_after_current_daily_inputs_are_complete():
    plan = project(age=1)["follower_recheck_plan"]
    assert plan["status"] == "completed" and plan["needed"] is False
    assert plan["planned_count"] == plan["remaining_count"] == 0


@pytest.mark.parametrize("kind", ["incomplete", "yesterday", "future"])
def test_incomplete_or_wrong_day_sources_keep_workload_unknown(kind):
    def change(cp, frozen, legacy, groups, eligible, pre, cache, original):
        if kind == "incomplete":
            pre["complete"] = False
        else:
            at = NOW + timedelta(days=-1 if kind == "yesterday" else 1)
            eligible["screened_at"] = pre["updated_at"] = at.isoformat()
    plan = project(transform=change)["follower_recheck_plan"]
    assert plan["status"] == "pending" and plan["needed"] is None
    assert all(plan[field] is None for field in ("checked_at", "planned_count", "remaining_count", "completed_count"))


def test_today_success_stays_in_daily_total_after_disappearing_from_live_queue():
    def success(cp, *args):
        cp["official_results"]["124"].update(
            follower_recheck=True, official_checked_at_taipei=NOW.isoformat())
    status = project(transform=success)
    plan = status["follower_recheck_plan"]
    assert status["summary"]["ready_pending"] == 0
    assert plan["planned_count"] == plan["completed_count"] == 1
    assert plan["remaining_count"] == plan["due_now_count"] == 0


def test_first_checks_are_not_reported_as_periodic_rechecks():
    def first(cp, frozen, legacy, groups, eligible, pre, cache, original):
        cp["official_results"].clear()
        pre["games"]["124"]["third_party_followers"] = 4500
    status = project(transform=first)
    assert status["summary"]["ready_pending"] == 1
    assert status["follower_recheck_plan"]["planned_count"] == 0


def test_429_attempt_preserves_daily_plan_and_does_not_inflate_completed_count():
    def limited(cp, *args):
        cp["attempt_events"] = [{"appid": 124, "when_taipei": NOW.isoformat(),
            "status": "rate_limited", "http": 429}]
        cp["next_request_after_taipei"] = (NOW + timedelta(hours=2)).isoformat()
    plan = project(transform=limited)["follower_recheck_plan"]
    assert plan["planned_count"] == plan["remaining_count"] == 1
    assert plan["completed_count"] == 0


def test_planning_deadline_cannot_move_live_collector_into_another_day():
    cp = checkpoint()
    frozen, legacy, groups = frozen_inputs()
    with pytest.raises(ValueError, match="current Taiwan day"):
        queue_inputs.make_queue(cp, frozen, legacy, groups, {"games": []}, {}, {}, {},
            now=NOW, refresh_by=NOW + timedelta(days=1))
