"""Daily private publication uses disposable local Git and no Steam requests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest

from radar_core.publication import PublicationError, PublicationScopeError, SubprocessGitRepository, snapshot_revision
from radar_backend.jobs.persist_daily_state import main
from radar_backend.publication.daily_state import (
    CACHE, CATALOG, DASHBOARD, ELIGIBLE, MASTER, PREFILTER, STATE,
    capture_daily_baseline, freeze_daily_state, publish_daily_reset, publish_daily_state,
)
from radar_backend.publication.steam import read_json, write_json
from radar_backend.state.official_merge import MergeConflict
from scripts.external_schedule import daily_slot

CLOCK = datetime(2026, 10, 8, 15, 59, 59, tzinfo=timezone.utc)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def seed(tmp_path, *, dashboard_failure=False):
    remote, root = tmp_path / "private.git", tmp_path / "backend"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(root)], check=True, capture_output=True)
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    values = {
        STATE: {"version": 1, "mode": "two_phase_steam_year", "anchor_date": "2026-10-07",
                "phase": "discovery", "days_scanned": 2},
        CATALOG: {"games": [{"appid": 10, "name": "Known"}], "count": 1},
        PREFILTER: {"version": 1, "games": {}, "next_index": 0, "complete": False},
        ELIGIBLE: {"version": 1, "games": [], "count": 0},
        MASTER: {"version": 2, "games": [{"appid": 10, "name": "Known", "followers": 5000}]},
        CACHE: {"games": {"10": {"followers": 5000, "checked_at": "2026-10-07T01:00:00Z"}}},
        DASHBOARD: {"previous": True},
    }
    for path, value in values.items():
        write_json(root / path, value)
    # This fixture represents the existing offline dashboard CLI port. Its real
    # queue projection is covered by the scheduler queue status tests.
    package = root / "radar_backend/jobs"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "publish_steam.py").write_text(
        "import argparse,json\nfrom pathlib import Path\n"
        "p=argparse.ArgumentParser();p.add_argument('command');p.add_argument('--observed-at');a=p.parse_args()\n"
        "path=Path('data/scheduler_queue_status.json')\n"
        + ("path.write_text('{}');raise SystemExit(1)\n" if dashboard_failure else
           "path.write_text(json.dumps({'observed_at':a.observed_at}))\n"))
    (root / ".gitignore").write_text("output/\n__pycache__/\n*.pyc\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "seed offline daily fixture")
    git(root, "push", "origin", "HEAD:main")
    return remote, root


def writer(tmp_path, remote):
    root = tmp_path / "writer"
    subprocess.run(["git", "clone", str(remote), str(root)], check=True, capture_output=True)
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    return root


def save(root, path, value):
    write_json(root / path, value)
    git(root, "add", path)
    git(root, "commit", "-m", "concurrent evidence")
    git(root, "push", "origin", "HEAD:main")


def remote_json(remote, path):
    return json.loads(git(remote, "show", f"main:{path}"))


def baseline(root):
    return capture_daily_baseline(root, input_revision=git(root, "rev-parse", "HEAD"), now=CLOCK)


def advance(root):
    state = read_json(root / STATE)
    state["days_scanned"] = 4
    write_json(root / STATE, state)


def test_capture_observation_and_actual_acknowledged_head(tmp_path):
    remote, root = seed(tmp_path)
    base = baseline(root)
    advance(root)
    frozen = freeze_daily_state(root, base, now=CLOCK)
    original = deepcopy(frozen)
    receipt = publish_daily_state(root, frozen)
    assert frozen == original
    assert remote_json(remote, STATE)["days_scanned"] == 4
    assert receipt.published_revision == git(remote, "rev-parse", "main")
    assert receipt.payload_revision == snapshot_revision(frozen)
    assert receipt.input_revision == base["input_revision"]
    assert remote_json(remote, DASHBOARD)["observed_at"] == CLOCK.isoformat()
    assert not list(root.rglob("__pycache__"))


def test_push_race_replays_frozen_inputs_and_preserves_unrelated_remote_data(tmp_path):
    remote, root = seed(tmp_path)
    other = writer(tmp_path, remote)
    base = baseline(root)
    advance(root)
    frozen = freeze_daily_state(root, base, now=CLOCK)
    clocks = []
    class Race(SubprocessGitRepository):
        raced = False
        def _run(self, *args, push=False):
            if push:
                clocks.append(read_json(root / DASHBOARD)["observed_at"])
                if not self.raced:
                    self.raced = True
                    save(other, "data/other.json", {"another_owner": True})
            return super()._run(*args, push=push)
    receipt = publish_daily_state(root, frozen, repository=Race(root, disposable_checkout=True))
    assert receipt.attempts == 2
    assert remote_json(remote, STATE)["days_scanned"] == 4
    assert remote_json(remote, "data/other.json") == {"another_owner": True}
    assert clocks == [CLOCK.isoformat(), CLOCK.isoformat()]


def test_noop_still_receives_real_git_acknowledgement(tmp_path):
    remote, root = seed(tmp_path)
    frozen = freeze_daily_state(root, baseline(root), now=CLOCK)
    first = publish_daily_state(root, frozen)
    class Counting(SubprocessGitRepository):
        pushes = 0
        def _run(self, *args, push=False):
            self.pushes += push
            return super()._run(*args, push=push)
    repo = Counting(root, disposable_checkout=True)
    second = publish_daily_state(root, frozen, repository=repo)
    assert second.changed is False
    assert second.published_revision == first.published_revision == git(remote, "rev-parse", "main")
    assert repo.pushes == 1


def test_daily_private_merge_keeps_remote_new_master_and_cache_appids(tmp_path):
    remote, root = seed(tmp_path)
    base = baseline(root)
    incoming = read_json(root / MASTER)
    incoming["games"].append({"appid": 20, "name": "Local", "followers": 6000})
    write_json(root / MASTER, incoming)
    cached = read_json(root / CACHE)
    cached["games"]["20"] = {"followers": 6000, "checked_at": "2026-10-08T01:00:00Z"}
    write_json(root / CACHE, cached)
    write_json(root / "output/steam_upcoming.json", {"games": incoming["games"]})
    frozen = freeze_daily_state(root, base, now=CLOCK)
    other = writer(tmp_path, remote)
    theirs = read_json(other / MASTER)
    theirs["games"].append({"appid": 30, "name": "Twitch", "followers": 7000, "source": "twitch"})
    save(other, MASTER, theirs)
    theirs_cache = read_json(other / CACHE)
    theirs_cache["games"]["30"] = {"followers": 7000, "checked_at": "2026-10-08T02:00:00Z"}
    save(other, CACHE, theirs_cache)
    receipt = publish_daily_state(root, frozen)
    assert {row["appid"] for row in remote_json(remote, MASTER)["games"]} == {10, 20, 30}
    assert set(remote_json(remote, CACHE)["games"]) == {"10", "20", "30"}
    assert receipt.published_revision == git(remote, "rev-parse", "main")


def test_master_and_cache_are_not_owned_without_this_runner_batch_output(tmp_path):
    remote, root = seed(tmp_path)
    base = baseline(root)
    advance(root)
    frozen = freeze_daily_state(root, base, now=CLOCK)
    assert MASTER not in frozen["observed"] and CACHE not in frozen["observed"]
    other = writer(tmp_path, remote)
    master = read_json(other / MASTER)
    master["games"][0]["followers"] = 9000
    save(other, MASTER, master)
    publish_daily_state(root, frozen)
    assert remote_json(remote, MASTER)["games"][0]["followers"] == 9000


def test_conflicting_newer_daily_progress_fails_without_remote_overwrite(tmp_path):
    remote, root = seed(tmp_path)
    base = baseline(root)
    advance(root)
    frozen = freeze_daily_state(root, base, now=CLOCK)
    other = writer(tmp_path, remote)
    state = read_json(other / STATE)
    state.update(anchor_date="2026-10-09", days_scanned=0)
    save(other, STATE, state)
    head = git(remote, "rev-parse", "main")
    with pytest.raises(MergeConflict):
        publish_daily_state(root, frozen)
    assert git(remote, "rev-parse", "main") == head
    assert remote_json(remote, STATE) == state


@pytest.mark.parametrize("required", [STATE, CATALOG])
def test_missing_latest_required_input_cannot_acknowledge_empty_replacement(tmp_path, required):
    remote, root = seed(tmp_path)
    frozen = freeze_daily_state(root, baseline(root), now=CLOCK)
    other = writer(tmp_path, remote)
    git(other, "rm", required)
    git(other, "commit", "-m", "remove required source")
    git(other, "push", "origin", "HEAD:main")
    head = git(remote, "rev-parse", "main")
    with pytest.raises(ValueError, match="missing"):
        publish_daily_state(root, frozen)
    assert git(remote, "rev-parse", "main") == head


def test_conflicting_optional_candidate_added_remotely_is_not_hidden(tmp_path):
    remote, root = seed(tmp_path)
    git(root, "rm", PREFILTER)
    git(root, "commit", "-m", "no optional prefilter yet")
    git(root, "push", "origin", "HEAD:main")
    base = baseline(root)
    advance(root)
    frozen = freeze_daily_state(root, base, now=CLOCK)
    other = writer(tmp_path, remote)
    save(other, PREFILTER, {"complete": True, "games": {"99": {"priority": True}}})
    with pytest.raises(MergeConflict):
        publish_daily_state(root, frozen)


def test_unchanged_optional_remote_deletion_stays_deleted(tmp_path):
    remote, root = seed(tmp_path)
    frozen = freeze_daily_state(root, baseline(root), now=CLOCK)
    other = writer(tmp_path, remote)
    git(other, "rm", ELIGIBLE)
    git(other, "commit", "-m", "remove optional handoff")
    git(other, "push", "origin", "HEAD:main")
    publish_daily_state(root, frozen)
    assert ELIGIBLE not in git(remote, "ls-tree", "-r", "--name-only", "main").splitlines()


def test_reset_clock_is_read_once_even_when_push_race_crosses_taiwan_midnight(tmp_path, monkeypatch):
    import radar_backend.publication.daily_state as module
    remote, root = seed(tmp_path)
    other = writer(tmp_path, remote)
    calls = []
    class CrossingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(True)
            return CLOCK if len(calls) == 1 else CLOCK + timedelta(seconds=2)
    monkeypatch.setattr(module, "datetime", CrossingClock)
    class Race(SubprocessGitRepository):
        raced = False
        def _run(self, *args, push=False):
            if push and not self.raced:
                self.raced = True
                save(other, "data/other.json", {"midnight": True})
            return super()._run(*args, push=push)
    receipt = publish_daily_reset(root, input_revision="initial", event_name="workflow_dispatch",
                                 trigger_source="cloudflare", refresh_today=True,
                                 target_slot="2026-10-08T10:00:00Z",
                                 repository=Race(root, disposable_checkout=True))
    assert receipt.attempts == 2
    assert calls == [True]
    state = remote_json(remote, STATE)
    assert state["anchor_date"] == state["last_reset_date_taipei"] == "2026-10-08"
    assert state["daily_refresh_slot"] == daily_slot(CLOCK.astimezone(module.TAIPEI).date())
    assert remote_json(remote, DASHBOARD)["observed_at"] == CLOCK.isoformat()


def test_cloudflare_current_day_complete_progress_is_resumed_without_reset(tmp_path):
    remote, root = seed(tmp_path)
    state = read_json(root / STATE)
    state.update(anchor_date="2026-10-08", phase="followers", days_scanned=365,
                 daily_refresh_slot="2026-10-07T16:00:00Z", last_reset_date_taipei="2026-10-08")
    save(root, STATE, state)
    before_catalog = read_json(root / CATALOG)
    publish_daily_reset(root, input_revision="initial", now=CLOCK, event_name="workflow_dispatch",
                        trigger_source="cloudflare", refresh_today=True, target_slot="2026-10-08T10:00:00Z")
    assert remote_json(remote, STATE) == state
    assert remote_json(remote, CATALOG) == before_catalog


def test_manual_force_retry_resumes_another_writers_same_day_progress(tmp_path):
    remote, root = seed(tmp_path)
    initial = read_json(root / STATE)
    initial.update(anchor_date="2026-10-08", days_scanned=200)
    save(root, STATE, initial)
    other = writer(tmp_path, remote)
    class Race(SubprocessGitRepository):
        raced = False
        def _run(self, *args, push=False):
            if push and not self.raced:
                self.raced = True
                advanced = read_json(other / STATE)
                advanced.update(days_scanned=300)
                save(other, STATE, advanced)
            return super()._run(*args, push=push)
    receipt = publish_daily_reset(root, input_revision="manual", now=CLOCK, event_name="workflow_dispatch",
                                 trigger_source="manual", refresh_today=True,
                                 repository=Race(root, disposable_checkout=True))
    assert receipt.attempts == 2
    assert remote_json(remote, STATE)["days_scanned"] == 300
    assert remote_json(remote, CATALOG)["count"] == 1


def test_reset_refuses_newer_anchor_and_stale_cloudflare_slot(tmp_path):
    remote, root = seed(tmp_path)
    state = read_json(root / STATE)
    state["anchor_date"] = "2026-10-09"
    save(root, STATE, state)
    before = git(remote, "rev-parse", "main")
    with pytest.raises(MergeConflict, match="newer"):
        publish_daily_reset(root, input_revision="old", now=CLOCK, event_name="workflow_dispatch",
                            trigger_source="manual", refresh_today=True)
    with pytest.raises(ValueError, match="skipped"):
        publish_daily_reset(root, input_revision="old", now=CLOCK, event_name="workflow_dispatch",
                            trigger_source="cloudflare", refresh_today=True, target_slot="2026-10-07T10:00:00Z")
    assert git(remote, "rev-parse", "main") == before


def test_dashboard_failure_retains_previous_latest_snapshot_and_warns(tmp_path, capsys):
    remote, root = seed(tmp_path, dashboard_failure=True)
    base = baseline(root)
    advance(root)
    receipt = publish_daily_state(root, freeze_daily_state(root, base, now=CLOCK))
    assert receipt.published_revision == git(remote, "rev-parse", "main")
    assert remote_json(remote, DASHBOARD) == {"previous": True}
    assert "warning" in capsys.readouterr().out


def test_rejected_cli_retains_frozen_progress_then_replays_without_recollection(tmp_path, monkeypatch):
    remote, root = seed(tmp_path)
    monkeypatch.chdir(root)
    assert main(["capture"]) == 0
    advance(root)
    original = read_json(root / STATE)
    output = root / "output/github-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    receipt = root / "output/daily-state-publication.json"
    write_json(receipt, {"old_ack": True})
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(PublicationError):
        main(["publish", "--max-attempts", "2"])
    assert not receipt.exists()
    assert not output.exists()
    pending = read_json(root / "output/daily-state-pending.json")
    assert pending["observed"][STATE]["value"] == original
    assert read_json(root / STATE)["days_scanned"] == 4  # Last rejected candidate remains local.
    hook.unlink()
    # The next invocation uses the saved frozen artifact despite local resets.
    git(root, "reset", "--hard", "origin/main")
    assert main(["publish"]) == 0
    assert remote_json(remote, STATE) == original
    assert read_json(receipt)["published_revision"] == git(remote, "rev-parse", "main")
    assert output.read_text() == "state_persisted=true\n"


@pytest.mark.parametrize("path", [STATE, CATALOG, MASTER])
def test_source_symlink_is_rejected_before_freezing_or_git_reset(tmp_path, path):
    _, root = seed(tmp_path)
    outside = tmp_path / "outside.json"
    write_json(outside, {"private": True})
    (root / path).unlink()
    (root / path).symlink_to(outside)
    with pytest.raises(ValueError, match="escapes|symlink"):
        baseline(root)


def test_corrupt_latest_input_fails_closed_without_remote_commit(tmp_path):
    remote, root = seed(tmp_path)
    frozen = freeze_daily_state(root, baseline(root), now=CLOCK)
    other = writer(tmp_path, remote)
    (other / CATALOG).write_text('{"games":[],"games":[]}')
    git(other, "add", CATALOG)
    git(other, "commit", "-m", "bad latest data")
    git(other, "push", "origin", "HEAD:main")
    before = git(remote, "rev-parse", "main")
    with pytest.raises(ValueError, match="Duplicate"):
        publish_daily_state(root, frozen)
    assert git(remote, "rev-parse", "main") == before


def test_same_appid_newer_official_evidence_is_never_rolled_back(tmp_path):
    remote, root = seed(tmp_path)
    base = baseline(root)
    original = read_json(root / MASTER)
    original["games"][0].update(followers=6000, follower_checked_at="2026-10-08T01:00:00Z")
    write_json(root / MASTER, original)
    write_json(root / "output/steam_upcoming.json", {"games": original["games"]})
    frozen = freeze_daily_state(root, base, now=CLOCK)
    other = writer(tmp_path, remote)
    latest = read_json(other / MASTER)
    latest["games"][0].update(followers=7000, follower_checked_at="2026-10-08T02:00:00Z")
    save(other, MASTER, latest)
    receipt = publish_daily_state(root, frozen)
    assert remote_json(remote, MASTER) == latest
    assert receipt.published_revision == git(remote, "rev-parse", "main")


def test_same_appid_divergent_official_cache_evidence_fails_closed(tmp_path):
    remote, root = seed(tmp_path)
    base = baseline(root)
    cache = read_json(root / CACHE)
    cache["games"]["10"].update(followers=6000, checked_at="2026-10-08T01:00:00Z")
    write_json(root / CACHE, cache)
    write_json(root / "output/steam_upcoming.json", {"games": []})
    frozen = freeze_daily_state(root, base, now=CLOCK)
    other = writer(tmp_path, remote)
    latest = read_json(other / CACHE)
    latest["games"]["10"].update(followers=7000, checked_at="2026-10-08T02:00:00Z")
    save(other, CACHE, latest)
    before = git(remote, "rev-parse", "main")
    with pytest.raises(MergeConflict):
        publish_daily_state(root, frozen)
    assert remote_json(remote, CACHE) == latest
    assert git(remote, "rev-parse", "main") == before


def test_candidate_progress_push_race_fails_closed_instead_of_claiming_persisted(tmp_path):
    remote, root = seed(tmp_path)
    other = writer(tmp_path, remote)
    base = baseline(root)
    advance(root)
    frozen = freeze_daily_state(root, base, now=CLOCK)
    class Race(SubprocessGitRepository):
        raced = False
        def _run(self, *args, push=False):
            if push and not self.raced:
                self.raced = True
                state = read_json(other / STATE)
                state.update(phase="date_precision", days_scanned=365)
                save(other, STATE, state)
            return super()._run(*args, push=push)
    with pytest.raises(MergeConflict):
        publish_daily_state(root, frozen, repository=Race(root, disposable_checkout=True))
    assert remote_json(remote, STATE)["phase"] == "date_precision"
    assert remote_json(remote, STATE)["days_scanned"] == 365


def test_pending_reuse_cannot_switch_baselines(tmp_path, monkeypatch):
    _, root = seed(tmp_path)
    monkeypatch.chdir(root)
    main(["capture"])
    basepath = root / "output/daily-state-baseline.json"
    original = read_json(basepath)
    write_json(root / "output/daily-state-pending.json", freeze_daily_state(root, original, now=CLOCK))
    altered = deepcopy(original)
    altered["input_revision"] = "different-baseline"
    write_json(basepath, altered)
    with pytest.raises(ValueError, match="another baseline"):
        main(["publish"])


@pytest.mark.parametrize("value", [True, "1", 2])
def test_baseline_version_is_strict_and_validation_precedes_git_reset(tmp_path, value):
    remote, root = seed(tmp_path)
    base = baseline(root)
    base["schema_version"] = value
    advance(root)
    before = git(remote, "rev-parse", "main")
    with pytest.raises(ValueError, match="schema"):
        freeze_daily_state(root, base, now=CLOCK)
    assert read_json(root / STATE)["days_scanned"] == 4
    assert git(remote, "rev-parse", "main") == before


def test_unowned_dirty_source_is_not_staged_or_discarded(tmp_path):
    remote, root = seed(tmp_path)
    frozen = freeze_daily_state(root, baseline(root), now=CLOCK)
    before = git(remote, "rev-parse", "main")
    path = root / "radar_backend/jobs/publish_steam.py"
    path.write_text(path.read_text() + "\n# unrelated edit\n")
    with pytest.raises(PublicationScopeError):
        publish_daily_state(root, frozen)
    assert git(remote, "rev-parse", "main") == before
    assert "unrelated edit" in path.read_text()


def test_receipt_pending_and_baseline_paths_cannot_collide(tmp_path, monkeypatch):
    _, root = seed(tmp_path)
    monkeypatch.chdir(root)
    main(["capture"])
    base = read_json(root / "output/daily-state-baseline.json")
    with pytest.raises(ValueError, match="overwrite"):
        main(["publish", "--receipt", "output/daily-state-baseline.json"])
    with pytest.raises(ValueError, match="overwrite"):
        main(["publish", "--pending", "output/daily-state-baseline.json"])
    assert read_json(root / "output/daily-state-baseline.json") == base


def test_source_temporary_symlink_is_rejected_before_any_external_write(tmp_path):
    remote, root = seed(tmp_path)
    base = baseline(root)
    advance(root)
    frozen = freeze_daily_state(root, base, now=CLOCK)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep external content")
    path = root / STATE
    path.with_suffix(path.suffix + ".tmp").symlink_to(outside)
    before = git(remote, "rev-parse", "main")
    with pytest.raises(ValueError, match="symlink|escapes"):
        publish_daily_state(root, frozen)
    assert outside.read_text() == "keep external content"
    assert git(remote, "rev-parse", "main") == before


@pytest.mark.parametrize("command,argument,path", [
    ("capture", "--output", "output/daily-state-baseline.json"),
    ("publish", "--receipt", "output/daily-state-publication.json"),
    ("publish", "--pending", "output/daily-state-pending.json"),
])
def test_artifact_temporary_symlink_is_rejected_before_external_write(tmp_path, monkeypatch,
                                                                    command, argument, path):
    remote, root = seed(tmp_path)
    monkeypatch.chdir(root)
    main(["capture"])
    outside = tmp_path / "outside.txt"
    outside.write_text("keep external artifact")
    destination = root / path
    destination.with_suffix(destination.suffix + ".tmp").symlink_to(outside)
    before = git(remote, "rev-parse", "main")
    with pytest.raises(ValueError, match="symlink|escapes"):
        main([command, argument, path])
    assert outside.read_text() == "keep external artifact"
    assert git(remote, "rev-parse", "main") == before


def test_artifact_literal_symlink_and_symlink_parent_are_rejected(tmp_path, monkeypatch):
    _, root = seed(tmp_path)
    monkeypatch.chdir(root)
    output = root / "output"
    output.mkdir()
    sentinel = output / "sentinel.json"
    write_json(sentinel, {"keep": True})
    link = output / "baseline.json"
    link.symlink_to(sentinel)
    with pytest.raises(ValueError, match="symlink"):
        main(["capture", "--output", str(link)])
    assert read_json(sentinel) == {"keep": True}
    link.unlink()
    directory = output / "redirect"
    directory.symlink_to(output, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        main(["capture", "--output", str(directory / "baseline.json")])
