"""One-off Steam CM clan members experiment, with two independent CM requests.

Read-only, anonymous login; no account password, Steam Guard, Web API key,
production follower cache, production cursor, or website updates.

Same 10 random unresolved AppIDs as the September 23 appdetails experiment,
plus 10 existing official XML controls from the frozen independent cohort.
"""
from __future__ import annotations
from datetime import datetime,timezone
import json,random,time
from pathlib import Path

OUT=Path("output/cm_follower_members_pilot")
GROUP_BASE=103582791429521408
UNRESOLVED_10=[5033980,5238680,5180640,5130990,5186020,5211940,5012690,5262940,5242800,4959570]

def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
def save(report):
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

def main():
    first=load("input/third_party_4000_priority.json")
    unknown=load("input/third_party_unresolved.json")
    official=load("experiments/steam_official_followers_20260922/checkpoint.json")
    assert len(first)==92 and len(unknown)==1358
    assert official["cohort"]=="steam_fresh_20260922_first_source_92"
    verified=official["verified"]
    first_by={int(x["appid"]):x for x in first}
    un_by={int(x["appid"]):x for x in unknown}
    controls=sorted(
        [first_by[int(aid)] for aid,x in verified.items()
         if int(aid) in first_by and isinstance(x.get("official_followers"),int)
         and isinstance(first_by[int(aid)].get("group_short_id"),int)],
        key=lambda x:(-int(verified[str(x["appid"])]["official_followers"]),int(x["appid"]))
    )[:10]
    assert len(controls)==10
    assert all(aid in un_by and isinstance(un_by[aid].get("group_short_id"),int)
               for aid in UNRESOLVED_10)
    sample=[]
    for row in controls:
        aid=int(row["appid"])
        sample.append({"appid":aid,"name":row.get("name"),"group_id64":str(GROUP_BASE+int(row["group_short_id"])),
                       "sample_type":"known_official","prior_official_followers":int(verified[str(aid)]["official_followers"])})
    for aid in UNRESOLVED_10:
        row=un_by[aid]
        sample.append({"appid":aid,"name":row.get("name"),
                       "group_id64":str(GROUP_BASE+int(row["group_short_id"])),
                       "sample_type":"unresolved","prior_official_followers":None})
    assert len({r["group_id64"] for r in sample})==20
    group_lookup={r["group_id64"]:r for r in sample}
    report={
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "test":"steam_cm_clan_members_request_friend_data_vs_activity_counts",
        "credential_mode":"anonymous_login_no_account_credentials",
        "cohort":"steam_fresh_20260922_post_adult_3238",
        "sample":sample,"login":None,"requests_sent":[],
        "response_eresult":None,"state_messages":{},
        "persona_messages_for_groups":0,"connection_events":[],
        "status":"started","elapsed_seconds":0
    }
    save(report)
    started=time.monotonic()
    client=None
    try:
        from gevent import Timeout
        from steam.client import SteamClient
        from steam.core.msg import MsgProto
        from steam.enums import EResult
        from steam.enums.emsg import EMsg
        client=SteamClient()

        @client.on(EMsg.ClientClanState)
        def handle_state(msg):
            b=msg.body
            gid=str(getattr(b,"steamid_clan",""))
            if gid not in group_lookup:
                return
            counts=getattr(b,"user_counts",None)
            member_present=bool(counts is not None and counts.HasField("members"))
            member_value=int(counts.members) if member_present else None
            fields=[]
            if counts is not None:
                fields=[f.name for f,_ in counts.ListFields()]
            item=report["state_messages"].setdefault(gid,{"appid":group_lookup[gid]["appid"],"responses":[]})
            item["responses"].append({"has_user_counts":bool(counts is not None),
                "member_field_present":member_present,"members":member_value,
                "count_fields":fields,
                "received_at_utc":datetime.now(timezone.utc).isoformat()})
            print("CM_CLAN_STATE",json.dumps({"appid":group_lookup[gid]["appid"],"gid":gid,
                "member_present":member_present,"members":member_value,"count_fields":fields}),flush=True)

        @client.on(EMsg.ClientPersonaState)
        def handle_persona(msg):
            for f in getattr(msg.body,"friends",[]):
                if str(getattr(f,"friendid","")) in group_lookup:
                    report["persona_messages_for_groups"]+=1

        @client.on(EMsg.ClientGetClanActivityCountsResponse)
        def handle_activity(msg):
            val=getattr(msg.body,"eresult",None)
            report["response_eresult"]=int(val) if val is not None else None
            print("CM_ACTIVITY_RESPONSE",report["response_eresult"],flush=True)

        @client.on(client.EVENT_DISCONNECTED)
        def on_disconnect(*args):
            report["connection_events"].append("disconnected")
        @client.on(client.EVENT_ERROR)
        def on_error(*args):
            report["connection_events"].append("error")
        @client.on(client.EVENT_RECONNECT)
        def on_reconnect(*args):
            report["connection_events"].append("reconnect")

        print("CM_PILOT_LOGIN anonymous sample=20",flush=True)
        with Timeout(45,False) as timeout:
            login=client.anonymous_login()
        report["login"]=int(login) if login is not None else None
        print("CM_PILOT_LOGIN_RESULT",report["login"],flush=True)
        if login!=EResult.OK:
            report["status"]="anonymous_login_failed_or_timeout"
            return

        ids=[int(r["group_id64"]) for r in sample]
        request=MsgProto(EMsg.ClientRequestFriendData)
        request.body.friends.extend(ids)
        # Explicitly request player name + presence (legacy Steam Client persona flags)
        request.body.persona_state_requested=0x01|0x02
        client.send(request)
        report["requests_sent"].append({"message":"ClientRequestFriendData","count":len(ids),
                                        "persona_flags":int(request.body.persona_state_requested)})
        print("CM_PILOT_REQUEST friend_data groups=",len(ids),flush=True)
        client.sleep(19)
        save(report)
        first_phase_states=len(report["state_messages"])
        print("CM_PILOT_AFTER_FRIEND_DATA states=",first_phase_states,
              "persona_groups=",report["persona_messages_for_groups"],flush=True)

        activity=MsgProto(EMsg.ClientGetClanActivityCounts)
        activity.body.steamid_clans.extend(ids)
        client.send(activity)
        report["requests_sent"].append({"message":"ClientGetClanActivityCounts","count":len(ids)})
        print("CM_PILOT_REQUEST activity groups=",len(ids),flush=True)
        client.sleep(19)
        report["status"]="complete_both_phases"
    except Exception as exc:
        report["status"]="exception"
        report["error_type"]=type(exc).__name__
        # Never print exception contents that might contain transport tokens.
    finally:
        report["elapsed_seconds"]=round(time.monotonic()-started,2)
        if client is not None:
            try:client.disconnect()
            except Exception:pass
        report["known_controls_with_members"]=sum(
            any(x.get("member_field_present") for x in report["state_messages"].get(r["group_id64"],{}).get("responses",[]))
            for r in sample if r["sample_type"]=="known_official")
        report["unresolved_with_members"]=sum(
            any(x.get("member_field_present") for x in report["state_messages"].get(r["group_id64"],{}).get("responses",[]))
            for r in sample if r["sample_type"]=="unresolved")
        report["known_controls_exact_matches"]=sum(
            any(x.get("members")==r["prior_official_followers"]
                for x in report["state_messages"].get(r["group_id64"],{}).get("responses",[])
                if x.get("member_field_present"))
            for r in sample if r["sample_type"]=="known_official")
        save(report)
        print("CM_PILOT_FINAL",json.dumps({
            "status":report["status"],"login":report["login"],
            "requests_sent":report["requests_sent"],"state_count":len(report["state_messages"]),
            "known_controls_with_members":report["known_controls_with_members"],
            "known_controls_exact_matches":report["known_controls_exact_matches"],
            "unresolved_with_members":report["unresolved_with_members"],
            "persona_groups":report["persona_messages_for_groups"],
            "activity_response":report["response_eresult"],
            "duration_seconds":report["elapsed_seconds"],
            "error_type":report.get("error_type")
        },ensure_ascii=False),flush=True)

if __name__=="__main__":
    main()
