"""A cooldown override is an explicit, bounded manual probe of official XML."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from tests.test_unified_twitch_followers import (
    NOW, checkpoint, known_group, ordinary_candidate, run_worker,
    twitch_candidate, worker, xml_response,
)


def deadline(hours):
    return (NOW.astimezone(worker.TZ) + timedelta(hours=hours)).isoformat()


def authorize(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("SCHEDULE_TRIGGER_SOURCE", "manual")


def prior_checkpoint(queue, *, cooldown):
    cp = checkpoint(queue)
    cp.update(
        next_request_after_taipei=cooldown,
        rate_limit_count=1,
        community_cooldown={
            "retry_at": cooldown, "attempts": 1,
            "observed_at": (NOW - timedelta(minutes=10)).isoformat(),
        },
        group_resolution_api_cooldown={
            "status": "api_rate_limited", "retry_at": deadline(10), "attempts": 3,
        },
    )
    return cp


@pytest.mark.parametrize("event,source", [
    ("schedule", "manual"), ("repository_dispatch", "manual"),
    ("workflow_dispatch", "cloudflare"), ("workflow_dispatch", None),
])
def test_override_rejects_nonmanual_origins_before_inputs_network_or_save(monkeypatch, event, source):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    if source is None:
        monkeypatch.delenv("SCHEDULE_TRIGGER_SOURCE", raising=False)
    else:
        monkeypatch.setenv("SCHEDULE_TRIGGER_SOURCE", source)
    monkeypatch.setattr("sys.argv", ["followers", "--skip-cooldown"])
    forbidden = Mock(side_effect=AssertionError("Rejected probes cannot touch state or network"))
    for name in ("read", "save", "git_push", "reverify_pending_store_dates",
                 "retry_pending_content_dispatches"):
        monkeypatch.setattr(worker, name, forbidden)
    monkeypatch.setattr(worker.requests, "Session", forbidden)

    with pytest.raises(ValueError):
        worker.main()

    forbidden.assert_not_called()


def test_scheduled_run_without_override_keeps_obeying_cooldown(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("SCHEDULE_TRIGGER_SOURCE", "cloudflare")
    first = known_group(twitch_candidate())
    original = prior_checkpoint([first], cooldown=deadline(5))

    cp, report, client, *_ = run_worker(
        monkeypatch, tmp_path, [first], [], cooldown=deadline(5),
        initial_checkpoint=original, legacy_cooldown=deadline(7),
    )

    client.get.assert_not_called()
    assert report["stop_reason"] == "official_429_cooldown_no_request"
    assert report["request_limit"] == 250
    assert report["manual_cooldown_override"] is False
    assert cp["scheduler_batch"]["manual_cooldown_override"] is False
    assert cp["next_request_after_taipei"] == deadline(5)
    assert cp["community_cooldown"] == original["community_cooldown"]


def test_authorized_probe_bypasses_both_cooldowns_once_and_accepts_true_zero(monkeypatch, tmp_path):
    authorize(monkeypatch)
    first, later = known_group(twitch_candidate()), known_group(ordinary_candidate())
    original = prior_checkpoint([first, later], cooldown=deadline(5))

    cp, report, client, verify, upsert, dispatch, sleep = run_worker(
        monkeypatch, tmp_path, [first, later], [xml_response(0)],
        cooldown=deadline(5), legacy_cooldown=deadline(7),
        initial_checkpoint=original, extra_args=("--skip-cooldown",), max_requests=250,
    )

    assert client.get.call_count == report["requests_this_run"] == 1
    assert report["request_limit"] == 1
    assert report["manual_cooldown_override"] is True
    assert report["official_new_this_run"] == 1
    assert cp["scheduler_batch"]["manual_cooldown_override"] is True
    assert cp["attempt_events"][-1]["manual_cooldown_override"] is True
    assert cp["official_results"]["123"]["official_followers"] == 0
    assert "124" not in cp["official_results"]
    assert cp["next_request_after_taipei"] is None
    assert cp["community_cooldown"] is None
    assert cp["rate_limit_count"] == 0
    assert cp["group_resolution_api_cooldown"] == original["group_resolution_api_cooldown"]
    assert worker.read(worker.FROZEN / "checkpoint.json")["next_request_after_taipei"] == deadline(7)
    verify.assert_not_called()
    upsert.assert_not_called()
    dispatch.assert_not_called()
    sleep.assert_not_called()

    # A prior manual receipt is history, not authorization for the next run.
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("SCHEDULE_TRIGGER_SOURCE", "cloudflare")
    automatic, next_report, next_client, *_ = run_worker(
        monkeypatch, tmp_path, [later], [], cooldown=deadline(3),
        legacy_cooldown=deadline(7), initial_checkpoint=cp,
    )
    next_client.get.assert_not_called()
    assert next_report["manual_cooldown_override"] is False
    assert next_report["stop_reason"] == "official_429_cooldown_no_request"
    assert automatic["scheduler_batch"]["manual_cooldown_override"] is False
    assert automatic["attempt_events"][-1]["manual_cooldown_override"] is True
    assert automatic["next_request_after_taipei"] == deadline(3)


@pytest.mark.parametrize("cp_hours,legacy_hours,community_hours,retry_after,expected_hours", [
    (3, 4, 3, "43200", 12),  # A longer Steam Retry-After wins.
    (9, 4, 9, "60", 9),      # A short header cannot shorten the current cooldown.
    (3, 10, 3, "60", 10),    # The legacy cooldown remains a lower bound as well.
    (3, 4, 13, "60", 13),    # Preserve a longer saved Community receipt too.
])
def test_probe_429_keeps_the_longest_deadline_and_increments_existing_failure_count(
        monkeypatch, tmp_path, cp_hours, legacy_hours, community_hours, retry_after, expected_hours):
    authorize(monkeypatch)
    first, later = known_group(twitch_candidate()), known_group(ordinary_candidate())
    original = prior_checkpoint([first, later], cooldown=deadline(cp_hours))
    original["community_cooldown"]["retry_at"] = deadline(community_hours)
    limited = SimpleNamespace(status_code=429, headers={"Retry-After": retry_after})

    cp, report, client, *_ = run_worker(
        monkeypatch, tmp_path, [first, later], [limited],
        cooldown=deadline(cp_hours), legacy_cooldown=deadline(legacy_hours),
        initial_checkpoint=original, extra_args=("--skip-cooldown",),
    )

    assert client.get.call_count == report["requests_this_run"] == report["request_limit"] == 1
    assert report["stop_reason"] == "first_http_429"
    assert report["manual_cooldown_override"] is True
    assert cp["rate_limit_count"] == 2
    assert cp["community_cooldown"]["attempts"] == 2
    assert cp["next_request_after_taipei"] == deadline(expected_hours)
    assert datetime.fromisoformat(cp["community_cooldown"]["retry_at"]) == datetime.fromisoformat(deadline(expected_hours))
    assert cp["official_results"] == {}
    assert cp["group_resolution_api_cooldown"] == original["group_resolution_api_cooldown"]
    assert cp["attempt_events"][-1]["manual_cooldown_override"] is True
    assert worker.read(worker.FROZEN / "checkpoint.json")["next_request_after_taipei"] == deadline(legacy_hours)


@pytest.mark.parametrize("failure,expected", [
    (503, "http_access_or_server_error"),
    (401, "http_access_or_server_error"),
    (403, "http_access_or_server_error"),
    ("network", "transport_or_xml_error"),
    ("mismatch", "invalid_official_xml"),
])
def test_probe_failures_do_not_clear_existing_cooldowns_or_claim_followers(
        monkeypatch, tmp_path, failure, expected):
    authorize(monkeypatch)
    first = known_group(twitch_candidate())
    original = prior_checkpoint([first], cooldown=deadline(5))
    if failure == "network":
        result = requests.ConnectionError("Offline simulated failure")
    elif failure == "mismatch":
        result = xml_response(5000, worker.group_to_gid(999))
    else:
        result = SimpleNamespace(status_code=failure, headers={})

    cp, report, client, *_ = run_worker(
        monkeypatch, tmp_path, [first], [result], cooldown=deadline(5),
        legacy_cooldown=deadline(7), initial_checkpoint=original,
        extra_args=("--skip-cooldown",),
    )

    assert client.get.call_count == report["request_limit"] == 1
    assert report["stop_reason"] == expected
    assert report["manual_cooldown_override"] is True
    assert cp["official_results"] == {}
    if failure == "mismatch":
        assert cp["next_request_after_taipei"] == original["next_request_after_taipei"]
        assert cp["community_cooldown"] == original["community_cooldown"]
        assert report["remaining_queue"] == 1
    else:
        assert datetime.fromisoformat(cp["next_request_after_taipei"]) >= datetime.fromisoformat(deadline(7))
    assert cp["rate_limit_count"] == original["rate_limit_count"]
    assert cp["group_resolution_api_cooldown"] == original["group_resolution_api_cooldown"]
    assert worker.read(worker.FROZEN / "checkpoint.json")["next_request_after_taipei"] == deadline(7)
