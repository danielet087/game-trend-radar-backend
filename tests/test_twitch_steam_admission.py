from copy import deepcopy
from datetime import date, datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

import pytest

from scripts.import_twitch_steam_discoveries import (
    APPDETAILS, STORE_BROWSE, apply_batch, build_candidate, cached_follower,
    collect, dispatch, validate_snapshot,
)
from scripts.twitch_steam_admission import (
    is_twitch_qualified, normalize_twitch_admission, validate_twitch_snapshot,
)

NOW = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
SHA = "a" * 40


def enrollment():
    return {"source": "igdb_first_release_date", "observed_at": "2026-10-01T08:00:00Z",
            "viewer_count": 8000, "min_viewers": 7000}


def proof():
    return {"schema_version": 1, "method": "twitch_igdb_external_steam_v1", "appid": 123,
            "twitch_game_id": "22", "igdb_id": "33", "checked_at": "2026-10-02T08:00:00Z",
            "source_frontend_commit": SHA, "source_enrollment": enrollment()}


def snapshot():
    registry = {"schema_version": 1, "games": {"22": {"game_id": "22", "igdb_id": "33",
        "tracking_sources": {"twitch_new": {"source": "twitch_new", "status": "active",
            "expires_at": "2026-10-20T00:00:00Z", "enrollment": enrollment()}}}}}
    discovery = {"schema_version": 1, "steam_source_id": "777", "games": {"22": {
        "twitch_game_id": "22", "igdb_id": "33", "active": True, "status": "matched",
        "method": "twitch_igdb_external_steam_v1", "checked_at": "2026-10-02T08:00:00Z",
        "twitch_enrollment": enrollment(), "steam_appids": ["123"], "links": [{
            "external_game_id": "444", "external_game_source": "777", "uid": "123",
            "steam_appid": "123", "game": "33"}]}}}
    catalog = {"count": 0, "games": []}
    return discovery, registry, catalog


def steam():
    instant = datetime(2026, 10, 2, 7, tzinfo=timezone.utc)
    item = {"appid": 123, "success": 1, "name": "Test game", "content_descriptorids": [],
        "release": {"coming_soon_display": "date_full", "is_coming_soon": False,
                    "steam_release_date": int(instant.timestamp())},
        "basic_info": {"short_description": "An adventure."}}
    details = {"steam_appid": 123, "type": "game", "name": "Test game",
               "release_date": {"date": "2026 年 10 月 2 日"}, "content_descriptors": {"ids": []}}
    return item, details


def row(followers=120):
    item, details = steam()
    result, status = build_candidate(123, proof(), item, details, followers,
                                    "2026-10-02T06:00:00Z", NOW, set())
    assert status == "accepted"
    return result


def batch(record=None):
    record = row() if record is None else record
    return {"schema_version": 1, "generated_at": "2026-10-02T09:00:00Z", "records": [record],
            "state_updates": {"123": {"status": "accepted", "updated_at": "2026-10-02T09:00:00Z",
                                      "twitch_admission": record["twitch_admission"]}}}


@pytest.mark.parametrize("field,value", [("method", "name_search"), ("appid", True),
    ("twitch_game_id", "１２"), ("igdb_id", "0"), ("source_frontend_commit", "main"),
    ("checked_at", "2026-10-02T08:00:00")])
def test_proof_rejects_forged_shape(field, value):
    bad = proof(); bad[field] = value
    assert normalize_twitch_admission(bad, 123) is None


@pytest.mark.parametrize("field,value", [("source", "steam_recent_release"),
    ("source", "user_requested_legacy_recovery"), ("viewer_count", 6999),
    ("viewer_count", 8000.0), ("min_viewers", True)])
def test_proof_requires_original_admission_threshold(field, value):
    bad = proof(); bad["source_enrollment"][field] = value
    assert normalize_twitch_admission(bad, 123) is None


def test_snapshot_checks_authoritative_ids_source_and_enrollment():
    discovery, tracking, catalog = snapshot()
    assert validate_snapshot(discovery, tracking, catalog, SHA, NOW) == [(123, proof())]
    assert validate_twitch_snapshot(proof(), tracking, discovery, NOW)
    discovery["games"]["22"]["links"][0]["external_game_source"] = "1"
    assert validate_snapshot(discovery, tracking, catalog, SHA, NOW) == []


@pytest.mark.parametrize("mutation", ["steam_only", "expired", "mismatched_enrollment", "mismatched_igdb", "pending", "future_checked"])
def test_snapshot_rejects_feedback_and_bad_state(mutation):
    discovery, tracking, catalog = snapshot()
    source = tracking["games"]["22"]["tracking_sources"]["twitch_new"]
    if mutation == "steam_only":
        tracking["games"]["22"]["tracking_sources"] = {"steam:123": source}
    elif mutation == "expired": source["expires_at"] = "2026-10-01T00:00:00Z"
    elif mutation == "mismatched_enrollment": source["enrollment"]["viewer_count"] = 9000
    elif mutation == "mismatched_igdb": tracking["games"]["22"]["igdb_id"] = "44"
    elif mutation == "pending": discovery["games"]["22"]["status"] = "pending"
    else: discovery["games"]["22"]["checked_at"] = "2026-10-03T00:00:00Z"
    assert validate_snapshot(discovery, tracking, catalog, SHA, NOW) == []


def test_low_followers_is_independent_admission_not_changed_steam_threshold():
    good = row(17)
    assert is_twitch_qualified(good)
    assert good["official_ge5000"] is False
    from scripts.build_public_steam_shards import valid_record
    assert valid_record(good)
    ordinary = deepcopy(good); ordinary.pop("twitch_admission")
    assert not valid_record(ordinary)


@pytest.mark.parametrize("mutation,reason", [("DLC", "not_a_steam_game"),
    ("descriptor", "adult_content"), ("ledger", "adult_content"),
    ("strong_rule", "adult_content"), ("uncertain", "uncertain_steam_date"),
    ("conflict", "steam_date_conflict"), ("wrong_app", "steam_identity_mismatch"),
    ("missing_type", "steam_type_unavailable")])
def test_steam_official_gates(mutation, reason):
    item, details = steam(); blocked = set()
    if mutation == "DLC": details["type"] = "dlc"
    elif mutation == "descriptor": details["content_descriptors"]["ids"] = [3]
    elif mutation == "ledger": blocked = {123}
    elif mutation == "strong_rule":
        item["tagids"] = [12095, 9130]
        item["basic_info"]["short_description"] = "An explicit sexual game."
    elif mutation == "uncertain": item["release"]["coming_soon_display"] = "date_quarter"; item["release"]["is_coming_soon"] = True
    elif mutation == "conflict": details["release_date"]["date"] = "2026-10-01"
    elif mutation == "wrong_app": details["steam_appid"] = 999
    else: details = {}
    candidate, status = build_candidate(123, proof(), item, details, 40, "2026-10-02T06:00:00Z", NOW, blocked)
    assert candidate is None and status == reason


def test_followers_unknown_boolean_and_future_cache_are_not_zero():
    assert cached_follower(123, [{"games": {"123": {"followers": True, "checked_at": "2026-10-02T01:00:00Z"}}}], NOW) is None
    assert cached_follower(123, [{"games": {"123": {"followers": 12, "checked_at": "2026-10-03T01:00:00Z"}}}], NOW) is None
    item, details = steam()
    assert build_candidate(123, proof(), item, details, None, "2026-10-02T06:00:00Z", NOW, set())[1] == "official_followers_unavailable"
    assert cached_follower(123, [{"games": {"123": {"followers": 0, "checked_at": "2026-10-02T01:00:00Z"}}}], NOW) == (0, "2026-10-02T01:00:00Z")


def test_apply_preserves_unrelated_master_and_newer_official_follower():
    current = row(6000); current["follower_checked_at"] = "2026-10-02T08:30:00Z"
    unrelated = {"appid": 999, "followers": 7000, "release_start": "2027-01-01"}
    output, state = apply_batch({"games": [current, unrelated], "progress": "keep"}, {}, batch())
    assert len(output["games"]) == 2 and output["progress"] == "keep"
    assert next(x for x in output["games"] if x["appid"] == 123)["followers"] == 6000
    assert state["games"]["123"]["status"] == "accepted"
    assert apply_batch(output, state, batch())[0] == output


def test_apply_respects_concurrent_adult_or_date_conflict():
    current = row(); current["content_descriptorids"] = [3]
    output, _ = apply_batch({"games": [current]}, {}, batch())
    assert output["games"][0] == current
    current = row(); current["sexual_content_screened"] = False
    output, _ = apply_batch({"games": [current]}, {}, batch())
    assert output["games"][0] == current
    current = row(); current.update(release_date_conflict=True, release_date_verified_at="2026-10-03T00:00:00Z")
    output, _ = apply_batch({"games": [current]}, {}, batch())
    assert output["games"][0] == current


def test_normal_refresh_and_master_gate_keep_independent_source():
    from scripts.update_steam_daily import merge_partial_segment, merge_segment
    from scripts.steam_master_date_gate import filter_confirmed_master_games
    original = row(); refreshed = deepcopy(original); refreshed.pop("twitch_admission")
    refreshed["followers"] = 200
    assert merge_partial_segment([original], [refreshed], today=date(2026, 10, 2))[0]["twitch_admission"] == proof()
    assert merge_segment([original], [], window_start=date(2026, 10, 1), window_end=date(2026, 11, 1), today=date(2026, 10, 2)) == [original]
    future = row(); instant = datetime(2026, 11, 2, 7, tzinfo=timezone.utc)
    future.update(release_start="2026-11-02", release_end="2026-11-02", release_timestamp_taipei_date="2026-11-02", release_time_utc=instant.isoformat())
    assert filter_confirmed_master_games([future], [], today=date(2026, 10, 2)) == [future]


def test_projection_and_authoritative_rebuild_keep_twitch_source():
    from scripts.build_public_steam_shards import build
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); frontend = root / "frontend"; source = root / "master.json"
        game = row()
        source.write_text(json.dumps({"games": [game]}))
        build(source, frontend, authoritative_future=True)
        projection = json.loads((frontend / "data/catalog.json").read_text())["games"][0]
        assert is_twitch_qualified(projection)
        # An ordinary master refresh must not erase a newer published source.
        source.write_text(json.dumps({"games": [{"appid": 999, "followers": 7000,
              "release_start": "2027-01-01", "release_display_precision": "date_full"}]}))
        build(source, frontend, authoritative_future=True)
        assert (frontend / "data/games/123.json").exists()
        assert 123 in json.loads((frontend / "data/catalog.json").read_text())["games"][0].values()


def fake_frontend(root: Path):
    discovery, tracking, catalog = snapshot()
    (root / "data").mkdir()
    for name, data in [("twitch_steam_discovery.json", discovery), ("twitch_tracking.json", tracking), ("steam_upcoming.json", catalog)]:
        (root / "data" / name).write_text(json.dumps(data))


def extend_frontend_appids(root: Path, appids: list[int]):
    path = root / "data/twitch_steam_discovery.json"
    discovery = json.loads(path.read_text()); record = discovery["games"]["22"]
    record["steam_appids"] = [str(appid) for appid in appids]
    record["links"] = [{"external_game_id": str(400+appid), "external_game_source": "777",
                       "uid": str(appid), "steam_appid": str(appid), "game": "33"} for appid in appids]
    path.write_text(json.dumps(discovery))


def metadata_responses(appid: int):
    item, details = steam(); item["appid"] = appid; details["steam_appid"] = appid
    a, b = Mock(status_code=200), Mock(status_code=200)
    a.json.return_value = {"response": {"store_items": [item]}}
    b.json.return_value = {str(appid): {"success": True, "data": details}}
    return a, b


def test_collect_uses_true_cached_count_and_rerun_does_not_request():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); fake_frontend(root)
        item, details = steam()
        session = Mock()
        response_a, response_b = Mock(status_code=200), Mock(status_code=200)
        response_a.json.return_value = {"response": {"store_items": [item]}}
        response_b.json.return_value = {"123": {"success": True, "data": details}}
        session.get.side_effect = [response_a, response_b]
        cache = {"games": {"123": {"followers": 11, "checked_at": "2026-10-02T06:00:00Z"}}}
        output = collect(root, SHA, {"games": []}, {}, session=session, now=NOW, caches=[cache], blocked=set())
        assert len(output["records"]) == 1 and output["records"][0]["followers"] == 11
        assert session.get.call_count == 2
        master, state = apply_batch({"games": []}, {}, output)
        session.reset_mock()
        again = collect(root, SHA, master, state, session=session, now=NOW, blocked=set())
        assert again["records"] == [] and session.get.call_count == 0


def test_retry_deadline_survives_hourly_frontend_commit():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); fake_frontend(root)
        state = {"games": {"123": {"status": "pending", "retry_at": "2026-10-03T00:00:00Z", "twitch_admission": proof(), "validation_version": 2}}}
        session = Mock()
        output = collect(root, "b"*40, {"games": []}, state, session=session, now=NOW, blocked=set())
        assert output["records"] == [] and session.get.call_count == 0


@pytest.mark.parametrize("mode,expected", [("released", "accepted"),
    ("unverified_missing", "uncertain_steam_date"), ("coming_soon", "uncertain_steam_date"),
    ("explicit_true", "uncertain_steam_date"), ("future_timestamp", "uncertain_steam_date"),
    ("future_today_timestamp", "uncertain_steam_date"),
    ("conflict", "steam_date_conflict")])
def test_omitted_protobuf_false_requires_official_released_corroboration(mode, expected):
    item, details = steam()
    item["release"].pop("is_coming_soon")
    item["release"].pop("coming_soon_display")
    details["release_date"]["coming_soon"] = False
    if mode == "unverified_missing": details["release_date"].pop("coming_soon")
    elif mode == "coming_soon": details["release_date"]["coming_soon"] = True
    elif mode == "explicit_true": item["release"]["is_coming_soon"] = True
    elif mode == "future_timestamp":
        item["release"]["steam_release_date"] = int((NOW + timedelta(days=1)).timestamp())
    elif mode == "future_today_timestamp":
        item["release"]["steam_release_date"] = int((NOW + timedelta(hours=1)).timestamp())
    elif mode == "conflict": details["release_date"]["date"] = "2026-10-01"
    candidate, status = build_candidate(123, proof(), item, details, 12, "2026-10-02T06:00:00Z", NOW, set())
    assert status == expected
    assert (candidate is not None) == (expected == "accepted")


def test_previous_parser_pending_retries_once_without_resetting_cached_followers():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); fake_frontend(root)
        item, details = steam(); item["release"].pop("is_coming_soon"); item["release"].pop("coming_soon_display")
        details["release_date"]["coming_soon"] = False
        session = Mock(); a, b = Mock(status_code=200), Mock(status_code=200)
        a.json.return_value = {"response": {"store_items": [item]}}
        b.json.return_value = {"123": {"success": True, "data": details}}
        session.get.side_effect = [a, b]
        state = {"games": {"123": {"status": "pending", "reason": "uncertain_steam_date",
            "retry_at": "2026-10-03T00:00:00Z", "twitch_admission": proof()}}}
        cache = {"games": {"123": {"followers": 13, "checked_at": "2026-10-02T06:00:00Z"}}}
        output = collect(root, "b"*40, {"games": []}, state, session=session, now=NOW, caches=[cache], blocked=set())
        assert output["records"][0]["followers"] == 13
        assert session.get.call_count == 2
        assert output["state_updates"]["123"]["validation_version"] == 3


def test_official_utc_store_day_crosses_midnight_in_taipei():
    item, details = steam()
    item["release"]["steam_release_date"] = int(datetime(2026, 9, 8, 16, 2, 5, tzinfo=timezone.utc).timestamp())
    details["release_date"] = {"date": "2026 年 9 月 8 日", "coming_soon": False}
    candidate, status = build_candidate(123, proof(), item, details, 42, "2026-10-02T06:00:00Z", NOW, set())
    assert status == "accepted"
    assert candidate["release_start"] == candidate["release_end"] == "2026-09-09"
    assert candidate["release_timestamp_taipei_date"] == "2026-09-09"
    assert candidate["release_store_date"] == "2026-09-08"
    assert candidate["release_date_normalization"] == "steam_utc_date_normalized_to_taipei"
    assert is_twitch_qualified(candidate)
    for bad_day in ["2026-09-07", "2026-09-10", "September 2026"]:
        details["release_date"]["date"] = bad_day
        bad, reason = build_candidate(123, proof(), item, details, 42, "2026-10-02T06:00:00Z", NOW, set())
        assert bad is None
        assert reason in {"steam_date_conflict", "uncertain_taiwan_store_date"}


def test_date_conflict_migration_rechecks_metadata_with_official_cache():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); fake_frontend(root)
        item, details = steam()
        item["release"]["steam_release_date"] = int(datetime(2026, 9, 8, 16, tzinfo=timezone.utc).timestamp())
        details["release_date"] = {"date": "2026-09-08", "coming_soon": False}
        session = Mock(); a, b = Mock(status_code=200), Mock(status_code=200)
        a.json.return_value = {"response": {"store_items": [item]}}
        b.json.return_value = {"123": {"success": True, "data": details}}
        session.get.side_effect = [a, b]
        state = {"games": {"123": {"status": "pending", "reason": "steam_date_conflict",
            "validation_version": 2, "retry_at": "2026-10-03T00:00:00Z", "twitch_admission": proof()}}}
        cache = {"games": {"123": {"followers": 13, "checked_at": "2026-10-02T06:00:00Z"}}}
        output = collect(root, SHA, {"games": []}, state, session=session, now=NOW, caches=[cache], blocked=set())
        assert output["records"][0]["release_start"] == "2026-09-09"
        assert output["records"][0]["followers"] == 13
        assert session.get.call_count == 2
        state = {"games": output["state_updates"]}
        session.reset_mock()
        again = collect(root, SHA, {"games": output["records"]}, state, session=session, now=NOW, caches=[cache], blocked=set())
        assert not again["records"] and session.get.call_count == 0


def test_rate_limit_stops_and_saves_pending_without_fake_followers():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); fake_frontend(root)
        session = Mock(); session.get.return_value = Mock(status_code=429)
        output = collect(root, SHA, {"games": []}, {}, session=session, now=NOW, blocked=set())
        assert output["stop_reason"] == "steam_rate_limited" and output["records"] == []
        assert output["state_updates"]["123"]["status"] == "pending"
        assert "followers" not in output["state_updates"]["123"]


def test_missing_followers_queues_while_next_cached_game_can_be_admitted():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); fake_frontend(root); extend_frontend_appids(root, [123, 124])
        session = Mock()
        session.get.side_effect = [*metadata_responses(123), *metadata_responses(124)]
        cache = {"games": {"124": {"followers": 88, "checked_at": "2026-10-02T06:00:00Z"}}}
        delays = []
        output = collect(root, SHA, {"games": []}, {}, session=session, now=NOW, caches=[cache],
                         blocked=set(), sleep=delays.append)
        assert [(row["appid"], row["followers"]) for row in output["records"]] == [(124, 88)]
        assert output["state_updates"]["123"]["reason"] == "queued_official_followers"
        assert output["state_updates"]["123"]["rate_limit_stage"] is None
        assert [row["appid"] for row in output["follower_candidates"]] == [123]
        assert output["cooldown_updates"] == {}
        assert all(not call.args[0].startswith("https://steamcommunity.com/") for call in session.get.call_args_list)
        assert any(delay > 1 for delay in delays)
        master, saved = apply_batch({"games": []}, {}, output)
        # Later uncached matches join the same queue; accepted rows do no work.
        extend_frontend_appids(root, [123, 124, 125])
        session.reset_mock(); session.get.side_effect = [*metadata_responses(123), *metadata_responses(125)]
        again = collect(root, "b"*40, master, saved, session=session, now=NOW+timedelta(minutes=1),
                        blocked=set(), sleep=lambda _: None)
        assert session.get.call_count == 4
        assert again["state_updates"]["125"]["reason"] == "queued_official_followers"
        assert again["state_updates"]["125"]["retry_at"] is None
        assert [row["appid"] for row in again["follower_candidates"]] == [123, 125]
        assert not again["records"]


def test_per_game_metadata_retry_is_not_rewritten_as_a_global_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); fake_frontend(root)
        state = {"games": {"123": {"status": "pending", "reason": "ConnectionError", "validation_version": 3,
            "retry_at": "2026-10-02T15:00:00Z", "twitch_admission": proof()}}}
        session = Mock()
        output = collect(root, "b"*40, {"games": []}, state, session=session, now=NOW, blocked=set())
        assert session.get.call_count == 0 and output["records"] == []
        assert output["cooldown_updates"] == {}


def test_metadata_429_is_service_wide_cooldown_and_not_bypassed_on_new_appid():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); fake_frontend(root); session = Mock()
        session.get.return_value = Mock(status_code=429, headers={"Retry-After": "900"})
        output = collect(root, SHA, {"games": []}, {}, session=session, now=NOW, blocked=set())
        assert output["state_updates"]["123"]["rate_limit_stage"] == "steam_store_browse"
        master, saved = apply_batch({"games": []}, {}, output)
        extend_frontend_appids(root, [123, 124]); session.reset_mock()
        again = collect(root, "b"*40, master, saved, session=session, now=NOW+timedelta(minutes=1), blocked=set())
        assert not session.get.called and not again["records"]
        assert again["stop_reason"] == "steam_metadata_cooldown"


def test_stale_cooldown_batch_cannot_shorten_a_concurrent_retry_deadline():
    import_batch = {"schema_version": 1, "generated_at": "2026-10-02T09:00:00Z", "records": [], "state_updates": {},
        "cooldown_updates": {"steam_appdetails": {"retry_at": "2026-10-02T10:00:00Z", "updated_at": "2026-10-02T09:00:00Z"}}}
    current = {"api_cooldowns": {"steam_appdetails": {"retry_at": "2026-10-02T15:00:00Z", "updated_at": "2026-10-02T09:01:00Z"}}}
    _, saved = apply_batch({"games": []}, current, import_batch)
    assert saved["api_cooldowns"] == current["api_cooldowns"]


def test_explicit_long_retry_after_is_never_shortened():
    from scripts.import_twitch_steam_discoveries import RateLimited, request
    session = Mock(); session.get.return_value = Mock(status_code=429, headers={"Retry-After": "86400"})
    with pytest.raises(RateLimited) as caught:
        request(session, APPDETAILS, params={"appids": 123})
    assert caught.value.stage == "steam_appdetails" and caught.value.retry_seconds == 86400


@pytest.mark.parametrize("url", [
    "https://steamcommunity.com/games/123/memberslistxml/", "http://steamcommunity.com/app/123/",
    "https://WWW.STEAMCOMMUNITY.COM:443/games/123/",
])
def test_community_request_is_rejected_before_network_access(url):
    from scripts.import_twitch_steam_discoveries import request
    session = Mock()
    with pytest.raises(RuntimeError, match="Community requests belong to the official Followers queue"):
        request(session, url)
    assert not session.get.called


def test_dispatch_requires_persisted_master_and_deduplicates_receipt():
    game = row(); state = {"games": {"123": {"status": "accepted"}}}
    session = Mock(); session.post.return_value = Mock(status_code=204)
    empty = dispatch({"games": []}, state, token="token", target="a/b", session=session, now=NOW)
    assert empty["state_updates"] == {} and not session.post.called
    output = dispatch({"games": [game]}, state, token="token", target="a/b", session=session, now=NOW)
    payload = session.post.call_args.kwargs["json"]
    assert payload["event_type"] == "steam_game_twitch_discovered"
    assert payload["client_payload"]["official_followers"] == 120
    master, saved = apply_batch({"games": [game]}, state, output)
    session.reset_mock()
    assert dispatch(master, saved, token="token", target="a/b", session=session, now=NOW)["state_updates"] == {}
    assert not session.post.called
    ordinary = {"appid": 999, "followers": 7000, "release_start": "2026-09-01"}
    current = {"games": [game, ordinary], "updated_at": "keep"}
    unchanged, _ = apply_batch(current, saved, output)
    assert unchanged == current


def test_failed_dispatch_is_pending_and_does_not_erase_enrollment():
    game = row(); state = {"games": {"123": {"status": "accepted", "twitch_admission": proof()}}}
    session = Mock(); session.post.return_value = Mock(status_code=403)
    output = dispatch({"games": [game]}, state, token="token", target="a/b", session=session, now=NOW)
    _, saved = apply_batch({"games": [game]}, state, output)
    assert saved["games"]["123"]["content_dispatch"]["status"] == "pending"
    assert saved["games"]["123"]["twitch_admission"] == proof()
    assert "token" not in json.dumps(output)


@pytest.mark.parametrize("unavailable", [False, True])
def test_maintenance_date_repair_retains_low_follower_source(unavailable):
    from scripts import repair_steam_master_store_dates as repair
    from scripts.steam_master_date_gate import parse_store_release_detail
    import sys
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); master = root / "master.json"; eligible = root / "eligible.json"; report = root / "report.json"
        original = row(12)
        original.update(release_start="2026-11-02", release_end="2026-11-02",
                        release_timestamp_taipei_date="2026-11-02", release_time_utc="2026-11-02T07:00:00Z")
        # Preserve the legacy repair's mass-deletion guard while isolating the
        # newly accepted source from the existing >=5000 history.
        regular = [{"appid": 1000+i, "followers": 6000, "release_start": "2026-09-01"} for i in range(80)]
        master.write_text(json.dumps({"games": [original, *regular]})); eligible.write_text(json.dumps({"games": []}))
        item, _ = steam()
        item["release"]["steam_release_date"] = int(datetime(2026, 11, 2, 7, tzinfo=timezone.utc).timestamp())
        detail = {"exact": False, "status": "unavailable"} if unavailable else parse_store_release_detail(item, today=NOW.date())
        with patch.object(sys, "argv", ["repair", "--master", str(master), "--eligible", str(eligible), "--report", str(report)]), \
             patch.object(repair, "taiwan_today", return_value=NOW.date()), \
             patch.object(repair, "fetch_store_release_details", return_value={123: detail}):
            repair.main()
        result = json.loads(master.read_text())
        saved = next(x for x in result["games"] if x["appid"] == 123)
        assert is_twitch_qualified(saved) and saved["twitch_admission"] == proof()
        assert saved["followers"] == 12 and saved["follower_checked_at"] == original["follower_checked_at"]
        assert len(result["games"]) == 81
