"""Read-only Steam AppID -> clan mapping and anonymous CM batch experiment.

Does not modify production JSON or cache. Runs only in a manual/push probe workflow.
Credentials: optional STEAM_WEB_API_KEY via the GitHub Actions secrets env only.
"""
from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

# 8 previously-qualified games, 4 below threshold; all from private cache.
APPIDS = [
    2272360, 1488490, 4358690, 2288340, 3669870, 3493540,
    3010850, 2254990, 3984090, 5013390, 4972300, 5195850,
]
OUT = Path("output/steam_clan_batch_probe.json")
XML_URL = "https://steamcommunity.com/games/{}/memberslistxml/"
VANITY_URL = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"


def probe_xml(session, appid):
    start = time.monotonic()
    entry = {"appid": appid, "xml_status": "unavailable"}
    try:
        response = session.get(XML_URL.format(appid), timeout=17)
        entry["xml_http"] = response.status_code
        entry["xml_seconds"] = round(time.monotonic() - start, 2)
        if response.status_code != 200:
            return entry
        root = ET.fromstring(response.content)
        gid = root.findtext(".//groupID64")
        members = root.findtext(".//memberCount")
        if gid and gid.isdigit():
            entry["group_id64"] = gid
        if members and members.strip().replace(",", "").isdigit():
            entry["xml_member_count"] = int(members.strip().replace(",", ""))
        entry["xml_status"] = "ok" if "xml_member_count" in entry else "missing_member_count"
    except (requests.RequestException, ET.ParseError, ValueError) as exc:
        entry["xml_status"] = type(exc).__name__
        entry["xml_seconds"] = round(time.monotonic() - start, 2)
    return entry


def probe_vanity(session, api_key, appid):
    result = {"attempted": True, "success": False}
    try:
        response = session.get(
            VANITY_URL,
            params={"key": api_key, "vanityurl": str(appid), "url_type": 3},
            timeout=15,
        )
        result["http"] = response.status_code
        if response.status_code == 200:
            body = response.json().get("response", {})
            result["response_success"] = body.get("success")
            gid = body.get("steamid")
            if body.get("success") == 1 and str(gid).isdigit():
                result["group_id64"] = str(gid)
                result["success"] = True
    except (requests.RequestException, ValueError) as exc:
        result["error_type"] = type(exc).__name__
    # Never save full URL, headers, body or exception contents: they may contain key.
    return result


def probe_cm(rows):
    result = {
        "status": "not_attempted",
        "login": None,
        "request_groups": 0,
        "response_eresult": None,
        "clan_states": {},
        "seconds": 0,
    }
    gids = list(dict.fromkeys(
        row["group_id64"] for row in rows if row.get("group_id64")
    ))[:10]
    if len(gids) < 2:
        result["status"] = "insufficient_group_ids"
        return result

    start = time.monotonic()
    client = None
    try:
        from gevent import Timeout
        from steam.client import SteamClient
        from steam.core.msg import MsgProto
        from steam.enums import EResult
        from steam.enums.emsg import EMsg

        client = SteamClient()
        target = set(gids)

        @client.on(EMsg.ClientClanState)
        def on_clan_state(message):
            body = message.body
            steamid = str(getattr(body, "steamid_clan", ""))
            if steamid not in target:
                return
            user_counts = getattr(body, "user_counts", None)
            members = getattr(user_counts, "members", None)
            result["clan_states"][steamid] = {
                "has_user_counts": bool(user_counts),
                "members": int(members) if members is not None else None,
            }

        with Timeout(38, False) as deadline:
            login = client.anonymous_login()
            if deadline is None:
                result["status"] = "anonymous_login_timeout"
                return result
        result["login"] = str(login)
        if login != EResult.OK:
            result["status"] = "anonymous_login_not_ok"
            return result

        message = MsgProto(EMsg.ClientGetClanActivityCounts)
        message.body.steamid_clans.extend(int(gid) for gid in gids)
        result["request_groups"] = len(gids)
        client.send(message)
        response = client.wait_msg(EMsg.ClientGetClanActivityCountsResponse, timeout=16)
        if response is not None:
            result["response_eresult"] = int(response.body.eresult)
        # ClanState updates can arrive separately from response.
        client.sleep(12)
        result["status"] = "received_clan_states" if result["clan_states"] else "no_clan_states"
    except Exception as exc:
        result["status"] = "exception"
        result["error_type"] = type(exc).__name__
        # Never print exception message or any credentials.
    finally:
        result["seconds"] = round(time.monotonic() - start, 2)
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
    return result


def main():
    start = time.monotonic()
    cache = json.loads(Path("data/steam_followers_cache.json").read_text(encoding="utf-8"))
    cached = cache.get("games", {})
    session = requests.Session()
    session.headers.update({"User-Agent": "GameTrendRadarClanProbe/1.0"})
    rows = []
    for index, appid in enumerate(APPIDS):
        if index:
            time.sleep(2.0)
        row = probe_xml(session, appid)
        old = cached.get(str(appid))
        if old:
            row["prior_cached_member_count"] = old.get("followers")
            row["prior_checked_at"] = old.get("checked_at")
        rows.append(row)
        print("XML", appid, row.get("xml_http"), row["xml_status"],
              "groupID64" if row.get("group_id64") else "no_groupID64",
              "members" if "xml_member_count" in row else "no_members",
              flush=True)
        if row.get("xml_http") == 429:
            print("Rate limit reached; stop XML requests.", flush=True)
            break

    key = os.environ.get("STEAM_WEB_API_KEY", "").strip()
    for row in rows[:6]:
        if not key:
            row["vanity"] = {"attempted": False, "reason": "key_not_available"}
            continue
        row["vanity"] = probe_vanity(session, key, row["appid"])
        if row["vanity"].get("group_id64") and not row.get("group_id64"):
            row["group_id64"] = row["vanity"]["group_id64"]
        print("VANITY", row["appid"], row["vanity"].get("http"),
              row["vanity"].get("success"), flush=True)
        time.sleep(0.6)

    cm = probe_cm(rows)
    for row in rows:
        gid = row.get("group_id64")
        state = cm["clan_states"].get(gid) if gid else None
        row["cm_member_count"] = state.get("members") if state else None
        if row["cm_member_count"] is not None and row.get("xml_member_count") is not None:
            row["cm_xml_difference"] = row["cm_member_count"] - row["xml_member_count"]

    stats = {
        "sampled": len(rows),
        "xml_with_member_count": sum("xml_member_count" in x for x in rows),
        "xml_with_group_id64": sum("group_id64" in x for x in rows),
        "vanity_successes": sum(x.get("vanity", {}).get("success", False) for x in rows),
        "cm_with_member_count": sum(x["cm_member_count"] is not None for x in rows),
        "cm_xml_compared": sum("cm_xml_difference" in x for x in rows),
        "cm_xml_exact_matches": sum(x.get("cm_xml_difference") == 0 for x in rows
                                   if "cm_xml_difference" in x),
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "read_only_clan_batch_pilot",
        "statistics": stats,
        "cm": cm,
        "samples": rows,
        "total_seconds": round(time.monotonic() - start, 2),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("SUMMARY", json.dumps(stats, ensure_ascii=False), flush=True)
    print("CM", json.dumps(cm, ensure_ascii=False), flush=True)
    print("TOTAL_SECONDS", report["total_seconds"], flush=True)


if __name__ == "__main__":
    main()
