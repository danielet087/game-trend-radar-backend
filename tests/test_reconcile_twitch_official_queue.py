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
                    now=now, caches=[cp] if cp is not None else [], blocked=set(),
                    sleep=lambda _: None)
    assert session.get.call_count == 2 * len(APPIDS)
    assert all(not call.args[0].startswith("https://steamcommunity.com/")
               for call in session.get.call_args_list)
    return batch


def test_precollect_admits_unknown_counts_without_resetting_progress(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, APPIDS)
    master = {"games": [], "candidate_progress": "keep", "other_state": {"cursor": 99}}
    state = {"schema_version": 1, "games": {"456": {"status": "excluded", "reason": "keep"}}}
    cp = checkpoint()
    originals = deepcopy((master, state, cp))
    prepared = precollect(tmp_path, master, state, cp)
    assert [record["appid"] for record in prepared["records"]] == APPIDS
    assert prepared["active_twitch_appids"] == APPIDS
    assert prepared["follower_candidates"] == []
    final_master, final_state, final_cp = apply_queue_batch(master, state, cp, prepared)
    assert (master, state, cp) == originals
    assert len(final_master["games"]) == 7
    assert final_master["candidate_progress"] == "keep"
    assert final_state["games"]["456"] == state["games"]["456"]
    for key in ("cursor", "completed_cursor", "cohort_id", "rate_limit_count",
                "official_results", "attempt_events", "next_request_after_taipei"):
        assert final_cp[key] == cp[key]
    assert final_cp["pending_candidates"] == {"999": cp["pending_candidates"]["999"]}
    assert set(final_cp["twitch_admitted_candidates"]) == set(map(str, APPIDS))
    for record in final_master["games"]:
        assert is_twitch_qualified(record)
        assert record["followers"] is None and record["follower_checked_at"] is None
        assert record["follower_source"] is None
    first = final_cp["twitch_admitted_candidates"]["123"]["steam_candidate"]
    assert first["release_start"] == "2026-09-09"


def test_true_low_and_zero_counts_remain_verified_alongside_unknown_counts(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, APPIDS)
    cp = checkpoint()
    checked = {123: NOW - timedelta(minutes=5), 124: NOW - timedelta(minutes=4)}
    for aid, count in ((123, 120), (124, 0)):
        cp["official_results"][str(aid)] = {
            "appid": aid, "official_followers": count,
            "official_checked_at_taipei": checked[aid].astimezone(ZoneInfo("Asia/Taipei")).isoformat(),
            "official_source": "Steam Community XML memberCount",
        }
    original = deepcopy(cp)
    batch = precollect(tmp_path, cp=cp)
    master, state, final_cp = apply_queue_batch({"games": []}, {}, cp, batch)
    games = {record["appid"]: record for record in master["games"]}
    assert set(games) == set(APPIDS) and batch["follower_candidates"] == []
    for aid, count in ((123, 120), (124, 0)):
        assert games[aid]["followers"] == count
        assert games[aid]["follower_checked_at"] == original["official_results"][str(aid)]["official_checked_at_taipei"]
        assert state["games"][str(aid)]["status"] == "accepted"
    assert all(games[aid]["followers"] is None for aid in APPIDS[2:])
    assert final_cp["official_results"] == original["official_results"]
    assert final_cp["cursor"] == original["cursor"]


def test_twitch_admission_ignores_community_cooldown_and_preserves_verified_counts(tmp_path):
    fake_frontend(tmp_path)
    extend_frontend_appids(tmp_path, APPIDS)
    cp = checkpoint()
    cp["community_cooldown"] = {"retry_at": stamp(NOW + timedelta(hours=4))}
    cp["next_request_after_taipei"] = "2026-10-02T21:00:00+08:00"
    cp["rate_limit_count"] = 5
    original = deepcopy(cp)
    prepared = precollect(tmp_path, cp=cp)
    master, state, final_cp = apply_queue_batch({"games": []}, {}, cp, prepared)
    assert len(master["games"]) == 7 and prepared["follower_candidates"] == []
    for key in ("community_cooldown", "next_request_after_taipei", "rate_limit_count", "cursor", "official_results"):
        assert final_cp[key] == original[key]
    # Replaying an unknown-count admission cannot erase a later real XML count.
    verified = deepcopy(master)
    verified["games"][0].update(followers=120, follower_checked_at=stamp(NOW + timedelta(minutes=1)),
                                follower_source="Steam Community XML memberCount")
    replayed, _, _ = apply_queue_batch(verified, state, final_cp, prepared)
    assert replayed["games"][0]["followers"] == 120


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
                if row["queue_source"] == "twitch_steam_discovery"]) == 0
    assert saved["games"]["123"]["content_dispatch"] == generated["state_updates"]["123"]["content_dispatch"]
    assert saved["games"]["124"] == state["games"]["124"]
