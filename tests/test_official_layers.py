"""Offline behavior through the new official queue/application boundaries."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_core.jobs import JobStatus
from radar_backend.adapters import official_followers as adapter
from radar_backend.application.official_followers import (
    OfficialBatchServices, OfficialPaths, run_official_batch,
)
from radar_backend.domain import official_queue as domain
from radar_backend.jobs import official_followers as job
from radar_backend.state import official_followers as state
from radar_backend.state import official_checkpoint as checkpoint_state
from scripts import steam_official_followers as compatibility

NOW = datetime(2026, 10, 8, 11, tzinfo=timezone.utc)


def test_hourly_and_growth_compatibility_exports_are_single_class_objects():
    assert compatibility.OfficialFollowerClient is adapter.OfficialFollowerClient
    assert compatibility.OfficialFollowerCache is state.OfficialFollowerCache
    assert compatibility.CooldownStore is state.CooldownStore
    assert compatibility.OfficialObservation is domain.OfficialObservation
    assert compatibility.FollowerOutcome is domain.FollowerOutcome


def test_domain_queue_rotates_injected_twitch_admissions_without_io():
    frozen = [
        {"appid": aid, "release_date": "2026-10-09", "name": "Historical"}
        for aid in range(10000, 11317)
    ]
    groups = [
        {"appid": aid, "group_short_id": aid}
        for aid in range(10000, 11358)
    ]
    legacy = {
        "cohort": "steam_fresh_20260922_post_adult_1317_near_release",
        "official_results": {str(row["appid"]): {} for row in frozen},
    }
    candidates = {
        str(aid): {
            "appid": aid, "release_date": "2026-10-09",
            "queue_source": "twitch_steam_discovery",
        }
        for aid in (10, 11, 12)
    }
    checkpoint = {
        "pending_candidates": candidates, "official_results": {},
        "attempt_events": [{
            "appid": 10, "queue_source": "twitch_steam_discovery",
            "when_taipei": NOW.isoformat(), "http": 429,
        }],
    }
    admission = Mock(side_effect=lambda row, *, now: row["appid"] != 12)
    cached = Mock(return_value=None)

    queue, status = domain.make_queue(
        checkpoint, frozen, legacy, groups, {"games": []}, {},
        {"games": {}}, {"verified": {}}, now=NOW,
        is_twitch_queue_candidate=admission, cached_follower=cached,
    )

    assert [row["appid"] for row in queue] == [11, 10]
    assert status["status"] == "no_current_day_prefilter"
    assert status["twitch_priority_pending"] == 2
    assert admission.call_count == 3
    assert cached.call_count == 2
    assert len(legacy["official_results"]) == 1317
    assert checkpoint["official_results"] == {}


@pytest.mark.parametrize("persisted,expected", [
    (True, JobStatus.COMPLETE), (False, JobStatus.FAILED),
])
def test_application_reuses_real_zero_and_requires_durable_ack(tmp_path, persisted, expected):
    """No legacy main/global patch is needed to exercise the batch policy."""
    paths = OfficialPaths(
        frozen=tmp_path / "frozen", eligible=tmp_path / "eligible.json",
        prefilter=tmp_path / "prefilter.json", official_cache=tmp_path / "cache.json",
        original_official=tmp_path / "original.json", checkpoint=tmp_path / "queue.json",
        master=tmp_path / "master.json", output=tmp_path / "out", cohort="test-cohort",
    )
    paths.checkpoint.touch()
    paths.master.touch()
    gid = domain.group_to_gid(123)
    candidate = {
        "appid": 123, "name": "Observed zero", "group_id64": gid,
        "release_date": "2026-10-09", "queue_source": "fresh_daily_prefilter_unresolved",
    }
    checkpoint = {
        "cohort": paths.cohort, "pending_candidates": {"123": candidate},
        "official_results": {}, "attempt_events": [], "rate_limit_count": 0,
        "next_request_after_taipei": None,
        "official_growth_observations": {"123": {
            "appid": 123, "group_id64": gid, "official_followers": 0,
            "official_checked_at_taipei": NOW.isoformat(),
        }},
    }
    inputs = {
        paths.frozen / "source_queue.json": [],
        paths.frozen / "source_unresolved.json": [],
        paths.frozen / "checkpoint.json": {"official_results": {}},
        paths.eligible: {"games": []}, paths.prefilter: {},
        paths.official_cache: {"games": {}}, paths.original_official: {},
        paths.checkpoint: checkpoint, paths.master: {"games": []},
    }
    saved = {}
    session = SimpleNamespace(headers={}, get=Mock(side_effect=AssertionError("No HTTP")))
    legacy_api = Mock(side_effect=AssertionError("A zero observation cannot trigger qualification"))
    services = OfficialBatchServices(
        read=lambda path: deepcopy(saved.get(path, inputs.get(path))),
        save=lambda path, value: saved.__setitem__(path, deepcopy(value)),
        exists=lambda path: path.exists(),
        clock=lambda: NOW, monotonic=lambda: 100.0, sleep=Mock(),
        session_factory=lambda: session,
        make_queue=lambda *args: ([candidate], {"status": "current_day_prefilter_complete"}),
        git_push=Mock(return_value=persisted),
        reverify_pending_store_dates=Mock(return_value=0),
        retry_pending_content_dispatches=Mock(return_value=0),
        verify_store_date_for_result=legacy_api, upsert_qualified_master=legacy_api,
        dispatch_content_event=legacy_api, follower_client_factory=adapter.OfficialFollowerClient,
        follower_cache_factory=state.OfficialFollowerCache, cooldown_factory=state.CooldownStore,
    )
    args = SimpleNamespace(max_requests=250, interval=8.0, max_seconds=3450, save_every=10)

    result = run_official_batch(args, paths=paths, services=services)

    report = saved[paths.output / "report.json"]
    assert result.status is expected
    assert result.collection_complete is True
    assert result.state_persisted is persisted
    assert result.successful is persisted
    assert report["requests_this_run"] == 0
    assert saved[paths.checkpoint]["official_results"]["123"]["official_followers"] == 0
    assert saved[paths.output / "attempts.json"][0]["cache_reused"] is True
    session.get.assert_not_called()
    legacy_api.assert_not_called()


def test_job_rejects_automated_cooldown_override_before_starting_batch(monkeypatch):
    batch = Mock(side_effect=AssertionError("No automated override"))
    monkeypatch.setattr(job, "run_official_batch", batch)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("SCHEDULE_TRIGGER_SOURCE", "cloudflare")
    with pytest.raises(ValueError, match="explicit manual"):
        job.run_job(paths=None, services=None, argv=["--skip-cooldown"])
    batch.assert_not_called()


@pytest.mark.parametrize("entry", [
    ["scripts/experiment_official_daily_catchup_250.py"],
    ["-m", "scripts.experiment_official_daily_catchup_250"],
    ["-m", "radar_backend.jobs.official_followers"],
])
def test_historical_cli_help_keeps_both_entry_forms(entry):
    repository = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, *entry, "--help"], cwd=repository,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--max-requests" in result.stdout
    assert "--skip-cooldown" in result.stdout


def test_canonical_job_composes_real_layers_for_an_offline_complete_queue(monkeypatch, tmp_path):
    """Run the production service factory; replace only the external Git ack."""
    defaults = job.default_paths()
    paths = OfficialPaths(**{
        key: tmp_path / value if isinstance(value, Path) else value
        for key, value in vars(defaults).items()
    })
    frozen = [
        {"appid": aid, "release_date": "2026-10-09", "name": "Historical"}
        for aid in range(10000, 11317)
    ]
    inputs = {
        paths.frozen / "source_queue.json": frozen,
        paths.frozen / "source_unresolved.json": [
            {"appid": aid, "group_short_id": aid} for aid in range(10000, 11358)
        ],
        paths.frozen / "checkpoint.json": {
            "cohort": "steam_fresh_20260922_post_adult_1317_near_release",
            "official_results": {str(row["appid"]): {} for row in frozen},
        },
        paths.eligible: {"games": [], "screened_at": NOW.isoformat()},
        paths.prefilter: {"games": {}, "complete": True, "updated_at": NOW.isoformat()},
        paths.official_cache: {"games": {}}, paths.original_official: {},
    }
    for path, value in inputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    session = SimpleNamespace(headers={}, get=Mock(side_effect=AssertionError("No HTTP")))
    acknowledgements = []

    def acknowledge(**ports):
        assert ports["checkpoint"] == paths.checkpoint
        assert ports["master"] == paths.master
        assert callable(ports["export_status"]) and callable(ports["rebase"])
        acknowledgements.append(True)
        return True

    monkeypatch.setattr(job, "default_paths", lambda: paths)
    monkeypatch.setattr(job, "clock", lambda: NOW)
    monkeypatch.setattr(job.requests, "Session", lambda: session)
    monkeypatch.setattr(checkpoint_state, "git_push", acknowledge)

    result = job.main(argv=[])

    assert result.status is JobStatus.COMPLETE
    assert result.successful is True
    assert result.published is False
    assert acknowledgements == [True]
    report = json.loads((paths.output / "report.json").read_text())
    assert report["requests_this_run"] == 0
    assert report["stop_reason"] == "nothing_pending"
    assert report["source_status"]["status"] == "current_day_prefilter_complete"
    saved = json.loads(paths.checkpoint.read_text())
    assert len(saved["pending_candidates"]) == 1317
    assert saved["official_results"] == {}
    session.get.assert_not_called()
