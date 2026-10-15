"""Three-way replay of frozen private state without overwriting other writers."""
from __future__ import annotations

from copy import deepcopy
import json

from radar_core.publication import snapshot_revision

from radar_backend.domain.official_queue import aware_time


class MergeConflict(ValueError):
    """The same evidence changed in two different ways."""


_MISSING = object()


def strict_json_loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    def constant(value):
        raise ValueError(f"Non-finite JSON number: {value}")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    snapshot_revision(value)
    return value


def _merge(latest, baseline, observed, path):
    if observed == baseline:
        return deepcopy(latest) if latest is not _MISSING else _MISSING
    if latest == baseline or latest == observed:
        return deepcopy(observed) if observed is not _MISSING else _MISSING
    if isinstance(latest, dict) and isinstance(observed, dict) and (
        isinstance(baseline, dict) or baseline is _MISSING
    ):
        before = {} if baseline is _MISSING else baseline
        merged = {}
        for key in sorted(set(latest) | set(before) | set(observed)):
            value = _merge(latest.get(key, _MISSING), before.get(key, _MISSING),
                           observed.get(key, _MISSING), f"{path}.{key}")
            if value is not _MISSING:
                merged[key] = value
        return merged
    raise MergeConflict(f"Concurrent state conflict at {path}")


def merge_json_three_way(latest, baseline, observed):
    """Apply only baseline-to-observed deltas; incompatible concurrent edits fail."""
    if any(not isinstance(value, dict) for value in (latest, baseline, observed)):
        raise ValueError("Three-way state must contain JSON objects")
    return _merge(latest, baseline, observed, "state")


def _games(value):
    rows = value.get("games", [])
    if not isinstance(rows, list):
        raise ValueError("Master games must be a list")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Master game must be an object")
        appid = row.get("appid")
        if isinstance(appid, bool) or not isinstance(appid, (int, str)):
            raise ValueError("Master game requires an AppID")
        key = str(appid)
        if not key.isascii() or not key.isdigit() or int(key) <= 0 or str(int(key)) != key:
            raise ValueError("Master game requires a canonical positive AppID")
        if key in result:
            raise ValueError("Master game AppIDs must be unique")
        result[key] = deepcopy(row)
    return result


def merge_master(latest, baseline, observed):
    """Merge game rows by AppID; newer unrelated rows and metadata survive."""
    if any(not isinstance(value, dict) for value in (latest, baseline, observed)):
        raise ValueError("Master state must be an object")
    remote_games, before_games, new_games = (_games(value) for value in (latest, baseline, observed))
    # An old worker can complete after a newer producer has verified this row.
    # Preserve that entire newer evidence bundle rather than mixing old/new
    # follower sources, dates, admission signatures and eligibility fields.
    for key, row in list(new_games.items()):
        remote = remote_games.get(key)
        if remote is None or row == before_games.get(key) or remote == before_games.get(key):
            continue
        remote_time = aware_time(remote.get("follower_checked_at") or remote.get("official_checked_at_taipei"))
        new_time = aware_time(row.get("follower_checked_at") or row.get("official_checked_at_taipei"))
        remote_known = type(remote.get("followers")) is int and remote["followers"] >= 0
        incoming_unknown = row.get("follower_status") == "unavailable_group_id" and row.get("followers") is None
        if (remote_known and incoming_unknown) or (remote_time is not None and new_time is not None and remote_time > new_time):
            new_games[key] = deepcopy(remote)
    games = merge_json_three_way(remote_games, before_games, new_games)
    metadata = [{key: deepcopy(item) for key, item in value.items() if key not in {"games", "count"}}
                for value in (latest, baseline, observed)]
    resolved_times = {}
    for field in ("updated_at", "post_followers_store_gate_checked_at"):
        remote, before, new = (value.get(field, _MISSING) for value in metadata)
        if (new is not _MISSING and remote is not _MISSING and new != before
                and remote != before and new != remote):
            remote_time, new_time = aware_time(remote), aware_time(new)
            if remote_time is None or new_time is None:
                raise MergeConflict(f"Malformed concurrent master time {field}")
            resolved_times[field] = remote if remote_time >= new_time else new
            for value in metadata:
                value.pop(field, None)
    merged = merge_json_three_way(*metadata)
    merged.update(resolved_times)
    merged["games"] = sorted(games.values(), key=lambda row: (
        str(row.get("release_start") or "9999-12-31"),
        -(row.get("followers") if type(row.get("followers")) is int else 0),
        int(row["appid"]),
    ))
    if any("count" in value for value in (latest, baseline, observed)):
        merged["count"] = len(merged["games"])
    return merged
