"""Read-only third-party Steam group BULK lookup on already known sample IDs.

Do NOT modify official Steam follower cache, state, or public JSON.
API docs: https://steam-groups.com/docs (third-party; not Valve).
Only public group IDs and already-public member counts are sent.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = 103582791429521408
API = "https://api.steam-groups.com"
OUT = Path("output/steam_groups_bulk_probe.json")

# Confirmed from our earlier Steam XML experiment (2026-09-19).
# Steam XML values are a time-stamped reference, not expected to remain exact.
SAMPLES = (
    (2272360, 103582791475268146, 89957),
    (1488490, 103582791470126135, 70339),
    (4358690, 103582791475597490, 51493),
    (2288340, 103582791475413357, 47307),
    (3669870, 103582791475403320, 47619),
    (3493540, 103582791475149948, 46678),
    (3010850, 103582791474574980, 46327),
    (2254990, 103582791474550392, 34624),
    (3984090, 103582791475350635, 34),
    (5013390, 103582791475816101, 16),
    (4972300, 103582791475783174, 139),
    (5195850, 103582791475811520, 2),
)


def summarize_response(resp, elapsed):
    row = {"http": resp.status_code, "seconds": round(elapsed, 3)}
    try:
        data = resp.json()
    except ValueError:
        row["json"] = False
        row["size_bytes"] = len(resp.content)
        return row, None
    row["json"] = True
    if isinstance(data, dict):
        row["keys"] = sorted(data.keys())
        error = data.get("error")
        if isinstance(error, dict):
            row["error_code"] = error.get("code")
        elif isinstance(error, str):
            row["error_code"] = error[:100]
    return row, data


def call(session, method, path, **kwargs):
    begin = time.monotonic()
    try:
        resp = session.request(method, API + path, timeout=16, **kwargs)
        summary, data = summarize_response(resp, time.monotonic() - begin)
        return summary, data
    except requests.RequestException as exc:
        return {"error_type": type(exc).__name__,
                "seconds": round(time.monotonic() - begin, 3)}, None


def index_groups(data):
    # Do not assume undocumented shape; accept documented data list only.
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return {}
    groups = {}
    for item in data["data"]:
        if not isinstance(item, dict):
            continue
        identity = item.get("id")
        if identity is not None:
            groups[str(identity)] = {
                "members": item.get("members"),
                "is_last_seen": item.get("isLastSeen"),
                "keys": sorted(item.keys()),
                "url": item.get("url"),
            }
    return groups


def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "GameTrendRadarReadOnlyBulkProbe/1.0"})
    result = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "official_source": "Steam XML memberCount from previous pilot",
              "provider": "third-party steam-groups.com; not endorsed by Valve",
              "probe": {}, "samples": []}
    meta, _ = call(session, "GET", "/api")
    result["probe"]["root"] = meta
    print("ROOT", json.dumps(meta), flush=True)
    if meta.get("http") in (429, 500, 502, 503):
        result["probe"]["stopped"] = "endpoint_throttle_or_server_error"
    else:
        # Steam group IDs are 64-bit Steam IDs. Third-party docs say numeric
        # IDs; test whether it expects short account IDs or full SteamID64.
        short_ids = [sid - BASE for _, sid, _ in SAMPLES]
        full_ids = [sid for _, sid, _ in SAMPLES]
        bulk_short, short_data = call(
            session, "POST", "/api/groups/bulk",
            json={"ids": short_ids, "limit": len(short_ids)},
        )
        short_found = index_groups(short_data)
        bulk_short["returned"] = len(short_found)
        bulk_short["not_found_count"] = len(short_data.get("notFound", [])) if isinstance(short_data, dict) and isinstance(short_data.get("notFound"), list) else None
        result["probe"]["bulk_short"] = bulk_short
        print("BULK_SHORT", json.dumps(bulk_short), flush=True)
        time.sleep(1.0)
        if bulk_short.get("http") not in (429, 500, 502, 503):
            bulk_full, full_data = call(
                session, "POST", "/api/groups/bulk",
                json={"ids": full_ids, "limit": len(full_ids)},
            )
            full_found = index_groups(full_data)
            bulk_full["returned"] = len(full_found)
            bulk_full["not_found_count"] = len(full_data.get("notFound", [])) if isinstance(full_data, dict) and isinstance(full_data.get("notFound"), list) else None
            result["probe"]["bulk_full"] = bulk_full
            print("BULK_FULL", json.dumps(bulk_full), flush=True)
        else:
            full_found = {}

        time.sleep(1.0)
        # GET single id is important to distinguish unsupported POST from
        # true absence of these official game groups.
        single_short, one_short = call(session, "GET", "/api/groups/" + str(short_ids[0]))
        single_short["found"] = bool(isinstance(one_short, dict) and (
            one_short.get("members") is not None
            or isinstance(one_short.get("data"), dict) and one_short["data"].get("members") is not None
        ))
        result["probe"]["single_short"] = single_short
        print("SINGLE_SHORT", json.dumps(single_short), flush=True)
        time.sleep(1.0)
        single_full, one_full = call(session, "GET", "/api/groups/" + str(full_ids[0]))
        single_full["found"] = bool(isinstance(one_full, dict) and (
            one_full.get("members") is not None
            or isinstance(one_full.get("data"), dict) and one_full["data"].get("members") is not None
        ))
        result["probe"]["single_full"] = single_full
        print("SINGLE_FULL", json.dumps(single_full), flush=True)

        for appid, sid, xml in SAMPLES:
            short_id = sid - BASE
            record = {"appid": appid, "group_id64": str(sid),
                      "group_short_id": str(short_id),
                      "prior_steam_xml_members": xml}
            for label, lookup, key in (
                ("short", short_found, str(short_id)),
                ("full", full_found, str(sid)),
            ):
                group = lookup.get(key)
                if group:
                    record["bulk_" + label + "_members"] = group["members"]
                    record["bulk_" + label + "_is_last_seen"] = group["is_last_seen"]
                    if isinstance(group["members"], int) and xml:
                        record["bulk_" + label + "_pct_difference"] = round(
                            (group["members"] - xml) * 100 / xml, 2)
            result["samples"].append(record)
            print("SAMPLE", appid,
                  "XML", xml,
                  "bulk_short", record.get("bulk_short_members"),
                  "bulk_full", record.get("bulk_full_members"),
                  flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print("REPORT", str(OUT), flush=True)


if __name__ == "__main__":
    main()
