"""Freeze and deliver private daily progress without recollecting on Git races."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
import os
import subprocess
import sys

from radar_core.publication import SubprocessGitRepository, publish_with_retry, snapshot_revision
from radar_backend.domain.candidates import fresh_state
from radar_backend.publication.steam import read_json, write_json
from radar_backend.state.official_merge import MergeConflict, merge_json_three_way, merge_master
from radar_backend.domain.daily_schedule import TAIPEI, daily_reset_required, daily_slot, schedule_decision

STATE = "data/steam_candidate_state.json"
CATALOG = "data/steam_candidates.json"
PREFILTER = "data/steam_prefilter_state.json"
ELIGIBLE = "data/steam_candidates_eligible.json"
MASTER = "data/steam_upcoming_master.json"
CACHE = "data/steam_followers_cache.json"
DASHBOARD = "data/scheduler_queue_status.json"
CANDIDATE_PATHS = (STATE, CATALOG, PREFILTER, ELIGIBLE)
SOURCE_PATHS = (*CANDIDATE_PATHS, MASTER, CACHE, DASHBOARD)
RESET_PATHS = (STATE, CATALOG, PREFILTER, DASHBOARD)
_DATETIME_TYPE = datetime


def aware_clock(now=None):
    clock = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(clock, _DATETIME_TYPE) or clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Daily publication requires a timezone-aware frozen clock")
    return clock


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"Daily {label} must be a JSON object")
    snapshot_revision(value)
    return value


def _source_path(root: Path, name: str):
    path = root / name
    boundary = root.resolve()
    # write_json writes its sibling temporary file before replace. Checking
    # only the final JSON path cannot prevent that write from following a link.
    for destination in (path, path.with_suffix(path.suffix + ".tmp")):
        if not destination.resolve().is_relative_to(boundary):
            raise ValueError("Daily source path escapes the checkout")
        for item in (destination, *destination.parents):
            if item == boundary:
                break
            if item.is_symlink():
                raise ValueError("Daily source paths must not contain symlinks")
    return path


def _file(root: Path, name: str, *, required=False):
    path = _source_path(root, name)
    if not path.exists():
        if required:
            raise ValueError(f"Daily input is missing: {name}")
        return {"present": False}
    return {"present": True, "value": _object(read_json(path), name)}


def _envelope(value, label):
    if not isinstance(value, dict) or type(value.get("present")) is not bool:
        raise ValueError(f"Malformed daily file envelope: {label}")
    if set(value) != ({"present", "value"} if value["present"] else {"present"}):
        raise ValueError(f"Malformed daily file envelope fields: {label}")
    if value["present"]:
        _object(value["value"], label)
    return value


def _validate_baseline(value):
    _object(value, "baseline")
    if (set(value) != {"schema_version", "input_revision", "captured_at", "files"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValueError("Unsupported daily baseline schema")
    if not isinstance(value["input_revision"], str) or not value["input_revision"].strip():
        raise ValueError("Daily baseline input revision is required")
    if not isinstance(value["captured_at"], str):
        raise ValueError("Daily baseline capture time must be an aware ISO timestamp")
    aware_clock(datetime.fromisoformat(value["captured_at"].replace("Z", "+00:00")))
    if not isinstance(value["files"], dict) or set(value["files"]) != set(SOURCE_PATHS):
        raise ValueError("Daily baseline must identify all source files")
    for name, item in value["files"].items():
        _envelope(item, name)
    if not all(value["files"][name]["present"] for name in (STATE, CATALOG)):
        raise ValueError("Daily candidate state and catalog baseline are required")
    return value


def capture_daily_baseline(root: Path, *, input_revision: str, now=None):
    """Capture collection's initial state before a worker can mutate any file."""
    clock = aware_clock(now)
    value = {"schema_version": 1, "input_revision": input_revision,
             "captured_at": clock.isoformat(),
             "files": {name: _file(root, name, required=name in (STATE, CATALOG)) for name in SOURCE_PATHS}}
    return deepcopy(_validate_baseline(value))


def freeze_daily_state(root: Path, baseline: dict, *, now=None):
    """Read observations once, before the first destructive Git refresh."""
    base = deepcopy(_validate_baseline(baseline))
    clock = aware_clock(now)
    names = [STATE, CATALOG]
    names.extend(name for name in (PREFILTER, ELIGIBLE) if (root / name).exists())
    # Preserve the old workflow's rule: cache/master belong to this delivery
    # only after this runner produced a Followers batch output.
    output = _source_path(root, "output/steam_upcoming.json")
    if output.is_file():
        read_json(output)
        names.extend((CACHE, MASTER))
    observed = {name: _file(root, name, required=True) for name in names}
    value = {"schema_version": 1, "baseline": base, "observed": observed,
             "observed_at": clock.isoformat()}
    _validate_frozen(value)
    return deepcopy(value)


def _validate_frozen(value):
    _object(value, "frozen progress")
    if (set(value) != {"schema_version", "baseline", "observed", "observed_at"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValueError("Unsupported frozen daily progress schema")
    _validate_baseline(value["baseline"])
    if not isinstance(value["observed_at"], str):
        raise ValueError("Daily progress observation time must be an aware ISO timestamp")
    aware_clock(datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00")))
    observed = value["observed"]
    if (not isinstance(observed, dict) or not {STATE, CATALOG}.issubset(observed)
            or not set(observed).issubset(set(SOURCE_PATHS) - {DASHBOARD})
            or ((MASTER in observed) != (CACHE in observed))):
        raise ValueError("Frozen daily progress owns an invalid source scope")
    for name, item in observed.items():
        if not _envelope(item, name)["present"]:
            raise ValueError("Frozen daily progress cannot remove an input file")
    return value


def _dashboard(root: Path, clock: datetime):
    """Retain the previous latest snapshot if the original offline export fails."""
    path = root / DASHBOARD
    previous = path.read_bytes() if path.exists() else None
    if previous is not None:
        _object(read_json(path), DASHBOARD)
    try:
        subprocess.run([sys.executable, "-B", "-m", "radar_backend.jobs.publish_steam",
                        "render-queue-status", "--observed-at", clock.isoformat()],
                       cwd=root, check=True, capture_output=True, text=True,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        _object(read_json(path), DASHBOARD)
    except (subprocess.CalledProcessError, OSError, ValueError):
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previous)
        print("::warning::Queue status export failed; retaining previous snapshot", flush=True)


def publish_daily_reset(root: Path, *, input_revision: str, event_name="",
                        trigger_source="", target_slot="", refresh_today=False,
                        now=None, max_attempts=5, repository=None):
    clock = aware_clock(now)
    proceed, reason = schedule_decision("daily-discovery", event_name, trigger_source,
                                        target_slot, refresh_today, clock)
    if not proceed:
        raise ValueError(f"Daily reset schedule was skipped: {reason}")
    day = clock.astimezone(TAIPEI).date()
    force = event_name == "workflow_dispatch" and trigger_source != "cloudflare"
    initial = _file(root, STATE).get("value", {})
    for name in (CATALOG, PREFILTER, DASHBOARD):
        _file(root, name)
    frozen = {"day": day.isoformat(), "observed_at": clock.isoformat(), "force": force,
              "event_name": event_name, "trigger_source": trigger_source,
              "target_slot": target_slot, "refresh_today": refresh_today,
              "initial_state": initial}

    def apply(latest_root):
        state = _file(latest_root, STATE)["value"] if (latest_root / STATE).exists() else {}
        for name in (CATALOG, PREFILTER, DASHBOARD):
            _file(latest_root, name)
        anchor = state.get("anchor_date")
        if anchor is not None:
            try:
                anchor_day = date.fromisoformat(anchor)
            except (ValueError, TypeError) as exc:
                raise ValueError("Malformed daily candidate anchor") from exc
            if anchor_day > day:
                raise MergeConflict("Refusing to reset a newer daily candidate anchor")
        # Manual force owns the state observed before publication began. If
        # another writer has already changed today's progress, a rejected push
        # must resume that state rather than repeatedly wiping its progress.
        effective_force = force and state == initial
        if daily_reset_required(state, day, force=effective_force):
            state = fresh_state(day, 365)
            write_json(latest_root / CATALOG, {"games": []})
            write_json(latest_root / PREFILTER,
                       {"version": 1, "next_index": 0, "head_next_index": 0,
                        "complete": False, "games": {}})
        state.update(daily_refresh_slot=daily_slot(day), last_reset_date_taipei=day.isoformat())
        write_json(latest_root / STATE, state)
        _dashboard(latest_root, clock)

    return publish_with_retry(repository or SubprocessGitRepository(root, disposable_checkout=True),
                              apply, paths=RESET_PATHS,
                              message="data: persist daily Steam discovery reset before collection",
                              input_revision=input_revision, payload_revision=snapshot_revision(frozen),
                              max_attempts=max_attempts)


def publish_daily_state(root: Path, frozen: dict, *, max_attempts=5, repository=None):
    """Replay one frozen private batch; an unknown progress race fails closed."""
    payload = deepcopy(_validate_frozen(frozen))
    base, observed = payload["baseline"]["files"], payload["observed"]
    clock = aware_clock(datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00")))
    paths = (*observed, DASHBOARD)

    def apply(latest_root):
        latest = {name: _file(latest_root, name, required=name in (STATE, CATALOG))
                  for name in {*paths, *CANDIDATE_PATHS}}
        candidate_names = tuple(name for name in CANDIDATE_PATHS if name in observed)
        # Candidate cursors and their catalogs describe one progress state.
        # Never stitch unrelated concurrent phases into a fabricated handoff.
        if any(observed[name] != base[name] for name in candidate_names):
            for name in CANDIDATE_PATHS:
                if latest[name] != base[name] and latest[name] != observed.get(name):
                    raise MergeConflict(f"Concurrent daily candidate progress: {name}")
        results = {}
        for name, envelope in observed.items():
            if envelope == base[name]:
                # Unchanged optional files do not undo a remote deletion.
                continue
            previous = base[name].get("value", {})
            incoming = envelope["value"]
            current = latest[name].get("value", {})
            merge = merge_master if name == MASTER else merge_json_three_way
            results[name] = merge(current, previous, incoming)
        for name, value in results.items():
            write_json(latest_root / name, value)
        _dashboard(latest_root, clock)

    return publish_with_retry(repository or SubprocessGitRepository(root, disposable_checkout=True),
                              apply, paths=paths,
                              message="data: advance private Steam two-stage initialization",
                              input_revision=payload["baseline"]["input_revision"],
                              payload_revision=snapshot_revision(payload), max_attempts=max_attempts)
