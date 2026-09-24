"""Refresh Chinese Store titles for ALREADY published Steam AppIDs only.

Do not scan candidates, touch private Followers state, recheck Followers, or
promote previously unqualified games. Update AppID shards and affected months.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from opencc import OpenCC

STORE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
HAN = re.compile(r"[\u3400-\u9fff]")
CONVERT = OpenCC("s2t")
LOG = logging.getLogger(__name__)


def read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def save_changed(path: Path, data: dict) -> bool:
    output = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == output:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(output, encoding="utf-8")
    return True


def candidate(value: object, english: str) -> str | None:
    """Reject Steam's English fallback. Never infer a translated title."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or name.casefold() == english.strip().casefold():
        return None
    if len(name) > 240 or not HAN.search(name):
        return None
    return name


def localized_batch(
    session: requests.Session, appids: list[int], language: str, interval: float
) -> dict[int, str]:
    payload = {
        "ids": [{"appid": appid} for appid in appids],
        "context": {"country_code": "TW", "language": language, "steam_realm": 1},
        "data_request": {"include_basic_info": True},
    }
    for attempt in range(5):
        try:
            response = session.get(
                STORE,
                params={"input_json": json.dumps(payload, separators=(",", ":"))},
                timeout=45,
            )
            if response.status_code == 429:
                LOG.warning("%s throttled; attempt %d/5", language, attempt + 1)
                time.sleep(15 * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            rows = (data.get("response") or {}).get("store_items") or []
            if not isinstance(rows, list):
                raise ValueError("Invalid Steam store_items")
            return {
                int(item["appid"]): item["name"].strip()
                for item in rows
                if isinstance(item, dict)
                and type(item.get("appid")) is int
                and item["appid"] in appids
                and isinstance(item.get("name"), str)
                and item["name"].strip()
            }
        except (requests.RequestException, ValueError, TypeError) as error:
            LOG.warning("Store lookup %s attempt %d/5: %s", language, attempt + 1, error)
            if attempt == 4:
                raise RuntimeError(f"Failed Steam {language} Store lookup") from error
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Steam {language} rate limit persisted; no partial publish")


def update_title(
    row: dict, tw: str | None, cn: str | None,
) -> dict:
    out = dict(row)
    # Existing original localized names are retained if the Store response
    # doesn't provide a translated title this time.
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
            out[converted] = CONVERT.convert(name.strip())

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


def refresh(
    data_dir: Path,
    *,
    session: requests.Session,
    batch_size: int = 30,
    interval: float = 2.0,
) -> dict:
    index_file = data_dir / "index.json"
    index = read(index_file)
    if index.get("version") != 2 or not isinstance(index.get("months"), list):
        raise RuntimeError("Sharded frontend index is missing/invalid")
    original_games = {}
    months = {}
    for month in index["months"]:
        if not re.fullmatch(r"\d{4}-\d{2}", str(month)):
            raise RuntimeError(f"Bad month in index: {month!r}")
        path = data_dir / "calendar" / f"{month}.json"
        doc = read(path)
        if not isinstance(doc.get("games"), list):
            raise RuntimeError(f"Bad calendar shard: {path}")
        months[month] = doc
        for row in doc["games"]:
            appid = int(row["appid"])
            if appid in original_games:
                raise RuntimeError(f"Duplicate AppID in calendar: {appid}")
            original_games[appid] = row

    selected = {
        appid: row for appid, row in original_games.items()
        if int(row.get("followers") or 0) >= 5000
    }
    ids = sorted(selected)
    if not ids:
        raise RuntimeError("No officially qualified games in public shards")
    results = {"tchinese": {}, "schinese": {}}
    total_batches = (len(ids) + batch_size - 1) // batch_size
    for n, start in enumerate(range(0, len(ids), batch_size), 1):
        batch = ids[start:start + batch_size]
        for language in ("tchinese", "schinese"):
            reply = localized_batch(session, batch, language, interval)
            results[language].update(reply)
            LOG.info(
                "LOCALIZED_LOOKUP language=%s batch=%d/%d returned=%d",
                language, n, total_batches, len(reply),
            )
            if interval:
                time.sleep(interval)

    changed_ids = set()
    tw_count = cn_count = 0
    updated_games = {}
    for appid, old in selected.items():
        english = str(old.get("name_en") or old.get("name") or "")
        tw = candidate(results["tchinese"].get(appid), english)
        cn = candidate(results["schinese"].get(appid), english)
        if tw: tw_count += 1
        if cn: cn_count += 1
        updated = update_title(old, tw, cn)
        # Preserve richer game metadata already stored in per-AppID file.
        per_game = data_dir / "games" / f"{appid}.json"
        full = read(per_game) if per_game.exists() else dict(old)
        merged = update_title(full, tw, cn)
        # Preserve core backend values and don't rewrite unrelated fields.
        changed = merged != full or updated != old
        if changed:
            changed_ids.add(appid)
            updated_games[appid] = merged
            save_changed(per_game, merged)

    changed_months = []
    if changed_ids:
        for month, doc in months.items():
            if not any(int(row["appid"]) in changed_ids for row in doc["games"]):
                continue
            doc["games"] = [
                updated_games.get(int(row["appid"]), row) for row in doc["games"]
            ]
            # Preserve month release counts/dates; only names/metadata changed.
            assert doc["count"] == len(doc["games"])
            save_changed(data_dir / "calendar" / f"{month}.json", doc)
            changed_months.append(month)

    stats = {
        "published_qualified": len(ids),
        "store_tw_titles": tw_count,
        "store_cn_titles": cn_count,
        "changed_games": len(changed_ids),
        "changed_months": changed_months,
        "at": datetime.now(timezone.utc).isoformat(),
        "changed_appids": sorted(changed_ids),
    }
    LOG.info("CHINESE_TITLE_REFRESH %s", json.dumps(stats, ensure_ascii=False))
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("frontend/data"))
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 35:
        raise SystemExit("--batch-size must be 1..35")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarChineseNameRefresh/1.0"
    result = refresh(
        args.data_dir,
        session=session,
        batch_size=args.batch_size,
        interval=args.interval,
    )
    print("CHINESE_TITLE_REFRESH_RESULT", json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
