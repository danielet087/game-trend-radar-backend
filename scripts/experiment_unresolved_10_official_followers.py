"""One-off official Steam Community follower-count probe of the SAME ten random
games selected by the prior official appdetails sample (seed=20260923).
All 10 came from the frozen 1,317 no-third-party-number cohort.

Does not query appdetails or third parties. Official XML is documented by Valve.
Rate-limit safety: a 429 stops immediately, preserving the ten-game sample
and unqueried statuses. 429 is NEVER classified as 0 Followers.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import requests

SEED=20260923
APPIDS=[5033980,5238680,5180640,5130990,5186020,5211940,5012690,5262940,5242800,4959570]
IN=Path("input/first_source_missing_both_unmeasured.json")
OUT=Path("output/steam_official_followers_random10")
BASE=103582791429521408

def save(name, value):
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

def main():
    rows=json.loads(IN.read_text(encoding="utf-8"))
    assert len(rows)==1317,len(rows)
    by_id={int(row["appid"]):row for row in rows}
    assert len(by_id)==1317
    assert all(i in by_id for i in APPIDS)
    assert all(by_id[i].get("steam_groups_followers") is None
               and by_id[i].get("games_popularity_followers") is None for i in APPIDS)
    requests_session=requests.Session()
    requests_session.headers.update({"User-Agent":"Mozilla/5.0 (compatible; GameTrendRadar-OfficialFollowersRandom10/1.0)"})
    result=[{
        "appid":i,"name":by_id[i].get("name"),"release_date":by_id[i].get("release_date"),
        "group_short_id":by_id[i].get("group_short_id"),
        "source":"Steam Community official memberslistxml memberCount",
        "http":None,"status":"not_queried","official_followers":None,
    } for i in APPIDS]
    started=time.monotonic()
    print("OFFICIAL_FOLLOWERS_RANDOM10_START",json.dumps({"seed":SEED,"appids":APPIDS}),flush=True)
    stop_reason=None
    for index,row in enumerate(result):
        appid=row["appid"]
        group_id=(BASE+int(row["group_short_id"])) if isinstance(row["group_short_id"],int) else None
        row["group_id64"]=str(group_id) if group_id is not None else None
        # /games/<appid>/memberslistxml is the exact endpoint used by prior
        # verified results, while gid/<64>/memberslistxml is the Valve-documented
        # 64-bit group-ID version. One request per game; no appdetails.
        endpoint=f"https://steamcommunity.com/gid/{group_id}/memberslistxml/?xml=1" if group_id else f"https://steamcommunity.com/games/{appid}/memberslistxml/?xml=1"
        try:
            resp=requests_session.get(endpoint,timeout=(8,25))
            row["http"]=resp.status_code
            row["checked_at_utc"]=datetime.now(timezone.utc).isoformat()
            if resp.status_code==429:
                row["status"]="rate_limited"
                stop_reason="steam_community_429"
                print("OFFICIAL_FOLLOWERS_429 STOP",appid,flush=True)
                break
            if resp.status_code!=200:
                row["status"]="http_error"
            else:
                root=ET.fromstring(resp.content)
                val=root.findtext(".//memberCount")
                xml_gid=root.findtext(".//groupID64")
                if val and val.strip().replace(",","").isdigit():
                    row["official_followers"]=int(val.strip().replace(",",""))
                    row["official_group_id64_from_xml"]=xml_gid
                    row["group_id_match"]=group_id is None or xml_gid==str(group_id)
                    row["status"]="ok" if row["group_id_match"] else "group_id_mismatch"
                    if not row["group_id_match"]:
                        row["official_followers"]=None
                else:
                    row["status"]="missing_member_count"
        except requests.RequestException as exc:
            row["status"]="network_error"
            row["error_type"]=type(exc).__name__
        except ET.ParseError:
            row["status"]="invalid_xml"
        print("OFFICIAL_FOLLOWERS_RANDOM10_ITEM",json.dumps({
            "appid":appid,"name":row["name"],"http":row["http"],
            "status":row["status"],"followers":row["official_followers"]
        },ensure_ascii=False),flush=True)
        save("sample_results.json",result)
        if index!=len(result)-1:
            time.sleep(25.0)
    report={
        "seed":SEED,"frozen_unresolved_count":1317,"sample_size":10,
        "sample_appids":APPIDS,
        "attempted":sum(row["status"]!="not_queried" for row in result),
        "official_numeric_followers":sum(isinstance(row["official_followers"],int) for row in result),
        "official_ge5000":sum(isinstance(row["official_followers"],int) and row["official_followers"]>=5000 for row in result),
        "http_429":sum(row["http"]==429 for row in result),
        "not_queried":sum(row["status"]=="not_queried" for row in result),
        "stop_reason":stop_reason,
        "elapsed_s":round(time.monotonic()-started,2),
        "source":"Valve documented Steam Community gid/{GroupID}/memberslistxml/?xml=1, not Steam Store appdetails",
    }
    save("sample_results.json",result)
    save("report.json",report)
    print("OFFICIAL_FOLLOWERS_RANDOM10_FINAL",json.dumps(report,ensure_ascii=False,sort_keys=True),flush=True)

if __name__=="__main__":main()
