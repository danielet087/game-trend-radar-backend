"""Growth delivery preserves remote work and requires actual Git acknowledgements."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from scripts.external_schedule import growth_collection_complete
from scripts.persist_growth_checkpoint import merge_growth_checkpoint, stamp_report

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = "experiments/steam_official_daily_catchup/checkpoint.json"
GID = "103582791429521531"
NOW = datetime(2026, 10, 2, 2, tzinfo=timezone.utc)


def observation(aid=1, at="2026-10-02T01:00:00Z", followers=5001):
    return {
        "appid": aid, "group_id64": GID, "official_followers": followers,
        "official_checked_at_taipei": at,
        "official_source": "Steam Community XML memberCount",
    }


def report(status="ok", at="2026-10-02T01:00:00Z"):
    return {
        "reason": "completed", "errors": [], "eligible": 1,
        "measurements": [{"appid": 1, "followers": 5001, "at": at}],
        "events": [{"status": status, "observed_at": at}],
    }


def limited(at, retry):
    return {
        "rate_limit_count": 1, "temporary_error_count": 0,
        "next_request_after_taipei": retry,
        "community_cooldown": {"updated_at": at, "observed_at": at, "retry_at": retry},
    }


def successful(at):
    return {
        "rate_limit_count": 0, "temporary_error_count": 0,
        "next_request_after_taipei": None, "community_cooldown": None,
        "community_last_success_at": at,
    }


def test_only_new_observations_are_merged_and_latest_queue_and_unknown_fields_survive():
    baseline = {"queue": [{"appid": 1}], "official_results": {"2": {"old": True}},
                "official_growth_observations": {"1": observation(at="2026-10-01T01:00:00Z")}}
    observed = deepcopy(baseline)
    observed["queue"] = []  # Even an accidental local queue edit is not growth-owned.
    observed["official_growth_observations"]["1"] = observation()
    observed["official_growth_observations"]["3"] = observation(3)
    latest = {**deepcopy(baseline), "queue": [{"appid": 9}], "private_future_field": {"keep": True},
              "official_results": {"2": {"new": True}}}
    latest["official_growth_observations"]["1"] = observation(at="2026-10-02T01:30:00Z", followers=6000)
    original = deepcopy(latest)
    merged = merge_growth_checkpoint(latest, baseline, observed, report())
    assert latest == original
    assert merged["queue"] == [{"appid": 9}]
    assert merged["private_future_field"] == {"keep": True}
    assert merged["official_results"] == {"2": {"new": True}}
    assert merged["official_growth_observations"]["1"]["official_followers"] == 6000
    assert merged["official_growth_observations"]["3"]["official_followers"] == 5001


def test_same_timestamp_keeps_remote_observation_and_removed_remote_rows_are_not_resurrected():
    baseline = {"official_growth_observations": {"2": observation(2)}}
    observed = {"official_growth_observations": {"1": observation(), "2": observation(2)}}
    latest = {"official_growth_observations": {"1": observation(followers=6000)}}
    merged = merge_growth_checkpoint(latest, baseline, observed, report())
    assert set(merged["official_growth_observations"]) == {"1"}
    assert merged["official_growth_observations"]["1"]["official_followers"] == 6000


def test_new_observation_preserves_unknown_remote_metadata():
    old = {**observation(at="2026-10-01T01:00:00Z"), "future_metadata": {"keep": True}}
    latest = {"official_growth_observations": {"1": old}}
    observed = {"official_growth_observations": {"1": observation()}}
    merged = merge_growth_checkpoint(latest, {}, observed, report())
    assert merged["official_growth_observations"]["1"]["official_followers"] == 5001
    assert merged["official_growth_observations"]["1"]["official_checked_at_taipei"] == "2026-10-02T01:00:00Z"
    assert merged["official_growth_observations"]["1"]["future_metadata"] == {"keep": True}


def test_success_clears_the_cooldown_the_collector_actually_read():
    baseline = limited("2026-10-01T23:00:00Z", "2026-10-02T00:00:00Z")
    merged = merge_growth_checkpoint(baseline, baseline, successful("2026-10-02T01:00:00Z"), report())
    assert merged["community_cooldown"] is None
    assert merged["next_request_after_taipei"] is None
    assert merged["rate_limit_count"] == 0


def test_stale_success_cannot_clear_a_newer_remote_429():
    baseline = successful("2026-10-01T23:00:00Z")
    latest = {**baseline, **limited("2026-10-02T01:30:00Z", "2026-10-02T02:00:00Z")}
    merged = merge_growth_checkpoint(latest, baseline, successful("2026-10-02T01:00:00Z"), report())
    assert merged == latest


def test_stale_429_cannot_restore_cooldown_after_newer_remote_success():
    baseline = {}
    latest = successful("2026-10-02T01:30:00Z")
    observed = limited("2026-10-02T01:00:00Z", "2026-10-02T02:00:00Z")
    assert merge_growth_checkpoint(latest, baseline, observed, report("rate_limited")) == latest


def test_new_timeout_retains_the_longest_existing_remote_retry_deadline():
    baseline = {}
    latest = limited("2026-10-02T00:00:00Z", "2026-10-02T03:00:00Z")
    latest["community_cooldown"]["future_field"] = "keep"
    observed = {"temporary_error_count": 1, "next_request_after_taipei": "2026-10-02T01:15:00Z"}
    merged = merge_growth_checkpoint(latest, baseline, observed, report("transport_or_xml_error"))
    assert datetime.fromisoformat(merged["next_request_after_taipei"]) == datetime(2026, 10, 2, 3, tzinfo=timezone.utc)
    assert merged["temporary_error_count"] == 1
    assert merged["community_cooldown"]["future_field"] == "keep"


def test_success_preserves_an_active_remote_failure_without_a_timestamp():
    latest = {"next_request_after_taipei": "2026-10-02T02:00:00Z", "temporary_error_count": 1}
    assert merge_growth_checkpoint(latest, {}, successful("2026-10-02T01:00:00Z"), report()) == latest


def test_stale_success_cannot_clear_a_new_timeout_using_an_older_429_receipt():
    baseline = limited("2026-10-01T23:00:00Z", "2026-10-02T00:00:00Z")
    latest = {**deepcopy(baseline), "next_request_after_taipei": "2026-10-02T02:00:00Z",
              "temporary_error_count": 1}
    assert merge_growth_checkpoint(latest, baseline, successful("2026-10-02T01:00:00Z"), report()) == latest


def test_interrupted_timeout_output_does_not_undo_newer_remote_success():
    latest = successful("2026-10-02T01:30:00Z")
    observed = {"temporary_error_count": 1, "next_request_after_taipei": "2026-10-02T02:00:00Z"}
    assert merge_growth_checkpoint(latest, {}, observed, {"measurements": []}) == latest


@pytest.mark.parametrize("malformed", [
    [], {"official_growth_observations": []}, {"community_cooldown": "broken"},
    {"rate_limit_count": True}, {"next_request_after_taipei": "2026-10-02"},
    {"official_growth_observations": {"1": {**observation(), "group_id64": "1"}}},
    {"official_growth_observations": {"1": {**observation(), "official_followers": False}}},
    {"official_growth_observations": {"1": {**observation(), "official_source": "SteamDB"}}},
])
def test_corrupt_state_never_becomes_an_empty_checkpoint(malformed):
    with pytest.raises(ValueError):
        merge_growth_checkpoint(malformed, {}, {}, report())


def test_missing_or_failed_delivery_never_reuses_a_previous_complete_receipt(tmp_path):
    path = tmp_path / "report.json"
    missing = stamp_report(path, state_persisted=False, published=False, now=NOW)
    assert missing["job_result"]["status"] == "failed"
    assert not growth_collection_complete(missing, NOW)
    path.write_text(json.dumps(report()), encoding="utf-8")
    complete = stamp_report(path, state_persisted=True, published=True, now=NOW)
    assert growth_collection_complete(complete, NOW)
    failed = stamp_report(path, state_persisted=True, published=False, now=NOW)
    assert failed["job_result"]["status"] == "failed"
    assert not growth_collection_complete(failed, NOW)


def workflow_steps():
    workflow = yaml.safe_load((ROOT / ".github/workflows/steam-public-growth.yml").read_text())
    return workflow, workflow["jobs"]["observe"]["steps"]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def delivery_repo(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "runner"
    subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    # The workflow now composes the complete real Python publication adapter.
    # Copy its runtime graph rather than a hand-picked legacy script subset.
    for name in ("scripts", "radar_backend", "collectors"):
        shutil.copytree(ROOT / name, repo / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(ROOT / ".gitignore", repo / ".gitignore")
    baseline = {"queue": [{"appid": 1}], "official_results": {"2": {"retained": True}}}
    write_json(repo / CHECKPOINT, baseline)
    git(repo, "add", ".gitignore", "scripts", "radar_backend", "collectors", CHECKPOINT)
    git(repo, "commit", "-m", "seed")
    git(repo, "push", "origin", "HEAD:main")
    write_json(repo / "output/growth-checkpoint-baseline.json", baseline)
    write_json(repo / "output/public-growth.json", report())
    write_json(repo / CHECKPOINT, {**baseline, "official_growth_observations": {"1": observation()}})
    return remote, repo, baseline


def run_checkpoint_step(repo):
    _, steps = workflow_steps()
    script = next(step["run"] for step in steps if step.get("id") == "checkpoint")
    output = repo / "github-output.txt"
    env = {**os.environ, "GITHUB_OUTPUT": str(output),
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, text=True, capture_output=True)
    return result, output.read_text() if output.exists() else ""


def test_real_git_merge_persists_observations_without_overwriting_new_remote_queue(tmp_path):
    remote, repo, baseline = delivery_repo(tmp_path)
    writer = tmp_path / "other-writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.com")
    write_json(writer / CHECKPOINT, {**baseline, "queue": [{"appid": 9}], "future_field": "preserved"})
    git(writer, "add", CHECKPOINT)
    git(writer, "commit", "-m", "advance queue")
    git(writer, "push", "origin", "HEAD:main")
    result, acknowledgement = run_checkpoint_step(repo)
    assert result.returncode == 0, result.stderr
    assert acknowledgement == "state_persisted=true\n"
    persisted = json.loads(git(remote, "show", f"main:{CHECKPOINT}"))
    assert persisted["queue"] == [{"appid": 9}]
    assert persisted["future_field"] == "preserved"
    assert persisted["official_growth_observations"]["1"] == observation()


def test_rejected_real_git_push_produces_no_durable_acknowledgement(tmp_path):
    remote, repo, _ = delivery_repo(tmp_path)
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    result, acknowledgement = run_checkpoint_step(repo)
    assert result.returncode != 0
    assert acknowledgement == ""
    persisted = json.loads(git(remote, "show", f"main:{CHECKPOINT}"))
    assert "official_growth_observations" not in persisted


def frontend_repo(tmp_path, backend):
    remote = tmp_path / "frontend.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
    frontend = backend / "frontend"
    subprocess.run(["git", "clone", str(remote), str(frontend)], check=True, capture_output=True)
    git(frontend, "config", "user.name", "Test")
    git(frontend, "config", "user.email", "test@example.com")
    (frontend / "scripts").mkdir()
    # A local publisher fixture: the measurement source controls the published
    # payload, allowing real commits/pushes without querying any external API.
    (frontend / "scripts/build_radar_insights.py").write_text(
        "import argparse, json\nfrom pathlib import Path\n"
        "p=argparse.ArgumentParser();p.add_argument('--data-dir');p.add_argument('--measurements');p.add_argument('--observed-at');a=p.parse_args()\n"
        "m=json.loads(Path(a.measurements).read_text())\n"
        "for n in ('insights-state.json','activity.json','growth.json'):\n"
        "    Path(a.data_dir,n).write_text(json.dumps(m['measurements']))\n", encoding="utf-8",
    )
    for name in ("insights-state.json", "activity.json", "growth.json"):
        write_json(frontend / "data" / name, [])
    git(frontend, "add", "scripts", "data")
    git(frontend, "commit", "-m", "seed public catalog")
    git(frontend, "push", "origin", "HEAD:main")
    # The production auth wrapper is replaced only in this local fixture.
    auth = tmp_path / "local-git-auth.sh"
    auth.write_text('exec git "$@"\n', encoding="utf-8")
    return remote, frontend


def run_publish_step(repo):
    _, steps = workflow_steps()
    script = next(step["run"] for step in steps if step.get("id") == "publish")
    # Expressions are expanded by Actions in production; local integration
    # uses a literal immutable input revision and an external token-free wrapper.
    script = script.replace("${{ steps.catalog.outputs.revision }}", "local-catalog-input")
    script = script.rstrip() + f' --auth-script "{repo.parent / "local-git-auth.sh"}"\n'
    output = repo / "publish-output.txt"
    env = {**os.environ, "GITHUB_OUTPUT": str(output), "FRONTEND_REPO_TOKEN": "local-test",
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, text=True, capture_output=True)
    return result, output.read_text() if output.exists() else ""


def test_frontend_publication_requires_a_real_push_and_acknowledges_a_verified_no_diff_retry(tmp_path):
    _, backend, _ = delivery_repo(tmp_path)
    remote, frontend = frontend_repo(tmp_path, backend)
    result, acknowledgement = run_publish_step(backend)
    assert result.returncode == 0, result.stderr
    assert acknowledgement == "published=true\n"
    assert json.loads(git(remote, "show", "main:data/growth.json")) == report()["measurements"]
    prior_head = git(remote, "rev-parse", "main")
    (backend / "publish-output.txt").unlink()
    result, acknowledgement = run_publish_step(backend)
    assert result.returncode == 0, result.stderr
    assert acknowledgement == "published=true\n"
    assert git(remote, "rev-parse", "main") == prior_head
    assert git(frontend, "rev-parse", "HEAD") == prior_head


def test_frontend_rejected_push_does_not_emit_publication_acknowledgement(tmp_path):
    _, backend, _ = delivery_repo(tmp_path)
    remote, _ = frontend_repo(tmp_path, backend)
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    result, acknowledgement = run_publish_step(backend)
    assert result.returncode != 0
    assert acknowledgement == ""
    assert json.loads(git(remote, "show", "main:data/growth.json")) == []


def test_workflow_guards_use_installed_core_and_completion_follows_durable_receipts():
    workflow, steps = workflow_steps()
    assert workflow["permissions"]["contents"] == "write"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    names = [step.get("name", "") for step in steps]
    assert names.index("Install collection and shared contract dependencies") < names.index("Validate external growth slot before collecting measurements")
    ids = {step.get("id"): index for index, step in enumerate(steps) if step.get("id")}
    assert ids["collect"] < ids["checkpoint"] < ids["publish"]
    assert names.index("Record durable state and publication acknowledgements") < names.index("Require complete growth coverage")
    assert "steps.checkpoint.outputs.state_persisted == 'true'" in steps[ids["publish"]]["if"]
    stamp = next(step for step in steps if step.get("name") == "Record durable state and publication acknowledgements")
    assert stamp["if"] == "always() && !cancelled()"
    assert "steps.checkpoint.outputs.state_persisted" in stamp["env"]["STATE_PERSISTED"]
    assert "steps.publish.outputs.published" in stamp["env"]["PUBLISHED"]
