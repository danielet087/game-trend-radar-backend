"""Daily official Followers observations for accepted games, through release +30d.

Does not edit eligibility, the master, shared cache or existing collection queues.
Stop on throttling. Reuse recent official values rather than repeat requests.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone, date
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET
import requests

try:
    from scripts.twitch_steam_admission import is_twitch_qualified
except ModuleNotFoundError:  # Direct script invocation from the repository root.
    from twitch_steam_admission import is_twitch_qualified

TAIPEI = timezone(timedelta(hours=8))


def read(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def eligible(row, today):
    try:
        released = date.fromisoformat(row["release_start"])
        count, aid = row["followers"], row["appid"]
        return (isinstance(aid, int) and not isinstance(aid, bool) and aid > 0
                and isinstance(count, int) and not isinstance(count, bool)
                and (is_twitch_qualified(row) or count >= 5000 or count > 3000 and row.get("recent_source") in {"tracked_release", "direct_release"})
                and row.get("release_precision", "day") == "day"
                and today - timedelta(days=30) <= released <= today + timedelta(days=365))
    except (KeyError, TypeError, ValueError):
        return False


def parse_count(body):
    root = ET.fromstring(body)
    for path in ("memberCount", "groupDetails/memberCount", ".//memberCount"):
        text = root.findtext(path, "").replace(",", "").strip()
        if text.isdigit():
            return int(text)
    raise ValueError("Missing Steam Community memberCount")


def collect(data_dir, output, *, max_requests=120, interval=30, now=None, session=None, sleep=time.sleep):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(TAIPEI).date()
    catalog = read(data_dir / "catalog.json", {})
    rows = catalog.get("games")
    if not isinstance(rows, list) or catalog.get("count") != len(rows):
        raise ValueError("A complete public catalog is required")
    history = read(data_dir / "insights-state.json", {}).get("records", {})
    rows = [row for row in rows if eligible(row, today)]
    # Oldest actual measurement first; lack of history is handled before recent checks.
    def latest(row):
        options = [dict(at=row.get("follower_checked_at"), followers=row.get("followers")),
                   *history.get(str(row["appid"]), {}).get("history", [])]
        options = [p for p in options if timestamp(p.get("at")) and timestamp(p["at"]) <= now
                   and isinstance(p.get("followers"), int) and not isinstance(p["followers"], bool) and p["followers"] >= 0]
        return max(options, key=lambda p: timestamp(p["at"]), default=None)
    rows.sort(key=lambda row: (latest(row) or {}).get("at") or "")
    client = session or requests.Session()
    client.headers.update({"User-Agent": "GameTrendRadar/1.0 (+https://github.com/danielet087/game-trend-radar)"})
    measurements, errors = [], []
    attempts = reused = 0
    previous_start = None
    started = time.monotonic()
    reason = "completed"
    for row in rows:
        previous = latest(row)
        if previous and timestamp(previous["at"]).astimezone(TAIPEI).date() == today:
            measurements.append({"appid": row["appid"], "followers": previous["followers"],
                                 "at": previous["at"], "source": "steam_community"})
            reused += 1
            continue
        if attempts >= max_requests or time.monotonic() - started > 3600:
            reason = "bounded_run"
            break
        if previous_start is not None:
            sleep(max(0, interval - (time.monotonic() - previous_start)))
        previous_start = time.monotonic()
        attempts += 1
        try:
            response = client.get(f"https://steamcommunity.com/games/{row['appid']}/memberslistxml/",
                                  params={"xml": 1}, timeout=25)
            if response.status_code == 429:
                reason = "rate_limited"
                break
            response.raise_for_status()
            count = parse_count(response.text)
            at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            measurements.append({"appid": row["appid"], "followers": count, "at": at, "source": "steam_community"})
        except (requests.RequestException, ValueError, ET.ParseError):
            errors.append(row["appid"])
            if len(errors) >= 3:
                reason = "source_unavailable"
                break
        # Partial progress is durable even if the runner is interrupted later.
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"measurements": measurements}, ensure_ascii=False), encoding="utf-8")
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "eligible": len(rows),
              "requests": attempts, "reused": reused, "errors": errors, "reason": reason,
              "measurements": measurements}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "measurements"}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=120)
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args()
    if args.interval < 30 or not 0 <= args.max_requests <= 120:
        parser.error("Keep at least 30 seconds between requests and at most 120 requests")
    collect(args.data_dir, args.output, max_requests=args.max_requests, interval=args.interval)


if __name__ == "__main__":
    main()
