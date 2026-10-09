"""Real offline Git acknowledgements, races and interruption recovery."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest

from radar_core.publication import PublicationError, PublicationReceipt, SubprocessGitRepository
from radar_backend.publication.official_checkpoint import (
    CHECKPOINT, DASHBOARD, MASTER, PENDING, RECEIPT, OfficialCheckpointPersistence,
    baseline_from_head, capture_pending, merge_official_checkpoint, publish_pending,
    pending_revision,
)
from radar_backend.publication.steam import read_json, write_json
from radar_backend.state.official_merge import MergeConflict, merge_json_three_way, merge_master
from radar_backend.application.official_followers import OfficialBatchServices, OfficialPaths, run_official_batch
from radar_backend.domain.official_queue import FollowerOutcome
from radar_backend.state.official_followers import OfficialFollowerCache, CooldownStore

NOW = datetime(2026, 10, 9, 1, tzinfo=timezone.utc)


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()


def checkpoint():
    return {"version": 1, "cohort": "steam_official_daily_catchup_dynamic_v1",
            "official_results": {}, "pending_candidates": {}, "attempt_events": [],
            "content_dispatches": {}, "rate_limit_count": 0,
            "next_request_after_taipei": None}


def observation(aid=123, count=10, when=None):
    return {"appid": aid, "official_followers": count,
            "official_checked_at_taipei": (when or NOW).isoformat(),
            "group_id64": str(103582791429521408 + aid),
            "official_source": "Steam Community XML memberCount"}


def delivery_repository(tmp_path):
    remote, seed, runner = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "runner"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "clone", str(remote), str(seed))
    git(seed, "config", "user.name", "Offline test")
    git(seed, "config", "user.email", "test@example.invalid")
    write_json(seed / CHECKPOINT, checkpoint())
    write_json(seed / MASTER, {"version": 1, "games": [], "count": 0})
    write_json(seed / DASHBOARD, {"old": "dashboard"})
    (seed / ".gitignore").write_text("output/\n__pycache__/\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "seed")
    git(seed, "push", "origin", "main")
    git(tmp_path, "clone", str(remote), str(runner))
    git(runner, "config", "user.name", "Offline test")
    git(runner, "config", "user.email", "test@example.invalid")
    return remote, seed, runner


def no_dashboard(root, clock):
    pass


def persistence(runner, **kwargs):
    publisher = OfficialCheckpointPersistence(runner, clock=lambda: NOW,
                                              renderer=no_dashboard, **kwargs)
    publisher.begin(read_json(runner / CHECKPOINT), read_json(runner / MASTER))
    return publisher


def save_result(runner, aid=123, count=10):
    state = read_json(runner / CHECKPOINT)
    state["official_results"][str(aid)] = observation(aid, count)
    state["attempt_events"].append({"appid": aid, "status": "ok",
                                    "official_followers": count, "when_taipei": NOW.isoformat()})
    write_json(runner / CHECKPOINT, state)
    return state


def test_clean_index_unpushed_commit_is_replayed_and_acknowledged(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    publisher = persistence(runner)
    expected = save_result(runner)
    git(runner, "add", CHECKPOINT)
    git(runner, "commit", "-m", "earlier push did not reach remote")
    assert git(runner, "status", "--porcelain") == ""
    receipt = publisher.persist()
    assert git(remote, "rev-parse", "main") == receipt.published_revision
    assert json.loads(git(remote, "show", f"main:{CHECKPOINT}")) == expected
    assert read_json(runner / PENDING)["recovery_eligible"] is False
    assert receipt.payload_revision == pending_revision(read_json(runner / PENDING))


def test_push_race_preserves_growth_and_remote_queue_changes_without_recollection(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    calls = []
    class RaceRepository(SubprocessGitRepository):
        def push(self, revision=None):
            calls.append(revision)
            if len(calls) == 1:
                latest = read_json(seed / CHECKPOINT)
                latest["official_growth_observations"] = {"456": observation(456)}
                latest["pending_candidates"]["999"] = {"appid": 999, "remote": "Twitch priority"}
                latest["group_resolution_api_cooldown"] = {"status": "newer remote"}
                write_json(seed / CHECKPOINT, latest)
                write_json(seed / MASTER, {"games": [{"appid": 999, "remote": True}], "count": 1})
                git(seed, "add", ".")
                git(seed, "commit", "-m", "concurrent producer")
                git(seed, "push", "origin", "main")
            return super().push(revision)
    repo = RaceRepository(runner, disposable_checkout=True)
    publisher = persistence(runner, repository=repo)
    save_result(runner)
    receipt = publisher.persist()
    assert receipt.attempts == 2 and len(calls) == 2
    merged = read_json(runner / CHECKPOINT)
    assert merged["official_results"]["123"]["official_followers"] == 10
    assert merged["official_growth_observations"]["456"] == observation(456)
    assert merged["pending_candidates"]["999"]["remote"] == "Twitch priority"
    assert merged["group_resolution_api_cooldown"] == {"status": "newer remote"}
    assert len(merged["attempt_events"]) == 1
    assert publisher.baseline["checkpoint"] == merged
    assert read_json(runner / MASTER)["count"] == 1


def test_rejected_push_keeps_original_recovery_batch_and_removes_old_receipt(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    publisher = persistence(runner)
    publisher.persist()
    expected = save_result(runner)
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(PublicationError):
        publisher.persist()
    assert not (runner / RECEIPT).exists()
    pending = read_json(runner / PENDING)
    assert pending["recovery_eligible"] is True
    assert pending["observed"]["checkpoint"] == expected
    assert read_json(runner / CHECKPOINT) == expected
    hook.unlink()
    receipt = publish_pending(runner, pending, receipt_path=runner / RECEIPT, renderer=no_dashboard)
    assert git(remote, "rev-parse", "main") == receipt.published_revision
    assert read_json(runner / CHECKPOINT) == expected


def test_noop_still_requires_real_push_acknowledgement(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    hook = remote / "hooks/pre-receive"
    # Git may bypass a hook for an up-to-date push, so an executable transport
    # wrapper deliberately rejects even the no-op acknowledgement.
    wrapper = tmp_path / "reject-push.sh"
    wrapper.write_text("#!/bin/sh\nexit 1\n")
    wrapper.chmod(0o755)
    publisher = persistence(runner, repository=SubprocessGitRepository(
        runner, disposable_checkout=True, push_command_prefix=(str(wrapper),)))
    with pytest.raises(PublicationError):
        publisher.persist()
    assert not (runner / RECEIPT).exists()


def test_dashboard_export_failure_preserves_old_snapshot_and_durable_results(tmp_path, capsys):
    remote, seed, runner = delivery_repository(tmp_path)
    def reject(root, clock):
        write_json(root / DASHBOARD, {"half": "rendered"})
        raise ValueError("unavailable source")
    publisher = persistence(runner)
    publisher.renderer = reject
    expected = save_result(runner)
    receipt = publisher.persist()
    assert read_json(runner / DASHBOARD) == {"old": "dashboard"}
    assert read_json(runner / CHECKPOINT) == expected
    assert git(remote, "rev-parse", "main") == receipt.published_revision
    assert "SCHEDULER_QUEUE_STATUS_EXPORT_FAILED" in capsys.readouterr().out


@pytest.mark.parametrize("source", [CHECKPOINT, MASTER])
def test_remote_deletion_of_original_source_cannot_be_recreated_as_ack(tmp_path, source):
    remote, seed, runner = delivery_repository(tmp_path)
    publisher = persistence(runner)
    git(seed, "rm", source)
    git(seed, "commit", "-m", "remote source deliberately removed")
    git(seed, "push", "origin", "main")
    with pytest.raises(ValueError, match="disappeared"):
        publisher.persist()
    assert not (runner / RECEIPT).exists()
    assert read_json(runner / PENDING)["recovery_eligible"] is True
    assert source not in git(remote, "ls-tree", "-r", "--name-only", "main").splitlines()


def test_constructor_and_api_reject_artifact_source_collision_before_changes(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    initial = (runner / CHECKPOINT).read_bytes()
    with pytest.raises(ValueError):
        OfficialCheckpointPersistence(runner, receipt_path=runner / CHECKPOINT)
    batch = capture_pending(runner, baseline_from_head(runner), baseline_from_head(runner), now=NOW)
    with pytest.raises(ValueError):
        publish_pending(runner, batch, receipt_path=runner / CHECKPOINT)
    with pytest.raises(ValueError):
        OfficialCheckpointPersistence(runner, pending_path=runner / RECEIPT)
    assert (runner / CHECKPOINT).read_bytes() == initial


def test_source_atomic_temporary_symlink_is_rejected_before_outside_write(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("outside must survive\n")
    (runner / (CHECKPOINT + ".tmp")).symlink_to(outside)
    with pytest.raises(ValueError, match="symlinks"):
        OfficialCheckpointPersistence(runner)
    assert outside.read_text() == "outside must survive\n"


def test_empty_remote_checkpoint_is_not_treated_as_initial_creation(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    publisher = persistence(runner)
    write_json(seed / CHECKPOINT, {})
    git(seed, "add", CHECKPOINT)
    git(seed, "commit", "-m", "invalid empty source")
    git(seed, "push", "origin", "main")
    with pytest.raises(ValueError, match="cohort"):
        publisher.persist()
    assert not (runner / RECEIPT).exists()


def test_symlink_rejection_never_restores_frozen_state_through_outside_directory(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    publisher = persistence(runner)
    save_result(runner)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "checkpoint.json").write_text("outside must survive\n")
    parent = (runner / CHECKPOINT).parent
    saved = parent.with_name("saved-checkpoint")
    parent.rename(saved)
    parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        publisher.persist()
    assert (outside / "checkpoint.json").read_text() == "outside must survive\n"


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e10000}'])
def test_corrupt_source_is_rejected_before_recovery_or_git_changes(tmp_path, raw):
    remote, seed, runner = delivery_repository(tmp_path)
    publisher = persistence(runner)
    (runner / CHECKPOINT).write_text(raw)
    with pytest.raises(ValueError):
        publisher.persist()
    assert not (runner / PENDING).exists()
    assert (runner / CHECKPOINT).read_text() == raw


def run_final_cli(runner):
    source_root = Path(__file__).resolve().parents[1]
    return subprocess.run([sys.executable, "-B", "-m", "radar_backend.jobs.persist_official_checkpoint"],
        cwd=runner, capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONPATH": str(source_root), "PYTHONDONTWRITEBYTECODE": "1"})


def test_interrupt_after_ack_preserves_observation_eleven_via_final_cli(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    publisher = persistence(runner)
    save_result(runner, 123)
    first = publisher.persist()
    expected = save_result(runner, 456)
    assert read_json(runner / PENDING)["recovery_eligible"] is False
    result = run_final_cli(runner)
    assert result.returncode == 0, result.stderr
    second = PublicationReceipt.from_dict(read_json(runner / RECEIPT))
    assert second.published_revision != first.published_revision
    assert json.loads(git(remote, "show", f"main:{CHECKPOINT}")) == expected
    assert read_json(runner / PENDING)["recovery_eligible"] is False


def test_final_fallback_uses_head_baseline_for_saved_uncommitted_observation(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    expected = save_result(runner)
    assert not (runner / PENDING).exists()
    result = run_final_cli(runner)
    assert result.returncode == 0, result.stderr
    assert json.loads(git(remote, "show", f"main:{CHECKPOINT}")) == expected
    assert read_json(runner / PENDING)["baseline"]["checkpoint"]["official_results"] == {}


def test_changed_acknowledged_batch_cannot_reuse_an_older_receipt(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    publisher = persistence(runner)
    publisher.persist()
    batch = read_json(runner / PENDING)
    batch["observed_at"] = "2026-10-09T10:00:00Z"
    write_json(runner / PENDING, batch)
    result = run_final_cli(runner)
    assert result.returncode != 0
    assert "differs from its frozen publication receipt" in result.stderr


def test_latest_follower_evidence_survives_stale_local_success():
    baseline = checkpoint()
    observed = deepcopy(baseline)
    latest = deepcopy(baseline)
    observed["official_results"]["123"] = observation(123, 10)
    newer = datetime(2026, 10, 9, 2, tzinfo=timezone.utc)
    latest["official_results"]["123"] = observation(123, 6000, newer)
    merged = merge_official_checkpoint(latest, baseline, observed)
    assert merged["official_results"]["123"] == latest["official_results"]["123"]


def test_newer_failure_cooldown_is_not_cleared_by_old_success():
    baseline = checkpoint()
    observed, latest = deepcopy(baseline), deepcopy(baseline)
    observed.update(community_last_success_at=NOW.isoformat(), community_cooldown=None,
                    next_request_after_taipei=None)
    latest.update(rate_limit_count=2, next_request_after_taipei="2026-10-09T04:00:00+00:00",
        community_cooldown={"observed_at": "2026-10-09T02:00:00+00:00",
                            "retry_at": "2026-10-09T04:00:00+00:00"})
    merged = merge_official_checkpoint(latest, baseline, observed)
    assert merged["community_cooldown"] == latest["community_cooldown"]
    assert merged["next_request_after_taipei"] == latest["next_request_after_taipei"]


def test_twitch_withdrawal_and_group_resolution_survive_unchanged_worker_fields():
    baseline = checkpoint()
    baseline["pending_candidates"]["123"] = {"appid": 123, "group_id64": None, "priority": "Twitch"}
    observed, latest = deepcopy(baseline), deepcopy(baseline)
    observed["official_results"]["456"] = observation(456)
    latest["pending_candidates"]["123"] = {"appid": 123, "group_id64": "1234", "normal": True}
    merged = merge_official_checkpoint(latest, baseline, observed)
    assert merged["pending_candidates"] == latest["pending_candidates"]


def test_cohort_switch_cannot_accept_stale_worker_state():
    latest = checkpoint()
    latest["cohort"] = "different-cohort"
    with pytest.raises(ValueError, match="cohort"):
        merge_official_checkpoint(latest, checkpoint(), checkpoint())


def test_master_union_recounts_games_and_preserves_newer_whole_row():
    baseline = {"games": [], "count": 0}
    latest = {"games": [{"appid": 123, "followers": 7000,
        "follower_checked_at": "2026-10-09T02:00:00Z", "admission": "new"}], "count": 1}
    observed = {"games": [{"appid": 123, "followers": 5000,
        "follower_checked_at": NOW.isoformat(), "admission": "old"}, {"appid": 456}], "count": 2}
    result = merge_master(latest, baseline, observed)
    assert result["count"] == 2
    assert next(row for row in result["games"] if row["appid"] == 123) == latest["games"][0]


def test_conflicting_scalar_and_duplicate_master_ids_fail_closed():
    with pytest.raises(MergeConflict):
        merge_json_three_way({"a": 1}, {"a": 0}, {"a": 2})
    with pytest.raises(ValueError):
        merge_master({"games": []}, {"games": []}, {"games": [{"appid": 123}, {"appid": 123}]})


def test_application_freezes_before_mutations_and_counts_own_outcome_after_remote_reload(tmp_path):
    remote, seed, runner = delivery_repository(tmp_path)
    publisher = persistence(runner)
    candidate = {"appid": 123, "group_id64": observation()["group_id64"],
                 "release_date": "2026-10-10", "queue_source": "ordinary"}
    paths = OfficialPaths(frozen=runner / "frozen", eligible=runner / "eligible.json",
        prefilter=runner / "prefilter.json", official_cache=runner / "cache.json",
        original_official=runner / "original.json", checkpoint=runner / CHECKPOINT,
        master=runner / MASTER, output=runner / "output/steam_official_daily_catchup",
        cohort=checkpoint()["cohort"])
    inputs = {paths.frozen / "source_queue.json": [], paths.frozen / "source_unresolved.json": [],
              paths.frozen / "checkpoint.json": {"official_results": {}},
              paths.eligible: {"games": []}, paths.prefilter: {},
              paths.official_cache: {"games": {}}, paths.original_official: {}}
    captured, fetches, pushed = [], [], []
    def begin(cp, master):
        captured.append(deepcopy(cp))
        publisher.begin(cp, master)
    def recheck(cp, master):
        cp["before_requests_mutation"] = True
        return 0
    class Client:
        def __init__(self, **kwargs):
            pass
        def fetch(self, *args, **kwargs):
            fetches.append(True)
            return FollowerOutcome("ok", NOW, followers=1000)
    def push():
        if not pushed:
            latest = read_json(seed / CHECKPOINT)
            newer = datetime(2026, 10, 9, 2, tzinfo=timezone.utc)
            latest["official_results"] = {"123": observation(123, 6000, newer),
                                           "888": observation(888, 7000, newer)}
            write_json(seed / CHECKPOINT, latest)
            git(seed, "add", CHECKPOINT)
            git(seed, "commit", "-m", "another producer already measured more")
            git(seed, "push", "origin", "main")
        publisher.persist()
        pushed.append(True)
        return True
    services = OfficialBatchServices(
        read=lambda path: deepcopy(inputs[path]) if path in inputs else read_json(path),
        save=write_json, exists=lambda path: path.is_file(),
        clock=lambda: NOW, monotonic=lambda: 100, sleep=lambda delay: None,
        session_factory=lambda: SimpleNamespace(headers={}),
        make_queue=lambda *args: ([candidate], {"status": "current_day_prefilter_complete"}),
        git_push=push, reverify_pending_store_dates=recheck,
        retry_pending_content_dispatches=lambda cp: 0,
        verify_store_date_for_result=lambda *args: pytest.fail("No Store HTTP below 5000"),
        upsert_qualified_master=lambda *args: pytest.fail("No master promotion"),
        dispatch_content_event=lambda *args: pytest.fail("No dispatch"),
        follower_client_factory=Client, follower_cache_factory=OfficialFollowerCache,
        cooldown_factory=CooldownStore, begin_persistence=begin)
    result = run_official_batch(SimpleNamespace(max_requests=1, interval=8,
        max_seconds=3450, save_every=1), paths=paths, services=services)
    report = read_json(paths.output / "report.json")
    assert "before_requests_mutation" not in captured[0]
    assert len(fetches) == 1 and len(pushed) == 2
    assert report["official_new_this_run"] == 1
    assert report["new_ge5000_this_run"] == 0
    assert report["remaining_queue"] == 0
    assert read_json(paths.checkpoint)["official_results"]["123"]["official_followers"] == 6000
    assert result.state_persisted is True
