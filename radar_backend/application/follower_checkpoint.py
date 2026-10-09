"""Freeze one follower batch and merge it on each Contents conflict retry."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from radar_backend.domain.follower_checkpoint import CheckpointConflict, merge_follower_records
from radar_backend.state.follower_checkpoint import (
    FollowerCheckpointAcknowledgement, checkpoint_bytes, follower_records, strict_json_loads,
)


@dataclass(frozen=True)
class PersistedFollowerCheckpoint:
    payload: dict[str, Any]
    acknowledgement: FollowerCheckpointAcknowledgement
    attempts: int


def persist_follower_checkpoint(
    frozen_payload: dict[str, Any], *, read_latest: Callable,
    write_merged: Callable, merge_records: Callable = merge_follower_records,
    max_attempts: int = 3,
) -> PersistedFollowerCheckpoint:
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("Checkpoint attempts must be a positive integer")
    # JSON round-trip validates the batch before the first external request.
    frozen = strict_json_loads(checkpoint_bytes(deepcopy(frozen_payload)).decode("utf-8"))
    observed = follower_records(frozen, strict=True)
    if type(frozen.get("version")) is not int or frozen["version"] != 1 or not isinstance(frozen.get("updated_at"), str):
        raise ValueError("Invalid frozen follower checkpoint envelope")
    clock = datetime.fromisoformat(frozen["updated_at"].replace("Z", "+00:00"))
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Frozen checkpoint clock must include an offset")
    for attempt in range(1, max_attempts + 1):
        latest = read_latest()
        merged = deepcopy(latest.metadata)
        merged.update(deepcopy(frozen))
        merged["games"] = merge_records(latest.games, observed)
        try:
            acknowledgement = write_merged(latest, merged)
        except CheckpointConflict:
            if attempt == max_attempts:
                raise
            continue
        return PersistedFollowerCheckpoint(merged, acknowledgement, attempt)
    raise AssertionError("Unreachable checkpoint retry state")
