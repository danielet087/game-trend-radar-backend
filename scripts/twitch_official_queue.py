"""Merge validated Twitch discoveries into the existing official Followers queue.

No Community request is made here.  Queue metadata is separate from verified
Followers results; retaining a discovery never invents a numeric result.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from scripts.screen_steam_candidates_before_followers import is_explicit_sex_game
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.twitch_steam_admission import (
    aware_time, decimal_id, is_twitch_qualified, normalize_twitch_admission,
    resolve_store_release_day, has_taiwan_store_date_authority,
)


TAIPEI = ZoneInfo("Asia/Taipei")
TWITCH_QUEUE_SOURCE = "twitch_steam_discovery"
TWITCH_QUEUE_PRIORITY = 100
WITHDRAW_REASONS = frozenset({
    "adult_content", "not_a_steam_game", "outside_new_game_window",
    "uncertain_steam_date", "uncertain_taiwan_store_date", "steam_date_conflict",
    "steam_identity_mismatch", "invalid_admission",
})
FOLLOWER_FIELDS = frozenset({
    "followers", "follower_checked_at", "follower_source", "official_ge5000",
    "official_followers", "official_checked_at_taipei",
})


def _now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Queue synchronization requires an aware time")
    return value


def _exact_day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        result = date.fromisoformat(value)
    except ValueError:
        return None
    return result if result.isoformat() == value else None


def _valid_descriptors(metadata: dict) -> bool:
    # The importer stores the official appdetails/Browse union as integer IDs.
    # A malformed descriptor list cannot become "no adult descriptors".
    values = metadata.get("content_descriptorids")
    if not isinstance(values, list) or any(type(value) is not int or value < 0 for value in values):
        return False
    alternate = metadata.get("content_descriptors")
    if alternate is not None:
        if isinstance(alternate, dict):
            alternate = alternate.get("ids")
        if not isinstance(alternate, list) or any(
            type(value) is not int or value < 0 for value in alternate
        ):
            return False
        if is_disallowed({"appid": metadata["appid"], "content_descriptorids": alternate}, set()):
            return False
    return True


def is_twitch_queue_candidate(row: object, now: datetime | None = None) -> bool:
    """Check eligibility for priority without claiming an official count."""
    if not isinstance(row, dict) or row.get("queue_source") != TWITCH_QUEUE_SOURCE:
        return False
    aid = decimal_id(row.get("appid"))
    proof = normalize_twitch_admission(row.get("twitch_admission"), aid)
    metadata = row.get("steam_candidate")
    day = _exact_day(row.get("release_date"))
    if (aid is None or proof is None or not isinstance(metadata, dict) or day is None
            or decimal_id(metadata.get("appid")) != aid
            or metadata.get("release_start") != day.isoformat()
            or row.get("steam_url") != f"https://store.steampowered.com/app/{aid}/"
            or metadata.get("store_url") != row["steam_url"]
            or not _valid_descriptors(metadata)):
        return False
    if "twitch_admission" in metadata:
        inner = normalize_twitch_admission(metadata["twitch_admission"], aid)
        if inner is None or inner != proof:
            return False
    if metadata.get("release_store_date") is not None and not has_taiwan_store_date_authority(metadata):
        normalized = resolve_store_release_day(metadata["release_store_date"],
                                               metadata.get("release_time_utc"))
        if (normalized is None or normalized["release_start"] != day.isoformat()
                or metadata.get("release_date_normalization", normalized["release_date_normalization"])
                   != normalized["release_date_normalization"]):
            return False
    if is_disallowed(metadata, excluded_appids()):
        return False
    if metadata.get("basic_info") is not None and not isinstance(metadata["basic_info"], dict):
        return False
    if metadata.get("tags") is not None and not isinstance(metadata["tags"], list):
        return False
    try:
        if is_explicit_sex_game(metadata):
            return False
    except (TypeError, ValueError):
        return False
    checked = aware_time(proof["checked_at"])
    verified = aware_time(metadata.get("release_date_verified_at"))
    if verified is None:
        return False
    if now is not None:
        now = _now(now)
        today = now.astimezone(TAIPEI).date()
        if (checked > now or verified > now
                or not today - timedelta(days=30) <= day <= today + timedelta(days=365)):
            return False
    # The count exists only in this temporary eligibility object.  It is never
    # returned, persisted in the queue, or published as a verified count.
    eligibility = {**metadata, "appid": int(aid), "twitch_admission": proof,
                   "followers": 0, "follower_checked_at": proof["checked_at"]}
    return is_twitch_qualified(eligibility)


def _restore_normal(pending: dict, aid: str, row: dict) -> None:
    normal = row.get("normal_candidate")
    if (isinstance(normal, dict) and decimal_id(normal.get("appid")) == aid
            and normal.get("queue_source") != TWITCH_QUEUE_SOURCE):
        pending[aid] = deepcopy(normal)
        if decimal_id(row.get("group_id64")) is not None:
            pending[aid]["group_id64"] = row["group_id64"]
        if isinstance(row.get("group_resolution"), dict):
            pending[aid]["group_resolution"] = deepcopy(row["group_resolution"])
    else:
        pending.pop(aid, None)


def sync_twitch_queue(checkpoint: dict, batch: dict, now: datetime) -> dict:
    """Add/deduplicate priority rows and withdraw expired Twitch admission.

    A normal pending row is saved under normal_candidate while priority is
    active, so expiration restores the original queue source and metadata.
    Existing verified results, attempt events and progress are left intact.
    """
    _now(now)
    if not isinstance(checkpoint, dict) or not isinstance(batch, dict):
        raise ValueError("Checkpoint and importer batch must be objects")
    result = deepcopy(checkpoint)
    pending = result.setdefault("pending_candidates", {})
    if not isinstance(pending, dict):
        raise ValueError("Pending candidates must be indexed by AppID")
    active = batch.get("active_twitch_appids")
    if active is not None:
        if not isinstance(active, list) or any(decimal_id(aid) is None for aid in active):
            raise ValueError("Active Twitch AppIDs must be a complete valid list")
        active = {decimal_id(aid) for aid in active}
    updates = batch.get("state_updates") or {}
    if not isinstance(updates, dict):
        raise ValueError("Importer state updates must be indexed by AppID")
    withdrawn = {
        str(aid) for aid, state in updates.items()
        if isinstance(state, dict) and (
            state.get("status") in {"excluded", "accepted"}
            or state.get("reason") in WITHDRAW_REASONS
        )
    }
    incoming = batch.get("follower_candidates") or []
    if not isinstance(incoming, list):
        raise ValueError("Follower candidates must be a list")
    eligible = {
        decimal_id(candidate["appid"]): candidate for candidate in incoming
        if is_twitch_queue_candidate(candidate, now)
    }
    for aid, row in list(pending.items()):
        if not isinstance(row, dict) or row.get("queue_source") != TWITCH_QUEUE_SOURCE:
            continue
        if (str(aid) in withdrawn or str(aid) not in eligible
                or (active is not None and str(aid) not in active)
                or not is_twitch_queue_candidate(row, now)):
            _restore_normal(pending, str(aid), row)

    for aid, candidate in eligible.items():
        if aid in withdrawn or (active is not None and aid not in active):
            continue
        prior = pending.get(aid)
        new = deepcopy(candidate)
        new["appid"] = int(aid)
        new["twitch_admission"] = normalize_twitch_admission(new["twitch_admission"], aid)
        new["queue_source"] = TWITCH_QUEUE_SOURCE
        new["priority"] = TWITCH_QUEUE_PRIORITY
        new.pop("normal_candidate", None)
        for field in FOLLOWER_FIELDS:
            new.pop(field, None)
            new["steam_candidate"].pop(field, None)
        if isinstance(prior, dict) and decimal_id(prior.get("appid")) == aid:
            if decimal_id(prior.get("group_id64")) is not None:
                new["group_id64"] = prior["group_id64"]
            if isinstance(prior.get("group_resolution"), dict):
                new["group_resolution"] = deepcopy(prior["group_resolution"])
            if prior.get("queue_source") == TWITCH_QUEUE_SOURCE:
                normal = prior.get("normal_candidate")
                if isinstance(normal, dict):
                    new["normal_candidate"] = deepcopy(normal)
            else:
                new["normal_candidate"] = deepcopy(prior)
        pending[aid] = new
    return result
