"""Publish frozen Steam inputs against the latest public Git snapshot.

Collectors never run in a publication retry. Existing catalog/date gates remain
in the builders; this adapter owns only Git delivery and its acknowledgement.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from radar_core.publication import SubprocessGitRepository, publish_with_retry, snapshot_revision
from radar_backend.domain.growth import timestamp
from radar_backend.adapters.public_shards import build

CATALOG_PATHS = (
    "data/index.json", "data/calendar", "data/games", "data/lists",
    "data/steam_upcoming.json", "data/catalog.json",
)
GROWTH_PATHS = ("data/insights-state.json", "data/activity.json", "data/growth.json")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path, *, optional: bool = False):
    if optional and not path.exists():
        return {}
    def bad_constant(value):
        raise ValueError(f"Non-finite JSON number: {value}")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                       parse_constant=bad_constant)
    snapshot_revision(value)  # Reject values unsupported by the shared contract.
    return value


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def validate_json_paths(root: Path, paths):
    """Existing corrupt files must not be silently replaced by builder defaults."""
    for name in paths:
        path = root / name
        if path.is_dir():
            for item in path.rglob("*.json"):
                read_json(item)
        elif path.exists():
            read_json(path)


def frontend_repository(frontend: Path, *, auth_script: Path | None = None):
    prefix = ("bash", str(auth_script.resolve())) if auth_script else ()
    return SubprocessGitRepository(frontend, push_command_prefix=prefix, disposable_checkout=True)


def require_current_followers(root: Path, source: dict):
    """An older source must not roll back a newer published official count."""
    for incoming in source["games"]:
        if not isinstance(incoming, dict) or type(incoming.get("appid")) is not int:
            raise ValueError("Invalid authoritative Steam AppID")
        path = root / "data/games" / f"{incoming['appid']}.json"
        if not path.exists():
            continue
        previous = read_json(path)
        if (incoming.get("follower_status") == "unavailable_group_id"
                and incoming.get("followers") is None
                and type(previous.get("followers")) is int and previous["followers"] >= 0):
            # The shard merger retains the known numeric evidence as one bundle.
            continue
        old_time = timestamp(previous.get("follower_checked_at"))
        new_time = timestamp(incoming.get("follower_checked_at"))
        if old_time and (new_time is None or new_time < old_time):
            raise ValueError(f"Stale official Followers input for AppID {incoming['appid']}")


def publish_catalog(frontend: Path, source: dict, *, input_revision: str,
                    auth_script: Path | None = None, max_attempts=8, repository=None, now=None):
    frozen = deepcopy(source)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Timezone-aware publication clock required")
    if not isinstance(frozen, dict) or not isinstance(frozen.get("games"), list) or not frozen["games"]:
        raise ValueError("Invalid or empty authoritative Steam catalog")
    revision = snapshot_revision({"steam_master": frozen})
    # The source file lives outside the refreshed target checkout and remains
    # identical across races. Neither fetch/reset nor a retry can recollect it.
    with tempfile.TemporaryDirectory(prefix="radar-steam-catalog-") as scratch:
        source_path = Path(scratch) / "master.json"
        write_json(source_path, frozen)
        def apply(root):
            validate_json_paths(root, (*CATALOG_PATHS, "data/excluded_date_appids.json"))
            require_current_followers(root, frozen)
            build(source_path, root, authoritative_future=True, now=clock)
        return publish_with_retry(
            repository or frontend_repository(frontend, auth_script=auth_script), apply,
            paths=CATALOG_PATHS, message="data: publish consistent Steam catalog",
            input_revision=input_revision, payload_revision=revision, max_attempts=max_attempts,
        )


def publish_growth(frontend: Path, report: dict, *, input_revision: str,
                   auth_script: Path | None = None, max_attempts=5, repository=None, now=None):
    frozen = deepcopy(report)
    if not isinstance(frozen, dict) or not isinstance(frozen.get("measurements"), list):
        raise ValueError("Malformed frozen growth collection result")
    clock = now or (timestamp(frozen["generated_at"]) if frozen.get("generated_at") else datetime.now(timezone.utc))
    if clock is None or clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Timezone-aware growth observation clock required")
    # Delivery metadata does not constitute a new measurement revision.
    payload = {key: value for key, value in frozen.items()
               if key not in {"job_result", "publication_receipt", "checkpoint_receipt"}}
    revision = snapshot_revision(payload)
    with tempfile.TemporaryDirectory(prefix="radar-steam-growth-") as scratch:
        measurements = Path(scratch) / "measurements.json"
        write_json(measurements, payload)
        def apply(root):
            validate_json_paths(root, (*GROWTH_PATHS, "data/catalog.json",
                                      "data/nintendo_upcoming.json", "data/nintendo_refresh_status.json"))
            subprocess.run([sys.executable, "-B", str(root / "scripts/build_radar_insights.py"),
                            "--data-dir", str(root / "data"), "--measurements", str(measurements),
                            "--observed-at", clock.isoformat()],
                           check=True, capture_output=True, text=True,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return publish_with_retry(
            repository or frontend_repository(frontend, auth_script=auth_script), apply,
            paths=GROWTH_PATHS,
            message="data: record official growth observations through release plus 30 days",
            input_revision=input_revision, payload_revision=revision, max_attempts=max_attempts,
        )
