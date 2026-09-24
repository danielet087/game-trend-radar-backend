"""Re-audit published FUTURE Steam release dates against the public Store display.

A timestamp from IStoreQueryService is not sufficient evidence of an announced
calendar day. Only Store Browse release.coming_soon_display == "date_full"
may remain in the future release calendar.

Released historical records are retained. This script never queries Followers.
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
from zoneinfo import ZoneInfo

import requests

STORE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
TAIPEI = ZoneInfo("Asia/Taipei")
LOG = logging.getLogger(__name__)
VALID_MONTH = re.compile(r"^\d{4}-\d{2}$")


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def write_if_changed(path: Path, payload: dict) -> bool:
    old = load(path, None)
    if isinstance(old, dict) and isinstance(payload, dict):
        a = {k: v for k, v in old.items() if k != "generated_at"}
        b = {k: v for k, v in payload.items() if k != "generated_at"}
        if a == b:
            return False
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def fetch_release_batch(
    session: requests.Session,
    appids: list[int],
    *,
    interval: float = 2.0,
) -> dict[int, dict]:
    payload = {
        "ids": [{"appid": appid} for appid in appids],
        "context": {"country_code": "TW", "language": "tchinese", "steam_realm": 1},
        "data_request": {"include_release": True},
    }
    for attempt in range(5):
        try:
            response = session.get(
                STORE,
                params={"input_json": json.dumps(payload, separators=(",", ":"))},
                timeout=45,
            )
            if response.status_code == 429:
                LOG.warning("Steam Store date audit throttled attempt=%d", attempt + 1)
                time.sleep(15 * (attempt + 1))
                continue
            response.raise_for_status()
            rows = (response.json().get("response") or {}).get("store_items") or []
            return {
                int(row["appid"]): row.get("release") or {}
                for row in rows
                if isinstance(row, dict)
                and type(row.get("appid")) is int
                and row["appid"] in appids
            }
        except (requests.RequestException, ValueError, TypeError) as exc:
            LOG.warning("Steam Store date audit retry %d: %s", attempt + 1, exc)
            if attempt == 4:
                raise RuntimeError("Steam Store date audit batch failed") from exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Steam Store date audit batch failed")


def verified_day(release: dict) -> str | None:
    if not isinstance(release, dict):
        return None
    if release.get("coming_soon_display") != "date_full":
        return None
    stamp = release.get("steam_release_date")
    if isinstance(stamp, bool):
        return None
    try:
        seconds = int(stamp)
        instant = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if not 2010 <= instant.year <= 2100:
        return None
    return instant.astimezone(TAIPEI).date().isoformat()


def audit(
    data_dir: Path,
    *,
    session: requests.Session,
    batch_size: int = 30,
    interval: float = 2.0,
    today=None,
) -> dict:
    now = datetime.now(timezone.utc)
    today = today or now.astimezone(TAIPEI).date()
    today_s = today.isoformat()

    index_path = data_dir / "index.json"
    index = load(index_path, {})
    months = index.get("months") or []
    if index.get("version") != 2 or not isinstance(months, list):
        raise RuntimeError("Invalid sharded Steam index")

    records: dict[int, dict] = {}
    for month in months:
        if not VALID_MONTH.fullmatch(str(month)):
            raise RuntimeError(f"Invalid month shard in index: {month!r}")
        doc = load(data_dir / "calendar" / f"{month}.json", {})
        rows = doc.get("games") or []
        if not isinstance(rows, list):
            raise RuntimeError(f"Invalid month shard: {month}")
        for row in rows:
            appid = int(row["appid"])
            if appid in records:
                raise RuntimeError(f"Duplicate published AppID {appid}")
            records[appid] = row

    future_ids = sorted(
        appid for appid, row in records.items()
        if str(row.get("release_start") or "") >= today_s
    )
    release_info: dict[int, dict] = {}
    for start in range(0, len(future_ids), batch_size):
        batch = future_ids[start:start + batch_size]
        release_info.update(fetch_release_batch(session, batch, interval=interval))
        LOG.info(
            "DATE_AUDIT_PROGRESS checked=%d/%d resolved=%d",
            min(start + len(batch), len(future_ids)), len(future_ids), len(release_info),
        )
        if interval and start + batch_size < len(future_ids):
            time.sleep(interval)

    kept: dict[int, dict] = {}
    excluded = []
    corrected = []
    exact = 0
    historical = 0

    for appid, old in records.items():
        old_day = str(old.get("release_start") or "")
        if old_day < today_s:
            kept[appid] = old
            historical += 1
            continue

        release = release_info.get(appid)
        day = verified_day(release or {})
        if day is None:
            excluded.append({
                "appid": appid,
                "name": old.get("display_name") or old.get("name_zh_tw") or old.get("name_en") or old.get("name"),
                "previous_release_start": old_day,
                "coming_soon_display": (
                    release.get("coming_soon_display")
                    if isinstance(release, dict) else "unavailable"
                ),
                "reason": "Steam Store does not currently publish a full exact date",
            })
            continue

        row = dict(old)
        if day != old_day:
            corrected.append({"appid": appid, "from": old_day, "to": day})
        row.update({
            "release_raw": day,
            "release_start": day,
            "release_end": day,
            "release_precision": "day",
            "release_display_precision": "date_full",
            "release_display_provider": "Steam IStoreBrowseService/GetItems",
            "release_date_timezone": "Asia/Taipei",
            "release_date_basis": "steam_store_browse_verified_full_date",
            "release_date_verified_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        })
        kept[appid] = row
        exact += 1

    # Update/delete one-AppID files first.
    games_dir = data_dir / "games"
    for appid, row in kept.items():
        per_game = games_dir / f"{appid}.json"
        full = load(per_game, {})
        if isinstance(full, dict) and full:
            full.update({
                key: row[key] for key in (
                    "release_raw", "release_start", "release_end", "release_precision",
                    "release_display_precision", "release_display_provider",
                    "release_date_timezone", "release_date_basis",
                    "release_date_verified_at",
                ) if key in row
            })
            # Preserve all richer metadata while syncing audited date fields.
            kept[appid] = full
            write_if_changed(per_game, full)
        else:
            write_if_changed(per_game, row)

    for item in excluded:
        path = games_dir / f"{item['appid']}.json"
        if path.exists():
            path.unlink()

    # Rebuild only changed month content; history remains present.
    by_month: dict[str, list[dict]] = defaultdict(list)
    for row in kept.values():
        day = str(row["release_start"])
        by_month[day[:7]].append(row)
    for month_rows in by_month.values():
        month_rows.sort(key=lambda g: (
            str(g.get("release_start") or "9999-12-31"),
            -int(g.get("followers") or 0),
            int(g["appid"]),
        ))

    old_months = set(str(x) for x in months)
    new_months = set(by_month)
    for month in sorted(old_months | new_months):
        path = data_dir / "calendar" / f"{month}.json"
        rows = by_month.get(month, [])
        if not rows:
            if path.exists():
                path.unlink()
            continue
        prior = load(path, {})
        payload = {
            "version": 2,
            "generated_at": now.isoformat(),
            "month": month,
            "count": len(rows),
            "games": rows,
        }
        if isinstance(prior, dict) and prior.get("generated_at"):
            payload["generated_at"] = prior["generated_at"]
        write_if_changed(path, payload)

    # Dynamic lists derive only from retained audited records.
    released_from = (today.fromordinal(today.toordinal() - 30)).isoformat()
    upcoming = [
        int(row["appid"]) for row in kept.values()
        if str(row.get("release_start")) >= today_s
        and int(row.get("followers") or 0) >= 5000
    ]
    released = [
        int(row["appid"]) for row in kept.values()
        if released_from <= str(row.get("release_start")) < today_s
        and (
            int(row.get("followers") or 0) >= 5000
            or (
                int(row.get("followers") or 0) > 3000
                and row.get("recent_source") in {"tracked_release", "direct_release"}
            )
        )
    ]
    upcoming.sort(key=lambda appid: (kept[appid]["release_start"], -int(kept[appid].get("followers") or 0)))
    released.sort(key=lambda appid: (kept[appid]["release_start"], -int(kept[appid].get("followers") or 0)))

    write_if_changed(data_dir / "lists" / "upcoming.json", {
        "version": 2, "generated_at": now.isoformat(),
        "count": len(upcoming), "appids": upcoming,
    })
    write_if_changed(data_dir / "lists" / "released.json", {
        "version": 2, "generated_at": now.isoformat(),
        "count": len(released), "appids": released,
    })
    write_if_changed(data_dir / "excluded_date_appids.json", {
        "version": 2,
        "policy": "Future games require Steam Store coming_soon_display=date_full.",
        "audited_at": now.isoformat(),
        "count": len(excluded),
        "appids": sorted(item["appid"] for item in excluded),
        "games": sorted(excluded, key=lambda x: (x["previous_release_start"], x["appid"])),
    })

    # Keep the legacy fallback aligned with the audited sharded catalogue.
    legacy_path = data_dir / "steam_upcoming.json"
    legacy = load(legacy_path, {})
    if isinstance(legacy, dict):
        legacy["generated_at"] = now.isoformat()
        legacy["count"] = len(kept)
        legacy["games"] = sorted(
            kept.values(),
            key=lambda g: (
                str(g.get("release_start") or "9999-12-31"),
                -int(g.get("followers") or 0),
                int(g["appid"]),
            ),
        )
        legacy["release_date_audited"] = True
        legacy["release_date_policy"] = "future_requires_store_date_full"
        write_if_changed(legacy_path, legacy)

    index.update({
        "generated_at": now.isoformat(),
        "game_count": len(kept),
        "months": sorted(new_months),
        "release_date_audited": True,
        "release_date_policy": "future_requires_store_date_full",
        "release_date_audited_at": now.isoformat(),
    })
    write_if_changed(index_path, index)

    result = {
        "published_before": len(records),
        "future_checked": len(future_ids),
        "future_exact_kept": exact,
        "future_uncertain_removed": len(excluded),
        "historical_retained": historical,
        "published_after": len(kept),
        "date_corrections": corrected,
        "removed_appids": sorted(item["appid"] for item in excluded),
    }
    print("STEAM_RELEASE_DATE_AUDIT", json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 35:
        raise SystemExit("--batch-size must be 1..35")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarReleaseDateAudit/1.0"
    audit(
        args.data_dir,
        session=session,
        batch_size=args.batch_size,
        interval=args.interval,
    )


if __name__ == "__main__":
    main()
