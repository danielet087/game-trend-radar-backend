"""Small browser projection, derived exclusively from accepted AppID records."""
import hashlib
import json
from pathlib import Path

FIELDS = (
    'appid', 'name', 'name_en', 'display_name', 'name_zh_tw', 'name_zh_cn',
    'name_zh_tw_traditional', 'name_zh_cn_traditional', 'name_en_traditional',
    'language_support', 'release_start', 'release_end', 'release_precision',
    'release_display_precision', 'release_date_timezone', 'followers',
    'follower_checked_at', 'recent_source', 'first_week_qualified_at',
    'header_image', 'header_image_2x', 'main_capsule_image', 'main_capsule_image_2x',
    'small_capsule_image', 'capsule_image', 'tags', 'genres',
    'content_enriched_at', 'tags_fetch_status',
)


def write_catalog_projection(data_dir: Path, rows: list[dict], generated_at: str) -> dict:
    # Hash every accepted field, not only the number of games: metadata-only
    # changes must invalidate the release snapshot too.
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    revision = hashlib.sha256(canonical.encode()).hexdigest()[:20]
    path = data_dir / 'catalog.json'
    payload = {
        'version': 3, 'revision': revision, 'generated_at': generated_at,
        'count': len(rows),
        'games': [{key: row[key] for key in FIELDS if key in row} for row in rows],
    }
    if path.exists():
        try:
            if json.loads(path.read_text(encoding='utf-8')).get('revision') == revision:
                return {'catalog_path': 'catalog.json', 'catalog_revision': revision}
        except (OSError, ValueError, TypeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    return {'catalog_path': 'catalog.json', 'catalog_revision': revision}
