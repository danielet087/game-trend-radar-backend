"""Build a read-only, evidence-backed, resumable Steam follower work queue.

Inputs are solely the frozen September 22 experiment artifact and saved experiment
checkpoints. No production follower cache/candidate state/frontend files are read.
No network follower requests are made by this script.
"""
from __future__ import annotations
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = Path("input")
GP = Path("experiments/games_popularity_20260922/checkpoint.json")
OFFICIAL = Path("experiments/steam_official_followers_20260922/checkpoint.json")
OUT = Path("output/followers_pending_plan")
COHORT="steam_fresh_20260922_post_adult_3238"

def read(path):
    return json.loads(path.read_text(encoding="utf-8"))

def write(name,value):
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

def main():
    sources=[
        ("third_party_4000_priority.json","first_source_priority"),
        ("third_party_below_4000.json","first_source_below"),
        ("third_party_unresolved.json","first_source_unknown"),
    ]
    games={}
    for filename,band in sources:
        for row in read(SOURCE/filename):
            aid=int(row["appid"])
            if aid in games: raise ValueError("Duplicate AppID")
            games[aid]={**row,"source_band":band}
    if len(games)!=3238:raise ValueError(f"Bad frozen cohort {len(games)}")
    gp=read(GP)
    official=read(OFFICIAL)
    if gp.get("cohort")!=COHORT:raise ValueError("Bad GP checkpoint cohort")
    if official.get("cohort")!="steam_fresh_20260922_first_source_92":
        raise ValueError("Bad official checkpoint cohort")
    gp_results=gp["results"]
    confirmed=official["verified"]
    if any(int(x) not in games for x in gp_results): raise ValueError("GP ID outside cohort")
    if any(int(x) not in games for x in confirmed): raise ValueError("Official ID outside cohort")
    now=datetime.now(ZoneInfo("Asia/Taipei"))
    groups={"official_pending_ge4000":[],"first_source_missing_both_unmeasured":[],
            "near_3000_3999":[],"measured_below_3000":[],"missing_metric_only":[]}
    already_official=0
    for aid,source in games.items():
        if str(aid) in confirmed:
            already_official+=1
            continue
        first=source.get("third_party_followers")
        gp_result=gp_results.get(str(aid),{})
        second=gp_result.get("followers") if gp_result.get("status")=="measured" else None
        vals=[v for v in (first,second) if isinstance(v,int) and not isinstance(v,bool)]
        top=max(vals) if vals else None
        rec={
            "appid":aid,"name":source.get("name"),
            "release_date":source.get("release_date"),
            "steam_url":source.get("steam_url"),
            "steam_groups_followers":first,
            "games_popularity_followers":second,
            "games_popularity_observed_at":gp_result.get("observed_at"),
            "games_popularity_status":gp_result.get("status","not_queried"),
            "max_third_party_followers":top,
            "official_status":"pending",
            "possible_past_release":bool(source.get("release_date") and source["release_date"]<now.date().isoformat()),
        }
        if top is not None and top>=4000:
            groups["official_pending_ge4000"].append(rec)
        elif top is not None and top>=3000:
            groups["near_3000_3999"].append(rec)
        elif first is None and second is None:
            groups["first_source_missing_both_unmeasured"].append(rec)
        elif top is not None and top<3000:
            groups["measured_below_3000"].append(rec)
        else:
            groups["missing_metric_only"].append(rec)
    groups["official_pending_ge4000"].sort(key=lambda x:(-(x["max_third_party_followers"] or 0),x["release_date"] or "",x["appid"]))
    groups["near_3000_3999"].sort(key=lambda x:(-(x["max_third_party_followers"] or 0),x["release_date"] or "",x["appid"]))
    groups["first_source_missing_both_unmeasured"].sort(key=lambda x:(x["possible_past_release"],x["release_date"] or "",x["appid"]))
    groups["measured_below_3000"].sort(key=lambda x:(-(x["max_third_party_followers"] or 0),x["release_date"] or "",x["appid"]))
    report={
        "cohort":COHORT,
        "generated_at_taipei":now.isoformat(),
        "date_note":"These are FROZEN 2026-09-22 dates, not a refreshed upcoming-year scan. Already-passed release days remain marked but are not silently erased.",
        "frozen_input":len(games),
        "official_verified":already_official,
        "official_qualified_5000":sum(int(row["official_followers"])>=5000 for row in confirmed.values()),
        "official_below_5000":sum(int(row["official_followers"])<5000 for row in confirmed.values()),
        "groups":{k:len(v) for k,v in groups.items()},
        "all_records_partitioned":already_official+sum(map(len,groups.values()))==len(games),
        "no_fake_zero_followers":True,
        "official_ge4000_queue_is_not_yet_officially_verified":True,
        "official_xml_currently_rate_limited_from_hosted_runner":True,
    }
    if not report["all_records_partitioned"]:raise ValueError("Partition mismatch")
    for label,items in groups.items():write(label+".json",items)
    write("report.json",report)
    print("FOLLOWER_QUEUE_FINAL "+json.dumps(report,ensure_ascii=False,sort_keys=True),flush=True)
if __name__=="__main__":main()
