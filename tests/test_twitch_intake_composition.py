"""Direct intake composition and historical call-time dependencies."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.adapters import official_catalog, twitch_intake as intake
from radar_backend.domain import steam_retry
from radar_backend.jobs import publish_steam
from scripts import import_twitch_steam_discoveries as legacy
from scripts import reconcile_twitch_official_queue as reconcile
from scripts import steam_retry_policy as retry
from scripts import twitch_official_queue as queue


NOW = datetime(2026, 10, 9, 9, tzinfo=timezone.utc)
SHA = "a" * 40


def snapshot(frontend):
    enrollment = {"source": "igdb_first_release_date", "observed_at": "2026-10-08T08:00:00Z",
                  "viewer_count": 8000, "min_viewers": 7000}
    tracking = {"schema_version": 1, "games": {"22": {"game_id": "22", "igdb_id": "33",
        "tracking_sources": {"twitch_new": {"source": "twitch_new", "status": "active",
            "expires_at": "2026-10-28T00:00:00Z", "enrollment": enrollment}}}}}
    discovery = {"schema_version": 1, "steam_source_id": "777", "games": {"22": {
        "twitch_game_id": "22", "igdb_id": "33", "active": True, "status": "matched",
        "method": "twitch_igdb_external_steam_v1", "checked_at": "2026-10-08T09:00:00Z",
        "twitch_enrollment": enrollment, "steam_appids": ["123"], "links": [{
            "external_game_id": "444", "external_game_source": "777", "uid": "123",
            "steam_appid": "123", "game": "33"}]}}}
    for name, value in (("twitch_tracking.json", tracking), ("twitch_steam_discovery.json", discovery),
                        ("steam_upcoming.json", {"count": 0, "games": []})):
        path = frontend / "data" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


def metadata():
    instant = datetime(2026, 10, 20, 7, tzinfo=timezone.utc)
    item = {"appid": 123, "success": 1, "name": "Verified game", "content_descriptorids": [],
            "release": {"coming_soon_display": "date_full", "is_coming_soon": True,
                        "steam_release_date": int(instant.timestamp())}}
    details = {"steam_appid": 123, "type": "game", "name": "Verified game",
               "release_date": {"date": "2026 年 10 月 20 日", "coming_soon": True},
               "content_descriptors": {"ids": []}}
    return item, details


def forbid_legacy(monkeypatch):
    for module, names in ((legacy, ("validate_snapshot", "build_candidate", "cached_follower",
                                    "collect", "dispatch", "apply_batch", "read_json", "write_json")),
                          (queue, ("is_twitch_queue_candidate", "sync_twitch_queue")),
                          (reconcile, ("apply_queue_batch",)),
                          (retry, ("rate_limit_policy", "transient_retry_policy"))):
        for name in names:
            monkeypatch.setattr(module, name, Mock(side_effect=AssertionError("Legacy callback forbidden")))


@pytest.mark.parametrize("count", [None, 0, 120])
def test_canonical_collect_and_queue_keep_real_counts_separate_from_pending_metadata(tmp_path, monkeypatch, count):
    forbid_legacy(monkeypatch)
    monkeypatch.setattr(intake, "excluded_appids", lambda: set())
    frontend = tmp_path / "frontend"
    snapshot(frontend)
    item, details = metadata()
    responses = iter(({"response": {"store_items": [item]}}, {"123": {"success": True, "data": details}}))
    get = Mock(side_effect=lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: next(responses), raise_for_status=lambda: None))
    session = SimpleNamespace(headers={}, get=get)
    sleep = Mock()
    caches = [{"pending_candidates": {"123": {"appid": 123, "group_id64": "103582791429521531"}}}] if count is None else [{"games": {"123": {"followers": count,
                                                        "checked_at": "2026-10-08T08:00:00Z"}}}]
    batch = intake.collect(frontend, SHA, {"games": []}, {}, session=session, now=NOW,
                           caches=caches, monotonic=lambda: 100, sleep=sleep)
    assert batch["active_twitch_appids"] == [123] and get.call_count == 2
    assert [call.kwargs["timeout"] for call in get.call_args_list] == [25, 25]
    sleep.assert_called_once_with(1.5)
    normal = {"appid": 123, "name": "Normal", "group_id64": "999", "unknown": {"kept": True}}
    original = {"pending_candidates": {"123": normal}, "official_results": {}, "cursor": 17}
    master, state, checkpoint = intake.apply_queue_batch({"games": []}, {}, original, batch)
    assert original["pending_candidates"]["123"] == normal and checkpoint["cursor"] == 17
    if count is None:
        assert master["games"] == [] and batch["records"] == []
        candidate = checkpoint["pending_candidates"]["123"]
        assert candidate["normal_candidate"] == normal and candidate["group_id64"] == "999"
        assert official_catalog.is_twitch_queue_candidate(candidate, now=NOW)
        for field in queue.FOLLOWER_FIELDS:
            assert field not in candidate and field not in candidate["steam_candidate"]
        restored = intake.sync_twitch_queue(checkpoint, {"follower_candidates": []}, NOW)
        assert restored["pending_candidates"]["123"] == normal
    else:
        assert batch["follower_candidates"] == [] and master["games"][0]["followers"] == count
        assert master["games"][0]["follower_checked_at"] == "2026-10-08T08:00:00Z"
        assert checkpoint == original and state["games"]["123"]["status"] == "accepted"
        assert official_catalog.cached_follower(123, caches, NOW) == (count, "2026-10-08T08:00:00Z")


def test_dispatch_receipt_job_uses_canonical_apply_and_writes_only_import_state(monkeypatch, tmp_path):
    forbid_legacy(monkeypatch)
    monkeypatch.setattr(intake, "excluded_appids", lambda: set())
    batch_path = tmp_path / "receipt.json"
    batch = {"schema_version": 1, "generated_at": intake.stamp(NOW), "records": [],
             "state_updates": {"123": {"updated_at": intake.stamp(NOW), "status": "accepted",
                                       "content_signature": "same", "content_dispatch": {"status": "dispatched"}}}}
    master, state, checkpoint = {"games": []}, {}, {"pending_candidates": {}, "cursor": 17}
    values = {batch_path: batch, intake.MASTER: master, intake.STATE: state, intake.CHECKPOINT: checkpoint}
    monkeypatch.setattr(publish_steam, "read_json", lambda path, **kw: deepcopy(values[path]))
    save = Mock()
    monkeypatch.setattr(publish_steam, "write_json", save)
    assert publish_steam.main(["apply-dispatch-batch", "--batch", str(batch_path)]) == 0
    save.assert_called_once()
    assert save.call_args.args[0] == intake.STATE
    assert save.call_args.args[1]["games"]["123"]["content_dispatch"]["status"] == "dispatched"
    assert master == {"games": []} and checkpoint == {"pending_candidates": {}, "cursor": 17}


def test_legacy_collect_resolves_current_rule_state_transport_and_constant_ports(monkeypatch):
    events = []
    monkeypatch.setattr(legacy, "read_json", lambda path: (events.append(path.name), {})[1])
    monkeypatch.setattr(legacy, "validate_snapshot", lambda *a: [(123, {"checked_at": "proof"})])
    monkeypatch.setattr(legacy, "retained_follower_candidate", lambda *a: None)
    monkeypatch.setattr(legacy, "excluded_appids", lambda: set())
    monkeypatch.setattr(legacy, "cached_follower", lambda *a: (12, "measured"))
    monkeypatch.setattr(legacy, "is_twitch_qualified", lambda *a: False)
    build = Mock(return_value=({"appid": 123, "followers": 12, "follower_checked_at": "measured"}, "accepted"))
    monkeypatch.setattr(legacy, "build_candidate", build)
    monkeypatch.setattr(legacy, "signature", lambda row: "patched")
    monkeypatch.setattr(legacy, "STORE_BROWSE", "browse")
    monkeypatch.setattr(legacy, "APPDETAILS", "details")
    monkeypatch.setattr(legacy, "STATE_VALIDATION_VERSION", 99)
    replies = iter(({"response": {"store_items": [{"appid": 123}]}}, {"123": {"success": True, "data": {}}}))
    request = Mock(side_effect=lambda *a, **kw: SimpleNamespace(json=lambda: next(replies)))
    monkeypatch.setattr(legacy, "request", request)
    session = SimpleNamespace(headers={})
    result = legacy.collect(Path("front"), SHA, {"games": []}, {}, session=session, now=NOW,
                            monotonic=lambda: 100, sleep=lambda seconds: None)
    assert events == ["twitch_steam_discovery.json", "twitch_tracking.json", "steam_upcoming.json"]
    assert [call.args[1] for call in request.call_args_list] == ["browse", "details"]
    assert build.call_count == 2 and build.call_args.args[4:6] == (12, "measured")
    assert result["state_updates"]["123"]["validation_version"] == 99
    assert result["state_updates"]["123"]["content_signature"] == "patched"


def test_legacy_cache_json_and_reconcile_resolve_current_dependencies(monkeypatch):
    clock = Mock(return_value=NOW)
    monkeypatch.setattr(legacy, "datetime", SimpleNamespace(now=clock))
    monkeypatch.setattr(legacy, "aware_time", lambda value: NOW if value == "custom" else None)
    assert legacy.cached_follower(123, [{"games": {"123": {"followers": 0, "checked_at": "custom"}}}]) == (0, "custom")
    clock.assert_called_once_with(timezone.utc)
    encoder = Mock(return_value="encoded")
    digest = Mock(return_value=SimpleNamespace(hexdigest=lambda: "digest"))
    monkeypatch.setattr(legacy, "json", SimpleNamespace(dumps=encoder))
    monkeypatch.setattr(legacy, "hashlib", SimpleNamespace(sha256=digest))
    assert legacy.identity_signature({"source_frontend_commit": SHA, "appid": 123}) == "digest"
    encoder.assert_called_once_with({"appid": 123}, sort_keys=True, separators=(",", ":"))
    digest.assert_called_once_with(b"encoded")
    apply, sync = Mock(return_value=({"new": True}, {"state": True})), Mock(return_value={"queue": True})
    monkeypatch.setattr(reconcile, "apply_batch", apply)
    monkeypatch.setattr(reconcile, "aware_time", lambda value: "frozen")
    monkeypatch.setattr(reconcile, "sync_twitch_queue", sync)
    batch = {"generated_at": "custom", "follower_candidates": []}
    assert reconcile.apply_queue_batch({}, {}, {"old": True}, batch) == ({"new": True}, {"state": True}, {"queue": True})
    sync.assert_called_once_with({"old": True}, batch, "frozen")


@pytest.mark.parametrize("header", ["0", "15", "-1", "bad", None, "Sun, 11 Oct 2026 09:00:00 GMT"])
@pytest.mark.parametrize("prior,attempt,backoff", [({}, 1, 300),
    ({"attempts": 0, "retry_attempts": 0}, 1, 300),
    ({"attempts": 2, "retry_attempts": 2}, 3, 1200),
    ({"attempts": True, "retry_attempts": True}, 1, 300)])
def test_canonical_retry_rules_and_legacy_entry_preserve_header_and_backoff_receipts(header, prior, attempt, backoff):
    server_seconds = {"0": 0, "15": 15, "Sun, 11 Oct 2026 09:00:00 GMT": 172800}
    seconds = server_seconds.get(header, backoff)
    expected = {"retry_at": intake.stamp(NOW + timedelta(seconds=seconds)),
                "observed_at": intake.stamp(NOW), "retry_seconds": seconds,
                "retry_source": "steam_retry_after" if header in server_seconds else "default_backoff",
                "retry_after": header, "attempts": attempt}
    transient = {"retry_at": intake.stamp(NOW + timedelta(seconds=backoff)),
                 "retry_attempts": attempt, "retry_source": "transient_backoff"}
    for entry in (retry, steam_retry):
        assert entry.rate_limit_policy("steam_store_browse", header, NOW, prior) == expected
        assert entry.transient_retry_policy(prior, NOW) == transient


def test_legacy_retry_uses_current_helper_callbacks_before_composing_deadline(monkeypatch):
    events = []
    monkeypatch.setattr(retry, "_utc", lambda now: (events.append("utc"), NOW)[1])
    monkeypatch.setattr(retry, "_attempt", lambda prior, field: (events.append(field), 7)[1])
    monkeypatch.setattr(retry, "_server_retry_seconds", lambda value, now: (events.append("header"), None)[1])
    monkeypatch.setattr(retry, "_backoff", lambda first, cap, attempt: (events.append((first, cap, attempt)), 42)[1])
    monkeypatch.setattr(retry, "_stamp", lambda at: at.isoformat())
    result = retry.rate_limit_policy("steam_appdetails", "bad", NOW)
    assert events == ["utc", "attempts", "header", (300, 1800, 7)]
    assert result["retry_at"] == (NOW + timedelta(seconds=42)).isoformat() and result["attempts"] == 7
