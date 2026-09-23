"""Dynamic Steam official Followers backfill, invoked by ChatGPT's hourly schedule.

Read-only upstream discovery + prefilter snapshots. Official XML outcomes are
written ONLY to the pre-existing isolated experiment checkpoint, NEVER to
production cache/front-end/discovery cursors. The old frozen 1,317 queue is
preserved; new eligible + third-party-unresolved AppIDs join automatically.
No GitHub cron. The external trigger has no work if no games are pending.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json, re, subprocess, time, xml.etree.ElementTree as ET
import requests

TZ=ZoneInfo("Asia/Taipei")
ROOT=Path("experiments/steam_official_nearfirst_20260922")
CHECKPOINT=ROOT/"checkpoint.json"
FROZEN=ROOT/"source_queue.json"
FROZEN_GROUPS=ROOT/"source_unresolved.json"
ELIGIBLE=Path("data/steam_candidates_eligible.json")
PREFILTER=Path("data/steam_prefilter_state.json")
CACHE=Path("data/steam_followers_cache.json")
OUT=Path("output/steam_official_dynamic_batch")
BASE=103582791429521408
DAY=re.compile(r"^20\d{2}-\d{2}-\d{2}$")


def now():
    return datetime.now(TZ)

def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+".tmp")
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.replace(path)

def load(path):
    return json.loads(path.read_text(encoding="utf-8"))

def durable_push():
    """Push only the isolated checkpoint. Do not alter formal app data."""
    try:
        subprocess.run(["git","add",str(CHECKPOINT)],check=True,timeout=20,capture_output=True)
        status=subprocess.run(["git","diff","--cached","--quiet"],timeout=15,capture_output=True)
        if status.returncode==0:
            return True
        subprocess.run(["git","commit","-m","experiment: checkpoint official dynamic Followers backfill"],
                       check=True,timeout=25,capture_output=True)
        subprocess.run(["git","pull","--rebase","origin","main"],
                       check=True,timeout=40,capture_output=True)
        subprocess.run(["git","push","origin","HEAD:main"],
                       check=True,timeout=40,capture_output=True)
        print("DYNAMIC_CHECKPOINT_PUSH OK",flush=True)
        return True
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
        print("DYNAMIC_CHECKPOINT_PUSH FAILED",type(exc).__name__,flush=True)
        return False

def date_for(row):
    date=row.get("release_start") or row.get("release_date")
    if not isinstance(date,str) or not DAY.fullmatch(date):
        return None
    try:
        datetime.fromisoformat(date)
    except ValueError:
        return None
    return date

def candidate_queue(frozen,group_rows,eligible,prefilter,cache,checkpoint):
    if not isinstance(frozen,list) or not isinstance(group_rows,list):
        raise ValueError("Missing frozen baseline source")
    if not isinstance(eligible.get("games"),list) or not isinstance(prefilter.get("games"),dict):
        raise ValueError("Missing valid daily/upstream eligible+prefilter snapshot")
    if eligible.get("count")!=len(eligible["games"]):
        raise ValueError("Daily upstream eligible count does not match actual rows")
    pre=prefilter["games"]
    known_groups={str(r["appid"]):r.get("group_short_id") for r in group_rows}
    old={str(r["appid"]):r for r in frozen}
    if len(old)!=len(frozen):
        raise ValueError("Frozen baseline contains duplicate AppIDs")
    official=set(checkpoint["official_results"])
    cache_ids=set((cache.get("games") or {}).keys())
    eligible_missing={}
    awaiting_prefilter=0
    for row in eligible["games"]:
        key=str(int(row["appid"]))
        if key not in pre:
            awaiting_prefilter+=1
            continue
        # No unverified / still-pending prefilter input is interpreted as zero.
        if pre[key].get("third_party_followers") is not None:
            continue
        if row.get("sexual_content_screened") is not True or date_for(row) is None:
            continue
        eligible_missing[key]=row
    # Snapshot was not expanded by Steam discovery since its prefilter completed?
    # Still preserve the frozen cohort even if the new daily snapshot has moved on.
    pending={}
    for key,row in old.items():
        if key in official or key in cache_ids:
            continue
        d=date_for(row)
        if d:
            grp=known_groups.get(key)
            pending[key]={"appid":int(key),"name":row.get("name"),"release_date":d,
                          "group_short_id":grp,"origin":"frozen_20260922"}
    dynamic_added=0
    for key,row in eligible_missing.items():
        if key in official or key in cache_ids:
            continue
        grp=pre[key].get("group_short_id")
        if grp is None:
            grp=known_groups.get(key)
        # Only items already present in the upstream prefilter snapshot qualify.
        d=date_for(row)
        if key not in pending:
            dynamic_added+=1
        pending[key]={"appid":int(key),"name":row.get("name"),"release_date":d,
                      "group_short_id":grp,"origin":"daily_eligible_missing"}
    today=now().date()
    def rank(row):
        d=datetime.fromisoformat(row["release_date"]).date()
        return (d<today,d.toordinal() if d>=today else -d.toordinal(),row["appid"])
    queue=sorted(pending.values(),key=rank)
    info={
        "frozen_baseline":len(frozen),
        "eligible_snapshot_count":len(eligible["games"]),
        "eligible_screened_at":eligible.get("screened_at"),
        "prefilter_updated_at":prefilter.get("updated_at"),
        "upstream_prefilter_complete":prefilter.get("complete"),
        "eligible_without_prefilter_record":awaiting_prefilter,
        "eligible_missing_followers":len(eligible_missing),
        "extra_daily_missing_not_in_frozen":sum(key not in old for key in eligible_missing),
        "new_entries_not_in_pending_frozen":dynamic_added,
        "official_checkpoint_entries":len(official),
        "production_cache_entries":len(cache_ids),
        "pending_total":len(queue),
        "pending_soonest":queue[0]["release_date"] if queue else None,
    }
    return queue,info

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--max-requests",type=int,default=250)
    parser.add_argument("--interval",type=float,default=8)
    parser.add_argument("--max-seconds",type=int,default=3000)
    parser.add_argument("--checkpoint-every",type=int,default=10)
    parser.add_argument("--dry-run",action="store_true")
    args=parser.parse_args()
    if not 1<=args.max_requests<=250 or args.interval<8 or not 60<=args.max_seconds<=3150:
        raise ValueError("Batch limits outside authorized 250, 8sec, 50min")
    cp=load(CHECKPOINT)
    if cp.get("cohort")!="steam_fresh_20260922_post_adult_1317_near_release":
        raise ValueError("Unknown official checkpoint cohort; refusing to overwrite")
    queue,info=candidate_queue(load(FROZEN),load(FROZEN_GROUPS),load(ELIGIBLE),
                               load(PREFILTER),load(CACHE),cp)
    begun=time.monotonic()
    started=now().isoformat()
    attempts=[]
    done=0
    stop="no_pending" if not queue else "batch_complete"
    phase_last=None
    def report():
        result={
            **info,"started_at_taipei":started,"updated_at_taipei":now().isoformat(),
            "total_official_checkpoint":len(cp["official_results"]),
            "new_official_this_batch":done,
            "new_ge5000":sum(a.get("official_followers",0)>=5000
                             for a in attempts if a.get("status")=="ok"),
            "pending_at_end_of_batch":max(0,len(queue)-done),
            "requests_this_batch":len(attempts),
            "http_429":sum(a.get("http")==429 for a in attempts),
            "rate_limit_count":cp.get("rate_limit_count",0),
            "next_request_after_taipei":cp.get("next_request_after_taipei"),
            "elapsed_seconds":round(time.monotonic()-begun,2),
            "status":stop,"interval_seconds":args.interval,
            "batch_cap":args.max_requests,
            "read_only_upstream":True,
            "production_data_unchanged":True,
        }
        save(OUT/"report.json",result)
        save(OUT/"attempts.json",attempts)
        save(OUT/"next_20_by_date.json",queue[done:done+20])
        print("DYNAMIC_PROGRESS "+json.dumps(result,ensure_ascii=False,sort_keys=True),flush=True)
        return result
    print("DYNAMIC_QUEUE "+json.dumps(info,ensure_ascii=False,sort_keys=True),flush=True)
    if args.dry_run:
        stop="dry_run_no_official_requests"
        print("DYNAMIC_FINAL "+json.dumps(report(),ensure_ascii=False,sort_keys=True),flush=True)
        return
    cooldown=cp.get("next_request_after_taipei")
    if cooldown and now()<datetime.fromisoformat(cooldown):
        stop="cooldown_active_no_requests"
        print("DYNAMIC_FINAL "+json.dumps(report(),ensure_ascii=False,sort_keys=True),flush=True)
        return
    if not queue:
        print("DYNAMIC_FINAL "+json.dumps(report(),ensure_ascii=False,sort_keys=True),flush=True)
        return
    session=requests.Session()
    session.headers.update({"User-Agent":"GameTrendRadarOfficialDynamic250/1.0"})
    staged=0
    for row in queue[:args.max_requests]:
        if time.monotonic()-begun>=args.max_seconds-32:
            stop="batch_time_budget"
            break
        if phase_last is not None:
            delay=args.interval-(time.monotonic()-phase_last)
            if delay>0:
                if time.monotonic()+delay>begun+args.max_seconds-32:
                    stop="batch_time_budget"
                    break
                time.sleep(delay)
        phase_last=time.monotonic()
        appid=row["appid"]
        sid=row.get("group_short_id")
        if sid is not None and (not isinstance(sid,int) or sid<0):
            stop="invalid_group_short_id"
            break
        gid=str(BASE+sid) if sid is not None else None
        url=(f"https://steamcommunity.com/gid/{gid}/memberslistxml/?xml=1"
             if gid else f"https://steamcommunity.com/games/{appid}/memberslistxml/?xml=1")
        entry={"appid":appid,"release_date":row["release_date"],
               "checked_at_taipei":now().isoformat(),"http":None,"status":"pending"}
        try:
            response=session.get(url,timeout=(7,24))
            entry["http"]=response.status_code
            if response.status_code==429:
                entry["status"]="rate_limited"
                cp["rate_limit_count"]=int(cp.get("rate_limit_count",0))+1
                hours=min(168,48*2**min(cp["rate_limit_count"]-1,2))
                cp["next_request_after_taipei"]=(now()+timedelta(hours=hours)).isoformat()
                stop="first_429_stop"
            elif response.status_code in (401,403) or response.status_code>=500:
                entry["status"]="upstream_access_or_server_error"
                cp["next_request_after_taipei"]=(now()+timedelta(hours=24)).isoformat()
                stop="upstream_access_or_server_error"
            elif response.status_code!=200:
                entry["status"]="unexpected_http"
                stop="unexpected_http_stop"
            else:
                xml=ET.fromstring(response.content)
                raw=xml.findtext(".//memberCount")
                xml_gid=xml.findtext(".//groupID64")
                if (raw is None or not raw.strip().replace(",","").isdigit()
                    or not xml_gid or not xml_gid.isdigit()
                    or (gid and xml_gid!=gid)):
                    entry["status"]="missing_count_or_group_mismatch"
                    stop="unverified_xml_stop"
                else:
                    members=int(raw.strip().replace(",",""))
                    entry["status"]="ok"
                    entry["official_followers"]=members
                    cp["official_results"][str(appid)]={
                        "appid":appid,"name":row["name"],
                        "release_date":row["release_date"],"group_id64":xml_gid,
                        "steam_url":f"https://store.steampowered.com/app/{appid}/",
                        "official_followers":members,
                        "official_ge5000":members>=5000,
                        "official_checked_at_taipei":entry["checked_at_taipei"],
                        "official_source":"Steam Community XML memberCount",
                        "origin":row["origin"],
                    }
                    cp["last_checked_at_taipei"]=entry["checked_at_taipei"]
                    cp["next_request_after_taipei"]=None
                    cp["rate_limit_count"]=0
                    done+=1
                    staged+=1
        except (requests.RequestException,ET.ParseError) as exc:
            entry["status"]="transport_or_xml_error"
            entry["error_type"]=type(exc).__name__
            cp["next_request_after_taipei"]=(now()+timedelta(hours=12)).isoformat()
            stop="transport_or_xml_error"
        attempts.append(entry)
        cp.setdefault("attempt_events",[]).append({
            "appid":appid,"when":entry["checked_at_taipei"],
            "status":"dynamic_"+entry["status"],"http":entry["http"],
        })
        save(CHECKPOINT,cp)
        print("DYNAMIC_ITEM "+json.dumps(entry,ensure_ascii=False),flush=True)
        if staged>=args.checkpoint_every or stop!="batch_complete":
            report()
            if not durable_push():
                stop="checkpoint_push_failure"
                break
            staged=0
        if entry["status"]!="ok":
            break
    final=report()
    if staged>0:
        if not durable_push():
            stop="checkpoint_push_failure"
            final=report()
    print("DYNAMIC_FINAL "+json.dumps(final,ensure_ascii=False,sort_keys=True),flush=True)

if __name__=="__main__":
    main()
