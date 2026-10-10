"""Title rules for games already present in the published Steam shards."""
from __future__ import annotations

import re
from typing import Callable

from radar_core.domain.twitch_admission import (
    UNKNOWN_FOLLOWER_FIELDS, aware_time, decimal_id, has_unavailable_group_followers,
    preserve_follower_measurement,
)


HAN = re.compile(r"[\u3400-\u9fff]")
TWITCH_PUBLISHED_FIELDS = (
    "followers", "follower_checked_at", "follower_source", "official_ge5000",
    "group_id64", "follower_status", "follower_unavailable_at", "steam_type", "sexual_content_screened",
    "release_start", "release_end", "release_precision", "release_display_precision",
    "release_date_timezone", "release_time_utc", "release_timestamp_taipei_date",
    "release_date_conflict", "release_store_date", "release_date_normalization",
    "release_display_provider", "release_date_verified_at",
)


def candidate(value: object, english: str, *, han=HAN) -> str | None:
    """Reject the Store's English fallback without inventing a translation."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or name.casefold() == english.strip().casefold():
        return None
    if len(name) > 240 or not han.search(name):
        return None
    return name


def update_title(
    row: dict, tw: str | None, cn: str | None, *, convert: Callable,
) -> dict:
    """Keep raw titles and existing display values while refreshing conversion."""
    out = dict(row)
    if tw:
        out["name_zh_tw"] = tw
    if cn:
        out["name_zh_cn"] = cn
    for original, converted in (
        ("name_zh_tw", "name_zh_tw_traditional"),
        ("name_zh_cn", "name_zh_cn_traditional"),
    ):
        name = out.get(original)
        if isinstance(name, str) and name.strip():
            out[converted] = convert(name.strip())

    english = str(out.get("name_en") or out.get("name") or f"Steam App {out['appid']}")
    title_tw = out.get("name_zh_tw_traditional")
    title_cn = out.get("name_zh_cn_traditional")
    out["display_name"] = title_tw or title_cn or english
    out["display_name_source"] = (
        "tchinese" if title_tw else
        "schinese_converted" if title_cn else "english"
    )
    out["storage_version"] = 2
    return out


def preserve_published_twitch_fields(old: dict, merged: dict) -> None:
    """Retain the accepted facts after the caller verifies Twitch admission."""
    old = preserve_follower_measurement(merged, old)
    if (decimal_id(old.get("appid")) == decimal_id(merged.get("appid"))
            and has_unavailable_group_followers(old) and has_unavailable_group_followers(merged)
            and aware_time(merged["follower_unavailable_at"]) > aware_time(old["follower_unavailable_at"])):
        # The detail may already contain a newer verified Twitch snapshot.
        # Its proof survives composition; its missing-group observation must
        # survive too, rather than predating that proof after a title refresh.
        old = {**old, **{field: merged[field] for field in UNKNOWN_FOLLOWER_FIELDS}}
    for field in TWITCH_PUBLISHED_FIELDS:
        if field in old:
            merged[field] = old[field]
        else:
            merged.pop(field, None)
