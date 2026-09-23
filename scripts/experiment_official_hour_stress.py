"""One-off 55-minute MAX official Steam Followers throughput probe.

Release-date-first 1,317-candidate frozen queue. Starts at the proven
28-second spacing; progresses through 22,18,15,12,10,8 seconds ONLY after
successes. Single request at a time. First 429 or access/server problem halts
the test. Writes every response locally and pushes progress to GitHub on each
five successes or each five minutes; never assumes 429 means 0 followers.

This is NOT a new GitHub cron, and not a production cache update.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json,subprocess,time
import xml.etree.ElementTree as ET
import requests

TZ=ZoneInfo("Asia/Taipei")
ROOT=Path("experiments/steam_official_nearfirst_20260922")
SOURCE=ROOT/"source_queue.json"
GROUPS=ROOT/"source_unresolved.json"
STATE=ROOT/"checkpoint.json"
OUT=Path("output/steam_official_hour_stress")
BASE=103582791429521408
COHORT="steam_fresh_20260922_post_adult_1317_near_release"
# (successful calls to complete in that level, minimum seconds between starts)
PHASES=[("baseline",8,28.0),("gentle",10,22.0),("moderate",12,18.0),
        ("faster",16,15.0),("high",20,12.0),("higher",20,10.0),
        ("limit_probe",10**9,8.0)]


def ts():
    return datetime.now(TZ).isoformat()

def store(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+".tmp")
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    temp.replace(path)

def push_checkpoint():
    # Persist intermediate progress beyond a runner/job timeout. Never push
    # source queue modifications or any production data.
    try:
        subprocess.run(["git","add",str(STATE)],check=True,stdout=subprocess.DEVNULL,timeout=15)
        diff=subprocess.run(["git","diff","--cached","--quiet"],timeout=15)
        if diff.returncode==0:
            return True
        subprocess.run(["git","commit","-m","experiment: persist official Followers stress-test progress"],
                       check=True,stdout=subprocess.DEVNULL,timeout=20)
        subprocess.run(["git","pull","--rebase","origin","main"],check=True,
                       stdout=subprocess.DEVNULL,timeout=30)
        subprocess.run(["git","push","origin","HEAD:main"],check=True,
                       stdout=subprocess.DEVNULL,timeout=40)
        print("STRESS_DURABLE_PUSH OK",flush=True)
        return True
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
        print("STRESS_DURABLE_PUSH FAILED type="+type(exc).__name__,flush=True)
        return False

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--max-seconds",type=int,default=3600)
    p.add_argument("--max-attempts",type=int,default=500)
    p.add_argument("--checkpoint-every",type=int,default=5)
    args=p.parse_args()
    if not 60<=args.max_seconds<=3660 or not 1<=args.max_attempts<=500:
        raise ValueError("Unsafe stress duration or maximum request count")
    cohort=json.loads(SOURCE.read_text(encoding="utf-8"))
    unresolved=json.loads(GROUPS.read_text(encoding="utf-8"))
    cp=json.loads(STATE.read_text(encoding="utf-8"))
    if len(cohort)!=1317 or len(unresolved)!=1358 or cp.get("cohort")!=COHORT:
        raise ValueError("Frozen cohort or checkpoint mismatch")
    bygroup={int(r["appid"]):r.get("group_short_id") for r in unresolved}
    ids=[int(x["appid"]) for x in cohort]
    if len(set(ids))!=1317 or any(a not in bygroup for a in ids):
        raise ValueError("Bad cohort AppIDs")
    if any(int(i) not in ids for i in cp["official_results"]):
        raise ValueError("Checkpoint contains AppID outside the frozen cohort")
    if cp.get("next_request_after_taipei") and datetime.now(TZ)<datetime.fromisoformat(cp["next_request_after_taipei"]):
        raise SystemExit("Steam official cooldown active; do not stress test before cooldown")
    initial_done=len(cp["official_results"])
    began=time.monotonic()
    start_taipei=ts()
    date=datetime.now(TZ).date().isoformat()
    todo=sorted((r for r in cohort if str(r["appid"]) not in cp["official_results"]),
                key=lambda r:(r["release_date"]<date,r["release_date"],int(r["appid"])))
    session=requests.Session()
    session.headers.update({"User-Agent":"GameTrendRadarOfficialFollowerStressPilot/1.0"})
    phase_idx=0
    phase_success=0
    phase_counts={name:{"successes":0,"http_429":0,"errors":0,"interval_s":interval}
                  for name,_,interval in PHASES}
    results=[]
    last_start=None
    attempted=0
    stop="time_budget" if todo else "complete"
    last_push=time.monotonic()
    deferred=0

    def dump():
        elapsed=round(time.monotonic()-began,2)
        attempted_429=sum(x["http"]==429 for x in results)
        successful=[x for x in results if x["status"]=="ok"]
        report={
            "experiment":"one_hour_official_follower_throughput",
            "start_taipei":start_taipei,"latest_update_taipei":ts(),
            "duration_seconds":elapsed,"duration_budget_seconds":args.max_seconds,
            "input_1317":len(cohort),"initial_done":initial_done,
            "new_official_records":len(successful),
            "total_official_records":len(cp["official_results"]),
            "official_ge5000_new":sum(x.get("official_followers",0)>=5000 for x in successful),
            "pending":len(cohort)-len(cp["official_results"]),
            "requests_sent":attempted,
            "http_429":attempted_429,
            "stop_reason":stop,
            "phases":phase_counts,
            "successful_per_elapsed_hour":round(len(successful)*3600/max(elapsed,0.1),2),
            "no_parallel_queries":True,
            "production_cache_modified":False,
        }
        store(OUT/"report.json",report)
        store(OUT/"attempts.json",results)
        store(STATE,cp)
        print("STRESS_PROGRESS "+json.dumps(report,ensure_ascii=False,sort_keys=True),flush=True)
        return report

    print("STRESS_START "+json.dumps({"already_verified":initial_done,
        "pending":len(todo),"date_prioritized_first_5":[int(x["appid"]) for x in todo[:5]],
        "start_taipei":start_taipei,"phases":PHASES,
        "max_seconds":args.max_seconds},ensure_ascii=False),flush=True)

    for row in todo[:args.max_attempts]:
        elapsed=time.monotonic()-began
        if elapsed>=args.max_seconds-35:
            stop="time_budget";break
        name,limit,interval=PHASES[phase_idx]
        if last_start is not None:
            delay=interval-(time.monotonic()-last_start)
            if delay>0:
                if time.monotonic()+delay>began+args.max_seconds-35:
                    stop="time_budget";break
                time.sleep(delay)
        last_start=time.monotonic()
        aid=int(row["appid"])
        sid=bygroup[aid]
        gid=str(BASE+sid) if isinstance(sid,int) else None
        url=(f"https://steamcommunity.com/gid/{gid}/memberslistxml/?xml=1"
             if gid else f"https://steamcommunity.com/games/{aid}/memberslistxml/?xml=1")
        current={"appid":aid,"name":row.get("name"),"release_date":row.get("release_date"),
                 "attempted_at_taipei":ts(),"phase":name,"interval_s":interval,
                 "http":None,"status":"unknown"}
        attempted+=1
        try:
            response=session.get(url,timeout=(7,24))
            current["http"]=response.status_code
            if response.status_code==429:
                current["status"]="rate_limited"
                phase_counts[name]["http_429"]+=1
                cp["rate_limit_count"]=int(cp.get("rate_limit_count",0))+1
                hours=48 if cp["rate_limit_count"]==1 else min(168,48*2**min(cp["rate_limit_count"]-1,2))
                cp["next_request_after_taipei"]=(datetime.now(TZ)+timedelta(hours=hours)).isoformat()
                cp.setdefault("attempt_events",[]).append({"appid":aid,"when":ts(),"http":429,
                    "status":"stress_rate_limit","next_try_after":cp["next_request_after_taipei"]})
                stop="first_http_429"
            elif response.status_code in (401,403) or response.status_code>=500:
                current["status"]="upstream_access_or_server_error"
                phase_counts[name]["errors"]+=1
                cp["next_request_after_taipei"]=(datetime.now(TZ)+timedelta(hours=24)).isoformat()
                cp.setdefault("attempt_events",[]).append({"appid":aid,"when":ts(),
                    "http":response.status_code,"status":"stress_upstream_error"})
                stop="upstream_access_or_server_error"
            elif response.status_code!=200:
                current["status"]="unexpected_http"
                phase_counts[name]["errors"]+=1
                stop="unexpected_http"
            else:
                root=ET.fromstring(response.content)
                members=root.findtext(".//memberCount")
                xml_gid=root.findtext(".//groupID64")
                if not members or not members.strip().replace(",","").isdigit() or not xml_gid or not xml_gid.isdigit() or (gid is not None and gid!=xml_gid):
                    current["status"]="missing_member_count_or_group_mismatch"
                    phase_counts[name]["errors"]+=1
                    stop="malformed_or_mismatched_group"
                else:
                    follower=int(members.strip().replace(",",""))
                    current["status"]="ok"
                    current["official_followers"]=follower
                    cp["official_results"][str(aid)]={
                        "appid":aid,"name":row.get("name"),
                        "release_date":row["release_date"],"group_id64":xml_gid,
                        "steam_url":row.get("steam_url"),
                        "official_followers":follower,"official_ge5000":follower>=5000,
                        "official_checked_at_taipei":ts(),
                        "official_source":"Steam Community memberslistxml memberCount",
                    }
                    cp["rate_limit_count"]=0
                    cp["next_request_after_taipei"]=None
                    cp["last_checked_at_taipei"]=ts()
                    cp.setdefault("attempt_events",[]).append({
                        "appid":aid,"when":ts(),"http":200,
                        "status":"stress_ok","interval_s":interval,
                    })
                    phase_counts[name]["successes"]+=1
                    phase_success+=1
                    deferred+=1
                    if phase_success>=limit and phase_idx<len(PHASES)-1:
                        phase_idx+=1
                        phase_success=0
                        print("STRESS_PHASE_ADVANCE phase="+PHASES[phase_idx][0]
                              +" interval="+str(PHASES[phase_idx][2]),flush=True)
        except (requests.RequestException,ET.ParseError) as exc:
            current["status"]="transport_or_xml_error"
            current["error_type"]=type(exc).__name__
            phase_counts[name]["errors"]+=1
            cp["next_request_after_taipei"]=(datetime.now(TZ)+timedelta(hours=12)).isoformat()
            stop="transport_or_xml_error"
        results.append(current)
        if current["status"]=="ok":
            print("STRESS_ITEM "+json.dumps({"appid":aid,"name":current["name"],
                  "followers":current["official_followers"],"interval_s":interval,
                  "elapsed_s":round(time.monotonic()-began,1)},ensure_ascii=False),flush=True)
        else:
            print("STRESS_STOP_ITEM "+json.dumps(current,ensure_ascii=False),flush=True)
        dump()
        if deferred>=args.checkpoint_every or time.monotonic()-last_push>=240 or stop not in ("time_budget","batch_complete"):
            ok=push_checkpoint()
            if not ok:
                stop="github_checkpoint_push_failure"
                dump()
                break
            deferred=0
            last_push=time.monotonic()
        if current["status"]!="ok":
            break
    else:
        if len(cp["official_results"])==len(cohort):
            stop="all_1317_complete"
        elif attempted>=args.max_attempts:
            stop="request_count_cap"
        else:
            stop="time_budget"
    final=dump()
    if deferred>0:
        if not push_checkpoint():
            stop="github_checkpoint_push_failure"
            final=dump()
    print("STRESS_FINAL "+json.dumps(final,ensure_ascii=False,sort_keys=True),flush=True)

if __name__=="__main__":
    main()
