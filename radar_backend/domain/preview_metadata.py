"""Pure Store appdetails projection and preview title selection."""
from __future__ import annotations

from typing import Any


PLAYER_CATEGORY_SOURCE = "Steam Store appdetails cc=TW categories"


def app_details_from_response(appid: int, data: dict[str, Any] | None) -> dict[str, Any] | None:
    item = (data or {}).get(str(appid), {})
    if isinstance(item, dict) and item.get("success") and isinstance(item.get("data"), dict):
        return item["data"]
    return None


def localized_names(
    english_details: dict[str, Any], traditional_details: dict[str, Any] | None,
) -> tuple[str, str | None]:
    english_name = str(english_details.get("name") or "").strip()
    traditional_name = str((traditional_details or {}).get("name") or "").strip()
    return english_name, traditional_name if traditional_name and traditional_name != english_name else None


def normalized_metadata_fields(
    appid: int, details: dict[str, Any], followers: int | None,
    checked_at: str | None, release: dict[str, Any],
) -> dict[str, Any]:
    return {
        "appid": appid,
        "name": str(details.get("name") or f"Steam App {appid}"),
        "name_en": str(details.get("name") or f"Steam App {appid}"),
        "name_zh_tw": None,
        **release,
        "followers": followers,
        "follower_checked_at": checked_at,
        "capsule_image": details.get("capsule_image") or details.get("header_image"),
        "header_image": details.get("header_image"),
        "store_url": f"https://store.steampowered.com/app/{appid}/",
    }


def has_official_player_categories(appid: int, details: dict[str, Any]) -> bool:
    return (
        details.get("type") == "game"
        and isinstance(details.get("steam_appid"), int)
        and not isinstance(details["steam_appid"], bool)
        and details["steam_appid"] == appid
        and isinstance(details.get("categories"), list)
    )
