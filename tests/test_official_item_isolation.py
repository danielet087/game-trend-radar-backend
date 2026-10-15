"""A bad group cannot monopolize otherwise healthy official candidates."""
from types import SimpleNamespace

import pytest

from tests.test_unified_twitch_followers import (
    known_group, ordinary_candidate, run_worker, worker, xml_response,
)


@pytest.mark.parametrize("failure", [404, 410, "mismatch"])
def test_individual_group_failure_keeps_evidence_and_collects_next_game(
        monkeypatch, tmp_path, failure):
    queue = [known_group(ordinary_candidate(aid)) for aid in (124, 125)]
    failed = (xml_response(6000, worker.group_to_gid(999)) if failure == "mismatch"
              else SimpleNamespace(status_code=failure, headers={}))
    cp, report, client, *_ = run_worker(
        monkeypatch, tmp_path, queue,
        [failed, xml_response(0, worker.group_to_gid(125))],
    )
    assert client.get.call_count == 2
    assert "124" not in cp["official_results"]
    assert cp["official_results"]["125"]["official_followers"] == 0
    assert cp["attempt_events"][0]["retry_scope"] == "candidate"
    assert report["remaining_queue"] == 1
    assert report["candidate_errors_this_run"] == 1
    assert report["stop_reason"] == "item_errors_pending_retry"
    assert report["job_result"]["status"] == "partial"
    assert report["job_result"]["collection_complete"] is False
    assert cp["next_request_after_taipei"] is None
    assert cp["scheduler_batch"]["active"] is False


def test_repeated_bad_groups_stop_after_three_without_fabricating_counts(monkeypatch, tmp_path):
    queue = [known_group(ordinary_candidate(aid)) for aid in range(124, 129)]
    cp, report, client, *_ = run_worker(
        monkeypatch, tmp_path, queue,
        [SimpleNamespace(status_code=404, headers={}) for _ in range(3)],
    )
    assert client.get.call_count == 3
    assert report["stop_reason"] == "consecutive_item_errors"
    assert report["remaining_queue"] == 5
    assert report["candidate_errors_this_run"] == 3
    assert cp["official_results"] == {}
    assert cp["scheduler_batch"]["active"] is False


def test_429_after_an_isolated_failure_still_stops_all_network_and_preserves_retry_after(
        monkeypatch, tmp_path):
    queue = [known_group(ordinary_candidate(aid)) for aid in (124, 125, 126)]
    cp, report, client, *_ = run_worker(
        monkeypatch, tmp_path, queue,
        [SimpleNamespace(status_code=404, headers={}),
         SimpleNamespace(status_code=429, headers={"Retry-After": "7200"})],
    )
    assert client.get.call_count == 2
    assert report["stop_reason"] == "first_http_429"
    assert report["job_result"]["status"] == "cooling_down"
    assert report["http_429_this_run"] == 1
    assert cp["community_cooldown"]["retry_seconds"] == 7200
    assert cp["official_results"] == {}
    assert cp["scheduler_batch"]["active"] is False


def test_pacing_cannot_start_another_request_after_collection_budget(monkeypatch, tmp_path):
    from radar_backend.application import official_followers as application

    original = application.run_official_batch

    def bounded(args, *, paths, services, **kwargs):
        # setup, first precheck, first post-pacing check, first request start,
        # first progress print, second precheck, pacing elapsed, final recheck.
        ticks = iter([100, 100, 100, 100, 100, 100, 100, 100, 281])
        from dataclasses import replace
        services = replace(services, monotonic=lambda: next(ticks, 281))
        args.max_seconds = 200
        return original(args, paths=paths, services=services, **kwargs)

    monkeypatch.setattr("radar_backend.jobs.official_followers.run_official_batch", bounded)
    queue = [known_group(ordinary_candidate(aid)) for aid in (124, 125)]
    cp, report, client, *_ = run_worker(
        monkeypatch, tmp_path, queue, [xml_response(0, worker.group_to_gid(124))],
    )
    assert client.get.call_count == 1
    assert report["stop_reason"] == "hour_time_budget"
    assert report["remaining_queue"] == 1
    assert cp["scheduler_batch"]["active"] is False
