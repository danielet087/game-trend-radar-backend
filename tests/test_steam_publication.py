"""Offline delivery tests use real local bare Git remotes, never Steam APIs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from radar_core.publication import PublicationError, SubprocessGitRepository, snapshot_revision
from radar_backend.jobs.publish_steam import main, parser
from radar_backend.publication.checkpoint_delivery import CHECKPOINT, publish_queue_batch
from radar_backend.publication.steam import CATALOG_PATHS, publish_catalog, publish_growth, read_json, write_json


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def frontend(tmp_path):
    remote = tmp_path / "public.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, capture_output=True)
    root = tmp_path / "frontend"
    subprocess.run(["git", "clone", str(remote), str(root)], check=True, capture_output=True)
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    release = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()
    row = {"appid": 123456789, "name": "Test Game", "followers": 5500,
           "follower_checked_at": "2026-10-02T01:00:00Z", "release_start": release,
           "release_end": release, "release_precision": "day",
           "release_display_precision": "date_full", "sexual_content_screened": True,
           "tags": ["preserved"], "content_enriched_at": "2026-10-02T02:00:00Z"}
    write_json(root / f"data/games/{row['appid']}.json", row)
    write_json(root / "data/index.json", {"version": 2, "release_date_audited": True})
    write_json(root / "data/excluded_date_appids.json", {"appids": []})
    git(root, "add", "data")
    git(root, "commit", "-m", "seed")
    git(root, "push", "origin", "HEAD:main")
    source = {"version": 2, "generated_at": "2026-10-02T03:00:00Z",
              "games": [{key: value for key, value in row.items()
                         if key not in {"tags", "content_enriched_at"}}]}
    return remote, root, source


def published_json(remote, path):
    return json.loads(git(remote, "show", f"main:{path}"))


def test_catalog_commit_covers_all_projections_and_receipt_is_actual_remote_head(tmp_path):
    remote, root, source = frontend(tmp_path)
    original = deepcopy(source)
    receipt = publish_catalog(root, source, input_revision="backend-source")
    assert source == original
    assert receipt.published_revision == git(remote, "rev-parse", "main")
    assert receipt.payload_revision == snapshot_revision({"steam_master": source})
    index = published_json(remote, "data/index.json")
    catalog = published_json(remote, "data/catalog.json")
    game = published_json(remote, f"data/games/{source['games'][0]['appid']}.json")
    assert game["tags"] == ["preserved"]
    assert index["catalog_revision"] == catalog["revision"]
    assert index["game_count"] == catalog["count"] == 1
    assert published_json(remote, "data/steam_upcoming.json")["games"][0]["appid"] == game["appid"]
    assert published_json(remote, "data/lists/upcoming.json")["appids"] == [game["appid"]]
    assert "published_revision" not in catalog  # Commit receipts cannot self-reference.
    assert receipt.target_snapshot_revision


def test_catalog_refresh_preserves_concurrent_rich_metadata_then_acknowledges_noop(tmp_path):
    remote, root, source = frontend(tmp_path)
    writer = tmp_path / "writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.com")
    row_path = f"data/games/{source['games'][0]['appid']}.json"
    row = read_json(writer / row_path)
    row["tags"] = ["latest metadata"]
    write_json(writer / row_path, row)
    git(writer, "add", row_path)
    git(writer, "commit", "-m", "new metadata")
    git(writer, "push", "origin", "HEAD:main")
    first = publish_catalog(root, source, input_revision="backend-source")
    assert published_json(remote, row_path)["tags"] == ["latest metadata"]
    second = publish_catalog(root, source, input_revision="backend-source")
    assert second.published_revision == first.published_revision
    assert second.changed is False
    assert second.attempts == 1
    assert second.target_snapshot_revision == first.target_snapshot_revision


def test_push_race_rebuilds_frozen_source_against_concurrent_metadata_without_recollection(tmp_path):
    remote, root, source = frontend(tmp_path)
    writer = tmp_path / "racing-writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.com")
    row_path = f"data/games/{source['games'][0]['appid']}.json"
    class RacingRepository(SubprocessGitRepository):
        raced = False
        def _run(self, *args, push=False):
            if push and not self.raced:
                self.raced = True
                row = read_json(writer / row_path)
                row["tags"] = ["arrived during push"]
                write_json(writer / row_path, row)
                git(writer, "add", row_path)
                git(writer, "commit", "-m", "race after frozen replay")
                git(writer, "push", "origin", "HEAD:main")
            return super()._run(*args, push=push)
    repository = RacingRepository(root, disposable_checkout=True)
    receipt = publish_catalog(root, source, input_revision="frozen-source", repository=repository)
    assert receipt.attempts == 2
    assert receipt.published_revision == git(remote, "rev-parse", "main")
    assert published_json(remote, row_path)["tags"] == ["arrived during push"]
    assert receipt.payload_revision == snapshot_revision({"steam_master": source})


def test_catalog_retry_uses_one_taiwan_day_even_if_system_clock_crosses_midnight(tmp_path, monkeypatch):
    import radar_backend.publication.steam as module
    remote, root, source = frontend(tmp_path)
    writer = tmp_path / "midnight-writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.com")
    row_path = f"data/games/{source['games'][0]['appid']}.json"
    # At 23:59 Taiwan this game belongs to upcoming, at 00:00 it belongs to
    # released. A push race must not change the batch's frozen projection day.
    source["games"][0]["release_start"] = "2026-10-08"
    source["games"][0]["release_end"] = "2026-10-08"
    times = []
    real_build = module.build
    def capture_build(*args, **kwargs):
        times.append(kwargs["now"])
        return real_build(*args, **kwargs)
    monkeypatch.setattr(module, "build", capture_build)
    class MidnightRace(SubprocessGitRepository):
        raced = False
        def _run(self, *args, push=False):
            if push and not self.raced:
                self.raced = True
                row = read_json(writer / row_path)
                row["tags"] = ["midnight race"]
                write_json(writer / row_path, row)
                git(writer, "add", row_path)
                git(writer, "commit", "-m", "race")
                git(writer, "push", "origin", "HEAD:main")
            return super()._run(*args, push=push)
    clock = datetime(2026, 10, 8, 15, 59, 59, tzinfo=timezone.utc)
    clock_calls = []
    class CrossingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            clock_calls.append(True)
            return clock if len(clock_calls) == 1 else clock + timedelta(seconds=2)
    monkeypatch.setattr(module, "datetime", CrossingClock)
    receipt = publish_catalog(root, source, input_revision="frozen",
                            repository=MidnightRace(root, disposable_checkout=True))
    assert receipt.attempts == 2
    assert times == [clock, clock]
    assert len(clock_calls) == 1
    assert published_json(remote, "data/lists/upcoming.json")["appids"] == [source["games"][0]["appid"]]


def test_catalog_stale_followers_cannot_roll_back_newer_published_measurement(tmp_path):
    remote, root, source = frontend(tmp_path)
    source["games"][0]["follower_checked_at"] = "2026-10-01T01:00:00Z"
    before = git(remote, "rev-parse", "main")
    with pytest.raises(ValueError, match="Stale official Followers"):
        publish_catalog(root, source, input_revision="old-source")
    assert git(remote, "rev-parse", "main") == before


@pytest.mark.parametrize("malformed", ['{"games":[]}', '{"games":[],"games":[]}', '{"games": NaN}'])
def test_corrupt_source_is_not_an_empty_authoritative_replacement(tmp_path, malformed):
    path = tmp_path / "bad.json"
    path.write_text(malformed)
    with pytest.raises((ValueError, TypeError)):
        publish_catalog(tmp_path, read_json(path), input_revision="bad-source")


def test_corrupt_latest_public_file_cannot_be_replaced_with_builder_default(tmp_path):
    remote, root, source = frontend(tmp_path)
    (root / "data/index.json").write_text("{broken")
    git(root, "add", "data/index.json")
    git(root, "commit", "-m", "corrupt upstream")
    git(root, "push", "origin", "HEAD:main")
    before = git(remote, "rev-parse", "main")
    with pytest.raises(ValueError):
        publish_catalog(root, source, input_revision="source")
    assert git(remote, "rev-parse", "main") == before


def test_rejected_catalog_push_never_returns_a_receipt(tmp_path):
    remote, root, source = frontend(tmp_path)
    before = git(remote, "rev-parse", "main")
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(PublicationError):
        publish_catalog(root, source, input_revision="source", max_attempts=2)
    assert git(remote, "rev-parse", "main") == before


def test_growth_cli_retry_passes_one_frozen_observation_clock_and_keeps_metadata(tmp_path, monkeypatch):
    import radar_backend.publication.steam as module
    remote, root, source = frontend(tmp_path)
    row = {**source["games"][0], "release_start": "2026-10-09", "release_end": "2026-10-09"}
    write_json(root / "data/catalog.json", {"count": 1, "games": [row]})
    (root / "scripts").mkdir()
    # A subprocess port fixture records the exact observation clock received by
    # the frontend CLI. The frontend repository tests its real builder separately
    # so this offline backend CI needs no sibling checkout or API access.
    (root / "scripts/build_radar_insights.py").write_text(
        "import argparse,json\nfrom pathlib import Path\nfrom datetime import datetime,timedelta\n"
        "p=argparse.ArgumentParser();p.add_argument('--data-dir');p.add_argument('--measurements');p.add_argument('--observed-at');a=p.parse_args()\n"
        "rows=json.loads(Path(a.data_dir,'catalog.json').read_text())['games'];r=rows[0]\n"
        "s={'started_at':a.observed_at,'records':{str(r['appid']):{'first_seen_at':a.observed_at}}}\n"
        "Path(a.data_dir,'insights-state.json').write_text(json.dumps(s))\n"
        "Path(a.data_dir,'activity.json').write_text(json.dumps({'clock':a.observed_at}))\n"
        "day=(datetime.fromisoformat(a.observed_at)+timedelta(hours=8)).date().isoformat()\n"
        "Path(a.data_dir,'growth.json').write_text(json.dumps({'as_of':day}))\n", encoding="utf-8")
    git(root, "add", "scripts", "data/catalog.json")
    git(root, "commit", "-m", "seed observation-clock CLI port")
    git(root, "push", "origin", "HEAD:main")
    writer = tmp_path / "growth-writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.com")
    clock = datetime(2026, 10, 8, 15, 59, 59, tzinfo=timezone.utc)
    calls, rendered = [], []
    class CrossingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(True)
            return clock if len(calls) == 1 else clock + timedelta(seconds=2)
    monkeypatch.setattr(module, "datetime", CrossingClock)
    class GrowthRace(SubprocessGitRepository):
        raced = False
        def _run(self, *args, push=False):
            if push:
                rendered.append(read_json(root / "data/insights-state.json")["started_at"])
                if not self.raced:
                    self.raced = True
                    write_json(writer / "data/unrelated.json", {"latest": True})
                    git(writer, "add", "data/unrelated.json")
                    git(writer, "commit", "-m", "race after baseline render")
                    git(writer, "push", "origin", "HEAD:main")
            return super()._run(*args, push=push)
    frozen = {"reason": "completed", "measurements": [{"appid": row["appid"], "followers": 5600,
              "at": "2026-10-08T15:00:00Z", "source": "steam_community"}]}
    receipt = publish_growth(root, frozen, input_revision="catalog-source",
                            repository=GrowthRace(root, disposable_checkout=True))
    assert receipt.attempts == 2
    assert calls == [True]
    assert rendered == [clock.isoformat()] * 2
    assert published_json(remote, "data/growth.json")["as_of"] == "2026-10-08"
    assert published_json(remote, "data/insights-state.json")["records"][str(row["appid"])]["first_seen_at"] == clock.isoformat()
    assert published_json(remote, "data/unrelated.json") == {"latest": True}
    assert not list((root / "scripts").rglob("*.pyc"))


@pytest.mark.parametrize("kind,batch", [
    ("groups", {"schema_version": 1, "results": []}),
    ("twitch", {"schema_version": 1, "records": [], "state_updates": []}),
    ("dispatch", {"schema_version": 1, "records": [{}], "state_updates": {}}),
    ("dispatch", {"schema_version": 1, "records": [], "state_updates": {}, "follower_candidates": []}),
])
def test_malformed_queue_batch_rejected_before_remote_or_network_operations(tmp_path, kind, batch):
    with pytest.raises(ValueError):
        publish_queue_batch(tmp_path, batch, kind=kind, input_revision="source")


def test_cli_defaults_preserve_production_paths_and_retry_budgets():
    assert parser().parse_args(["catalog"]).input == Path("data/steam_upcoming_master.json")
    assert parser().parse_args(["catalog"]).max_attempts == 8
    for command in ("growth", "growth-checkpoint"):
        assert parser().parse_args([command]).max_attempts == 5
    assert parser().parse_args(["growth"]).report == Path("output/public-growth.json")


def test_discovery_only_cli_removes_old_receipt_without_claiming_publication(tmp_path, monkeypatch):
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"published":true}')
    output = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert main(["catalog", "--skip-missing-batch", str(tmp_path / "missing.json"),
                 "--receipt", str(receipt)]) == 0
    assert not receipt.exists()
    assert not output.exists()


@pytest.mark.parametrize("command,flag", [("catalog", "--input"), ("growth", "--report"),
                                        ("growth-checkpoint", "--baseline"), ("queue", "--batch")])
def test_receipt_input_collision_never_deletes_the_frozen_input(tmp_path, command, flag):
    path = tmp_path / "frozen.json"
    original = '{"retained":true}'
    path.write_text(original)
    args = [command, flag, str(path), "--receipt", str(path)]
    if command == "queue":
        args += ["--kind", "twitch"]
    with pytest.raises(ValueError, match="immutable input"):
        main(args)
    assert path.read_text() == original


@pytest.mark.parametrize("kind", ["twitch", "groups", "dispatch"])
def test_real_queue_apply_cli_and_projection_share_one_acknowledged_commit(tmp_path, kind):
    # This fixture executes the actual queue CLI and export graph. Its Steam
    # inputs are local copies of the frozen source ledgers, with no collector.
    source_root = Path(__file__).resolve().parents[1]
    remote = tmp_path / "queue.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, capture_output=True)
    root = tmp_path / "backend"
    subprocess.run(["git", "clone", str(remote), str(root)], check=True, capture_output=True)
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    for name in ("scripts", "radar_backend", "collectors"):
        shutil.copytree(source_root / name, root / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(source_root / ".gitignore", root / ".gitignore")
    for path in (
        "experiments/steam_official_nearfirst_20260922/source_queue.json",
        "experiments/steam_official_nearfirst_20260922/checkpoint.json",
        "experiments/steam_official_nearfirst_20260922/source_unresolved.json",
        "experiments/steam_official_followers_20260922/checkpoint.json",
        "data/steam_candidates_eligible.json", "data/steam_prefilter_state.json",
        "data/steam_followers_cache.json",
        "data/steam_adult_exclusion.json",
    ):
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / path, destination)
    checkpoint = {"official_results": {}, "pending_candidates": {}, "private_future_field": "retained"}
    write_json(root / CHECKPOINT, checkpoint)
    write_json(root / "data/steam_upcoming_master.json", {"games": []})
    write_json(root / "data/twitch_steam_import_state.json", {})
    # Dispatch owns only receipts/status. Valid compact source formatting must
    # survive exactly, rather than being rewritten by the generic queue CLI.
    if kind == "dispatch":
        (root / CHECKPOINT).write_text(json.dumps(checkpoint, separators=(",", ":")))
        (root / "data/steam_upcoming_master.json").write_text('{"games":[]}')
    original_checkpoint_bytes = (root / CHECKPOINT).read_bytes()
    original_master_bytes = (root / "data/steam_upcoming_master.json").read_bytes()
    git(root, "add", ".")
    git(root, "commit", "-m", "seed complete offline runtime")
    git(root, "push", "origin", "HEAD:main")
    batch = {"schema_version": 1, "generated_at": "2026-10-02T01:00:00Z",
             "records": [], "state_updates": {}, "stop_reason": "complete"}
    if kind == "groups":
        batch = {"schema_version": 1, "generated_at": "2026-10-02T01:00:00Z",
                 "results": {}, "requests_this_run": 0, "stop_reason": "complete"}
    clock = datetime(2026, 10, 8, 15, 59, 59, tzinfo=timezone.utc)
    rendered = []
    repository = None
    if kind == "groups":
        writer = tmp_path / "queue-writer"
        subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
        git(writer, "config", "user.name", "Test")
        git(writer, "config", "user.email", "test@example.com")
        class QueueRace(SubprocessGitRepository):
            raced = False
            def _run(self, *args, push=False):
                if push:
                    rendered.append(read_json(root / "data/scheduler_queue_status.json")["generated_at"])
                    if not self.raced:
                        self.raced = True
                        write_json(writer / CHECKPOINT, {**checkpoint, "remote_unknown_field": "latest"})
                        git(writer, "add", CHECKPOINT)
                        git(writer, "commit", "-m", "race after queue projection")
                        git(writer, "push", "origin", "HEAD:main")
                return super()._run(*args, push=push)
        repository = QueueRace(root, disposable_checkout=True)
    receipt = publish_queue_batch(root, batch, kind=kind, input_revision="frozen-batch", now=clock,
                                  repository=repository)
    assert receipt.published_revision == git(remote, "rev-parse", "main")
    assert published_json(remote, CHECKPOINT)["private_future_field"] == "retained"
    assert published_json(remote, "data/scheduler_queue_status.json")["schema_version"] == 1
    if kind == "groups":
        assert receipt.attempts == 2
        assert rendered == ["2026-10-08T15:59:59Z"] * 2
        assert published_json(remote, CHECKPOINT)["remote_unknown_field"] == "latest"
    if kind == "dispatch":
        assert (root / CHECKPOINT).read_bytes() == original_checkpoint_bytes
        assert (root / "data/steam_upcoming_master.json").read_bytes() == original_master_bytes
    assert not list(root.rglob("*.pyc"))
