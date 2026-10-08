"""Acknowledged hourly checkpoints and frozen recovery after runner errors."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

from radar_core.publication import PublicationReceipt, SubprocessGitRepository, publish_with_retry, snapshot_revision
from radar_backend.domain.official_queue import aware_time
from radar_backend.publication.steam import read_json, write_json
from radar_backend.state.growth_checkpoint import COMMUNITY_FIELDS, merge_growth_checkpoint
from radar_backend.state.official_merge import merge_json_three_way, merge_master, strict_json_loads

CHECKPOINT = "experiments/steam_official_daily_catchup/checkpoint.json"
MASTER = "data/steam_upcoming_master.json"
DASHBOARD = "data/scheduler_queue_status.json"
PENDING = "output/steam_official_daily_catchup/checkpoint-pending.json"
RECEIPT = "output/steam_official_daily_catchup/checkpoint-publication.json"
PATHS = {"checkpoint": CHECKPOINT, "master": MASTER, "dashboard": DASHBOARD}
COHORT = "steam_official_daily_catchup_dynamic_v1"


def validate_source_locations(root):
    root = Path(root).resolve()
    for relative in PATHS.values():
        for candidate in (root / relative, root / (relative + ".tmp")):
            while candidate != root:
                if candidate.is_symlink():
                    raise ValueError("Official source paths must not use symlinks")
                candidate = candidate.parent


def artifact_path(path, root):
    root = Path(root).resolve()
    candidate = Path(path).absolute()
    for item in (candidate, *candidate.parents):
        if item.is_symlink():
            raise ValueError("Recovery artifact paths must not use symlinks")
    candidate = candidate.resolve()
    if candidate.suffix != ".json":
        raise ValueError("Recovery artifacts must be JSON files")
    if candidate.with_suffix(candidate.suffix + ".tmp").is_symlink():
        raise ValueError("Recovery artifact temporary files must not use symlinks")
    if candidate.is_relative_to(root) and not candidate.is_relative_to(root / "output"):
        raise ValueError("Recovery artifacts must remain outside persisted source data")
    return candidate


def artifact_paths(root, pending, receipt):
    pending, receipt = artifact_path(pending, root), artifact_path(receipt, root)
    if pending == receipt or pending.with_suffix(".json.tmp") == receipt or receipt.with_suffix(".json.tmp") == pending:
        raise ValueError("Pending batch and receipt require distinct files")
    return pending, receipt


def _validate_checkpoint(value, *, allow_empty=True):
    if not isinstance(value, dict):
        raise ValueError("Official checkpoint must be an object")
    if value or not allow_empty:
        if value.get("cohort") != COHORT:
            raise ValueError("Official checkpoint cohort changed or is missing")
        if type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError("Official checkpoint version changed or is missing")
    for field in ("official_results", "pending_candidates", "unresolved_candidates",
                  "content_dispatches", "official_growth_observations"):
        if field in value and not isinstance(value[field], dict):
            raise ValueError(f"Malformed official checkpoint {field}")
    if not isinstance(value.get("attempt_events", []), list):
        raise ValueError("Malformed official checkpoint attempt events")
    if any(not isinstance(event, dict) for event in value.get("attempt_events", [])):
        raise ValueError("Official attempt events must be objects")
    snapshot_revision(value)


def _new_events(baseline, observed):
    before = {snapshot_revision(event) for event in baseline.get("attempt_events", [])}
    return [deepcopy(event) for event in observed.get("attempt_events", [])
            if snapshot_revision(event) not in before]


def merge_official_checkpoint(latest, baseline, observed):
    for value in (latest, baseline, observed):
        _validate_checkpoint(value)
    for field in ("cohort", "version"):
        values = [value[field] for value in (latest, baseline, observed) if field in value]
        if values and any(value != values[0] for value in values):
            raise ValueError(f"Official checkpoint {field} changed")
    excluded = {*COMMUNITY_FIELDS, "official_growth_observations", "attempt_events", "official_results",
                "group_resolution_api_cooldown"}
    ordinary = [{key: deepcopy(item) for key, item in value.items() if key not in excluded}
                for value in (latest, baseline, observed)]
    merged = merge_json_three_way(*ordinary)
    result_states = [deepcopy(value.get("official_results", {})) for value in (latest, baseline, observed)]
    for key, row in list(result_states[2].items()):
        remote = result_states[0].get(key)
        if not isinstance(remote, dict) or not isinstance(row, dict):
            continue
        remote_time, new_time = aware_time(remote.get("official_checked_at_taipei")), aware_time(row.get("official_checked_at_taipei"))
        if (row != result_states[1].get(key) and remote != result_states[1].get(key)
                and remote_time is not None and new_time is not None and remote_time > new_time):
            result_states[2][key] = deepcopy(remote)
    if any("official_results" in value for value in (latest, baseline, observed)):
        merged["official_results"] = merge_json_three_way(*result_states)
    events = _new_events(baseline, observed)
    community = merge_growth_checkpoint(latest, baseline, observed, {
        "events": [{**event, "observed_at": event.get("when_taipei")}
                   for event in events],
    })
    for field in COMMUNITY_FIELDS:
        if field in community:
            merged[field] = deepcopy(community[field])
    if "official_growth_observations" in latest:
        merged["official_growth_observations"] = deepcopy(latest["official_growth_observations"])
    if "group_resolution_api_cooldown" in latest:
        merged["group_resolution_api_cooldown"] = deepcopy(latest["group_resolution_api_cooldown"])
    merged_events = deepcopy(latest.get("attempt_events", []))
    known = {snapshot_revision(event) for event in merged_events}
    for event in events:
        identity = snapshot_revision(event)
        if identity not in known:
            merged_events.append(event)
            known.add(identity)
    if len(merged_events) > 2000:
        merged_events = merged_events[-1500:]
    if "attempt_events" in latest or "attempt_events" in observed:
        merged["attempt_events"] = merged_events
    return merged


def _validate_pending(batch):
    fields = {"schema_version", "kind", "paths", "input_revision", "baseline", "baseline_present", "observed",
              "observed_at", "recovery_eligible", "acknowledged_revision"}
    if (not isinstance(batch, dict) or set(batch) != fields
            or type(batch.get("schema_version")) is not int or batch["schema_version"] != 1
            or batch.get("kind") != "official-checkpoint" or batch.get("paths") != PATHS
            or type(batch.get("recovery_eligible")) is not bool
            or not isinstance(batch.get("input_revision"), str) or not batch["input_revision"]):
        raise ValueError("Malformed official checkpoint recovery batch")
    if aware_time(batch.get("observed_at")) is None:
        raise ValueError("Recovery batch requires a frozen aware clock")
    if (not isinstance(batch.get("baseline_present"), dict)
            or set(batch["baseline_present"]) != {"checkpoint", "master"}
            or any(type(value) is not bool for value in batch["baseline_present"].values())):
        raise ValueError("Recovery batch must declare its original source presence")
    for field in ("baseline", "observed"):
        value = batch[field]
        if not isinstance(value, dict) or set(value) != {"checkpoint", "master"}:
            raise ValueError("Malformed recovery state")
        _validate_checkpoint(value["checkpoint"])
        merge_master(value["master"], value["master"], value["master"])
    _validate_checkpoint(batch["observed"]["checkpoint"], allow_empty=False)
    if batch["baseline_present"]["checkpoint"]:
        _validate_checkpoint(batch["baseline"]["checkpoint"], allow_empty=False)
    revision = batch["acknowledged_revision"]
    if batch["recovery_eligible"]:
        if revision is not None:
            raise ValueError("Pending recovery must not claim an acknowledged revision")
    elif (not isinstance(revision, str) or len(revision) not in {40, 64}
          or any(char not in "0123456789abcdef" for char in revision)):
        raise ValueError("Acknowledged recovery requires a Git revision")
    snapshot_revision(batch)


def pending_revision(batch):
    """Lifecycle acknowledgement fields do not change the frozen input hash."""
    return snapshot_revision({key: value for key, value in batch.items()
                              if key not in {"recovery_eligible", "acknowledged_revision"}})


def _render_dashboard(root, observed_at):
    subprocess.run([sys.executable, "-B", "-m", "radar_backend.jobs.publish_steam",
                    "render-queue-status", "--observed-at", observed_at.isoformat()],
                   cwd=root, check=True, capture_output=True, text=True,
                   timeout=60,
                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def publish_pending(root, batch, *, receipt_path, repository=None, max_attempts=5,
                    renderer=None):
    """Only replay evidence; collection and dispatch never run during retry."""
    frozen = deepcopy(batch)
    _validate_pending(frozen)
    if not frozen["recovery_eligible"]:
        raise ValueError("Recovery batch already acknowledged")
    root = Path(root).resolve()
    validate_source_locations(root)
    receipt_path = artifact_path(receipt_path, root)
    receipt_path.unlink(missing_ok=True)
    revision = pending_revision(frozen)
    observed_at = aware_time(frozen["observed_at"])
    render = renderer or _render_dashboard

    def apply(latest_root):
        validate_source_locations(latest_root)
        for field, relative in (("checkpoint", CHECKPOINT), ("master", MASTER)):
            if frozen["baseline_present"][field] and not (latest_root / relative).is_file():
                raise ValueError(f"Latest official {field} disappeared")
        latest = {"checkpoint": read_json(latest_root / CHECKPOINT, optional=True),
                  "master": read_json(latest_root / MASTER, optional=True)}
        if (latest_root / CHECKPOINT).is_file():
            _validate_checkpoint(latest["checkpoint"], allow_empty=False)
        checkpoint = merge_official_checkpoint(latest["checkpoint"],
            frozen["baseline"]["checkpoint"], frozen["observed"]["checkpoint"])
        master = merge_master(latest["master"], frozen["baseline"]["master"],
                              frozen["observed"]["master"])
        write_json(latest_root / CHECKPOINT, checkpoint)
        write_json(latest_root / MASTER, master)
        dashboard_path = latest_root / DASHBOARD
        previous_dashboard = dashboard_path.read_bytes() if dashboard_path.is_file() else None
        temporary = dashboard_path.with_suffix(dashboard_path.suffix + ".tmp")
        previous_temporary = temporary.read_bytes() if temporary.is_file() else None
        try:
            render(latest_root, observed_at)
        except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired) as exc:
            # The dashboard is optional derived output. Its failure must not
            # discard actual observations or extend/recalculate their cooldown.
            if previous_dashboard is None:
                (latest_root / DASHBOARD).unlink(missing_ok=True)
            else:
                (latest_root / DASHBOARD).write_bytes(previous_dashboard)
            if previous_temporary is None:
                temporary.unlink(missing_ok=True)
            else:
                temporary.write_bytes(previous_temporary)
            print("SCHEDULER_QUEUE_STATUS_EXPORT_FAILED", type(exc).__name__, flush=True)

    receipt = publish_with_retry(
        repository or SubprocessGitRepository(root, disposable_checkout=True),
        apply, paths=tuple(PATHS.values()),
        message="state: save official Followers checkpoint and verified master",
        input_revision=frozen["input_revision"], payload_revision=revision,
        max_attempts=max_attempts,
    )
    write_json(receipt_path, receipt.to_dict())
    return receipt


def capture_pending(root, baseline, observed, *, now=None, input_revision=None, baseline_present=None):
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Checkpoint publication requires an aware clock")
    batch = {"schema_version": 1, "kind": "official-checkpoint", "paths": dict(PATHS),
             "input_revision": input_revision or snapshot_revision(baseline),
             "baseline": deepcopy(baseline), "observed": deepcopy(observed),
             "baseline_present": dict(baseline_present or {"checkpoint": True, "master": True}),
             "observed_at": clock.isoformat(), "recovery_eligible": True,
             "acknowledged_revision": None}
    _validate_pending(batch)
    return batch


def baseline_from_head(root, *, return_presence=False, revision="HEAD"):
    root = Path(root).resolve()
    baseline = {}
    # First validate HEAD itself. A Git read error cannot be represented as an
    # optional absent source, nor may a directory/symlink blob act as state.
    if revision != "HEAD" and (not isinstance(revision, str) or len(revision) not in {40, 64}
                              or any(char not in "0123456789abcdef" for char in revision)):
        raise ValueError("Recovery baseline requires a Git revision")
    subprocess.check_output(["git", "rev-parse", "--verify", revision], cwd=root,
                            text=True, timeout=20)
    present = {}
    for field, path in (("checkpoint", CHECKPOINT), ("master", MASTER)):
        entry = subprocess.check_output(["git", "ls-tree", revision, "--", path], cwd=root,
                                        text=True, timeout=20).strip()
        present[field] = bool(entry)
        if not entry:
            baseline[field] = {}
        else:
            if not entry.startswith("100644 blob ") and not entry.startswith("100755 blob "):
                raise ValueError("Official HEAD source must be a regular JSON blob")
            raw = subprocess.check_output(["git", "show", f"{revision}:{path}"], cwd=root,
                                          text=True, timeout=20)
            baseline[field] = strict_json_loads(raw)
    return (baseline, present) if return_presence else baseline


class OfficialCheckpointPersistence:
    """One worker's baseline survives repeated checkpoint acknowledgements."""
    def __init__(self, root, *, pending_path=None, receipt_path=None, clock=None,
                 repository=None, renderer=None):
        self.root = Path(root).resolve()
        validate_source_locations(self.root)
        self.pending_path, self.receipt_path = artifact_paths(self.root,
            pending_path or self.root / PENDING, receipt_path or self.root / RECEIPT)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.repository, self.renderer = repository, renderer
        self.baseline = None
        self.baseline_present = None

    def begin(self, checkpoint, master):
        validate_source_locations(self.root)
        _validate_checkpoint(checkpoint, allow_empty=False)
        merge_master(master, master, master)
        self.baseline_present = {"checkpoint": (self.root / CHECKPOINT).is_file(),
                                 "master": (self.root / MASTER).is_file()}
        self.baseline = deepcopy({
            "checkpoint": checkpoint if (self.root / CHECKPOINT).is_file() else {},
            "master": master if (self.root / MASTER).is_file() else {},
        })

    def persist(self):
        self.pending_path, self.receipt_path = artifact_paths(self.root, self.pending_path, self.receipt_path)
        self.receipt_path.unlink(missing_ok=True)
        if self.baseline is None:
            self.baseline, self.baseline_present = baseline_from_head(self.root, return_presence=True)
        validate_source_locations(self.root)
        observed = {"checkpoint": read_json(self.root / CHECKPOINT, optional=True),
                    "master": read_json(self.root / MASTER, optional=True)}
        batch = capture_pending(self.root, self.baseline, observed, now=self.clock(),
                                baseline_present=self.baseline_present)
        write_json(self.pending_path, batch)
        try:
            receipt = publish_pending(self.root, batch, receipt_path=self.receipt_path,
                                     repository=self.repository, renderer=self.renderer)
        except Exception:
            # refresh may have replaced the files. Preserve the original frozen
            # evidence for a final recovery instead of saving a remote no-op.
            validate_source_locations(self.root)
            write_json(self.root / CHECKPOINT, observed["checkpoint"])
            write_json(self.root / MASTER, observed["master"])
            raise
        self.baseline = {"checkpoint": read_json(self.root / CHECKPOINT),
                         "master": read_json(self.root / MASTER)}
        self.baseline_present = {"checkpoint": True, "master": True}
        batch.update(recovery_eligible=False, acknowledged_revision=receipt.published_revision)
        write_json(self.pending_path, batch)
        return receipt
