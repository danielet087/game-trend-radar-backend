"""Offline prepare/official-cache/finalize integration for the shared queue."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from scripts.import_twitch_steam_discoveries import collect, dispatch
from scripts.reconcile_twitch_official_queue import apply_queue_batch
from scripts.twitch_steam_admission import is_twitch_qualified
from tests.test_twitch_steam_admission import (
    NOW, SHA, extend_frontend_appids, fake_frontend, metadata_responses,
    row,
)


APPIDS = list(range(123, 130))


@pytest.fixture(autouse=True)
def prevent_network(monkeypatch):
    def fail_network(*args, **kwargs):
        raise AssertionError("Integration tests must use mocked responses")
    monkeypatch.setattr("requests.sessions.Session.request", fail_network)


def stamp(instant):
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def checkpoint():
    return {"schema_version": 1, "cursor": 517, "completed_cursor": 198,
            "rate_limit_count": 2, "cohort_id": "keep_existing_cohort",
            "pending_candidates": {
                "123": {"appid": 123, "name": "Normal source title", "release_date": "2026-10-20",
                        "group_id64": 103582791429999999, "queue_source": "fresh_daily_prefilter_unresolved"},
                "999": {"appid": 999, "name": "Ordinary queue game", "release_date": "2026-10-03",
                        "group_id64": 103582791429888888, "queue_source": "frozen_original"}},
            "official_results": {"456": {"appid": 456, "official_followers": 12,
                                           "official_checked_at_taipei": "2026-10-02T08:00:00+08:00"}},
            "attempt_events": [{"appid": 456, "status": "ok", "official_followers": 12}],
            "next_request_after_taipei": None}


def responses(appids=APPIDS):
    results = []
    for aid in appids:
        browse, details = metadata_responses(aid)
        if aid == 123:
            # Official UTC release day explains the Taiwan next-day calendar.
            item = browse.json.return_value["response"]["store_items"][0]
            instant = datetime(2026, 9, 8, 16, 2, 5, tzinfo=timezone.utc)
            item["release"]["steam_release_date"] = int(instant.timestamp())
            details.json.return_value[str(aid)]["data"]["release_date"]["date"] = "2026 年 9 月 8 日"
        results.extend((browse, details))
    return results


def precollect(root, master=None, state=None, cp=None, *, now=NOW):
    session = Mock()
    session.get.side_effect = responses()
    batch = collect(root, SHA, master or {"games": []}, state or {}, session=session,
                    now=now, caches=[*( [cp] if cp is not None else []),
                    {"pending_candidates": {str(aid): {"appid": aid, "group_id64": str(103582791429521408 + aid)}
                                            for aid in APPIDS}}], blocked=set(),
                    sleep=lambda _: None)
    assert session.get.call_count == 2 * len(APPIDS)
    assert all(not call.args[0].startswith("https://steamcommunity.com/")
               for call in session.get.call_args_list)
    return batch


def test_precollect_merges_seven_priority_candidates_without_resetting_progress(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, APPIDS)
    master = {"games": [], "candidate_progress": "keep", "other_state": {"cursor": 99}}
    state = {"schema_version": 1, "games": {"456": {"status": "excluded", "reason": "keep"}}}
    cp = checkpoint()
    originals = deepcopy((master, state, cp))

    prepared = precollect(tmp_path, master, state, cp)
    assert prepared["records"] == []
    assert prepared["active_twitch_appids"] == APPIDS
    assert len(prepared["follower_candidates"]) == 7
    for candidate in prepared["follower_candidates"]:
        assert "followers" not in candidate["steam_candidate"]
        assert candidate["steam_candidate"]["sexual_content_screened"] is True
    final_master, final_state, final_cp = apply_queue_batch(master, state, cp, prepared)

    assert (master, state, cp) == originals
    assert final_master == master
    assert final_state["games"]["456"] == state["games"]["456"]
    for key in ("cursor", "completed_cursor", "cohort_id", "rate_limit_count",
                "official_results", "attempt_events", "next_request_after_taipei"):
        assert final_cp[key] == cp[key]
    assert final_cp["pending_candidates"]["999"] == cp["pending_candidates"]["999"]
    for aid in APPIDS:
        queued = final_cp["pending_candidates"][str(aid)]
        assert queued["priority"] == 100
        assert queued["queue_source"] == "twitch_steam_discovery"
        assert queued["twitch_admission"]["appid"] == aid
    first = final_cp["pending_candidates"]["123"]
    assert first["normal_candidate"] == cp["pending_candidates"]["123"]
    assert first["group_id64"] == cp["pending_candidates"]["123"]["group_id64"]
    assert first["release_date"] == "2026-09-09"


def test_true_low_and_zero_counts_finalize_taiwan_dates_and_withdraw_overlay(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, APPIDS)
    original_cp = checkpoint()
    prepared = precollect(tmp_path, cp=original_cp)
    master, state, cp = apply_queue_batch({"games": []}, {}, original_cp, prepared)

    checked = {123: NOW + timedelta(minutes=5), 124: NOW + timedelta(minutes=6)}
    # These are the existing worker's genuine completed official XML results.
    for aid, count in ((123, 120), (124, 0)):
        cp["official_results"][str(aid)] = {
            **deepcopy(cp["pending_candidates"][str(aid)]),
            "official_followers": count,
            "official_checked_at_taipei": checked[aid].astimezone(
                ZoneInfo("Asia/Taipei")).isoformat(),
            "official_source": "Steam Community XML memberCount",
        }
    before_post = deepcopy(cp)
    post = precollect(tmp_path, master, state, cp, now=NOW + timedelta(minutes=10))
    assert [(record["appid"], record["followers"]) for record in post["records"]] == [(123, 120), (124, 0)]
    assert len(post["follower_candidates"]) == 5
    final_master, final_state, final_cp = apply_queue_batch(master, state, cp, post)

    assert cp == before_post
    games = {record["appid"]: record for record in final_master["games"]}
    assert set(games) == {123, 124}
    for aid, count in ((123, 120), (124, 0)):
        record = games[aid]
        assert is_twitch_qualified(record)
        assert record["followers"] == count
        assert record["follower_checked_at"] == before_post["official_results"][str(aid)]["official_checked_at_taipei"]
        assert record["twitch_admission"]["appid"] == aid
        assert record["official_ge5000"] is False
        assert final_state["games"][str(aid)]["status"] == "accepted"
        assert final_state["games"][str(aid)]["follower_candidate"] is None
    assert games[123]["release_start"] == "2026-09-09"
    assert games[123]["release_store_date"] == "2026-09-08"
    assert games[123]["release_date_normalization"] == "steam_utc_date_normalized_to_taipei"
    assert final_cp["pending_candidates"]["123"] == original_cp["pending_candidates"]["123"]
    assert "124" not in final_cp["pending_candidates"]
    assert final_cp["official_results"] == before_post["official_results"]
    assert final_cp["cursor"] == original_cp["cursor"]


def test_prepare_and_finalize_preserve_worker_cooldown_and_rate_count(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, APPIDS)
    cooldown = {"retry_at": stamp(NOW + timedelta(hours=4)), "updated_at": stamp(NOW),
                "observed_at": stamp(NOW), "retry_source": "steam_retry_after",
                "retry_after": "14400", "retry_seconds": 14400, "attempts": 5}
    state = {"games": {}}
    cp = checkpoint()
    cp["community_cooldown"] = deepcopy(cooldown)
    cp["next_request_after_taipei"] = "2026-10-02T21:00:00+08:00"
    cp["rate_limit_count"] = 5
    cp["community_last_success_at"] = "2026-10-02T08:00:00+08:00"
    original = deepcopy(cp)
    prepared = precollect(tmp_path, state=state, cp=cp)
    master, saved, final_cp = apply_queue_batch({"games": []}, state, cp, prepared)
    assert len(prepared["follower_candidates"]) == 7
    assert master["games"] == []
    for key in ("community_cooldown", "next_request_after_taipei", "rate_limit_count",
                "community_last_success_at", "cursor", "official_results"):
        assert final_cp[key] == original[key]
    assert "steam_community" not in saved.get("api_cooldowns", {})

    # A previously verified result can finalize even while the worker waits.
    final_cp["official_results"]["123"] = {
        "appid": 123, "official_followers": 120,
        "official_checked_at_taipei": "2026-10-02T17:05:00+08:00",
    }
    post = precollect(tmp_path, master, saved, final_cp, now=NOW + timedelta(minutes=10))
    accepted_master, accepted_state, accepted_cp = apply_queue_batch(master, saved, final_cp, post)
    assert accepted_master["games"][0]["followers"] == 120
    assert accepted_state["games"]["123"]["status"] == "accepted"
    for key in ("community_cooldown", "next_request_after_taipei", "rate_limit_count",
                "community_last_success_at", "cursor"):
        assert accepted_cp[key] == original[key]
    assert "steam_community" not in accepted_state.get("api_cooldowns", {})


def test_dispatch_only_receipt_does_not_touch_or_withdraw_pending_queue(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, APPIDS)
    master, state, cp = apply_queue_batch(
        {"games": []}, {}, checkpoint(), precollect(tmp_path))
    # Mix a newly accepted record with the still-pending checkpoint, ensuring
    # there is an actual dispatch receipt rather than an empty no-op batch.
    master["games"] = [row(120)]
    state["games"]["123"]["status"] = "accepted"
    # No configured dispatch token: receipt computation itself remains offline.
    generated = dispatch(master, state, token="", target="", now=NOW + timedelta(minutes=1))
    assert "follower_candidates" not in generated
    assert generated["state_updates"]["123"]["content_dispatch"]["reason"] == "content_dispatch_not_configured"
    original = deepcopy(cp)
    final_master, saved, final_cp = apply_queue_batch(master, state, cp, generated)
    assert final_cp == original
    assert final_master == master
    assert len([row for row in final_cp["pending_candidates"].values()
                if row["queue_source"] == "twitch_steam_discovery"]) == 7
    assert saved["games"]["123"]["content_dispatch"] == generated["state_updates"]["123"]["content_dispatch"]
    assert saved["games"]["124"] == state["games"]["124"]
