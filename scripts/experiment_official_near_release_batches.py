"""Steam official follower-count near-release queue (frozen 2026-09-22 cohort).

Read-only except its own isolated checkpoint. Prioritizes release dates on/after
current Asia/Taipei date, preserves historical rows without mislabeling them zero,
and stops on the FIRST 429. Never reads/writes production data, cache or cursor.

Steam Community gid memberslistxml is documented by Valve; /games/{appid} path
was previously verified for this experiment. Verify XML groupID64 if mapping exists.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import time
import xml.etree.ElementTree as ET
import requests

TZ=ZoneInfo("Asia/Taipei")
BASE=103582791429521408
COHORT="steam_fresh_20260922_post_adult_1317_near_release"
INPUT="experiments/steam_official_nearfirst_20260922/source_queue.json"
SOURCE="experiments/steam_official_nearfirst_20260922/source_unresolved.json"
URL="https://steamcommunity.com/gid/{}/memberslistxml/?xml=1"

def now():
    return datetime.now(TZ)

def atomic(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+".tmp")
    temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    temp.replace(path)

def report_for(cohort,checkpoint,status,started,batch_requested=0):
    done=checkpoint["official_results"]
    pending=[r for r in cohort if str(r["appid"]) not in done]
    day=now().date().isoformat()
    today=[r for r in pending if r["release_date"]==day]
    week=[r for r in pending if day<=r["release_date"]<=(now().date()+timedelta(days=7)).isoformat()]
    return {
        "cohort":COHORT,
        "status":status,
        "checked_at_taipei":now().isoformat(),
        "total":len(cohort),
        "completed":len(done),
        "official_ge5000":sum(row["official_followers"]>=5000 for row in done.values()),
        "official_below5000":sum(row["official_followers"]<5000 for row in done.values()),
        "pending":len(pending),
        "pending_today":len(today),
        "pending_next_7_days":len(week),
        "pending_past_release":sum(r["release_date"]<day for r in pending),
        "requests_this_run":batch_requested,
        "next_request_after_taipei":checkpoint.get("next_request_after_taipei"),
        "rate_limit_count":checkpoint.get("rate_limit_count",0),
        "elapsed_seconds":round(time.monotonic()-started,2),
        "production_data_unchanged":True,
    }

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--checkpoint",default="experiments/steam_official_nearfirst_20260922/checkpoint.json")
    p.add_argument("--output",default="output/steam_official_nearfirst")
    p.add_argument("--max-requests",type=int,default=3)
    p.add_argument("--interval",type=float,default=28)
    p.add_argument("--max-seconds",type=int,default=210)
    a=p.parse_args()
    if not 1<=a.max_requests<=10 or a.interval<20 or a.max_seconds>600:
        raise ValueError("Unsafe batch settings")

    rows=json.loads(Path(INPUT).read_text(encoding="utf-8"))
    mapping=json.loads(Path(SOURCE).read_text(encoding="utf-8"))
    if len(rows)!=1317 or len({int(r["appid"]) for r in rows})!=1317 or len(mapping)!=1358:
        raise ValueError("Frozen source cohort changed")
    group_ids={int(r["appid"]):r.get("group_short_id") for r in mapping}
    cohort={}
    for r in rows:
        appid=int(r["appid"])
        if appid not in group_ids:
            raise ValueError("Missing AppID in original unmapped prefilter")
        if r.get("steam_groups_followers") is not None or r.get("games_popularity_followers") is not None:
            raise ValueError("Not a true zero-source candidate")
        if not isinstance(r.get("release_date"),str) or len(r["release_date"])!=10:
            raise ValueError("Missing exact release date")
        group=group_ids[appid]
        if group is not None and (not isinstance(group,int) or group<0):
            raise ValueError(f"Malformed Group ID for AppID {appid}")
        cohort[appid]={
            "appid":appid,"name":r.get("name"),"release_date":r["release_date"],
            "group_id64":str(BASE+group) if group is not None else None,
            "steam_url":r.get("steam_url"),
        }

    cp_path=Path(a.checkpoint)
    state=json.loads(cp_path.read_text(encoding="utf-8")) if cp_path.exists() else {}
    if state and state.get("cohort")!=COHORT:
        raise ValueError("Checkpoint cohort mismatch")
    if not state:
        state={
            "version":1,"cohort":COHORT,"official_results":{},
            "rate_limit_count":0,"next_request_after_taipei":None,
            "last_checked_at_taipei":None,
            "attempt_events":[],
        }
    done=state["official_results"]
    if any(int(appid) not in cohort for appid in done):
        raise ValueError("Saved official result outside frozen cohort")
    if any(not isinstance(x.get("official_followers"),int) for x in done.values()):
        raise ValueError("Malformed checkpoint numeric official count")

    out=Path(a.output)
    started=time.monotonic()
    attempted=0
    def persist(status):
        rep=report_for(list(cohort.values()),state,status,started,attempted)
        atomic(cp_path,state)
        atomic(out/"report.json",rep)
        atomic(out/"official_results.json",list(done.values()))
        atomic(out/"next_20_by_date.json",[
            row for row in sorted(
                (r for r in cohort.values() if str(r["appid"]) not in done),
                key=lambda r:(r["release_date"]<now().date().isoformat(),r["release_date"],r["appid"])
            )[:20]
        ])
        print("NEARFIRST_PROGRESS "+json.dumps(rep,ensure_ascii=False,sort_keys=True),flush=True)
        return rep

    ready=state.get("next_request_after_taipei")
    if ready and now()<datetime.fromisoformat(ready):
        result=persist("cooldown_active_no_request")
        print("NEARFIRST_FINAL "+json.dumps(result,ensure_ascii=False,sort_keys=True),flush=True)
        return

    persist("starting")
    today=now().date().isoformat()
    todo=sorted(
        (r for r in cohort.values() if str(r["appid"]) not in done),
        key=lambda r:(r["release_date"]<today,r["release_date"],r["appid"])
    )
    last=None
    stop="all_complete" if not todo else "batch_complete"
    session=requests.Session()
    session.headers.update({"User-Agent":"GameTrendRadarSteamOfficialSmallBatch/1.0"})
    for row in todo[:a.max_requests]:
        if time.monotonic()-started>a.max_seconds-35:
            stop="time_budget";break
        if last is not None:
            delay=a.interval-(time.monotonic()-last)
            if delay>0:time.sleep(delay)
        last=time.monotonic()
        aid=row["appid"]
        attempted+=1
        timestamp=now().isoformat()
        try:
            endpoint=(URL.format(row["group_id64"]) if row["group_id64"]
                      else f"https://steamcommunity.com/games/{aid}/memberslistxml/?xml=1")
            response=session.get(endpoint,timeout=(7,22))
            status=response.status_code
            if status==429:
                state["rate_limit_count"]+=1
                hours=48 if state["rate_limit_count"]==1 else min(168,48*2**min(state["rate_limit_count"]-1,2))
                state["next_request_after_taipei"]=(now()+timedelta(hours=hours)).isoformat()
                state["attempt_events"].append({"appid":aid,"when":timestamp,"http":429,"status":"rate_limited",
                    "next_try_after":state["next_request_after_taipei"]})
                stop="official_429_stop";persist(stop);break
            if status in (401,403):
                state["next_request_after_taipei"]=(now()+timedelta(hours=72)).isoformat()
                state["attempt_events"].append({"appid":aid,"when":timestamp,"http":status,"status":"access_denied"})
                stop="access_denied_stop";persist(stop);break
            if status>=500:
                state["next_request_after_taipei"]=(now()+timedelta(hours=12)).isoformat()
                state["attempt_events"].append({"appid":aid,"when":timestamp,"http":status,"status":"server_error"})
                stop="upstream_error_stop";persist(stop);break
            if status!=200:
                state["attempt_events"].append({"appid":aid,"when":timestamp,"http":status,"status":"http_error"})
                stop="http_error_stop";persist(stop);break
            root=ET.fromstring(response.content)
            raw=root.findtext(".//memberCount")
            got_gid=root.findtext(".//groupID64")
            if (not got_gid or not got_gid.isdigit() or
                    (row["group_id64"] is not None and got_gid!=row["group_id64"])):
                state["attempt_events"].append({"appid":aid,"when":timestamp,"http":status,
                     "status":"group_id_mismatch","expected":row["group_id64"],"got":got_gid})
                stop="group_id_mismatch_stop";persist(stop);break
            if not raw or not raw.strip().replace(",","").isdigit():
                state["attempt_events"].append({"appid":aid,"when":timestamp,"http":status,
                                               "status":"missing_member_count"})
                stop="missing_member_count_stop";persist(stop);break
            follower=int(raw.strip().replace(",",""))
            done[str(aid)]={
                **row,"group_id64":got_gid,"official_followers":follower,
                "official_ge5000":follower>=5000,
                "official_checked_at_taipei":timestamp,
                "official_source":"Steam Community gid64 memberslistxml memberCount",
            }
            state["last_checked_at_taipei"]=timestamp
            state["rate_limit_count"]=0
            state["next_request_after_taipei"]=None
            state["attempt_events"].append({"appid":aid,"when":timestamp,"http":200,"status":"ok"})
            persist("scanning")
        except (requests.RequestException,ET.ParseError) as exc:
            state["attempt_events"].append({"appid":aid,"when":timestamp,
                                          "status":"temporary_error","error_type":type(exc).__name__})
            state["next_request_after_taipei"]=(now()+timedelta(hours=12)).isoformat()
            stop="temporary_error_stop";persist(stop);break
    final=persist(stop)
    print("NEARFIRST_FINAL "+json.dumps(final,ensure_ascii=False,sort_keys=True),flush=True)

if __name__=="__main__":
    main()
