"""Third read-only probe: clan metadata with the existing API key + CM message routing.

No Steam user credentials or tokens are accepted. Neither key nor HTTP
request/response bodies are logged. Never modifies production state or cache.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

APPIDS = (2272360, 1488490, 3984090)
OUT = Path("output/steam_clan_diagnostics.json")
VANITY = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
METADATA = "https://api.steampowered.com/ICommunityService/GetClanMetadata/v1/"


def group_mapping(key):
    if not key:
        return {"status": "missing_key", "groups": []}
    session = requests.Session()
    rows = []
    for appid in APPIDS:
        row = {"appid": appid, "success": False}
        try:
            resp = session.get(
                VANITY,
                params={"key": key, "vanityurl": str(appid), "url_type": 3},
                timeout=15,
            )
            row["http"] = resp.status_code
            if resp.status_code == 200:
                data = resp.json().get("response", {})
                sid = data.get("steamid")
                row["success"] = bool(data.get("success") == 1 and str(sid).isdigit())
                if row["success"]:
                    row["group_id64"] = str(sid)
        except (requests.RequestException, ValueError) as exc:
            row["error_type"] = type(exc).__name__
        rows.append(row)
        time.sleep(1)
    return {"status": "complete", "groups": rows}


def numeric_member_fields(value, prefix="", depth=0):
    """Only collect count-like fields, never dump whole API response."""
    if depth > 4:
        return {}
    results = {}
    if isinstance(value, dict):
        for key, sub in value.items():
            name = str(key)
            dotted = (prefix + "." if prefix else "") + name
            if isinstance(sub, (str, int, float)) and any(
                fragment in name.lower()
                for fragment in ("member", "follower", "subscriber", "user_count", "num_user")
            ):
                if isinstance(sub, (int, float)) or str(sub).isdigit():
                    results[dotted] = sub
            elif isinstance(sub, dict):
                results.update(numeric_member_fields(sub, dotted, depth + 1))
    return results


def metadata_probe(key, groups):
    if not key:
        return [{"status": "missing_key"}]
    sess = requests.Session()
    items = []
    for row in groups[:2]:
        if not row.get("group_id64"):
            continue
        item = {"appid": row["appid"], "status": "unavailable"}
        try:
            response = sess.get(
                METADATA,
                params={"key": key, "steamid": row["group_id64"]},
                timeout=15,
            )
            item["http"] = response.status_code
            if response.status_code == 200:
                body = response.json()
                item["top_level_fields"] = sorted(body) if isinstance(body, dict) else []
                item["potential_member_counts"] = numeric_member_fields(body)
            item["status"] = "ok" if response.status_code == 200 else "http_error"
        except (requests.RequestException, ValueError) as exc:
            item["status"] = "exception"
            item["error_type"] = type(exc).__name__
        items.append(item)
        print("METADATA", item["appid"], item.get("http"), item["status"],
              "count_fields", len(item.get("potential_member_counts", {})), flush=True)
        if item.get("http") in (429, 401, 403):
            break
        time.sleep(2)
    return items


def cm_diagnostic(group_ids):
    result = {
        "status": "not_attempted", "anonymous_login_result": None,
        "group_count": min(3, len(group_ids)), "response_result": None,
        "clan_states": {}, "events": [], "connection_events": [],
        "duration_seconds": 0,
    }
    if len(group_ids) < 2:
        result["status"] = "insufficient_group_ids"
        return result
    try:
        from steam.client import SteamClient
        from steam.core.msg import MsgProto
        from steam.enums import EResult
        from steam.enums.emsg import EMsg
    except Exception as exc:
        result["status"] = "import_failure"
        result["error_type"] = type(exc).__name__
        return result

    client = SteamClient()
    started = time.monotonic()
    try:
        @client.on(EMsg.ClientGetClanActivityCountsResponse)
        def on_count_response(message):
            val = getattr(message.body, "eresult", None)
            result["response_result"] = int(val) if val is not None else None
            result["events"].append("ClientGetClanActivityCountsResponse")

        @client.on(EMsg.ClientClanState)
        def on_state(message):
            body = message.body
            gid = str(getattr(body, "steamid_clan", ""))
            if gid in group_ids:
                counts = getattr(body, "user_counts", None)
                result["clan_states"][gid] = {
                    "has_user_counts": bool(counts and counts.HasField("members")),
                    "members": int(counts.members)
                    if counts is not None and counts.HasField("members") else None,
                }
                result["events"].append("ClientClanState_known_group")
            else:
                result["events"].append("ClientClanState_other_group")

        @client.on(client.EVENT_DISCONNECTED)
        def on_disconnect(*args):
            result["connection_events"].append("disconnected")

        @client.on(client.EVENT_ERROR)
        def on_error(*args):
            result["connection_events"].append("error")

        @client.on(client.EVENT_RECONNECT)
        def on_reconnect(*args):
            result["connection_events"].append("reconnect")

        login = client.anonymous_login()
        result["anonymous_login_result"] = int(login)
        print("CM login code", int(login), flush=True)
        if login != EResult.OK:
            result["status"] = "login_failure"
            return result

        msg = MsgProto(EMsg.ClientGetClanActivityCounts)
        msg.body.steamid_clans.extend(int(gid) for gid in group_ids[:3])
        client.send(msg)
        result["status"] = "sent"
        print("CM sent clan activity count request for", min(3, len(group_ids)),
              "groups", flush=True)
        # Existing probe waited 16 seconds + 12 seconds; this one watches all
        # response/connection events for at most 34 seconds.
        client.sleep(34)
        result["status"] = (
            "clan_states_received" if result["clan_states"]
            else "response_without_state" if result["response_result"] is not None
            else "no_response_or_state"
        )
    except Exception as exc:
        result["status"] = "exception"
        result["error_type"] = type(exc).__name__
    finally:
        result["duration_seconds"] = round(time.monotonic() - started, 2)
        try:
            client.disconnect()
        except Exception:
            pass
    print("CM_DIAGNOSTIC", json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main():
    key = os.environ.get("STEAM_WEB_API_KEY", "").strip()
    mapping = group_mapping(key)
    groups = mapping["groups"]
    meta = metadata_probe(key, groups)
    ids = [row["group_id64"] for row in groups if "group_id64" in row]
    cm = cm_diagnostic(ids)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "test": "read_only_steam_clan_diagnostics",
        "vanity": mapping,
        "metadata": meta,
        "cm": cm,
        "auth": "anonymous_only_no_credentials",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("SUMMARY", json.dumps({
        "mapping_success": sum(row.get("success", False) for row in groups),
        "metadata_http": [item.get("http") for item in meta],
        "metadata_count_fields": [item.get("potential_member_counts") for item in meta],
        "cm_status": cm["status"], "cm_count": len(cm["clan_states"]),
        "cm_response": cm["response_result"],
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
