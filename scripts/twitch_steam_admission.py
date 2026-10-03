"""Persistent, explicit Twitch admission shared by both Steam publishers.

This is a separate admission source, not a replacement Followers threshold.
Only the importer constructs it after checking the immutable Twitch registry.
Keep this module identical in the main and content backend repositories.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

METHOD = "twitch_igdb_external_steam_v1"
EVIDENCE_SOURCES = frozenset({
    "igdb_first_release_date", "twitch_original_release_date", "twitch_directory_dom",
})
TAIPEI = ZoneInfo("Asia/Taipei")
TW_STORE_DATE_AUTHORITY = "steam_taiwan_store_date_authoritative"
TW_STORE_DATE_PROVIDER = "Steam Store appdetails cc=TW l=tchinese"


def decimal_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    text = str(value)
    return text if re.fullmatch(r"[1-9][0-9]*", text, flags=re.ASCII) else None


def aware_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def resolve_store_release_day(store_day: object, release_time_utc: object, *,
                              allow_taiwan_store_authority: bool = False) -> dict | None:
    """Keep a verified TW display day when the two official date sources differ."""
    instant = aware_time(release_time_utc)
    if instant is None or not isinstance(store_day, str):
        return None
    try:
        announced = date.fromisoformat(store_day)
    except ValueError:
        return None
    if announced.isoformat() != store_day:
        return None
    utc_day = instant.astimezone(timezone.utc).date()
    taipei_day = instant.astimezone(TAIPEI).date()
    if announced == taipei_day:
        basis = "steam_store_date_matches_taipei"
    elif announced == utc_day and taipei_day == utc_day + timedelta(days=1):
        basis = "steam_utc_date_normalized_to_taipei"
    elif allow_taiwan_store_authority:
        return {"release_start": store_day, "release_store_date": store_day,
                "release_date_normalization": TW_STORE_DATE_AUTHORITY}
    else:
        return None
    return {"release_start": taipei_day.isoformat(), "release_store_date": store_day,
            "release_date_normalization": basis}


def has_taiwan_store_date_authority(row: object) -> bool:
    """Require the Twitch identity and a self-consistent official TW date audit."""
    if not has_twitch_admission(row):
        return False
    instant = aware_time(row.get("release_time_utc"))
    day = row.get("release_store_date")
    try:
        exact_day = isinstance(day, str) and date.fromisoformat(day).isoformat() == day
    except ValueError:
        return False
    if instant is None or not exact_day:
        return False
    timestamp_day = instant.astimezone(TAIPEI).date().isoformat()
    return (
        row.get("release_date_normalization") == TW_STORE_DATE_AUTHORITY
        and row.get("release_display_provider") == TW_STORE_DATE_PROVIDER
        and aware_time(row.get("release_date_verified_at")) is not None
        and row.get("release_start") == row.get("release_end") == day
        and row.get("release_precision") == "day"
        and row.get("release_display_precision") == "date_full"
        and row.get("release_date_timezone") == "Asia/Taipei"
        and row.get("release_timestamp_taipei_date") == timestamp_day
        and type(row.get("release_date_conflict")) is bool
        and row["release_date_conflict"] == (day != timestamp_day)
    )


def valid_enrollment(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("source") in EVIDENCE_SOURCES
        and aware_time(value.get("observed_at")) is not None
        and type(value.get("viewer_count")) is int
        and value["viewer_count"] >= 7000
        and type(value.get("min_viewers")) is int
        and value["min_viewers"] == 7000
        and value.get("qualification") != "unverified"
    )


def normalize_twitch_admission(proof: object, appid: object) -> dict | None:
    aid = decimal_id(appid)
    if not isinstance(proof, dict) or aid is None:
        return None
    if (
        type(proof.get("schema_version")) is not int
        or proof["schema_version"] != 1
        or proof.get("method") != METHOD
        or decimal_id(proof.get("appid")) != aid
        or decimal_id(proof.get("twitch_game_id")) is None
        or decimal_id(proof.get("igdb_id")) is None
        or aware_time(proof.get("checked_at")) is None
        or not isinstance(proof.get("source_frontend_commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", proof["source_frontend_commit"]) is None
        or not valid_enrollment(proof.get("source_enrollment"))
    ):
        return None
    if aware_time(proof["source_enrollment"]["observed_at"]) > aware_time(proof["checked_at"]):
        return None
    return {
        "schema_version": 1, "method": METHOD, "appid": int(aid),
        "twitch_game_id": decimal_id(proof["twitch_game_id"]),
        "igdb_id": decimal_id(proof["igdb_id"]),
        "checked_at": proof["checked_at"],
        "source_frontend_commit": proof["source_frontend_commit"],
        "source_enrollment": deepcopy(proof["source_enrollment"]),
    }


def has_twitch_admission(row: object) -> bool:
    return isinstance(row, dict) and normalize_twitch_admission(
        row.get("twitch_admission"), row.get("appid")
    ) is not None


def validate_twitch_snapshot(proof: object, registry: dict, discovery: dict,
                            now: datetime | None = None) -> bool:
    """Verify the producer's links and enrollment in the proof's immutable SHA."""
    if not isinstance(proof, dict):
        return False
    proof = normalize_twitch_admission(proof, proof.get("appid"))
    if proof is None or registry.get("schema_version") != 1 or discovery.get("schema_version") != 1:
        return False
    if not isinstance(registry.get("games"), dict) or not isinstance(discovery.get("games"), dict):
        return False
    gid, igdb, aid = proof["twitch_game_id"], proof["igdb_id"], str(proof["appid"])
    entry, row = registry["games"].get(gid), discovery["games"].get(gid)
    if not isinstance(entry, dict) or not isinstance(row, dict):
        return False
    source = (entry.get("tracking_sources") or {}).get("twitch_new")
    if not isinstance(source, dict):
        return False
    expiry = aware_time(source.get("expires_at"))
    if source.get("expires_at") is not None and expiry is None:
        return False
    clock = now or aware_time(proof["checked_at"])
    if expiry is not None and expiry <= clock:
        return False
    source_id = decimal_id(discovery.get("steam_source_id"))
    if (
        decimal_id(entry.get("game_id")) != gid
        or source.get("source") != "twitch_new" or source.get("status") != "active"
        or source.get("enrollment") != proof["source_enrollment"]
        or decimal_id(entry.get("igdb_id") or (entry.get("last_observation") or {}).get("igdb_id")) != igdb
        or row.get("status") != "matched" or row.get("active") is not True
        or row.get("method") != METHOD or decimal_id(row.get("twitch_game_id")) != gid
        or decimal_id(row.get("igdb_id")) != igdb
        or row.get("twitch_enrollment") != proof["source_enrollment"]
        or row.get("checked_at") != proof["checked_at"]
        or source_id is None or not isinstance(row.get("steam_appids"), list)
        or not isinstance(row.get("links"), list)
    ):
        return False
    declared = {decimal_id(value) for value in row["steam_appids"]}
    if None in declared or aid not in declared:
        return False
    linked = set()
    for link in row["links"]:
        if not isinstance(link, dict):
            return False
        appid = decimal_id(link.get("steam_appid"))
        if (appid not in declared or decimal_id(link.get("uid")) != appid
                or decimal_id(link.get("external_game_id")) is None
                or decimal_id(link.get("external_game_source")) != source_id
                or decimal_id(link.get("game")) != igdb):
            return False
        linked.add(appid)
    return linked == declared


def is_twitch_qualified(row: object) -> bool:
    """Require the same exact-date/content proof after admission is persisted."""
    if not has_twitch_admission(row):
        return False
    instant = aware_time(row.get("release_time_utc"))
    day = row.get("release_start")
    consistent_timestamp_day = (
        row.get("release_date_conflict") is not True
        and instant is not None
        and instant.astimezone(TAIPEI).date().isoformat() == day
        and row.get("release_timestamp_taipei_date", day) == day
    )
    return (
        row.get("steam_type") == "game"
        and row.get("sexual_content_screened") is True
        and row.get("release_precision") == "day"
        and row.get("release_display_precision") == "date_full"
        and row.get("release_date_timezone") == "Asia/Taipei"
        and (consistent_timestamp_day or has_taiwan_store_date_authority(row))
        and row.get("release_end") == day
        and type(row.get("followers")) is int
        and row["followers"] >= 0
        and aware_time(row.get("follower_checked_at")) is not None
    )


def preserve_twitch_admission(existing: dict, incoming: dict) -> dict:
    """A normal Followers refresh cannot remove a separately accepted source."""
    result = dict(incoming)
    prior = normalize_twitch_admission(existing.get("twitch_admission"), existing.get("appid"))
    if prior is not None and decimal_id(existing.get("appid")) == decimal_id(incoming.get("appid")):
        newer = normalize_twitch_admission(incoming.get("twitch_admission"), incoming.get("appid"))
        if newer is None or aware_time(prior["checked_at"]) > aware_time(newer["checked_at"]):
            result["twitch_admission"] = prior
        for key in ("steam_type", "sexual_content_screened"):
            if key not in result and key in existing:
                result[key] = existing[key]
    return result
