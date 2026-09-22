"""Fast adult-content filter for freshly fetched Steam candidates.

Strategy:
1) Compare two fresh Steam search passes:
   - inclusive: ignore_preferences=1
   - standard: default preference filtering
2) Treat items seen only in the inclusive pass as high-risk candidates.
3) Also audit standard-pass items carrying adult-related Steam community tags.
4) Query Steam official appdetails ONE app at a time only for that reduced suspect pool.
5) Exclude only official content descriptor IDs:
     3 = Adult Only Sexual Content
     4 = Frequent Nudity or Sexual Content
   Descriptor 1 = Some Nudity or Sexual Content is intentionally retained.

Demos are allowed. No legacy project candidate/cache files are read.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

import requests

API = "https://store.steampowered.com/api/appdetails"
EXCLUDE_IDS = {3, 4}
DESCRIPTOR_NAMES = {
    1: "Some Nudity or Sexual Content",
    2: "Frequent Violence or Gore",
    3: "Adult Only Sexual Content",
    4: "Frequent Nudity or Sexual Content",
    5: "General Mature Content",
}
# Community tags are only used to choose extra audit candidates.
# Final exclusion is ALWAYS based on official content_descriptors.
ADULT_RELATED_TAGS = {
    12095: "Sexual Content",
    6650: "Nudity",
    9130: "Hentai",
    65443: "Adult Content",
}


def save(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_tags(item):
    raw = item.get("steam_tagids") or "[]"
    try:
        parsed = ast.literal_eval(raw) if isinstance(raw, str) else raw
    except Exception:
        return set()
    out = set()
    if isinstance(parsed, (list, tuple)):
        for x in parsed:
            try:
                out.add(int(x))
            except (TypeError, ValueError):
                pass
    return out


def descriptor_ids(data):
    raw = (data.get("content_descriptors") or {}).get("ids") or []
    out = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return sorted(set(out))


def fetch_one(session, appid, stats):
    params = {"appids": appid, "cc": "tw", "l": "english"}
    for attempt in range(8):
        try:
            response = session.get(API, params=params, timeout=(10, 40))
            stats["http_requests"] += 1
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if response.status_code == 429:
                    stats["http_429"] += 1
                delay = min(4 * (2 ** attempt), 64)
                stats["backoff_seconds"] += delay
                print(f"RETRY appid={appid} status={response.status_code} sleep={delay}", flush=True)
                time.sleep(delay)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("non-object JSON")
            return payload.get(str(appid))
        except (requests.ConnectionError, requests.Timeout, ValueError) as exc:
            delay = min(3 * (2 ** attempt), 48)
            stats["transport_retries"] += 1
            stats["backoff_seconds"] += delay
            print(f"RETRY appid={appid} error={type(exc).__name__} sleep={delay}", flush=True)
            time.sleep(delay)
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--inclusive", required=True)
    p.add_argument("--standard", required=True)
    p.add_argument("--out", default="output/steam_adult_filter_experiment")
    p.add_argument("--pause", type=float, default=0.30)
    args = p.parse_args()

    inclusive = json.loads(Path(args.inclusive).read_text(encoding="utf-8"))
    standard = json.loads(Path(args.standard).read_text(encoding="utf-8"))
    if not isinstance(inclusive, list) or not isinstance(standard, list):
        raise SystemExit("Both inputs must be JSON lists")

    inc = {int(x["appid"]): x for x in inclusive}
    std = {int(x["appid"]): x for x in standard}
    if len(inc) != len(inclusive) or len(std) != len(standard):
        raise SystemExit("Duplicate AppID in fresh scan input")

    union_ids = set(inc) | set(std)
    union = {appid: inc.get(appid) or std[appid] for appid in union_ids}
    inclusive_only = set(inc) - set(std)

    standard_tag_suspects = {
        appid for appid, item in std.items()
        if parse_tags(item) & set(ADULT_RELATED_TAGS)
    }

    suspects = sorted(inclusive_only | standard_tag_suspects)
    suspect_reasons = {}
    for appid in suspects:
        reasons = []
        if appid in inclusive_only:
            reasons.append("hidden_without_ignore_preferences")
        matched_tags = sorted(parse_tags(union[appid]) & set(ADULT_RELATED_TAGS))
        if matched_tags:
            reasons.append("adult_related_tags:" + ",".join(str(x) for x in matched_tags))
        suspect_reasons[appid] = reasons

    started = time.monotonic()
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; GameTrendRadar-AdultFilterExperiment/2.0)"
    })
    stats = {
        "inclusive_count": len(inc),
        "standard_count": len(std),
        "union_count": len(union),
        "inclusive_only_count": len(inclusive_only),
        "standard_adult_tag_audit_count": len(standard_tag_suspects),
        "official_appdetails_suspect_count": len(suspects),
        "http_requests": 0,
        "http_429": 0,
        "transport_retries": 0,
        "backoff_seconds": 0,
        "pause_seconds_between_apps": args.pause,
    }
    print(
        "ADULT_FILTER_START "
        + json.dumps({k: stats[k] for k in (
            "union_count","inclusive_only_count","standard_adult_tag_audit_count",
            "official_appdetails_suspect_count"
        )}, sort_keys=True),
        flush=True,
    )

    verified = {}
    unresolved = []
    for i, appid in enumerate(suspects, 1):
        entry = fetch_one(session, appid, stats)
        if not isinstance(entry, dict) or not entry.get("success"):
            unresolved.append(appid)
        else:
            data = entry.get("data") or {}
            verified[appid] = {
                "type": data.get("type"),
                "descriptor_ids": descriptor_ids(data),
                "release_date_details": (data.get("release_date") or {}).get("date"),
                "coming_soon": (data.get("release_date") or {}).get("coming_soon"),
            }
        if i == 1 or i % 25 == 0 or i == len(suspects):
            print(
                f"ADULT_FILTER_PROGRESS checked={i}/{len(suspects)} "
                f"resolved={len(verified)} unresolved={len(unresolved)} "
                f"adult_confirmed={sum(bool(EXCLUDE_IDS & set(x['descriptor_ids'])) for x in verified.values())} "
                f"requests={stats['http_requests']} elapsed={round(time.monotonic()-started,1)}s",
                flush=True,
            )
        time.sleep(args.pause)

    # One slower retry for unresolved products.
    retry_unresolved = list(unresolved)
    unresolved = []
    for appid in retry_unresolved:
        time.sleep(1.0)
        entry = fetch_one(session, appid, stats)
        if not isinstance(entry, dict) or not entry.get("success"):
            unresolved.append(appid)
            continue
        data = entry.get("data") or {}
        verified[appid] = {
            "type": data.get("type"),
            "descriptor_ids": descriptor_ids(data),
            "release_date_details": (data.get("release_date") or {}).get("date"),
            "coming_soon": (data.get("release_date") or {}).get("coming_soon"),
        }

    generated = datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
    adult = []
    retained = []
    standard_leaks = []

    for appid in sorted(union):
        game = union[appid]
        info = verified.get(appid)
        excluded_ids = sorted(EXCLUDE_IDS & set((info or {}).get("descriptor_ids") or []))
        if excluded_ids:
            record = {
                "appid": appid,
                "name": game.get("name"),
                "release_date": game.get("release_date"),
                "release_text": game.get("release_text"),
                "steam_url": game.get("steam_url"),
                "descriptor_ids": info["descriptor_ids"],
                "excluded_descriptor_ids": excluded_ids,
                "exclude_reasons": [DESCRIPTOR_NAMES[x] for x in excluded_ids],
                "candidate_reasons": suspect_reasons.get(appid, []),
                "detected_at_taipei": generated,
                "source": "Steam Store api/appdetails content_descriptors",
            }
            adult.append(record)
            if appid in std:
                standard_leaks.append(record)
        else:
            retained.append(game)

    adult.sort(key=lambda x: (x.get("release_date") or "", x["appid"]))
    retained.sort(key=lambda x: (x.get("release_date") or "", int(x["appid"])))

    exclusion_cache = {
        "version": 1,
        "generated_at_taipei": generated,
        "criteria": {
            "exclude_content_descriptor_ids": [3, 4],
            "descriptor_names": {str(k): v for k, v in DESCRIPTOR_NAMES.items()},
            "descriptor_1_is_retained": True,
            "demo_allowed": True,
            "primary_candidate_rule": "Present in fresh ignore_preferences=1 scan but absent from fresh standard scan.",
            "audit_candidate_rule": "Also verify standard-scan games tagged Sexual Content/Nudity/Hentai/Adult Content.",
            "final_decision_source": "Steam official api/appdetails content_descriptors only.",
        },
        "source_scan_counts": {
            "inclusive": len(inc),
            "standard": len(std),
            "union": len(union),
        },
        "excluded_count": len(adult),
        "games": adult,
    }

    out = Path(args.out)
    save(out / "steam_adult_exclusion.json", exclusion_cache)
    save(out / "steam_candidates_after_adult_filter.json", retained)
    save(out / "unresolved_appids.json", unresolved)
    save(out / "official_verification_suspects.json", [
        {
            "appid": appid,
            "name": union[appid].get("name"),
            "release_date": union[appid].get("release_date"),
            "candidate_reasons": suspect_reasons[appid],
            "official_result": verified.get(appid),
        }
        for appid in suspects
    ])

    stats.update({
        "official_resolved_count": len(verified),
        "unresolved_count": len(unresolved),
        "adult_excluded_count": len(adult),
        "retained_count": len(retained),
        "standard_scan_adult_descriptor_leaks": len(standard_leaks),
        "standard_scan_adult_descriptor_leak_appids": [x["appid"] for x in standard_leaks],
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "classification": "Official descriptor 3 and/or 4 only; descriptor 1 kept.",
    })
    save(out / "report.json", stats)
    print("ADULT_FILTER_FINAL " + json.dumps(stats, ensure_ascii=False, sort_keys=True), flush=True)

    # Do not persist a reusable cache unless every suspect was actually resolved.
    if unresolved:
        sys.exit(2)


if __name__ == "__main__":
    main()
