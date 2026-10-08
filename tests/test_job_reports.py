"""A durable checkpoint, coverage completion and public delivery are distinct."""

from copy import deepcopy
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_core.jobs import JobResult, JobStatus
from scripts import experiment_official_daily_catchup_250 as worker
from scripts.external_schedule import growth_collection_complete, stamp_growth_publication
from scripts.steam_candidate_pipeline import candidate_job_result
from tests.test_unified_twitch_followers import (
    NOW, checkpoint, known_group, ordinary_candidate, xml_response,
)


def growth_report():
    return {
        "reason": "completed", "eligible": 1, "errors": [],
        "measurements": [{"appid": 10, "followers": 5000, "at": NOW.isoformat()}],
    }


def test_legacy_growth_reports_remain_compatible_but_typed_completion_is_required():
    report = growth_report()
    assert growth_collection_complete(report, NOW)
    result = JobResult(
        job="public-growth", status=JobStatus.COMPLETE, collection_complete=True,
        state_persisted=True, requires_publication=True,
    )
    report["job_result"] = result.to_dict()
    assert not growth_collection_complete(report, NOW)
    report["job_result"] = JobResult(
        job="public-growth", status=JobStatus.COMPLETE, collection_complete=True,
        state_persisted=True, requires_publication=True, published=True,
    ).to_dict()
    assert growth_collection_complete(report, NOW)
    # A success flag cannot turn incomplete measurement coverage into success.
    assert not growth_collection_complete({**report, "eligible": 2}, NOW)


@pytest.mark.parametrize("invalid", [
    None, {}, {"schema_version": 2},
    {"status": "complete", "successful": True},
])
def test_invalid_typed_result_cannot_fall_back_to_a_legacy_completed_reason(invalid):
    assert not growth_collection_complete({**growth_report(), "job_result": invalid}, NOW)


@pytest.mark.parametrize("reason,pending,source,status,successful", [
    ("batch_request_limit", 1, "current_day_prefilter_complete", JobStatus.PARTIAL, False),
    ("hour_time_budget", 2, "current_day_prefilter_complete", JobStatus.PARTIAL, False),
    ("official_429_cooldown_no_request", 1, "current_day_prefilter_complete", JobStatus.COOLING_DOWN, False),
    ("first_http_429", 1, "current_day_prefilter_complete", JobStatus.COOLING_DOWN, False),
    ("nothing_pending", 0, "no_current_day_prefilter", JobStatus.PARTIAL, False),
    ("nothing_pending", 0, "current_day_prefilter_complete", JobStatus.COMPLETE, True),
    ("git_checkpoint_failure", 0, "current_day_prefilter_complete", JobStatus.FAILED, False),
])
def test_saved_hourly_checkpoint_does_not_imply_current_day_completion(reason, pending, source, status, successful):
    result = worker.official_job_result({
        "stop_reason": reason, "remaining_queue": pending,
        "source_status": {"status": source},
    }, state_persisted=True)
    assert result.status is status
    assert result.successful is successful
    assert result.published is False


@pytest.mark.parametrize("pending_evidence", [
    {"source_status": {"status": "current_day_prefilter_complete", "parked_group_xml_appids": ["4435490"]}},
    {"awaiting_group_resolution": 1},
    {"unresolved_candidates": {"4435490": {"official_followers": None}}},
])
def test_empty_active_queue_with_unresolved_official_mapping_is_partial(pending_evidence):
    report = {
        "stop_reason": "nothing_pending", "remaining_queue": 0,
        "source_status": {"status": "current_day_prefilter_complete"},
        "awaiting_group_resolution": 0,
        **pending_evidence,
    }
    result = worker.official_job_result(report, state_persisted=True)
    assert result.status is JobStatus.PARTIAL
    assert result.collection_complete is result.successful is False
    assert result.state_persisted is True


def test_resolved_mapping_allows_an_empty_queue_to_complete():
    result = worker.official_job_result({
        "stop_reason": "nothing_pending", "remaining_queue": 0,
        "source_status": {"status": "current_day_prefilter_complete", "parked_group_xml_appids": []},
        "awaiting_group_resolution": 0, "unresolved_candidates": {},
    }, state_persisted=True)
    assert result.status is JobStatus.COMPLETE
    assert result.successful is True


def prepare_main(monkeypatch, tmp_path, *, push_outcomes, one_request):
    candidate = known_group(ordinary_candidate())
    queue = [candidate] if one_request else []
    cp_path = tmp_path / "checkpoint.json"
    master_path = tmp_path / "master.json"
    cp_path.touch()
    master_path.touch()
    monkeypatch.setattr(worker, "CHECKPOINT", cp_path)
    monkeypatch.setattr(worker, "MASTER", master_path)
    cp = checkpoint(queue)
    documents = {
        worker.FROZEN / "source_queue.json": [],
        worker.FROZEN / "source_unresolved.json": [],
        worker.FROZEN / "checkpoint.json": {"official_results": {}},
        worker.ELIGIBLE: {"games": []}, worker.PREFILTER: {},
        worker.OFFICIAL_CACHE: {"games": {}}, worker.ORIGINAL_OFFICIAL: {},
        cp_path: cp, master_path: {"games": []},
    }
    saved = {}
    monkeypatch.setattr(worker, "read", lambda path: deepcopy(saved.get(path, documents.get(path))))
    monkeypatch.setattr(worker, "save", lambda path, data: saved.__setitem__(path, deepcopy(data)))
    monkeypatch.setattr(worker, "clock", lambda: NOW.astimezone(worker.TZ))
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(worker.time, "sleep", Mock())
    push = Mock(side_effect=push_outcomes)
    monkeypatch.setattr(worker, "git_push", push)
    monkeypatch.setattr(worker, "make_queue", lambda *args: (deepcopy(queue), {
        "status": "current_day_prefilter_complete", "twitch_priority_pending": 0,
    }))
    for name in ("reverify_pending_store_dates", "retry_pending_content_dispatches"):
        monkeypatch.setattr(worker, name, Mock(return_value=0))
    monkeypatch.setattr(worker, "verify_store_date_for_result", Mock(return_value=True))
    monkeypatch.setattr(worker, "upsert_qualified_master", Mock())
    monkeypatch.setattr(worker, "dispatch_content_event", Mock(return_value="dispatched"))
    client = SimpleNamespace(headers={}, get=Mock(return_value=xml_response(10, candidate["group_id64"])))
    monkeypatch.setattr(worker.requests, "Session", lambda: client)
    monkeypatch.setattr("sys.argv", ["followers", "--save-every", "1"])
    return saved, push, client


@pytest.mark.parametrize("push_outcomes,one_request,reason,persisted", [
    ([False], False, "final_git_checkpoint_failure", False),
    ([False, True], True, "git_checkpoint_failure", True),
])
def test_main_exits_nonzero_when_checkpoint_push_fails(monkeypatch, tmp_path, push_outcomes, one_request, reason, persisted):
    saved, push, client = prepare_main(
        monkeypatch, tmp_path, push_outcomes=push_outcomes, one_request=one_request,
    )
    with pytest.raises(SystemExit) as raised:
        worker.main()
    assert raised.value.code == 1
    report = saved[worker.OUT / "report.json"]
    assert report["stop_reason"] == reason
    assert report["job_result"]["status"] == "failed"
    assert report["job_result"]["state_persisted"] is persisted
    assert report["job_result"]["successful"] is False
    assert push.call_count == len(push_outcomes)
    assert client.get.call_count == int(one_request)


def test_clean_index_still_retries_a_previously_unpushed_checkpoint(monkeypatch):
    from scripts import export_scheduler_queue_status as exporter

    monkeypatch.setattr(exporter, "export_status", Mock())
    rebase = Mock()
    monkeypatch.setattr(worker, "rebase_checkpoint", rebase)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        # Simulate an earlier failed push whose commit has no staged changes.
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.git_push() is True
    rebase.assert_called_once()
    assert ["git", "push", "origin", "HEAD:main"] in calls
    assert not any(command[:2] == ["git", "commit"] for command in calls)


def test_candidate_pipeline_reports_collection_and_persistence_separately():
    complete = {"phase": "complete", "initial_complete": True}
    local = candidate_job_result(complete)
    assert local.collection_complete is True
    assert local.state_persisted is local.successful is False
    assert local.status is JobStatus.PARTIAL
    durable = candidate_job_result(complete, state_persisted=True)
    assert durable.status is JobStatus.COMPLETE
    assert durable.successful is True
    cooling = candidate_job_result({
        "phase": "followers", "initial_complete": False,
        "last_attempt": {"rate_limit_events": 1},
    }, state_persisted=True)
    assert cooling.status is JobStatus.COOLING_DOWN
    assert cooling.successful is False


def test_publication_receipt_is_immutable_and_failed_retry_cannot_reuse_old_success():
    source = growth_report()
    original = deepcopy(source)
    success = stamp_growth_publication(
        source, NOW, state_persisted=True, published=True,
        target_slot="2026-10-01T17:15:00Z", input_revision="catalog-1",
    )
    assert source == original
    assert success["job_result"]["successful"] is True
    assert success["job_result"]["target_slot"] == "2026-10-01T17:15:00Z"
    assert success["job_result"]["input_revision"] == "catalog-1"
    rollback = stamp_growth_publication(success, NOW, state_persisted=True, published=False)
    assert rollback["job_result"]["status"] == "failed"
    assert rollback["job_result"]["collection_complete"] is True
    assert not growth_collection_complete(rollback, NOW)
    assert success["job_result"]["successful"] is True


def test_reused_and_zero_eligible_growth_coverage_can_complete_after_confirmed_delivery():
    reused = {**growth_report(), "requests": 0, "reused": 1}
    assert stamp_growth_publication(reused, NOW, state_persisted=True, published=True)["job_result"]["successful"]
    empty = {"reason": "completed", "eligible": 0, "measurements": [], "errors": []}
    assert growth_collection_complete(
        stamp_growth_publication(empty, NOW, state_persisted=True, published=True), NOW,
    )
    # Empty eligible counts cannot claim success if a required publish failed.
    assert not stamp_growth_publication(empty, NOW, state_persisted=False, published=False)["job_result"]["successful"]


@pytest.mark.parametrize("reason,status", [("rate_limited", "cooling_down"), ("bounded_run", "partial")])
def test_published_partial_progress_does_not_suppress_the_next_recovery_check(reason, status):
    partial = {**growth_report(), "reason": reason, "eligible": 2}
    result = stamp_growth_publication(partial, NOW, state_persisted=True, published=True)
    assert result["job_result"]["status"] == status
    assert result["job_result"]["successful"] is False
    assert not growth_collection_complete(result, NOW)
