"""The existing browser catalog fields and full-row revision contract."""
from __future__ import annotations

import hashlib
import json

from radar_backend.domain.catalog_metadata import PLAYER_CATEGORY_FIELDS


FIELDS = (
    'appid', 'name', 'name_en', 'display_name', 'name_zh_tw', 'name_zh_cn',
    'name_zh_tw_traditional', 'name_zh_cn_traditional', 'name_en_traditional',
    'language_support', 'release_start', 'release_end', 'release_precision',
    'release_display_precision', 'release_date_timezone', 'followers',
    'follower_checked_at', 'recent_source', 'first_week_qualified_at',
    'header_image', 'header_image_2x', 'main_capsule_image', 'main_capsule_image_2x',
    'small_capsule_image', 'capsule_image', 'tags', 'genres',
    'tag_ids', 'tag_labels_zh_tw', 'genre_labels_zh_tw',
    'content_enriched_at', 'tags_fetch_status',
    *PLAYER_CATEGORY_FIELDS,
    'twitch_admission', 'steam_type', 'sexual_content_screened',
    'release_time_utc', 'release_timestamp_taipei_date', 'release_date_conflict',
    'release_store_date', 'release_date_normalization',
    'release_display_provider', 'release_date_verified_at',
)


def catalog_revision(rows: list[dict], *, json_module=json, hashlib_module=hashlib) -> str:
    """Hash complete accepted rows, including fields omitted from the projection."""
    canonical = json_module.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    )
    return hashlib_module.sha256(canonical.encode()).hexdigest()[:20]


def catalog_payload(
    rows: list[dict], generated_at: str, revision: str, *, fields=FIELDS,
) -> dict:
    return {
        'version': 3, 'revision': revision, 'generated_at': generated_at,
        'count': len(rows),
        'games': [{key: row[key] for key in fields if key in row} for row in rows],
    }
