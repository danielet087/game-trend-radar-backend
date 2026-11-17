"""Accepted games are measured weekly, every 2 days near release, finally on the release day."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from radar_backend.domain import follower_cadence as cadence
from radar_backend.domain.growth import growth_collection_complete, valid_scheduled
from radar_backend.domain.official_queue import GROUP_BASE
from radar_backend.publication.growth import growth_publication_payload
from scripts.classify_growth_attempt import deferred_cooldown
from tests.test_growth_daily_resume import Clock, run

TODAY = date(2026, 10, 12)  # Clock() is 2026-10-12 07:15 Taipei.


def complete(result, now):
    """The collector's own check runs before the unpublished job receipt is attached."""
    return growth_collection_complete({k: v for k, v in result.items() if k != "job_result"}, now)


def cohort(clock, games):
    """games: appid -> (release day offset from today, days since the last official check)."""
    rows, results = [], {}
    for aid, (release_in, age) in games.items():
        checked = clock.now() - timedelta(days=age)
        rows.append({"appid": aid, "followers": 6000 + aid,
                     "release_start": (TODAY + timedelta(days=release_in)).isoformat(),
                     "follower_checked_at": checked.isoformat()})
        results[str(aid)] = {"appid": aid, "group_id64": str(GROUP_BASE + aid), "official_followers": 6000 + aid,
                             "official_checked_at_taipei": checked.isoformat()}
    return rows, {"official_results": results, "pending_candidates": {}}


def test_the_rule_is_weekly_then_every_two_days_then_final_on_release_day():
    release = date(2026, 11, 30)
    assert cadence.interval_days(release, date(2026, 11, 15)) == 7
    assert cadence.interval_days(release, date(2026, 11, 16)) == 2
    assert not cadence.due(release, date(2026, 11, 1), date(2026, 11, 7))
    assert cadence.due(release, date(2026, 11, 1), date(2026, 11, 8))
    assert cadence.next_due(release, date(2026, 11, 1), date(2026, 11, 2)) == date(2026, 11, 8)
    # Entering the last 14 days shortens the interval immediately.
    assert cadence.due(release, date(2026, 11, 14), date(2026, 11, 16))
    assert cadence.next_due(release, date(2026, 11, 28), date(2026, 11, 28)) == date(2026, 11, 30)
    assert cadence.due(release, date(2026, 11, 29), release)
    assert not cadence.due(release, release, release)
    assert cadence.due(release, None, date(2026, 11, 2))
    assert cadence.due(release, date(2026, 11, 9), date(2026, 11, 2))  # future evidence


def test_only_due_games_are_requested_and_the_rest_count_as_scheduled():
    clock = Clock()
    rows, checkpoint = cohort(clock, {
        1: (40, 3),   # weekly, measured 3 days ago: scheduled
        2: (40, 8),   # weekly, 8 days ago: due
        3: (10, 1),   # last 14 days, yesterday: scheduled
        4: (10, 2),   # last 14 days, 2 days ago: due
        5: (0, 1),    # release day: final measurement due
    })
    result, client, _ = run(clock, rows, checkpoint)
    assert sorted(aid for aid, _, _ in client.calls) == [2, 4, 5]
    assert result["requests"] == 3 and result["coverage_schema_version"] == 3
    assert [row["appid"] for row in result["scheduled"]] == [1, 3]
    assert result["scheduled"][0]["next_due_taipei"] == "2026-10-16"
    assert result["scheduled"][1]["next_due_taipei"] == "2026-10-13"
    assert result["coverage"] == {"eligible": 5, "measured": 3, "scheduled": 2, "unavailable": 0, "pending": 0}
    assert result["measurement_complete"] and result["collection_complete"]
    assert all(valid_scheduled(row, clock.now()) for row in result["scheduled"])


@pytest.mark.parametrize("change", [
    lambda row: row.update(next_due_taipei="2026-10-20"),
    lambda row: row.update(release_date="2026-10-12"),
    lambda row: row.update(at="2026-10-12T00:00:00Z"),
    lambda row: row.update(at="2026-10-01T00:00:00Z"),
    lambda row: row.update(followers=-1),
    lambda row: row.update(source="third_party"),
])
def test_a_tampered_or_due_scheduled_row_breaks_completion(change):
    clock = Clock()
    rows, checkpoint = cohort(clock, {1: (40, 3), 2: (40, 8)})
    result, _, _ = run(clock, rows, checkpoint)
    assert complete(result, clock.now())
    broken = deepcopy(result)
    change(broken["scheduled"][0])
    assert not valid_scheduled(broken["scheduled"][0], clock.now())
    assert not complete(broken, clock.now())


def test_version_two_reports_cannot_carry_scheduled_rows():
    clock = Clock()
    rows, checkpoint = cohort(clock, {1: (40, 3), 2: (40, 8)})
    result, _, _ = run(clock, rows, checkpoint)
    assert complete(result, clock.now())
    legacy = {**deepcopy(result), "coverage_schema_version": 2}
    assert not complete(legacy, clock.now())


def test_publication_keeps_scheduled_coverage_against_the_published_catalog():
    clock = Clock()
    rows, checkpoint = cohort(clock, {1: (40, 3), 2: (40, 8)})
    result, _, _ = run(clock, rows, checkpoint)
    catalog = {"count": len(rows), "games": [dict(row, group_id64=str(GROUP_BASE + row["appid"])) for row in rows]}
    payload = growth_publication_payload(result, catalog, clock.now())
    assert payload["publication_coverage_complete"] is True
    assert payload["coverage"] == {"eligible": 2, "measured": 1, "scheduled": 1, "unavailable": 0, "pending": 0}


def test_cooldown_deferral_accounts_for_scheduled_games():
    clock = Clock()
    rows, checkpoint = cohort(clock, {1: (40, 3), 2: (40, 8)})
    checkpoint["next_request_after_taipei"] = (clock.now() + timedelta(hours=2)).isoformat()
    checkpoint["community_cooldown"] = {"observed_at": clock.now().isoformat(),
                                        "retry_at": checkpoint["next_request_after_taipei"]}
    result, client, _ = run(clock, rows, checkpoint, max_cooldown_wait=0)
    assert client.calls == [] and result["reason"] == "rate_limited"
    assert result["coverage"] == {"eligible": 2, "measured": 0, "scheduled": 1, "unavailable": 0, "pending": 1}
    from radar_core.jobs import JobResult
    delivered = deepcopy(result)
    job = JobResult.from_dict(delivered["job_result"])
    delivered["job_result"] = JobResult(job=job.job, status=job.status, reason=job.reason,
        collection_complete=False, state_persisted=True, published=True, requires_publication=True,
        target_slot=job.target_slot).to_dict()
    delivered["published_eligible_appids"] = result["eligible_appids"]
    assert deferred_cooldown(result, delivered, clock.now())
