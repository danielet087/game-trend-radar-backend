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
from radar_backend.domain.growth import validated_group_resolution
from radar_backend.publication.growth import growth_checkpoint_payload, growth_publication_payload, stamp_growth_publication
from radar_backend.publication.checkpoint_delivery import publish_growth_checkpoint
from radar_backend.publication.steam import publish_growth
from radar_backend.state.official_followers import GROWTH_GROUPS, OfficialFollowerCache
from radar_core.publication import PublicationError, SubprocessGitRepository

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


def resolution(status="not_found", at="2026-10-02T01:00:00Z"):
    receipt = {"status": status, "checked_at": at,
               "source": "Steam ISteamUser/ResolveVanityURL", "attempted": True}
    if status in {"not_found", "resolved"}:
        receipt.update(http=200, api_result=42 if status == "not_found" else 1)
    if status != "resolved":
        receipt["retry_at"] = "2026-10-03T01:00:00Z"
    return receipt


def resolution_row(aid=1, status="not_found", at="2026-10-02T01:00:00Z"):
    row = {"appid": aid, "group_resolution": resolution(status, at)}
    if status == "resolved":
        row["group_id64"] = GID
    return row


def availability_report():
    return {"coverage_schema_version": 2, "generated_at": NOW.isoformat(),
            "collection_date_taipei": "2026-10-02", "eligible_appids": [1],
            "eligible": 1, "measurements": [], "pending": [], "errors": [],
            "events": [], "reason": "completed", "collection_complete": True,
            "measurement_complete": False, "unavailable": [
                {"appid": 1, "reason": "official_group_not_found", "group_resolution": resolution()}]}


def test_identity_receipts_persist_separately_without_erasing_official_history():
    official = observation(at="2026-10-01T01:00:00Z", followers=9500)
    baseline = {"official_growth_observations": {"2": {**official, "appid": 2}}}
    cache = OfficialFollowerCache(deepcopy(baseline))
    cache.remember_group_resolution(1, resolution_row(status="network_error"))
    assert cache.group_resolution(1)["status"] == "network_error"
    cache.remember_group_resolution(1, resolution_row(at="2026-10-02T01:30:00Z"))
    merged = merge_growth_checkpoint(baseline, baseline, cache.checkpoint, availability_report())
    assert merged[GROWTH_GROUPS]["1"]["group_resolution"]["status"] == "not_found"
    assert merged["official_growth_observations"] == baseline["official_growth_observations"]
    assert "followers" not in merged[GROWTH_GROUPS]["1"]
    assert "official_followers" not in merged[GROWTH_GROUPS]["1"]
    assert OfficialFollowerCache(merged).group_id(1) is None


@pytest.mark.parametrize("field", ["pending_candidates", "unresolved_candidates", "official_growth_observations"])
def test_stale_missing_group_cannot_overwrite_a_remotely_resolved_group(field):
    observed = {GROWTH_GROUPS: {"1": resolution_row()}}
    live = resolution_row(status="resolved", at="2026-10-02T01:30:00Z")
    if field == "official_growth_observations":
        live = observation(at="2026-10-02T01:30:00Z", followers=9600)
    latest = {field: {"1": live}, "queue": [{"appid": 99}]}
    merged = merge_growth_checkpoint(latest, {}, observed, availability_report())
    assert merged == latest
    assert OfficialFollowerCache(merged).group_id(1) == GID


def test_canonical_resolution_merge_keeps_newest_remote_evidence_and_unknown_metadata():
    latest = {GROWTH_GROUPS: {"1": {**resolution_row(at="2026-10-02T01:30:00Z"), "future_field": "keep"}}}
    observed = {GROWTH_GROUPS: {"1": resolution_row(status="resolved")}}
    assert merge_growth_checkpoint(latest, {}, observed, report()) == latest
    observed[GROWTH_GROUPS]["1"] = resolution_row(status="resolved", at="2026-10-02T01:45:00Z")
    merged = merge_growth_checkpoint(latest, {}, observed, report())
    assert merged[GROWTH_GROUPS]["1"]["group_id64"] == GID
    assert merged[GROWTH_GROUPS]["1"]["future_field"] == "keep"


def test_known_official_group_is_not_replaced_by_newer_missing_mapping():
    baseline = {"official_results": {"1": observation(at="2026-10-01T01:00:00Z")}}
    cache = OfficialFollowerCache(deepcopy(baseline))
    cache.remember_group_resolution(1, resolution_row())
    assert cache.checkpoint == baseline


def test_missing_group_receipt_with_a_conflicting_group_id_is_rejected():
    with pytest.raises(ValueError, match="Unverified growth group resolution"):
        OfficialFollowerCache({}).remember_group_resolution(1, {**resolution_row(), "group_id64": GID})


@pytest.mark.parametrize("bad", [
    {"status": "not_found", "checked_at": "2026-10-02T01:00:00Z", "source": "Steam ISteamUser/ResolveVanityURL"},
    {**resolution(), "api_result": 1}, {**resolution(), "attempted": False},
    {**resolution(), "http": 429}, {**resolution(), "source": "SteamDB"},
])
def test_unverified_identity_failure_cannot_be_persisted_as_unavailable(bad):
    assert validated_group_resolution(bad) is None
    with pytest.raises(ValueError):
        OfficialFollowerCache({}).remember_group_resolution(1, bad)
    with pytest.raises(ValueError):
        merge_growth_checkpoint({}, {}, {GROWTH_GROUPS: {"1": {"appid": 1, "group_resolution": bad}}}, report())


def api_cooldown(status, at, retry=None):
    return {"status": status, "observed_at": at, "retry_at": retry,
            "source": "Steam ISteamUser/ResolveVanityURL", "attempts": 0 if status == "available" else 1}


def test_new_group_api_success_persists_and_stale_failures_cannot_restore_cooldown():
    latest = {"group_resolution_api_cooldown": api_cooldown("available", "2026-10-02T01:30:00Z")}
    stale = {"group_resolution_api_cooldown": api_cooldown("api_rate_limited", "2026-10-02T01:00:00Z", "2026-10-02T02:00:00Z")}
    assert merge_growth_checkpoint(latest, {}, stale, report()) == latest
    assert merge_growth_checkpoint(stale, stale, latest, report())["group_resolution_api_cooldown"] == latest["group_resolution_api_cooldown"]
    assert merge_growth_checkpoint(stale, {}, {"group_resolution_api_cooldown": api_cooldown("available", "2026-10-02T00:30:00Z")}, report()) == stale


@pytest.mark.parametrize("bad", [
    api_cooldown("available", "2026-10-02T01:00:00Z", "2026-10-02T02:00:00Z"),
    api_cooldown("api_rate_limited", "2026-10-02T01:00:00Z", "2026-10-02T00:00:00Z"),
    {**api_cooldown("available", "2026-10-02T01:00:00Z"), "attempts": True},
])
def test_invalid_group_api_cooldown_does_not_corrupt_checkpoint(bad):
    with pytest.raises(ValueError):
        merge_growth_checkpoint({}, {}, {"group_resolution_api_cooldown": bad}, report())


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
    write_json(frontend / "data/catalog.json", {"count": 1, "games": [
        {"appid": 1, "followers": 6000, "release_start": "2026-10-02"}]})
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


def test_latest_catalog_cohort_is_verified_even_when_count_did_not_change():
    original = availability_report()
    actual = {"count": 1, "games": [{"appid": 2, "followers": 6000, "release_start": "2026-10-02"}]}
    adjusted = growth_publication_payload(original, actual, NOW)
    assert original["collection_complete"] is True
    assert adjusted["published_eligible_appids"] == [2]
    assert adjusted["eligible_appids"] == [1]
    assert adjusted["reason"] == "accepted_catalog_changed"
    assert adjusted["measurements"] == []
    assert not growth_collection_complete(adjusted, NOW)


@pytest.mark.parametrize("new_evidence", [
    {"group_id64": GID},
    {"followers": 9500, "follower_checked_at": "2026-10-02T01:45:00Z"},
])
def test_latest_public_resolution_invalidates_a_frozen_unavailability_outcome(new_evidence):
    actual = {"count": 1, "games": [
        {"appid": 1, "followers": 6000, "release_start": "2026-10-02", **new_evidence}]}
    adjusted = growth_publication_payload(availability_report(), actual, NOW)
    assert adjusted["published_resolvable_appids"] == [1]
    assert adjusted["reason"] == "official_group_now_resolved"
    assert not adjusted["publication_coverage_complete"]
    assert adjusted["measurements"] == []


@pytest.mark.parametrize("change", ["accepted", "resolved"])
def test_real_growth_publication_retry_binds_coverage_to_the_acknowledged_catalog(tmp_path, change):
    _, backend, _ = delivery_repo(tmp_path)
    remote, frontend = frontend_repo(tmp_path, backend)
    writer = tmp_path / "catalog-writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.com")

    class CatalogRace(SubprocessGitRepository):
        raced = False
        def _run(self, *args, push=False):
            if push and not self.raced:
                self.raced = True
                catalog = json.loads((writer / "data/catalog.json").read_text())
                if change == "accepted":
                    catalog["games"].append({"appid": 2, "followers": 7000, "release_start": "2026-10-02"})
                    catalog["count"] = 2
                else:
                    catalog["games"][0]["group_id64"] = GID
                write_json(writer / "data/catalog.json", catalog)
                git(writer, "add", "data/catalog.json")
                git(writer, "commit", "-m", "catalog changed while publishing measurements")
                git(writer, "push", "origin", "HEAD:main")
            return super()._run(*args, push=push)

    frozen = availability_report()
    delivered = {}
    receipt = publish_growth(frontend, frozen, input_revision="original-catalog",
                             repository=CatalogRace(frontend, disposable_checkout=True),
                             now=NOW, published_report=delivered)
    assert receipt.attempts == 2
    assert receipt.published_revision == git(remote, "rev-parse", "main")
    assert delivered["published_eligible_appids"] == ([1, 2] if change == "accepted" else [1])
    assert delivered["reason"] == ("accepted_catalog_changed" if change == "accepted" else "official_group_now_resolved")
    assert frozen == availability_report()
    assert delivered["measurements"] == []
    assert json.loads(git(remote, "show", "main:data/growth.json")) == []
    assert not growth_collection_complete(delivered, NOW)


def test_rejected_growth_push_does_not_acknowledge_rendered_coverage(tmp_path):
    _, backend, _ = delivery_repo(tmp_path)
    remote, frontend = frontend_repo(tmp_path, backend)
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    delivered = {"existing": "untouched"}
    with pytest.raises(PublicationError):
        publish_growth(frontend, report(), input_revision="original-catalog", now=NOW,
                       max_attempts=1, published_report=delivered)
    assert delivered == {"existing": "untouched"}


def test_new_remote_checkpoint_resolution_prevents_stale_unavailable_daily_success(tmp_path):
    remote, backend, baseline = delivery_repo(tmp_path)
    _, frontend = frontend_repo(tmp_path, backend)
    writer = tmp_path / "resolution-writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.com")
    observed = {**baseline, GROWTH_GROUPS: {"1": resolution_row()}}
    write_json(backend / CHECKPOINT, observed)

    class ResolutionRace(SubprocessGitRepository):
        raced = False
        def _run(self, *args, push=False):
            if push and not self.raced:
                self.raced = True
                live = {**baseline, GROWTH_GROUPS: {
                    "1": resolution_row(status="resolved", at="2026-10-02T01:30:00Z")}}
                write_json(writer / CHECKPOINT, live)
                git(writer, "add", CHECKPOINT)
                git(writer, "commit", "-m", "official group resolved concurrently")
                git(writer, "push", "origin", "HEAD:main")
            return super()._run(*args, push=push)

    persisted = {}
    state_receipt = publish_growth_checkpoint(
        backend, baseline, observed, availability_report(), input_revision="original-backend",
        repository=ResolutionRace(backend, disposable_checkout=True), persisted_report=persisted, now=NOW)
    assert state_receipt.attempts == 2
    committed = json.loads(git(remote, "show", f"main:{CHECKPOINT}"))
    assert committed[GROWTH_GROUPS]["1"]["group_id64"] == GID
    assert persisted["checkpoint_resolvable_appids"] == [1]
    assert persisted["checkpoint_group_resolutions"]["1"]["status"] == "resolved"
    assert persisted["reason"] == "official_group_now_resolved"
    delivered = {}
    publication = publish_growth(frontend, persisted, input_revision="old-public-catalog",
                                 now=NOW, published_report=delivered)
    assert publication.published_revision
    assert delivered["published_resolvable_appids"] == []  # Catalog has not caught up yet.
    stamped = stamp_growth_publication(delivered, NOW, state_persisted=True, published=True)
    assert stamped["job_result"]["state_persisted"] and stamped["job_result"]["published"]
    assert stamped["job_result"]["status"] == "partial"
    assert not growth_collection_complete(stamped, NOW)
    assert stamped["measurements"] == []


def test_durable_unavailable_receipt_completes_delivery_without_fabricating_measurements(tmp_path):
    remote, backend, baseline = delivery_repo(tmp_path)
    public_remote, frontend = frontend_repo(tmp_path, backend)
    observed = {**baseline, GROWTH_GROUPS: {"1": resolution_row()}}
    write_json(backend / CHECKPOINT, observed)
    persisted = {}
    publish_growth_checkpoint(backend, baseline, observed, availability_report(),
                              input_revision="official-result", persisted_report=persisted, now=NOW)
    assert persisted["checkpoint_group_resolutions"]["1"] == resolution()
    delivered = {}
    publish_growth(frontend, persisted, input_revision="public-catalog", now=NOW, published_report=delivered)
    stamped = stamp_growth_publication(delivered, NOW, state_persisted=True, published=True)
    assert stamped["job_result"]["status"] == "complete"
    assert growth_collection_complete(stamped, NOW)
    assert stamped["measurement_complete"] is False
    assert stamped["measurements"] == []
    assert json.loads(git(public_remote, "show", "main:data/growth.json")) == []
    committed = json.loads(git(remote, "show", f"main:{CHECKPOINT}"))
    assert "official_growth_observations" not in committed
    assert "official_followers" not in committed[GROWTH_GROUPS]["1"]


def test_new_checkpoint_retryable_receipt_does_not_reuse_an_old_unavailability_outcome():
    merged = {GROWTH_GROUPS: {"1": resolution_row(status="network_error", at="2026-10-02T01:30:00Z")}}
    current = growth_checkpoint_payload(availability_report(), merged, now=NOW)
    assert current["checkpoint_resolvable_appids"] == []
    assert current["checkpoint_group_resolutions"]["1"]["status"] == "network_error"
    assert current["reason"] == "official_group_resolution_changed"
    assert not growth_collection_complete(current, NOW)
    assert current["coverage"] == {"eligible": 1, "measured": 0, "unavailable": 0, "pending": 1}
    assert current["unavailable"] == []
    assert current["superseded_unavailable"][0]["group_resolution"] == resolution()


@pytest.mark.parametrize("current_receipt", [
    None,
    resolution(at="2026-10-02T00:30:00Z"),
    resolution(at="2026-10-02T03:00:00Z"),
    {**resolution(at="2026-10-02T01:30:00Z"), "retry_at": "2026-10-02T01:45:00Z"},
])
def test_checkpoint_acknowledgement_does_not_claim_complete_with_missing_or_stale_proof(current_receipt):
    merged = {} if current_receipt is None else {GROWTH_GROUPS: {
        "1": {"appid": 1, "group_resolution": current_receipt}}}
    current = growth_checkpoint_payload(availability_report(), merged, now=NOW)
    assert current["collection_complete"] is False
    assert current["reason"] == "official_group_resolution_changed"
    assert current["pending"] == [{"appid": 1, "reason": "official_group_resolution_changed"}]
    assert current["measurements"] == []
    assert current["unavailable"] == []
    assert current["superseded_unavailable"][0]["appid"] == 1


def test_newer_valid_checkpoint_unavailability_can_still_complete_daily_coverage():
    merged = {GROWTH_GROUPS: {"1": resolution_row(at="2026-10-02T01:30:00Z")}}
    current = growth_checkpoint_payload(availability_report(), merged, now=NOW)
    assert current["reason"] == "completed"
    assert growth_collection_complete(current, NOW)
    assert current["measurement_complete"] is False
    assert current["coverage"] == {"eligible": 1, "measured": 0, "unavailable": 1, "pending": 0}


def test_public_coverage_clock_accepts_newer_durable_receipt_without_redating_collection():
    delivery = datetime(2026, 10, 2, 1, 40, tzinfo=timezone.utc)
    original = availability_report()
    original["generated_at"] = "2026-10-02T01:20:00+00:00"
    original.update(eligible=2, eligible_appids=[1, 2], measurements=[{
        "appid": 2, "followers": 7600, "at": "2026-10-02T01:10:00Z", "source": "steam_community"}])
    checkpoint = {GROWTH_GROUPS: {"1": resolution_row(at="2026-10-02T01:30:00Z")},
                  "official_growth_observations": {"2": observation(2, at="2026-10-02T01:10:00Z", followers=7600)}}
    state_report = growth_checkpoint_payload(original, checkpoint, now=delivery)
    catalog = {"count": 2, "games": [
        {"appid": aid, "followers": 6000, "release_start": "2026-10-02"} for aid in (1, 2)]}
    published = growth_publication_payload(state_report, catalog, delivery)
    assert published["coverage_checked_at"] == delivery.isoformat()
    assert published["generated_at"] == original["generated_at"]
    assert published["measurements"] == original["measurements"]
    assert published["unavailable"] == original["unavailable"]
    assert published["checkpoint_group_resolutions"]["1"]["checked_at"] == "2026-10-02T01:30:00Z"
    assert published["coverage"] == {"eligible": 2, "measured": 1, "unavailable": 1, "pending": 0}
    assert published["collection_complete"] and not published["measurement_complete"]
    stamped = stamp_growth_publication(published, delivery, state_persisted=True, published=True)
    assert growth_collection_complete(stamped, delivery)


@pytest.mark.parametrize("bad_clock", [
    None, "broken", "2026-10-02T01:40:00", "2026-10-02T01:00:00Z",
    "2026-10-02T01:41:00Z", "2026-10-01T15:59:00Z",
])
def test_invalid_or_future_public_coverage_clock_cannot_complete_daily_tracking(bad_clock):
    delivery = datetime(2026, 10, 2, 1, 40, tzinfo=timezone.utc)
    original = availability_report()
    original["generated_at"] = "2026-10-02T01:20:00+00:00"
    original["coverage_checked_at"] = bad_clock
    assert not growth_collection_complete(original, delivery)


def test_real_publisher_passes_coverage_clock_while_freezing_measurement_and_event_clock(tmp_path):
    _, backend, _ = delivery_repo(tmp_path)
    remote, frontend = frontend_repo(tmp_path, backend)
    # The subprocess fixture captures the exact frozen observation clock and
    # payload received by the same frontend CLI used in production.
    (frontend / "scripts/build_radar_insights.py").write_text(
        "import argparse,json\nfrom pathlib import Path\n"
        "p=argparse.ArgumentParser();p.add_argument('--data-dir');p.add_argument('--measurements');p.add_argument('--observed-at');a=p.parse_args()\n"
        "m=json.loads(Path(a.measurements).read_text());m['builder_observed_at']=a.observed_at\n"
        "for n in ('insights-state.json','activity.json','growth.json'):\n"
        "    Path(a.data_dir,n).write_text(json.dumps(m))\n", encoding="utf-8")
    git(frontend, "add", "scripts/build_radar_insights.py")
    git(frontend, "commit", "-m", "capture independent coverage evaluation clock")
    git(frontend, "push", "origin", "HEAD:main")
    generated = "2026-10-02T01:20:00+00:00"
    delivery = datetime(2026, 10, 2, 1, 40, tzinfo=timezone.utc)
    original = availability_report()
    original["generated_at"] = generated
    checkpoint = {GROWTH_GROUPS: {"1": resolution_row(at="2026-10-02T01:30:00Z")}}
    state_report = growth_checkpoint_payload(original, checkpoint, now=delivery)
    delivered = {}
    publish_growth(frontend, state_report, input_revision="frozen-collector-catalog",
                   now=delivery, published_report=delivered)
    published = json.loads(git(remote, "show", "main:data/growth.json"))
    assert published["builder_observed_at"] == generated
    assert published["generated_at"] == generated
    assert published["coverage_checked_at"] == delivery.isoformat()
    assert delivered["coverage_checked_at"] == delivery.isoformat()
    assert published["checkpoint_group_resolutions"]["1"]["checked_at"] == "2026-10-02T01:30:00Z"
    assert published["measurement_complete"] is False
    assert published["measurements"] == []
    stamped = stamp_growth_publication(delivered, delivery, state_persisted=True, published=True)
    assert growth_collection_complete(stamped, delivery)


def test_growth_push_retry_rechecks_receipt_expiry_at_actual_attempt_clock(tmp_path, monkeypatch):
    import radar_backend.publication.steam as publisher
    _, backend, _ = delivery_repo(tmp_path)
    remote, frontend = frontend_repo(tmp_path, backend)
    (frontend / "scripts/build_radar_insights.py").write_text(
        "import argparse,json\nfrom pathlib import Path\n"
        "p=argparse.ArgumentParser();p.add_argument('--data-dir');p.add_argument('--measurements');p.add_argument('--observed-at');a=p.parse_args()\n"
        "m=json.loads(Path(a.measurements).read_text());m['builder_observed_at']=a.observed_at\n"
        "for n in ('insights-state.json','activity.json','growth.json'):\n"
        "    Path(a.data_dir,n).write_text(json.dumps(m))\n", encoding="utf-8")
    git(frontend, "add", "scripts/build_radar_insights.py")
    git(frontend, "commit", "-m", "capture retried coverage evaluation")
    git(frontend, "push", "origin", "HEAD:main")
    writer = tmp_path / "coverage-clock-racer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.com")
    first = datetime(2026, 10, 2, 1, 40, tzinfo=timezone.utc)
    second = datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc)
    clocks = []

    class ProgressingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = first if not clocks else second
            clocks.append(value)
            return value

    monkeypatch.setattr(publisher, "datetime", ProgressingClock)
    renders = []

    class CoverageClockRace(SubprocessGitRepository):
        raced = False
        def _run(self, *args, push=False):
            if push:
                renders.append(json.loads((frontend / "data/growth.json").read_text()))
                if not self.raced:
                    self.raced = True
                    write_json(writer / "data/unrelated.json", {"newer": True})
                    git(writer, "add", "data/unrelated.json")
                    git(writer, "commit", "-m", "concurrent public metadata advance")
                    git(writer, "push", "origin", "HEAD:main")
            return super()._run(*args, push=push)

    original = availability_report()
    original["generated_at"] = "2026-10-02T01:20:00+00:00"
    latest = {**resolution(at="2026-10-02T01:30:00Z"), "retry_at": "2026-10-02T01:45:00Z"}
    checkpoint = {GROWTH_GROUPS: {"1": {"appid": 1, "group_resolution": latest}}}
    state_report = growth_checkpoint_payload(original, checkpoint, now=first)
    delivered = {}
    receipt = publish_growth(frontend, state_report, input_revision="original-frozen-catalog",
                             repository=CoverageClockRace(frontend, disposable_checkout=True),
                             published_report=delivered)
    assert receipt.attempts == 2
    assert clocks == [first, second]
    assert [render["coverage_checked_at"] for render in renders] == [first.isoformat(), second.isoformat()]
    assert [render["builder_observed_at"] for render in renders] == [original["generated_at"]] * 2
    assert renders[0]["collection_complete"] is True
    assert renders[1]["collection_complete"] is False
    assert delivered["coverage_checked_at"] == second.isoformat()
    assert delivered["generated_at"] == original["generated_at"]
    assert delivered["coverage"] == {"eligible": 1, "measured": 0, "unavailable": 0, "pending": 1}
    assert delivered["unavailable"] == [] and delivered["measurements"] == []
    stamped = stamp_growth_publication(delivered, second, state_persisted=True, published=True)
    assert not growth_collection_complete(stamped, second)


def test_availability_expiring_between_checkpoint_and_publication_clears_public_complete_flag():
    start = datetime(2026, 10, 2, 1, 40, tzinfo=timezone.utc)
    original = availability_report()
    original["generated_at"] = "2026-10-02T01:35:00Z"
    latest = {**resolution(at="2026-10-02T01:30:00Z"), "retry_at": "2026-10-02T01:45:00Z"}
    committed = {GROWTH_GROUPS: {"1": {"appid": 1, "group_resolution": latest}}}
    state_report = growth_checkpoint_payload(original, committed, now=start)
    assert growth_collection_complete(state_report, start)
    catalog = {"count": 1, "games": [{"appid": 1, "followers": 6000, "release_start": "2026-10-02"}]}
    public_report = growth_publication_payload(state_report, catalog, NOW)
    assert public_report["publication_coverage_complete"] is False
    assert public_report["collection_complete"] is False
    assert public_report["reason"] == "official_group_resolution_changed"
    assert public_report["coverage"] == {"eligible": 1, "measured": 0, "unavailable": 0, "pending": 1}
    assert public_report["unavailable"] == []
    assert public_report["superseded_unavailable"][0]["group_resolution"] == resolution()


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
