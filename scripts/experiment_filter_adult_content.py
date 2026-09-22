"""Filter a freshly generated Steam candidate list using official mature-content descriptors.

Adult exclusion policy:
  3 = Adult Only Sexual Content
  4 = Frequent Nudity or Sexual Content
Descriptor 1 (Some Nudity or Sexual Content) is intentionally NOT excluded.

Input is an experimental artifact, never the legacy project candidate/cache files.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

import requests

DESCRIPTOR_NAMES = {
    1: "Some Nudity or Sexual Content",
    2: "Frequent Violence or Gore",
    3: "Adult Only Sexual Content",
    4: "Frequent Nudity or Sexual Content",
    5: "General Mature Content",
}
EXCLUDE_IDS = {3, 4}
API = "https://store.steampowered.com/api/appdetails"


def save(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def request_batch(session, appids, stats, batch_size_hint):
    params = {
        "appids": ",".join(str(x) for x in appids),
        "cc": "tw",
        "l": "english",
    }
    for attempt in range(7):
        try:
            response = session.get(API, params=params, timeout=(12, 60))
            stats["http_requests"] += 1
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if response.status_code == 429:
                    stats["http_429"] += 1
                delay = min(5 * (2 ** attempt), 80)
                stats["backoff_seconds"] += delay
                print(f"RETRY batch={len(appids)} status={response.status_code} sleep={delay}", flush=True)
                time.sleep(delay)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("AppDetails returned non-object JSON")
            return payload
        except (requests.ConnectionError, requests.Timeout, ValueError) as exc:
            delay = min(4 * (2 ** attempt), 60)
            stats["transport_retries"] += 1
            stats["backoff_seconds"] += delay
            print(f"RETRY batch={len(appids)} error={type(exc).__name__} sleep={delay}", flush=True)
            time.sleep(delay)
    if len(appids) > 1:
        mid = len(appids) // 2
        stats["batch_splits"] += 1
        left = request_batch(session, appids[:mid], stats, max(1, batch_size_hint // 2))
        right = request_batch(session, appids[mid:], stats, max(1, batch_size_hint // 2))
        left.update(right)
        return left
    return {}


def normalize_ids(data):
    raw = (data.get("content_descriptors") or {}).get("ids") or []
    out = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return sorted(set(out))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", default="output/steam_adult_filter_experiment")
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--pause", type=float, default=0.20)
    args = parser.parse_args()

    source = Path(args.input)
    games = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(games, list):
        raise SystemExit("Input must be a JSON list")
    ids = [int(x["appid"]) for x in games]
    if len(ids) != len(set(ids)):
        raise SystemExit("Input contains duplicate appids")

    by_id = {int(x["appid"]): x for x in games}
    started = time.monotonic()
    now = datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; GameTrendRadar-AdultFilterExperiment/1.0)"})
    stats = {
        "input_count": len(games), "http_requests": 0, "http_429": 0,
        "transport_retries": 0, "batch_splits": 0, "backoff_seconds": 0,
        "batch_size": args.batch_size,
    }
    details = {}
    missing = []

    print(f"ADULT_FILTER_START input={len(games)} batch_size={args.batch_size}", flush=True)
    for offset in range(0, len(ids), args.batch_size):
        batch = ids[offset: offset + args.batch_size]
        payload = request_batch(session, batch, stats, args.batch_size)
        for appid in batch:
            entry = payload.get(str(appid))
            if not isinstance(entry, dict) or not entry.get("success"):
                missing.append(appid)
                continue
            data = entry.get("data") or {}
            details[appid] = {
                "type": data.get("type"),
                "descriptor_ids": normalize_ids(data),
                "release_date_details": (data.get("release_date") or {}).get("date"),
                "coming_soon": (data.get("release_date") or {}).get("coming_soon"),
            }
        done = min(offset + len(batch), len(ids))
        if done == len(ids) or done % 300 < args.batch_size:
            print(
                f"ADULT_FILTER_PROGRESS checked={done}/{len(ids)} "
                f"resolved={len(details)} missing={len(missing)} "
                f"requests={stats['http_requests']} elapsed={round(time.monotonic()-started,1)}s",
                flush=True,
            )
        time.sleep(args.pause)

    # Retry unresolved apps individually once through the same robust request path.
    unresolved_before_retry = list(dict.fromkeys(missing))
    missing = []
    for appid in unresolved_before_retry:
        payload = request_batch(session, [appid], stats, 1)
        entry = payload.get(str(appid))
        if not isinstance(entry, dict) or not entry.get("success"):
            missing.append(appid)
            continue
        data = entry.get("data") or {}
        details[appid] = {
            "type": data.get("type"),
            "descriptor_ids": normalize_ids(data),
            "release_date_details": (data.get("release_date") or {}).get("date"),
            "coming_soon": (data.get("release_date") or {}).get("coming_soon"),
        }
        time.sleep(args.pause)

    adult = []
    retained = []
    descriptor_counts = {}
    for game in games:
        appid = int(game["appid"])
        info = details.get(appid)
        if info is None:
            # Fail closed for experiment reporting: unresolved is NOT labelled adult.
            retained.append({**game, "adult_filter_status": "unresolved"})
            continue
        d_ids = info["descriptor_ids"]
        for d in d_ids:
            descriptor_counts[str(d)] = descriptor_counts.get(str(d), 0) + 1
        excluded = sorted(EXCLUDE_IDS.intersection(d_ids))
        if excluded:
            adult.append({
                "appid": appid,
                "name": game.get("name"),
                "release_date": game.get("release_date"),
                "release_text": game.get("release_text"),
                "steam_url": game.get("steam_url"),
                "descriptor_ids": d_ids,
                "excluded_descriptor_ids": excluded,
                "exclude_reasons": [DESCRIPTOR_NAMES[x] for x in excluded],
                "detected_at_taipei": now,
                "source": "Steam Store api/appdetails content_descriptors",
            })
        else:
            retained.append({
                **game,
                "adult_filter_status": "retained",
                "descriptor_ids": d_ids,
            })

    adult.sort(key=lambda x: (x.get("release_date") or "", x["appid"]))
    retained.sort(key=lambda x: (x.get("release_date") or "", int(x["appid"])))
    exclusion_file = {
        "version": 1,
        "generated_at_taipei": now,
        "criteria": {
            "exclude_content_descriptor_ids": sorted(EXCLUDE_IDS),
            "descriptor_names": {str(k): v for k, v in DESCRIPTOR_NAMES.items()},
            "keep_descriptor_1": True,
            "demo_allowed": True,
            "note": "This cache excludes sexually explicit/adult-focused products using Steam official descriptors 3 and/or 4. It does not exclude products merely because descriptor 1 is present.",
        },
        "source_candidate_count": len(games),
        "excluded_count": len(adult),
        "games": adult,
    }
    out = Path(args.out)
    save(out / "steam_adult_exclusion.json", exclusion_file)
    save(out / "steam_candidates_after_adult_filter.json", retained)
    save(out / "unresolved_appids.json", missing)

    stats.update({
        "resolved_count": len(details),
        "unresolved_count": len(missing),
        "excluded_adult_count": len(adult),
        "retained_count": len(retained),
        "descriptor_counts": descriptor_counts,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "classification_rule": "Exclude content_descriptors ids 3 or 4; keep id 1.",
        "input_source": str(source),
    })
    save(out / "report.json", stats)
    print("ADULT_FILTER_FINAL " + json.dumps(stats, ensure_ascii=False, sort_keys=True), flush=True)

    # We require full AppDetails resolution before persisting a reusable exclusion list.
    if missing:
        sys.exit(2)


if __name__ == "__main__":
    main()
