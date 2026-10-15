"""Explicit-port intake checks for frozen evidence, queue ownership and HTTP bounds."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.application import twitch_intake as app
from radar_backend.adapters.steam_twitch_intake import RateLimited

NOW = datetime(2026, 10, 9, 2, 30, tzinfo=timezone.utc)
SHA = "a" * 40
FRONTEND = Path("frozen-frontend")


class RequestFailure(Exception):
    pass


def stamp(now):
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def aware(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def admission(appid=7):
    return {"appid": appid, "checked_at": stamp(NOW), "identity": str(appid)}


def candidate(appid=7, followers=0):
    return {"appid": appid, "name": f"Game {appid}", "followers": followers,
            "follower_checked_at": stamp(NOW), "release_start": "2026-10-09",
            "twitch_admission": admission(appid), "qualified": True}


def queued(row):
    result = deepcopy(row)
    result.pop("followers", None)
    result.pop("follower_checked_at", None)
    return {"appid": row["appid"], "steam_candidate": result}


def signature(row):
    return f"{row['appid']}:{row['followers']}:{row['release_start']}"


def transient(prior, observed):
    attempts = prior.get("retry_attempts", 0) + 1
    return {"retry_at": stamp(observed + timedelta(minutes=5 * attempts)),
            "retry_attempts": attempts, "retry_source": "transient"}


class TimeBudget:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, value):
        self.sleeps.append(value)
        self.value += value


def ports(*appids):
    p = SimpleNamespace()
    p.time = TimeBudget()
    p.session = Mock()
    p.documents = [{"kind": "discovery"}, {"kind": "tracking"}, {"kind": "catalog"}]
    p.read = Mock(side_effect=p.documents)
    p.validate = Mock(return_value=[(aid, admission(aid)) for aid in (appids or (7,))])
    p.clock = Mock(return_value=NOW)
    p.request = Mock(side_effect=lambda session, url, **kw: Mock(json=Mock(return_value=(
        {"response": {"store_items": [{"appid": kw['params'].get('appids', 7)}]}}
        if url == app.STORE_BROWSE else
        {str(kw['params']['appids']): {"success": True, "data": {"ok": True}}}
    ))))
    p.build = Mock(side_effect=lambda aid, proof, item, details, count, checked, now, blocked:
                   (dict(candidate(aid, count), follower_checked_at=checked), "accepted"))
    p.cache = Mock(return_value=None)
    p.retained = Mock(side_effect=lambda aid, proof, prior, now, blocked: prior.get("follower_candidate"))
    p.excluded = Mock(return_value={99})
    p.policy = Mock(return_value={"retry_seconds": 60, "retry_at": stamp(NOW + timedelta(seconds=60)),
                                 "observed_at": stamp(NOW), "attempts": 1,
                                 "retry_source": "server", "retry_after": "60"})
    p.transient = Mock(side_effect=transient)
    p.factory = Mock(return_value=p.session)
    p.kw = dict(session_factory=p.factory, read_json=p.read, validate_snapshot=p.validate,
                excluded_appids=p.excluded, retained_follower_candidate=p.retained,
                is_twitch_qualified=lambda row: isinstance(row, dict) and row.get("qualified") is True,
                signature=signature, cached_follower=p.cache, aware_time=aware,
                identity_signature=lambda proof: proof["identity"], build_candidate=p.build,
                follower_candidate=queued, stamp=stamp, request=p.request,
                rate_limit_policy=p.policy, transient_retry_policy=p.transient,
                request_exception=RequestFailure, rate_limited_type=RateLimited,
                monotonic=p.time.monotonic, sleep=p.time.sleep,
                clock=lambda: NOW)
    return p


def collect(p, master=None, previous=None, **kwargs):
    known = {"pending_candidates": {str(aid): {"appid": aid, "group_id64": str(103582791429521408 + aid)}
             for aid, _proof in p.validate.return_value}}
    supplied = {**p.kw, "now": NOW, "caches": [known], **kwargs}
    return app.collect(FRONTEND, SHA, master or {"games": []}, previous or {}, **supplied)


def test_collect_reads_one_immutable_snapshot_and_only_metadata_endpoints():
    p = ports()
    batch = collect(p)
    assert [call.args[0] for call in p.read.call_args_list] == [
        FRONTEND / "data/twitch_steam_discovery.json", FRONTEND / "data/twitch_tracking.json",
        FRONTEND / "data/steam_upcoming.json"]
    p.validate.assert_called_once_with(*p.documents, SHA, NOW)
    p.factory.assert_called_once_with()
    p.session.headers.update.assert_called_once_with({"User-Agent": "GameTrendRadarTwitchSteamImport/1.0"})
    assert [call.args[1] for call in p.request.call_args_list] == [app.STORE_BROWSE, app.APPDETAILS]
    first, second = p.request.call_args_list
    payload = json.loads(first.kwargs["params"]["input_json"])
    assert payload == {"ids": [{"appid": 7}], "context": {
        "country_code": "TW", "language": "english", "steam_realm": 1},
        "data_request": {"include_release": True, "include_basic_info": True, "include_tag_count": 20}}
    assert second.kwargs["params"] == {"appids": 7, "cc": "TW", "l": "tchinese"}
    assert first.kwargs["timeout"] == second.kwargs["timeout"] == 25
    assert p.time.sleeps == [1.5]
    assert batch["generated_at"] == stamp(NOW) and batch["source_frontend_commit"] == SHA
    assert batch["records"] == [] and batch["active_twitch_appids"] == [7]
    assert batch["state_updates"]["7"]["reason"] == "queued_official_followers"
    assert "followers" not in batch["follower_candidates"][0]["steam_candidate"]


def test_explicit_observation_does_not_read_wall_clock_for_queue_and_probes():
    p = ports()
    wall = Mock(side_effect=AssertionError("Frozen observation must win"))
    output = collect(p, datetime_type=SimpleNamespace(now=wall))
    wall.assert_not_called()
    assert output["generated_at"] == stamp(NOW)
    assert p.build.call_args.args[5:7] == (stamp(NOW), NOW)


def test_missing_observation_uses_the_same_clock_result_for_snapshot_and_generated_at():
    p = ports()
    output = collect(p, now=None, clock=p.clock)
    p.clock.assert_called_once_with()
    assert output["generated_at"] == stamp(NOW)
    assert p.validate.call_args.args[-1] is NOW


def test_accepted_master_is_persistent_membership_without_new_metadata_or_queue():
    p = ports()
    row = candidate(followers=120)
    previous = {"games": {"7": {"status": "accepted", "validation_version": 4,
        "content_signature": signature(row), "twitch_admission": row["twitch_admission"]}}}
    p.retained.return_value = queued(row)
    output = collect(p, master={"games": [row]}, previous=previous)
    assert output["state_updates"] == {} and output["follower_candidates"] == []
    assert output["active_twitch_appids"] == [7]
    p.request.assert_not_called()
    p.cache.assert_not_called()


@pytest.mark.parametrize("field,value", [("status", "pending"), ("validation_version", 3),
    ("content_signature", "old"), ("twitch_admission", {"old": True}),
    ("retry_at", "future"), ("retry_attempts", 1), ("rate_limit_stage", "old"),
    ("rate_limit_attempts", 1), ("retry_source", "old"), ("follower_candidate", {"appid": 7})])
def test_accepted_master_clears_stale_admission_state_without_a_fetch(field, value):
    p = ports()
    row = candidate(followers=17)
    prior = {"status": "accepted", "validation_version": 4,
             "content_signature": signature(row), "twitch_admission": row["twitch_admission"], field: value}
    output = collect(p, master={"games": [row]}, previous={"games": {"7": prior}})
    update = output["state_updates"]["7"]
    assert update["status"] == "accepted" and update["reason"] == "steam_verified"
    for key in ("retry_at", "rate_limit_stage", "retry_source", "retry_after", "follower_candidate"):
        assert update[key] is None
    assert update["retry_attempts"] == update["rate_limit_attempts"] == 0
    p.request.assert_not_called()


def test_full_active_membership_and_retained_metadata_are_seeded_before_deadline():
    p = ports(9, 7)
    expected = {str(aid): {"follower_candidate": queued(candidate(aid))} for aid in (9, 7)}
    output = collect(p, previous={"games": expected}, monotonic=Mock(side_effect=[0, 900]))
    assert output["stop_reason"] == "deadline"
    assert output["active_twitch_appids"] == [9, 7]
    assert [row["appid"] for row in output["follower_candidates"]] == [7, 9]
    assert p.retained.call_count == 2
    p.request.assert_not_called()


@pytest.mark.parametrize("stage", ["steam_store_browse", "steam_appdetails"])
def test_shared_metadata_cooldown_preserves_all_seeded_queue_candidates(stage):
    p = ports(7, 9)
    previous = {"games": {str(aid): {"follower_candidate": queued(candidate(aid))} for aid in (7, 9)},
                "api_cooldowns": {stage: {"retry_at": stamp(NOW + timedelta(seconds=60)),
                "attempts": 3, "retry_source": "server", "retry_after": "60"}}}
    original = deepcopy(previous)
    output = collect(p, previous=previous, clock=p.clock)
    assert previous == original
    assert output["stop_reason"] == "steam_metadata_cooldown"
    assert set(output["state_updates"]) == {"7"}
    assert output["state_updates"]["7"]["rate_limit_stage"] == stage
    assert output["state_updates"]["7"]["rate_limit_attempts"] == 3
    assert [row["appid"] for row in output["follower_candidates"]] == [7, 9]
    p.request.assert_not_called()


@pytest.mark.parametrize("mismatch", [False, True])
def test_retry_deadline_follows_proof_identity_not_frontend_commit(mismatch):
    p = ports()
    prior = {"retry_at": stamp(NOW + timedelta(minutes=5)), "validation_version": 4,
             "twitch_admission": {**admission(), "source_frontend_commit": "b" * 40}}
    if mismatch:
        prior["twitch_admission"]["identity"] = "different"
    output = collect(p, previous={"games": {"7": prior}}, clock=p.clock)
    assert p.request.call_count == (2 if mismatch else 0)
    assert bool(output["state_updates"]) is mismatch


@pytest.mark.parametrize("reason", ["uncertain_steam_date", "uncertain_taiwan_store_date", "steam_date_conflict"])
def test_parser_migration_can_recheck_an_older_validation_version(reason):
    p = ports()
    prior = {"retry_at": stamp(NOW + timedelta(hours=24)), "reason": reason,
             "validation_version": 3, "twitch_admission": admission()}
    collect(p, previous={"games": {"7": prior}}, clock=p.clock)
    assert p.request.call_count == 2


@pytest.mark.parametrize("count", [0, 17, 4999, 5000])
def test_genuine_cached_followers_including_zero_finalize_and_withdraw_queue(count):
    p = ports()
    p.cache.return_value = (count, stamp(NOW))
    previous = {"games": {"7": {"follower_candidate": queued(candidate())}}}
    output = collect(p, previous=previous)
    assert output["records"][0]["followers"] == count
    assert output["follower_candidates"] == []
    assert output["state_updates"]["7"]["follower_candidate"] is None
    assert p.build.call_count == 2
    assert output["state_updates"]["7"]["retry_at"] is None


def test_master_follower_evidence_is_checked_only_after_external_cache_miss():
    p = ports()
    p.cache.side_effect = [None, (0, stamp(NOW))]
    row = candidate()
    row["qualified"] = False
    collect(p, master={"games": [row]}, caches=[{"cache": 1}])
    assert p.cache.call_args_list[0].args == (7, [{"cache": 1}], NOW)
    assert p.cache.call_args_list[1].args == (7, [{"games": {"7": {
        "followers": 0, "checked_at": stamp(NOW)}}}], NOW)


@pytest.mark.parametrize("reason,excluded,retained", [
    ("steam_store_unavailable", False, True), ("steam_type_unavailable", False, True),
    ("steam_content_descriptors_unavailable", False, True), ("adult_content", True, False),
    ("not_a_steam_game", True, False), ("outside_new_game_window", True, False),
    ("uncertain_steam_date", False, False), ("uncertain_taiwan_store_date", False, False),
    ("steam_identity_mismatch", False, False), ("steam_date_conflict", False, False),
    ("invalid_admission", False, False)])
def test_metadata_rejection_preserves_only_temporary_pending_candidates(reason, excluded, retained):
    p = ports()
    p.build.return_value = None
    p.build.side_effect = lambda *args: (None, reason)
    saved = queued(candidate())
    output = collect(p, previous={"games": {"7": {"follower_candidate": saved}}}, clock=p.clock)
    update = output["state_updates"]["7"]
    assert update["status"] == ("excluded" if excluded else "pending")
    assert update["reason"] == reason
    assert output["follower_candidates"] == ([saved] if retained else [])
    assert bool(p.transient.called) is retained
    if not retained:
        assert update["follower_candidate"] is None
        assert update["retry_at"] == stamp(NOW + timedelta(hours=24))


def test_second_candidate_validation_can_fail_after_a_successful_metadata_probe():
    p = ports()
    p.cache.return_value = 17, stamp(NOW)
    p.build.side_effect = [(candidate(), "accepted"), (None, "invalid_admission")]
    output = collect(p, clock=p.clock)
    assert output["records"] == []
    assert output["state_updates"]["7"]["reason"] == "invalid_admission"
    p.transient.assert_called_once_with({}, NOW)


@pytest.mark.parametrize("error", [RequestFailure("http"), ValueError("json"), TypeError("shape"), RuntimeError("deadline")])
def test_bounded_metadata_failures_are_recorded_and_keep_verified_queue(error):
    p = ports()
    p.request.side_effect = error
    saved = queued(candidate())
    output = collect(p, previous={"games": {"7": {"follower_candidate": saved}}}, clock=p.clock)
    assert output["state_updates"]["7"]["reason"] == type(error).__name__
    assert output["follower_candidates"] == [saved]
    assert output["stop_reason"] == "complete"


@pytest.mark.parametrize("error", [KeyError("bad metadata"), AttributeError("bad metadata")])
def test_unhandled_programming_errors_remain_visible(error):
    p = ports()
    p.request.side_effect = error
    with pytest.raises(type(error)):
        collect(p)


def test_rate_limit_captures_response_time_and_stops_later_candidates():
    p = ports(7, 9)
    observed = NOW + timedelta(minutes=10)
    policy = {"retry_seconds": 900, "retry_at": stamp(observed + timedelta(minutes=15)),
              "observed_at": stamp(observed), "attempts": 2, "retry_source": "server", "retry_after": "900"}
    p.request.side_effect = RateLimited("steam_store_browse", 900, policy=policy)
    output = collect(p, clock=Mock(return_value=observed))
    assert output["generated_at"] == stamp(NOW)
    assert output["stop_reason"] == "steam_rate_limited"
    assert set(output["state_updates"]) == {"7"}
    state = output["state_updates"]["7"]
    assert state["updated_at"] == stamp(observed) and state["retry_at"] == policy["retry_at"]
    assert state["rate_limit_attempts"] == 2 and state["retry_attempts"] == 0
    assert output["cooldown_updates"]["steam_store_browse"] == {**policy, "updated_at": stamp(observed)}
    p.policy.assert_not_called()


def test_rate_limited_without_policy_uses_current_fallback_policy_port():
    p = ports()
    p.request.side_effect = RateLimited("steam_appdetails", 60)
    collect(p, clock=p.clock)
    p.policy.assert_called_once_with("steam_appdetails", "60", NOW)


def test_successful_services_recover_attempt_counters_using_response_clock():
    p = ports()
    observed = NOW + timedelta(seconds=10)
    expired = stamp(NOW - timedelta(seconds=1))
    previous = {"api_cooldowns": {stage: {"retry_at": expired, "attempts": 4,
                "server_evidence": {"keep": True}} for stage in ("steam_store_browse", "steam_appdetails")}}
    original = deepcopy(previous)
    output = collect(p, previous=previous, clock=Mock(return_value=observed))
    assert previous == original
    for stage in previous["api_cooldowns"]:
        recovered = output["cooldown_updates"][stage]
        assert recovered["attempts"] == 0
        assert recovered["updated_at"] == recovered["last_success_at"] == stamp(observed)
        assert recovered["server_evidence"] == {"keep": True}
    assert p.request.call_args_list[0].kwargs["prior_cooldown"] == previous["api_cooldowns"]["steam_store_browse"]


def test_small_budget_caps_both_sleep_and_each_request_timeout():
    p = ports()
    output = collect(p, max_seconds=2)
    assert p.request.call_args_list[0].kwargs["timeout"] == 2
    assert p.request.call_args_list[1].kwargs["timeout"] == 0.5
    assert p.time.sleeps == [1.5]
    assert output["stop_reason"] == "complete"


def test_budget_exhausted_during_pacing_records_transient_without_second_request():
    p = ports()
    output = collect(p, max_seconds=1, clock=p.clock)
    assert p.time.sleeps == [1]
    assert p.request.call_count == 1
    assert output["state_updates"]["7"]["reason"] == "RuntimeError"
    assert output["stop_reason"] == "complete"


def test_snapshot_read_and_validation_failures_precede_any_network():
    p = ports()
    p.validate.side_effect = ValueError("Untrusted snapshot")
    with pytest.raises(ValueError, match="Untrusted snapshot"):
        collect(p)
    p.request.assert_not_called()
    p.excluded.assert_not_called()


@pytest.mark.parametrize("batch", [{"generated_at": "invalid"}, {"generated_at": "invalid", "follower_candidates": []},
                                  {"generated_at": stamp(NOW), "follower_candidates": None}])
def test_queue_reconciliation_is_controlled_by_key_presence_and_follows_apply(batch):
    trace = []
    def apply(master, state, value):
        trace.append(("apply", master, state, value))
        return {"saved": "master"}, {"saved": "state"}
    def parse(value):
        trace.append(("parse", value))
        return aware(value)
    def sync(cp, value, now):
        trace.append(("sync", cp, value, now))
        return {"saved": "checkpoint"}
    checkpoint = {"original": True}
    result = app.apply_queue_batch({}, {}, checkpoint, batch, apply_batch=apply, aware_time=parse, sync_twitch_queue=sync, fallback_allowed=lambda row: True)
    assert [step[0] for step in trace] == (["apply", "parse", "sync"] if "follower_candidates" in batch else ["apply", "parse"])
    assert result[:2] == ({"saved": "master"}, {"saved": "state"})
    assert result[2] == ({"saved": "checkpoint"} if "follower_candidates" in batch else checkpoint)
    if "follower_candidates" not in batch:
        assert result[2] is checkpoint


def test_failed_apply_does_not_parse_or_touch_checkpoint():
    fail = Mock(side_effect=ValueError("bad batch"))
    parse, sync = Mock(), Mock()
    with pytest.raises(ValueError, match="bad batch"):
        app.apply_queue_batch({}, {}, {}, {}, apply_batch=fail, aware_time=parse, sync_twitch_queue=sync, fallback_allowed=lambda row: True)
    parse.assert_not_called()
    sync.assert_not_called()


def dispatch_ports():
    session = Mock()
    session.post.return_value = Mock(status_code=204)
    p = SimpleNamespace(session=session, factory=Mock(return_value=session), clock=Mock(return_value=NOW))
    p.kw = dict(session_factory=p.factory, clock=p.clock, is_twitch_qualified=lambda row: bool(row and row.get("qualified")),
                signature=signature, stamp=stamp, request_exception=RequestFailure,
                environ_get=Mock(return_value="example/source"), monotonic=lambda: 0)
    return p


def dispatch(p, master=None, state=None, **kwargs):
    return app.dispatch(master or {"games": [candidate()]}, state or {"games": {"7": {"status": "accepted"}}},
                        **{**p.kw, "token": "token", "target": "owner/content", "now": NOW, **kwargs})


def test_dispatch_receipt_and_payload_share_frozen_signature_and_observation():
    p = dispatch_ports()
    output = dispatch(p)
    receipt = output["state_updates"]["7"]["content_dispatch"]
    assert receipt == {"status": "dispatched", "signature": signature(candidate()), "attempted_at": stamp(NOW),
                       "target_repository": "owner/content", "event_type": "steam_game_twitch_discovered", "http": 204}
    call = p.session.post.call_args
    assert call.args == ("https://api.github.com/repos/owner/content/dispatches",)
    assert call.kwargs["timeout"] == 25
    assert call.kwargs["headers"] == {"Authorization": "Bearer token", "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"}
    assert call.kwargs["json"] == {"event_type": "steam_game_twitch_discovered", "client_payload": {
        "appid": 7, "official_followers": 0, "release_date": "2026-10-09", "official_checked_at_taipei": stamp(NOW),
        "twitch_admission": admission(), "signature": signature(candidate()), "source_repository": "example/source"}}
    p.kw["environ_get"].assert_called_once_with("GITHUB_REPOSITORY", "danielet087/game-trend-radar-backend")
    p.clock.assert_not_called()


@pytest.mark.parametrize("token,target", [("", "owner/content"), ("token", ""), ("", "")])
def test_dispatch_without_configuration_still_keeps_a_pending_receipt(token, target):
    p = dispatch_ports()
    output = dispatch(p, token=token, target=target)
    assert output["state_updates"]["7"]["content_dispatch"]["reason"] == "content_dispatch_not_configured"
    p.session.post.assert_not_called()
    p.kw["environ_get"].assert_not_called()


@pytest.mark.parametrize("status", [200, 201, 202, 400, 403, 429, 500])
def test_dispatch_only_204_marks_a_successful_receipt(status):
    p = dispatch_ports()
    p.session.post.return_value.status_code = status
    output = dispatch(p)
    receipt = output["state_updates"]["7"]["content_dispatch"]
    assert receipt["status"] == "pending" and receipt["http"] == status
    p.session.post.return_value.raise_for_status.assert_not_called()


def test_dispatch_request_exception_stays_pending_with_reason():
    p = dispatch_ports()
    p.session.post.side_effect = RequestFailure("unavailable")
    receipt = dispatch(p)["state_updates"]["7"]["content_dispatch"]
    assert receipt["status"] == "pending" and receipt["reason"] == "RequestFailure"
    assert "http" not in receipt


@pytest.mark.parametrize("field,value", [("status", "pending"), ("qualified", False), ("receipt", "matched")])
def test_dispatch_skips_unaccepted_unqualified_and_identical_receipts(field, value):
    p = dispatch_ports()
    row = candidate()
    pending = {"status": "accepted"}
    if field == "status":
        pending["status"] = value
    elif field == "qualified":
        row["qualified"] = value
    else:
        pending["content_dispatch"] = {"status": "dispatched", "signature": signature(row)}
    output = dispatch(p, master={"games": [row]}, state={"games": {"7": pending}})
    assert output["state_updates"] == {}
    p.session.post.assert_not_called()


def test_dispatch_deadline_keeps_prior_receipts_and_stops_later_work():
    p = dispatch_ports()
    output = dispatch(p, master={"games": [candidate(7), candidate(9)]},
                      state={"games": {"7": {"status": "accepted"}, "9": {"status": "accepted"}}},
                      monotonic=Mock(side_effect=[0, 0, 600]))
    assert set(output["state_updates"]) == {"7"}
    assert output["stop_reason"] == "deadline"
    assert p.session.post.call_count == 1


def test_dispatch_without_now_reads_clock_once_and_preserves_input_state():
    p = dispatch_ports()
    master, state = {"games": [candidate()]}, {"games": {"7": {"status": "accepted", "other": {"keep": True}}}}
    before = deepcopy((master, state))
    dispatch(p, master=master, state=state, now=None)
    assert (master, state) == before
    p.clock.assert_called_once_with()
