"""Refresh only published qualified games through explicit source and I/O ports."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Callable

from radar_backend.domain.published_titles import preserve_published_twitch_fields


def refresh(
    data_dir: Path,
    *,
    session,
    batch_size: int = 30,
    interval: float = 2.0,
    read: Callable,
    save_changed: Callable,
    exists: Callable,
    localized_batch: Callable,
    candidate: Callable,
    update_title: Callable,
    is_twitch_qualified: Callable,
    preserve_twitch_admission: Callable,
    write_catalog_projection: Callable,
    sleep: Callable,
    clock: Callable,
    logger,
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
        if int(row.get("followers") or 0) >= 5000 or is_twitch_qualified(row)
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
            logger.info(
                "LOCALIZED_LOOKUP language=%s batch=%d/%d returned=%d",
                language, n, total_batches, len(reply),
            )
            if interval:
                sleep(interval)

    changed_ids = set()
    tw_count = cn_count = 0
    updated_games = {}
    for appid, old in selected.items():
        english = str(old.get("name_en") or old.get("name") or "")
        tw = candidate(results["tchinese"].get(appid), english)
        cn = candidate(results["schinese"].get(appid), english)
        if tw:
            tw_count += 1
        if cn:
            cn_count += 1
        updated = update_title(old, tw, cn)
        per_game = data_dir / "games" / f"{appid}.json"
        full = read(per_game) if exists(per_game) else dict(old)
        merged = update_title(preserve_twitch_admission(old, full), tw, cn)
        if is_twitch_qualified(old):
            preserve_published_twitch_fields(old, merged)
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
            assert doc["count"] == len(doc["games"])
            save_changed(data_dir / "calendar" / f"{month}.json", doc)
            changed_months.append(month)

    rows = sorted(
        [updated_games.get(appid, row) for appid, row in original_games.items()],
        key=lambda row: (row["release_start"], -int(row.get("followers") or 0), int(row["appid"])),
    )
    now = clock().isoformat()
    projection = write_catalog_projection(data_dir, rows, now)
    save_changed(data_dir / "steam_upcoming.json", {
        "version": 2, "generated_at": now, "count": len(rows), "games": rows,
    })
    index.update(projection)
    index["generated_at"] = now
    save_changed(data_dir / "index.json", index)

    stats = {
        "published_qualified": len(ids),
        "store_tw_titles": tw_count,
        "store_cn_titles": cn_count,
        "changed_games": len(changed_ids),
        "changed_months": changed_months,
        "at": clock().isoformat(),
        "changed_appids": sorted(changed_ids),
    }
    logger.info("CHINESE_TITLE_REFRESH %s", json.dumps(stats, ensure_ascii=False))
    return stats
