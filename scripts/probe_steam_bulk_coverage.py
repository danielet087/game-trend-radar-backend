"""Read-only Steam group bulk coverage on stratified follower-cache samples.

Cross-check 60 already-seen AppIDs: ResolveVanityURL via the existing Web API
secret -> short group ID -> third-party bulk counts. NO new XML Steam requests,
NO write to follower cache, candidate cursor, frontend, or any public repo.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path("data/steam_followers_cache.json")
OUTPUT = Path("output/steam_bulk_coverage_probe.json")
VANITY = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
BULK = "https://api.steam-groups.com/api/groups/bulk"
STEAM_GROUP_BASE = 103582791429521408


def pick(cache):
    # Stratified, deterministic sample; test threshold false negatives.
    buckets = [
        sorted((k for k, v in cache.items() if int(v["followers"]) >= 5000),
               key=lambda x: int(cache[x]["followers"]), reverse=True),
        sorted((k for k, v in cache.items() if 100 <= int(v["followers"]) < 5000),
               key=lambda x: int(cache[x]["followers"]), reverse=True),
        sorted((k for k, v in cache.items() if int(v["followers"]) < 100),
               key=lambda x: int(cache[x]["followers"]), reverse=True),
    ]
    amounts = (20, 20, 20)
    return [appid for group, amount in zip(buckets, amounts) for appid in group[:amount]]


def query_group_id(session, key, appid):
    started = time.monotonic()
    result = {"appid": appid, "http": None, "status": "error"}
    try:
        response = session.get(
            VANITY, params={"key": key, "vanityurl": str(appid), "url_type": 3},
            timeout=12)
        result["http"] = response.status_code
        if response.status_code == 200:
            record = response.json().get("response", {})
            group_id = record.get("steamid")
            result["steam_success_code"] = record.get("success")
            if record.get("success") == 1 and str(group_id).isdigit():
                result["gid64"] = str(group_id)
                if int(group_id) >= STEAM_GROUP_BASE:
                    result["short_id"] = int(group_id) - STEAM_GROUP_BASE
                    result["status"] = "found"
                else:
                    result["status"] = "unexpected_steamid"
            else:
                result["status"] = "not_found"
        else:
            result["status"] = "http_error"
    except (requests.RequestException, ValueError) as exc:
        result["status"] = "exception"
        result["error_type"] = type(exc).__name__
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def main():
    key = os.environ.get("STEAM_WEB_API_KEY", "").strip()
    cache = json.loads(ROOT.read_text(encoding="utf-8"))["games"]
    selected = pick(cache)
    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarCoverageProbe/1.0"
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "test": "one_time_read_only_60_cache_appids",
        "key_present": bool(key),
        "selection": "20 >=5000, 20 100..4999, 20 <100 (per bucket availability)",
        "requested_count": len(selected),
        "mapping": [], "bulk": {}, "summary": {},
    }
    started = time.monotonic()
    if key:
        for idx, appid in enumerate(selected):
            if idx:
                time.sleep(0.35)
            row = query_group_id(session, key, appid)
            row["prior_xml_followers"] = int(cache[appid]["followers"])
            row["prior_checked_at"] = cache[appid].get("checked_at")
            report["mapping"].append(row)
            print("MAP", appid, row["status"], row["http"],
                  round(row["seconds"], 2), flush=True)
            if row["http"] in (429, 401, 403) or (
                row["http"] is not None and row["http"] >= 500
            ):
                print("STOP: Steam API throttle/authorization/server response",
                      flush=True)
                break

    candidates = [r for r in report["mapping"] if r.get("short_id") is not None]
    if candidates:
        mids = list(dict.fromkeys(row["short_id"] for row in candidates))
        bulk_started = time.monotonic()
        try:
            response = session.post(
                BULK, json={"ids": mids, "limit": len(mids)}, timeout=18)
            report["bulk"]["http"] = response.status_code
            report["bulk"]["seconds"] = round(time.monotonic() - bulk_started, 3)
            if response.status_code == 200:
                blob = response.json()
                groups = {
                    str(row["id"]): row
                    for row in blob.get("data", [])
                    if isinstance(row, dict) and row.get("id") is not None
                }
                report["bulk"]["not_found"] = len(blob.get("notFound", []))
                report["bulk"]["returned"] = len(groups)
                for row in candidates:
                    found = groups.get(str(row["short_id"]))
                    row["third_party_returned"] = found is not None
                    if found:
                        row["third_party_members"] = found.get("members")
                        row["third_party_is_last_seen"] = found.get("isLastSeen")
                        if isinstance(found.get("members"), int):
                            row["percent_difference"] = round(
                                (found["members"] - row["prior_xml_followers"])
                                * 100 / max(1, row["prior_xml_followers"]), 2)
                        print("COUNT", row["appid"], row["prior_xml_followers"],
                              row.get("third_party_members"), flush=True)
        except (requests.RequestException, ValueError) as exc:
            report["bulk"]["error_type"] = type(exc).__name__

    compared = [r for r in candidates if isinstance(r.get("third_party_members"), int)]
    qualified = [r for r in report["mapping"] if r["prior_xml_followers"] >= 5000]
    positives = [r for r in compared if r["third_party_members"] >= 5000]
    report["summary"] = {
        "selected": len(selected),
        "mapped": len(candidates),
        "resolved_rate_pct": round(len(candidates) * 100 / len(report["mapping"]), 2)
            if report["mapping"] else 0,
        "mapped_vs_selected_pct": round(len(candidates) * 100 / len(selected), 2)
            if selected else 0,
        "third_party_returned": len(compared),
        "third_party_vs_selected_pct": round(len(compared) * 100 / len(selected), 2)
            if selected else 0,
        "old_xml_qualified_5000": len(qualified),
        "third_party_qualified_5000": len(positives),
        "old_xml_qualified_but_missing_third_party": sum(
            r.get("third_party_members") is None for r in qualified),
        "old_xml_qualified_but_third_party_below_5000": sum(
            isinstance(r.get("third_party_members"), int)
            and r["third_party_members"] < 5000 for r in qualified),
        "positive_member_count_difference": sum(
            r["third_party_members"] > r["prior_xml_followers"] for r in compared),
        "negative_member_count_difference": sum(
            r["third_party_members"] < r["prior_xml_followers"] for r in compared),
        "total_seconds": round(time.monotonic() - started, 2),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print("SUMMARY", json.dumps(report["summary"], ensure_ascii=False), flush=True)
    print("BULK", json.dumps(report["bulk"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
