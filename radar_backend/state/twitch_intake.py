"""JSON storage and conflict-aware application of verified Twitch intake evidence."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path


def read_json(path: Path, *, optional: bool = False, json_module=json) -> dict:
    if optional and not path.exists():
        return {}
    value = json_module.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return value


def write_json(path: Path, value: dict, *, json_module=json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json_module.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def apply_batch(master: dict, state: dict, batch: dict, *,
                excluded_appids, is_twitch_qualified, is_disallowed,
                preserve_twitch_admission, keep_newer_release,
                aware_time, decimal_id, deepcopy_fn=deepcopy) -> tuple[dict, dict]:
    """Merge only verified records and newer per-AppID updates against latest main."""
    if batch.get("schema_version") != 1 or not isinstance(batch.get("records"), list) or not isinstance(batch.get("state_updates"), dict):
        raise ValueError("Malformed import batch")
    if not isinstance(master.get("games"), list) or not isinstance(state.get("games", {}), dict):
        raise ValueError("Malformed master/import state")
    result = deepcopy_fn(master)
    merged_state = deepcopy_fn(state)
    by_id = {int(row["appid"]): deepcopy_fn(row) for row in result["games"]}
    blocked = excluded_appids()
    for row in batch["records"]:
        if not is_twitch_qualified(row) or is_disallowed(row, blocked):
            raise ValueError("Batch contains unqualified Steam admission")
        appid = int(row["appid"])
        current = by_id.get(appid, {})
        if current and (is_disallowed(current, blocked) or current.get("sexual_content_screened") is False):
            continue
        candidate = preserve_twitch_admission(current, keep_newer_release(current, row))
        current_follower_time = aware_time(current.get("follower_checked_at"))
        if current_follower_time is not None and current_follower_time > aware_time(candidate["follower_checked_at"]):
            for key in ("followers", "follower_checked_at", "follower_source", "official_ge5000"):
                if key in current:
                    candidate[key] = current[key]
        merged = {**current, **candidate}
        if not is_twitch_qualified(merged) or is_disallowed(merged, blocked):
            # A concurrent authoritative date/adult update wins; do not overwrite it.
            continue
        by_id[appid] = merged
    if batch["records"]:
        result["games"] = sorted(by_id.values(), key=lambda row: (row.get("release_start") or "", int(row["appid"])))
    if result["games"] != master["games"]:
        result["updated_at"] = batch["generated_at"]
    merged_state.setdefault("schema_version", 1)
    updates = merged_state.setdefault("games", {})
    for aid, update in batch["state_updates"].items():
        if decimal_id(aid) is None or not isinstance(update, dict) or aware_time(update.get("updated_at")) is None:
            raise ValueError("Malformed per-AppID import state update")
        current = updates.get(aid, {})
        previous_time = aware_time(current.get("updated_at"))
        if previous_time is not None and previous_time > aware_time(update["updated_at"]):
            continue
        merged = {**current, **deepcopy_fn(update)}
        if "content_dispatch" not in update and current.get("content_signature") != update.get("content_signature"):
            merged.pop("content_dispatch", None)
        updates[aid] = merged
    for stage, cooldown in batch.get("cooldown_updates", {}).items():
        if stage not in {"steam_store_browse", "steam_appdetails"}:
            raise ValueError("Malformed Steam cooldown stage")
        until, updated = aware_time(cooldown.get("retry_at")), aware_time(cooldown.get("updated_at"))
        if until is None or updated is None:
            raise ValueError("Malformed Steam cooldown deadline")
        current = merged_state.setdefault("api_cooldowns", {}).get(stage, {})
        previous_updated = aware_time(current.get("updated_at"))
        if previous_updated is None or updated >= previous_updated:
            # A concurrent longer cooldown must not be shortened by an older
            # batch, even when both were measured within the same timestamp.
            previous_until = aware_time(current.get("retry_at"))
            if previous_until is not None and previous_until > until:
                cooldown = {**cooldown, "retry_at": current["retry_at"]}
            merged_state["api_cooldowns"][stage] = deepcopy_fn(cooldown)
    if merged_state != state:
        merged_state["updated_at"] = batch["generated_at"]
    return result, merged_state

