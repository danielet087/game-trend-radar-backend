"""Behavior checks for Twitch priority in the existing official-count worker."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import experiment_official_daily_catchup_250 as worker
from scripts.twitch_official_queue import is_twitch_queue_candidate
from tests.test_twitch_steam_admission import NOW, row


def twitch_candidate(appid=123, release_day=None):
    metadata = row()
    metadata["appid"] = appid
    metadata["twitch_admission"]["appid"] = appid
    metadata["store_url"] = f"https://store.steampowered.com/app/{appid}/"
    if release_day is not None:
        for field in ("release_raw", "release_start", "release_end",
                      "release_timestamp_taipei_date", "release_store_date"):
            metadata[field] = release_day
        metadata["release_time_utc"] = f"{release_day}T07:00:00Z"
        metadata["release_date_normalization"] = "steam_store_date_matches_taipei"
    for key in ("followers", "follower_checked_at", "follower_source", "official_ge5000"):
        metadata.pop(key, None)
    candidate = {
        "appid": appid, "name": metadata["name"],
        "release_date": metadata["release_start"], "group_id64": None,
        "steam_url": metadata["store_url"], "queue_source": "twitch_steam_discovery",
        "twitch_admission": deepcopy(metadata["twitch_admission"]),
        "steam_candidate": metadata,
    }
    assert is_twitch_queue_candidate(candidate, now=NOW)
    return candidate


def ordinary_candidate(appid=124):
    return {"appid": appid, "name": "Ordinary game", "release_date": "2026-10-03",
            "group_id64": None, "steam_url": f"https://store.steampowered.com/app/{appid}/",
            "queue_source": "fresh_daily_prefilter_ge4000_pending_official"}


def checkpoint(pending=()):
    return {"version": 1, "cohort": worker.COHORT,
            "pending_candidates": {str(item["appid"]): deepcopy(item) for item in pending},
            "official_results": {}, "attempt_events": [],
            "next_request_after_taipei": None, "rate_limit_count": 0}


def queue_inputs():
    # The historical cohort is already completed; newly discovered work must
    # still enter the same queue without resetting that progress.
    frozen = [{"appid": aid, "release_date": "2026-10-03", "name": "Old candidate"}
              for aid in range(10000, 11317)]
    groups = [{"appid": aid, "group_short_id": aid} for aid in range(10000, 11358)]
    legacy = {"cohort": "steam_fresh_20260922_post_adult_1317_near_release",
              "official_results": {str(item["appid"]): {} for item in frozen}}
    return frozen, legacy, groups


def current_daily(*appids):
    return ({"screened_at": NOW.isoformat(), "games": [
        {"appid": aid, "name": "Daily candidate", "release_start": "2026-10-03",
         "release_precision": "day", "release_display_precision": "date_full",
         "sexual_content_screened": True} for aid in appids]},
        {"updated_at": NOW.isoformat(), "complete": True, "games": {
            str(aid): {"third_party_followers": 4500, "group_short_id": aid}
            for aid in appids}})


def test_completed_cohort_accepts_new_twitch_priority_without_duplicate_or_losing_evidence(monkeypatch):
    monkeypatch.setattr(worker, "clock", lambda: NOW.astimezone(worker.TZ))
    twitch = twitch_candidate()
    cp = checkpoint([twitch])
    frozen, legacy, groups = queue_inputs()
    eligible, prefilter = current_daily(123, 124)

    queue, source = worker.make_queue(cp, frozen, legacy, groups, eligible, prefilter,
                                     {"games": {}}, {"verified": {}})

    assert [item["appid"] for item in queue] == [123, 124]
    assert queue[0]["twitch_admission"] == twitch["twitch_admission"]
    assert queue[0]["release_date"] == twitch["release_date"]
    assert queue[0]["normal_candidate"]["queue_source"] != "twitch_steam_discovery"
    assert source["twitch_priority_pending"] == 1
    assert len(legacy["official_results"]) == 1317


@pytest.mark.parametrize("untried_release", ["2026-09-09", "2026-09-04"])
def test_twitch_rotation_gives_untried_and_older_attempts_a_turn_before_latest_failure(
        monkeypatch, untried_release):
    monkeypatch.setattr(worker, "clock", lambda: NOW.astimezone(worker.TZ))
    failed = twitch_candidate(123, "2026-09-09")
    untried = twitch_candidate(124, untried_release)
    older_attempt = twitch_candidate(126, "2026-09-09")
    later_normal = ordinary_candidate(127)
    later_normal["release_date"] = "2026-10-04"
    cp = checkpoint([failed, untried, older_attempt, later_normal, ordinary_candidate(125)])
    cp["attempt_events"] = [
        {"appid": 126, "queue_source": "twitch_steam_discovery",
         "when_taipei": "2026-10-01T18:00:00+08:00", "http": 429, "status": "rate_limited"},
        {"appid": 123, "queue_source": "twitch_steam_discovery",
         "when_taipei": NOW.astimezone(worker.TZ).isoformat(), "http": 429, "status": "rate_limited"},
        # An ordinary-source event must not falsely count as a Twitch attempt.
        {"appid": 124, "queue_source": "fresh_daily_prefilter_ge4000_pending_official",
         "when_taipei": NOW.astimezone(worker.TZ).isoformat(), "http": 429, "status": "rate_limited"},
    ]
    frozen, legacy, groups = queue_inputs()

    queue, source = worker.make_queue(cp, frozen, legacy, groups, {"games": []}, {},
                                     {"games": {}}, {"verified": {}})

    assert [item["appid"] for item in queue] == [124, 126, 123, 125, 127]
    assert source["twitch_priority_pending"] == 3
    assert cp["official_results"] == {}
    assert queue[2]["twitch_admission"] == failed["twitch_admission"]


@pytest.mark.parametrize("count,checked,expected", [
    (0, NOW.isoformat(), []),
    (88, NOW.isoformat(), []),
    (True, NOW.isoformat(), [123]),
    (88, "2026-10-03T09:00:00Z", [123]),
])
def test_twitch_reuses_only_real_usable_official_counts(monkeypatch, count, checked, expected):
    monkeypatch.setattr(worker, "clock", lambda: NOW.astimezone(worker.TZ))
    cp = checkpoint([twitch_candidate()])
    cp["official_results"]["123"] = {"official_followers": count,
                                     "official_checked_at_taipei": checked}
    frozen, legacy, groups = queue_inputs()

    queue, _ = worker.make_queue(cp, frozen, legacy, groups, {"games": []}, {},
                                 {"games": {}}, {"verified": {}})

    assert [item["appid"] for item in queue] == expected


def xml_response(count, gid="103582791429521531"):
    body = f"<memberList><groupID64>{gid}</groupID64><memberCount>{count}</memberCount></memberList>"
    return SimpleNamespace(status_code=200, headers={"Content-Type": "text/xml"},
                           content=body.encode(), text=body)


def known_group(candidate):
    candidate = deepcopy(candidate)
    candidate["group_id64"] = worker.group_to_gid(candidate["appid"])
    return candidate


def run_worker(monkeypatch, tmp_path, queue, responses, *, cooldown=None, max_requests=250,
               extra_args=(), initial_checkpoint=None, legacy_cooldown=None):
    cp = checkpoint(queue)
    cp.update(deepcopy(initial_checkpoint or {}))
    cp["next_request_after_taipei"] = cooldown
    cp_path = tmp_path / "checkpoint.json"
    master_path = tmp_path / "master.json"
    cp_path.touch()
    master_path.touch()
    monkeypatch.setattr(worker, "CHECKPOINT", cp_path)
    monkeypatch.setattr(worker, "MASTER", master_path)
    master = {"games": []}
    documents = {
        worker.FROZEN / "source_queue.json": [],
        worker.FROZEN / "source_unresolved.json": [],
        worker.FROZEN / "checkpoint.json": {"official_results": {},
                                           "next_request_after_taipei": legacy_cooldown},
        worker.ELIGIBLE: {"games": []}, worker.PREFILTER: {},
        worker.OFFICIAL_CACHE: {"games": {}}, worker.ORIGINAL_OFFICIAL: {},
        cp_path: cp, master_path: master,
    }
    saved = {}
    monkeypatch.setattr(worker, "read", lambda path: deepcopy(saved[path] if path in saved else documents[path]))
    monkeypatch.setattr(worker, "save", lambda path, data: saved.__setitem__(path, deepcopy(data)))
    monkeypatch.setattr(worker, "clock", lambda: NOW.astimezone(worker.TZ))
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    sleep = Mock()
    monkeypatch.setattr(worker.time, "sleep", sleep)
    monkeypatch.setattr(worker, "git_push", Mock(return_value=True))
    monkeypatch.setattr(worker, "make_queue", lambda *args: (deepcopy(queue), {
        "status": "current_day_prefilter_complete", "added_from_daily": 0,
        "twitch_priority_pending": sum(item["queue_source"] == "twitch_steam_discovery"
                                       for item in queue),
    }))
    monkeypatch.setattr(worker, "reverify_pending_store_dates", Mock(return_value=0))
    monkeypatch.setattr(worker, "retry_pending_content_dispatches", Mock(return_value=0))
    verify = Mock(return_value=True)
    upsert = Mock()
    dispatch = Mock(return_value="dispatched")
    monkeypatch.setattr(worker, "verify_store_date_for_result", verify)
    monkeypatch.setattr(worker, "upsert_qualified_master", upsert)
    monkeypatch.setattr(worker, "dispatch_content_event", dispatch)
    client = SimpleNamespace(headers={}, get=Mock(side_effect=responses))
    monkeypatch.setattr(worker.requests, "Session", lambda: client)
    monkeypatch.setattr("sys.argv", ["followers", "--max-requests", str(max_requests), *extra_args])

    worker.main()

    return saved[cp_path], saved[worker.OUT / "report.json"], client, verify, upsert, dispatch, sleep


@pytest.mark.parametrize("followers", [0, 120, 5000])
def test_twitch_official_success_keeps_evidence_for_shared_importer_at_any_count(
        monkeypatch, tmp_path, followers):
    candidate = known_group(twitch_candidate())
    cp, report, client, verify, upsert, dispatch, _ = run_worker(
        monkeypatch, tmp_path, [candidate], [xml_response(followers)])

    result = cp["official_results"]["123"]
    assert result["official_followers"] == followers
    assert result["official_checked_at_taipei"] == NOW.astimezone(worker.TZ).isoformat()
    assert result["twitch_admission"] == candidate["twitch_admission"]
    assert result["steam_candidate"] == candidate["steam_candidate"]
    assert "followers" not in result["steam_candidate"]
    assert report["twitch_official_success_this_run"] == 1
    assert client.get.call_count == 1
    verify.assert_not_called()
    upsert.assert_not_called()
    dispatch.assert_not_called()


def test_ordinary_qualified_game_keeps_existing_store_gate_and_dispatch(monkeypatch, tmp_path):
    cp, report, client, verify, upsert, dispatch, _ = run_worker(
        monkeypatch, tmp_path, [known_group(ordinary_candidate())],
        [xml_response(6000, worker.group_to_gid(124))])

    assert cp["official_results"]["124"]["official_followers"] == 6000
    assert report["official_new_this_run"] == 1
    assert client.get.call_count == 1
    verify.assert_called_once()
    upsert.assert_called_once()
    dispatch.assert_called_once()


def test_daily_growth_official_measurement_is_reused_without_a_community_request(monkeypatch, tmp_path):
    candidate = known_group(ordinary_candidate())
    cached = {"official_growth_observations": {"124": {
        "appid": 124, "group_id64": candidate["group_id64"], "official_followers": 6200,
        "official_checked_at_taipei": NOW.isoformat(),
    }}}
    cp, report, client, verify, upsert, dispatch, _ = run_worker(
        monkeypatch, tmp_path, [candidate], [], initial_checkpoint=cached)
    client.get.assert_not_called()
    assert cp["official_results"]["124"]["official_followers"] == 6200
    assert cp["attempt_events"][0]["cache_reused"] is True
    assert report["requests_this_run"] == 0
    assert report["official_new_this_run"] == 1
    verify.assert_called_once()
    upsert.assert_called_once()
    dispatch.assert_called_once()


def test_next_run_leaves_saved_high_follower_twitch_results_to_shared_importer(monkeypatch):
    candidate = twitch_candidate()
    cp = checkpoint()
    cp["official_results"]["123"] = {
        **candidate, "official_followers": 8000, "official_ge5000": True,
        "official_checked_at_taipei": NOW.astimezone(worker.TZ).isoformat(),
        "official_source": "Steam Community XML memberCount",
    }
    original = deepcopy(cp)
    monkeypatch.setattr(worker, "clock", lambda: NOW.astimezone(worker.TZ))
    network = Mock(side_effect=AssertionError("Twitch results use the shared importer"))
    upsert = Mock()
    dispatch = Mock()
    monkeypatch.setattr(worker.requests, "Session", network)
    monkeypatch.setattr(worker, "fetch_store_release_details", network)
    monkeypatch.setattr(worker, "upsert_qualified_master", upsert)
    monkeypatch.setattr(worker, "dispatch_content_event", dispatch)

    assert worker.reverify_pending_store_dates(cp, {"games": []}) == 0
    assert worker.retry_pending_content_dispatches(cp) == 0
    assert cp == original
    network.assert_not_called()
    upsert.assert_not_called()
    dispatch.assert_not_called()


def test_shared_loop_stops_after_first_429_and_preserves_all_pending_work(monkeypatch, tmp_path):
    response = SimpleNamespace(status_code=429, headers={"Retry-After": "7200"})
    first, later = known_group(twitch_candidate()), known_group(ordinary_candidate())
    cp, report, client, verify, upsert, dispatch, _ = run_worker(
        monkeypatch, tmp_path, [first, later], [response])

    assert client.get.call_count == 1
    assert f"gid/{worker.group_to_gid(123)}/" in client.get.call_args.args[0]
    assert cp["official_results"] == {}
    assert set(cp["pending_candidates"]) == {"123", "124"}
    assert cp["next_request_after_taipei"] == "2026-10-02T19:00:00+08:00"
    assert report["stop_reason"] == "first_http_429"
    assert report["requests_this_run"] == 1
    assert report["remaining_queue"] == 2
    verify.assert_not_called()
    upsert.assert_not_called()
    dispatch.assert_not_called()


def test_shared_cooldown_blocks_twitch_and_normal_xml_queries(monkeypatch, tmp_path):
    cp, report, client, _, _, _, _ = run_worker(
        monkeypatch, tmp_path, [twitch_candidate(), ordinary_candidate()], [],
        cooldown="2026-10-02T18:00:00+08:00")

    client.get.assert_not_called()
    assert cp["official_results"] == {}
    assert set(cp["pending_candidates"]) == {"123", "124"}
    assert report["stop_reason"] == "official_429_cooldown_no_request"
    assert report["requests_this_run"] == 0


def test_priority_and_normal_share_request_budget_and_pacing(monkeypatch, tmp_path):
    cp, report, client, _, _, _, sleep = run_worker(
        monkeypatch, tmp_path, [known_group(twitch_candidate()), known_group(ordinary_candidate()), known_group(ordinary_candidate(125))],
        [xml_response(30), xml_response(100, worker.group_to_gid(124))], max_requests=2)

    assert [call.args[0].split("/gid/")[1].split("/")[0]
            for call in client.get.call_args_list] == [worker.group_to_gid(123), worker.group_to_gid(124)]
    assert set(cp["official_results"]) == {"123", "124"}
    assert report["requests_this_run"] == 2
    assert report["remaining_queue"] == 1
    sleep.assert_called_once_with(8.0)


def test_unknown_priority_id_waits_for_resolution_without_blocking_known_normal(monkeypatch, tmp_path):
    unknown, ready = twitch_candidate(), known_group(ordinary_candidate())
    cp, report, client, *_ = run_worker(
        monkeypatch, tmp_path, [unknown, ready], [xml_response(20, worker.group_to_gid(124))])
    assert client.get.call_count == 1
    assert f"/gid/{ready['group_id64']}/" in client.get.call_args.args[0]
    assert cp["pending_candidates"]["123"]["group_id64"] is None
    assert report["awaiting_group_resolution"] == 1
    assert report["remaining_queue"] == 1
    assert [event["appid"] for event in cp["attempt_events"]] == [124]


@pytest.mark.parametrize("gid", [None, True, "76561198000000001", worker.GROUP_BASE])
def test_unknown_or_invalid_group_never_uses_community_fallback(monkeypatch, tmp_path, gid):
    candidate = twitch_candidate()
    candidate["group_id64"] = gid
    cp, report, client, *_ = run_worker(monkeypatch, tmp_path, [candidate], [])
    client.get.assert_not_called()
    assert report["stop_reason"] == "awaiting_group_resolution_no_request"
    assert report["remaining_queue"] == 1
    assert cp["attempt_events"] == []
    assert cp["rate_limit_count"] == 0


def test_daily_refresh_preserves_resolved_group_and_lookup_evidence(monkeypatch):
    monkeypatch.setattr(worker, "clock", lambda: NOW.astimezone(worker.TZ))
    candidate = known_group(ordinary_candidate())
    candidate["group_resolution"] = {"status": "resolved", "checked_at": NOW.isoformat()}
    cp = checkpoint([candidate])
    frozen, legacy, groups = queue_inputs()
    eligible, prefilter = current_daily(124)
    prefilter["games"]["124"]["group_short_id"] = None
    queue, _ = worker.make_queue(cp, frozen, legacy, groups, eligible, prefilter,
                                 {"games": {}}, {"verified": {}})
    assert queue[0]["group_id64"] == candidate["group_id64"]
    assert queue[0]["group_resolution"] == candidate["group_resolution"]
