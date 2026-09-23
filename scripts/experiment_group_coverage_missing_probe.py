"""Isolated read-only probe for steam-groups.com unresolved-group coverage.

No Steam XML, no production JSON reads or writes. Uses frozen first-pass artifact.
Probes short-ID direct lookups and bulk for same group IDs, plus name search
to distinguish group database absence from API bulk response parsing issues.
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import requests

ROOT="https://api.steam-groups.com"
INPUT=Path("input")
OUT=Path("output/group_coverage_probe")
GROUP_BASE=103582791429521408
def load(name):
    return json.loads((INPUT/name).read_text(encoding="utf-8"))
def call(session, method, path, **kwargs):
    t=time.monotonic()
    try:
        response=session.request(method,ROOT+path,timeout=(8,20),**kwargs)
        row={"http":response.status_code,"duration_s":round(time.monotonic()-t,3)}
        if response.status_code==429:
            return row,None
        try:
            payload=response.json()
        except ValueError:
            row["invalid_json"]=True
            return row,None
        if isinstance(payload,dict):
            row["keys"]=sorted(payload)
            if isinstance(payload.get("error"),dict):
                row["error_code"]=payload["error"].get("code")
        return row,payload
    except requests.RequestException as exc:
        return {"error_type":type(exc).__name__},None
def item_matches(data,short_id):
    if not isinstance(data,dict):
        return None
    item=data.get("data") if isinstance(data.get("data"),dict) else data
    try:
        if int(item.get("id"))==short_id and isinstance(item.get("members"),int):
            return {"id":short_id,"members":item["members"],
                    "last_seen":item.get("isLastSeen")}
    except (ValueError,TypeError):
        pass
    return None
def main():
    unknown=[row for row in load("third_party_unresolved.json")
             if isinstance(row.get("group_short_id"),int)]
    known=[row for row in load("third_party_4000_priority.json")
           if isinstance(row.get("group_short_id"),int)]
    unknown.sort(key=lambda r:r["appid"])
    known.sort(key=lambda r:r["appid"])
    sample_unknown=[unknown[round(i*(len(unknown)-1)/29)] for i in range(30)]
    sample_known=[known[round(i*(len(known)-1)/9)] for i in range(10)]
    session=requests.Session()
    session.headers.update({"User-Agent":"GameTrendRadar-GroupMissingDiagnostic/1.0"})
    probes=[]
    for label,items in (("unknown",sample_unknown),("known",sample_known)):
        for row in items:
            short=int(row["group_short_id"])
            summary,data=call(session,"GET",f"/api/groups/{short}")
            probes.append({
                "set":label,"appid":row["appid"],"game_name":row.get("name"),
                "group_short_id":short,
                "original_bulk_followers":row.get("third_party_followers"),
                "direct":summary,"direct_match":item_matches(data,short),
            })
            if len(probes)==1 or len(probes)%10==0:
                recovered=sum(x["set"]=="unknown" and x["direct_match"] is not None for x in probes)
                print("GROUP_DIRECT_PROGRESS",len(probes),"/40 recovered_unknown",recovered,flush=True)
            if summary.get("http")==429:
                break
            time.sleep(0.35)
    selected_unknown=[int(x["group_short_id"]) for x in sample_unknown]
    bulk_short,bulk_data=call(session,"POST","/api/groups/bulk",
                              json={"ids":selected_unknown,"limit":len(selected_unknown)})
    bulk_short["found_count"]=len(bulk_data.get("data",[])) if isinstance(bulk_data,dict) and isinstance(bulk_data.get("data"),list) else None
    bulk_short["not_found_count"]=len(bulk_data.get("notFound",[])) if isinstance(bulk_data,dict) and isinstance(bulk_data.get("notFound"),list) else None
    # Test whether IDs might be SteamID64 even though older probe demonstrated short IDs
    full_ids=[GROUP_BASE+x for x in selected_unknown[:5]]
    bulk_full,full_data=call(session,"POST","/api/groups/bulk",
                            json={"ids":full_ids,"limit":len(full_ids)})
    bulk_full["found_count"]=len(full_data.get("data",[])) if isinstance(full_data,dict) and isinstance(full_data.get("data"),list) else None
    search_rows=[]
    for game in sample_unknown[:8]:
        name=game.get("name") or ""
        summary,data=call(session,"GET","/api/groups",
            params={"q":name,"searchType":"exact","limit":10})
        entries=data.get("data",[]) if isinstance(data,dict) else []
        search_rows.append({"appid":game["appid"],"name":name,
                            "summary":summary,"results_count":len(entries),
                            "ids":[x.get("id") for x in entries[:10] if isinstance(x,dict)]})
        if summary.get("http")==429:break
        time.sleep(0.35)
    stats={
        "source":"steam-groups.com group API",
        "timestamp_utc":datetime.now(timezone.utc).isoformat(),
        "unknown_input":len(unknown),"known_input":len(known),
        "unknown_direct_tested":sum(x["set"]=="unknown" for x in probes),
        "unknown_direct_recovered":sum(x["set"]=="unknown" and x["direct_match"] is not None for x in probes),
        "known_direct_tested":sum(x["set"]=="known" for x in probes),
        "known_direct_success":sum(x["set"]=="known" and x["direct_match"] is not None for x in probes),
        "unknown_direct_404":sum(x["set"]=="unknown" and x["direct"].get("http")==404 for x in probes),
        "unknown_direct_429":sum(x["set"]=="unknown" and x["direct"].get("http")==429 for x in probes),
        "bulk_missing_short":bulk_short,
        "bulk_missing_full64":bulk_full,
        "name_search_tests":len(search_rows),
        "name_search_nonempty":sum(x["results_count"]>0 for x in search_rows),
        "note":"A 404 means not indexed in this third-party source; it is not proof Steam game has no Followers."
    }
    OUT.mkdir(parents=True,exist_ok=True)
    for name,obj in (("report.json",stats),("direct_probes.json",probes),("name_search_probes.json",search_rows)):
        (OUT/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("GROUP_COVERAGE_FINAL",json.dumps(stats,ensure_ascii=False,sort_keys=True),flush=True)
if __name__=="__main__":main()
