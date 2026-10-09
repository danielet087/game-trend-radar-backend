"""Deliver frozen queue evidence without replacing newer remote state."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from radar_core.publication import SubprocessGitRepository, publish_with_retry, snapshot_revision
from radar_backend.publication.steam import read_json, validate_json_paths, write_json
from radar_backend.state.growth_checkpoint import merge_growth_checkpoint

CHECKPOINT = "experiments/steam_official_daily_catchup/checkpoint.json"
TWITCH_PATHS = (CHECKPOINT, "data/steam_upcoming_master.json",
                "data/twitch_steam_import_state.json", "data/scheduler_queue_status.json")
GROUP_PATHS = (CHECKPOINT, "data/scheduler_queue_status.json")
DISPATCH_PATHS = ("data/twitch_steam_import_state.json", "data/scheduler_queue_status.json")


def backend_repository(root):
    return SubprocessGitRepository(root, disposable_checkout=True)


def publish_growth_checkpoint(root: Path, baseline: dict, observed: dict, report: dict,
                              *, input_revision: str, max_attempts=5, repository=None):
    frozen = deepcopy({"baseline": baseline, "observed": observed, "report": report})
    # Validate all frozen state before the first reset or remote operation.
    merge_growth_checkpoint(baseline, baseline, observed, report)
    revision = snapshot_revision(frozen)
    def apply(latest_root):
        path = latest_root / CHECKPOINT
        merged = merge_growth_checkpoint(read_json(path), frozen["baseline"],
                                         frozen["observed"], frozen["report"])
        write_json(path, merged)
    return publish_with_retry(
        repository or backend_repository(root), apply, paths=(CHECKPOINT,),
        message="state: save official growth observations and shared Community cooldown",
        input_revision=input_revision, payload_revision=revision, max_attempts=max_attempts,
    )


def publish_queue_batch(root: Path, batch: dict, *, kind: str, input_revision: str,
                        max_attempts=5, repository=None, now=None):
    frozen = deepcopy(batch)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Timezone-aware queue publication clock required")
    if kind not in {"twitch", "groups", "dispatch"}:
        raise ValueError("Unknown queue delivery kind")
    if (not isinstance(frozen, dict) or frozen.get("schema_version") != 1
            or (kind == "groups" and not isinstance(frozen.get("results"), dict))
            or (kind != "groups" and (not isinstance(frozen.get("records"), list)
                                      or not isinstance(frozen.get("state_updates"), dict)))):
        raise ValueError("Malformed frozen queue batch")
    if kind == "dispatch" and (frozen["records"] or "follower_candidates" in frozen):
        raise ValueError("Dispatch receipt must not modify admissions or queue candidates")
    paths = GROUP_PATHS if kind == "groups" else DISPATCH_PATHS if kind == "dispatch" else TWITCH_PATHS
    revision = snapshot_revision(frozen)
    with tempfile.TemporaryDirectory(prefix="radar-steam-queue-") as scratch:
        batch_path = Path(scratch) / "batch.json"
        write_json(batch_path, frozen)
        def apply(latest_root):
            validate_json_paths(latest_root, TWITCH_PATHS)
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            if kind == "dispatch":
                command = [sys.executable, "-B", "-m", "radar_backend.jobs.publish_steam",
                           "apply-dispatch-batch", "--batch", str(batch_path)]
            else:
                script = "radar_backend.jobs.resolve_official_groups" if kind == "groups" else "radar_backend.jobs.reconcile_twitch_official_queue"
                command = [sys.executable, "-B", "-m", script, "--phase", "apply", "--batch", str(batch_path)]
            subprocess.run(command,
                           cwd=latest_root, check=True, capture_output=True, text=True, env=environment)
            # Export is also offline, and a failure cannot acknowledge a stale
            # queue view as a successful coherent snapshot.
            subprocess.run([sys.executable, "-B", "-m", "radar_backend.jobs.publish_steam",
                            "render-queue-status", "--observed-at", clock.isoformat()],
                           cwd=latest_root, check=True, capture_output=True, text=True, env=environment)
        return publish_with_retry(
            repository or backend_repository(root), apply, paths=paths,
            message={"twitch": "data: merge Twitch discoveries into official Followers queue",
                     "groups": "data: resolve official groups ahead of Followers queue",
                     "dispatch": "data: save unified queue Twitch content dispatch receipts"}[kind],
            input_revision=input_revision, payload_revision=revision, max_attempts=max_attempts,
        )
