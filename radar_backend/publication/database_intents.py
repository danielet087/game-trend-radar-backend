"""Commit official content intents before sending external dispatches.

The collector keeps its existing in-memory objects and frozen merge baseline.
Git publication remains a separate acknowledgement; D1 is never consulted here.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from radar_core.publication import snapshot_revision
from radar_backend.publication.official_checkpoint import (
    CHECKPOINT, MASTER, capture_pending, merge_official_checkpoint,
)
from radar_backend.state.official_merge import merge_master, strict_json_loads

REPOSITORY = "danielet087/game-trend-radar-backend"


def _replace(target, value):
    """Retain references held by Store rechecks and the active queue worker."""
    for key in set(target) - set(value):
        del target[key]
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(target.get(key), dict):
            _replace(target[key], item)
        else:
            target[key] = deepcopy(item)


def persist_dispatch_state(publisher, checkpoint, master, *, primary=None):
    if primary is None:
        from radar_core.storage import primary
    if not primary.enabled():
        return None
    if publisher is None or publisher.baseline is None:
        raise ValueError("A loaded official baseline is required before dispatch")
    frozen = deepcopy({"checkpoint": checkpoint, "master": master})
    baseline = deepcopy(publisher.baseline)
    from radar_backend.publication.steam import write_json
    # A DB outage must leave replayable evidence before the first request.
    write_json(publisher.root / CHECKPOINT, frozen["checkpoint"])
    write_json(publisher.root / MASTER, frozen["master"])
    write_json(publisher.pending_path, capture_pending(publisher.root, baseline, frozen,
        now=publisher.clock(), baseline_present=publisher.baseline_present))
    digest = snapshot_revision({"baseline": baseline, "observed": frozen})
    for attempt in range(5):
        head, files = primary.read_files(REPOSITORY, paths=(CHECKPOINT, MASTER))
        latest = {"checkpoint": strict_json_loads(files[CHECKPOINT].decode("utf-8")),
                  "master": strict_json_loads(files[MASTER].decode("utf-8"))}
        merged = {
            "checkpoint": merge_official_checkpoint(latest["checkpoint"], baseline["checkpoint"], frozen["checkpoint"]),
            "master": merge_master(latest["master"], baseline["master"], frozen["master"]),
        }
        changed = {path: (json.dumps(merged[field], ensure_ascii=False, indent=2, sort_keys=True,
                                     allow_nan=False) + "\n").encode("utf-8")
                   for field, path in (("checkpoint", CHECKPOINT), ("master", MASTER))}
        try:
            receipt = primary.write_files(REPOSITORY, changed, base_generation=head["generation"],
                operation_id="steam-content-" + digest, payload_revision=digest)
        except Exception as error:
            if getattr(error, "code", None) != "STALE_GENERATION" or attempt == 4:
                raise
            continue
        if (receipt.get("source_id") != REPOSITORY + "@main"
                or receipt.get("operation_id") != "steam-content-" + digest
                or receipt.get("file_digests") != {path: hashlib.sha256(raw).hexdigest() for path, raw in changed.items()}
                or receipt.get("deleted")):
            raise ValueError("Official database acknowledgement differs from its frozen write")
        _replace(checkpoint, merged["checkpoint"])
        _replace(master, merged["master"])
        # Keep the next ordinary checkpoint relative to the DB-acknowledged
        # state. A Git failure still leaves its separate pending artifact.
        publisher.baseline = deepcopy(merged)
        publisher.baseline_present = {"checkpoint": True, "master": True}
        write_json(publisher.root / CHECKPOINT, checkpoint)
        write_json(publisher.root / MASTER, master)
        return receipt
    raise RuntimeError("Official dispatch persistence exhausted its CAS retries")
