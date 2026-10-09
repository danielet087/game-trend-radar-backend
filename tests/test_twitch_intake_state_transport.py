"""Offline persistence, latest-state replay and bounded Steam metadata transport."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.adapters import steam_twitch_intake as http
from radar_backend.state import twitch_intake as state

NOW = datetime(2026, 10, 9, 2, 30, tzinfo=timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def aware(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def decimal_id(value):
    return str(value) if isinstance(value, (str, int)) and str(value).isdigit() and int(value) > 0 else None


def row(aid=7, *, count=17, at=NOW, release="2026-10-09", **extra):
    return {"appid": aid, "followers": count, "follower_checked_at": stamp(at),
            "follower_source": "official", "official_ge5000": count >= 5000,
            "release_start": release, "qualified": True, **extra}


def batch(*records, updates=None, cooldowns=None):
    result = {"schema_version": 1, "generated_at": stamp(NOW), "records": list(records),
              "state_updates": updates or {}}
    if cooldowns is not None:
        result["cooldown_updates"] = cooldowns
    return result


def ports(**values):
    return {"excluded_appids": Mock(return_value={99}),
            "is_twitch_qualified": lambda value: isinstance(value, dict) and value.get("qualified") is True,
            "is_disallowed": lambda value, blocked: value.get("appid") in blocked or value.get("adult") is True,
            "preserve_twitch_admission": lambda current, incoming: deepcopy(incoming),
            "keep_newer_release": lambda current, incoming: deepcopy(incoming),
            "aware_time": aware, "decimal_id": decimal_id, **values}


def apply(master=None, saved=None, value=None, **kwargs):
    return state.apply_batch(master or {"games": []}, saved or {}, value or batch(), **ports(**kwargs))


def test_optional_read_returns_empty_without_reading_missing_path(tmp_path):
    missing = tmp_path / "missing.json"
    assert state.read_json(missing, optional=True) == {}
    with pytest.raises(FileNotFoundError):
        state.read_json(missing)


@pytest.mark.parametrize("raw", ["[]", "null", "true", "3", '"text"'])
def test_read_requires_json_object_and_reports_filename(tmp_path, raw):
    target = tmp_path / "input.json"
    target.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="Expected JSON object: input.json"):
        state.read_json(target)


def test_optional_read_does_not_hide_existing_broken_json(tmp_path):
    target = tmp_path / "broken.json"
    target.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        state.read_json(target, optional=True)


def test_read_preserves_standard_json_duplicate_key_behavior(tmp_path):
    target = tmp_path / "duplicate.json"
    target.write_text('{"count": 1, "count": 2}', encoding="utf-8")
    assert state.read_json(target) == {"count": 2}


def test_atomic_write_keeps_utf8_pretty_newline_and_replaces_existing_temporary(tmp_path):
    target = tmp_path / "nested" / "state.data.json"
    target.parent.mkdir()
    temporary = target.with_suffix(".json.tmp")
    target.write_text("old", encoding="utf-8")
    temporary.write_text("old temporary", encoding="utf-8")
    value = {"遊戲": "台灣", "nested": [1, 2]}
    assert state.write_json(target, value) is None
    assert target.read_bytes() == (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    assert not temporary.exists()
    assert state.read_json(target) == value


def test_write_creates_parents_before_encoding_failure_and_keeps_existing_content(tmp_path):
    target = tmp_path / "new" / "state.json"
    encoder = SimpleNamespace(dumps=Mock(side_effect=ValueError("encoding")))
    with pytest.raises(ValueError, match="encoding"):
        state.write_json(target, {"value": object()}, json_module=encoder)
    assert target.parent.is_dir()
    assert not target.exists() and not target.with_suffix(".json.tmp").exists()
    encoder.dumps.assert_called_once()


@pytest.mark.parametrize("stage", ["mkdir", "encode", "write", "replace"])
def test_write_errors_propagate_in_original_atomic_io_order(stage):
    trace = []
    class PathPort:
        suffix = ".json"
        @property
        def parent(self):
            return self
        def mkdir(self, **kwargs):
            trace.append(("mkdir", kwargs))
            if stage == "mkdir":
                raise OSError(stage)
        def with_suffix(self, suffix):
            trace.append(("suffix", suffix))
            return self
        def write_text(self, raw, **kwargs):
            trace.append(("write", raw, kwargs))
            if stage == "write":
                raise OSError(stage)
        def replace(self, path):
            trace.append(("replace", path))
            if stage == "replace":
                raise OSError(stage)
    target = PathPort()
    def encode(value, **kwargs):
        trace.append(("encode", value, kwargs))
        if stage == "encode":
            raise ValueError(stage)
        return "{}"
    with pytest.raises(ValueError if stage == "encode" else OSError, match=stage):
        state.write_json(target, {}, json_module=SimpleNamespace(dumps=encode))
    expected = {"mkdir": ["mkdir"], "encode": ["mkdir", "suffix", "encode"],
                "write": ["mkdir", "suffix", "encode", "write"],
                "replace": ["mkdir", "suffix", "encode", "write", "replace"]}
    assert [event[0] for event in trace] == expected[stage]
    assert trace[0][1] == {"parents": True, "exist_ok": True}


@pytest.mark.parametrize("kind", ["schema", "records", "updates", "master", "state"])
def test_malformed_batches_fail_before_copying_or_loading_exclusions(kind):
    master, saved, value = {"games": []}, {}, batch()
    if kind == "schema":
        value["schema_version"] = 2
    elif kind == "records":
        value["records"] = {}
    elif kind == "updates":
        value["state_updates"] = []
    elif kind == "master":
        master["games"] = {}
    else:
        saved["games"] = []
    copy, exclusions = Mock(), Mock()
    with pytest.raises(ValueError, match="Malformed"):
        state.apply_batch(master, saved, value, **ports(deepcopy_fn=copy, excluded_appids=exclusions))
    copy.assert_not_called()
    exclusions.assert_not_called()


def test_apply_copies_existing_metadata_and_state_update_evidence_before_mutation():
    master = {"games": [row(9, rich={"keep": [1]})], "other": {"keep": [2]}}
    saved = {"schema_version": 1, "games": {"8": {"nested": [3]}}}
    value = batch(row(), updates={"7": {"updated_at": stamp(NOW), "nested": [4]}})
    before = deepcopy((master, saved, value))
    result, merged = apply(master, saved, value)
    assert (master, saved, value) == before
    result["other"]["keep"].append(3)
    next(game for game in result["games"] if game["appid"] == 9)["rich"]["keep"].append(2)
    merged["games"]["8"]["nested"].append(4)
    merged["games"]["7"]["nested"].append(5)
    assert (master, saved, value) == before


@pytest.mark.parametrize("change", [{"qualified": False}, {"adult": True}, {"appid": 99}])
def test_unqualified_or_adult_batch_records_fail_without_mutating_inputs(change):
    master, saved = {"games": [row(9)]}, {"games": {}}
    incoming = row(**change)
    before = deepcopy((master, saved, incoming))
    with pytest.raises(ValueError, match="unqualified Steam admission"):
        apply(master, saved, batch(incoming))
    assert (master, saved, incoming) == before


@pytest.mark.parametrize("change", [{"adult": True}, {"sexual_content_screened": False}])
def test_current_authoritative_adult_screen_wins_without_candidate_merge(change):
    current = row(count=6000, **change)
    newer, preserve = Mock(), Mock()
    result, _ = apply({"games": [current]}, value=batch(row()),
                      keep_newer_release=newer, preserve_twitch_admission=preserve)
    assert result["games"] == [current]
    newer.assert_not_called()
    preserve.assert_not_called()


def test_newer_release_is_resolved_before_preserving_twitch_proof():
    current, incoming = row(count=20), row(count=30)
    calls = []
    def release(old, new):
        calls.append(("date", old, new))
        return {**new, "release_start": "2026-10-10"}
    def proof(old, new):
        calls.append(("proof", old, new))
        return {**new, "twitch_admission": {"old": "retained"}}
    result, _ = apply({"games": [current]}, value=batch(incoming),
                      keep_newer_release=release, preserve_twitch_admission=proof)
    assert [call[0] for call in calls] == ["date", "proof"]
    assert calls[1][2]["release_start"] == "2026-10-10"
    assert result["games"][0]["twitch_admission"] == {"old": "retained"}


@pytest.mark.parametrize("delta,expected", [(timedelta(seconds=-1), 17), (timedelta(), 17), (timedelta(seconds=1), 6000)])
def test_latest_official_followers_win_only_on_strictly_newer_timestamp(delta, expected):
    current = row(count=6000, at=NOW + delta, follower_source="new-source", official_ge5000=True,
                  unknown={"metadata": "keep"})
    result, _ = apply({"games": [current]}, value=batch(row(count=17)))
    merged = result["games"][0]
    assert merged["followers"] == expected
    assert merged["unknown"] == {"metadata": "keep"}
    if delta > timedelta():
        assert merged["follower_source"] == "new-source" and merged["official_ge5000"] is True
        assert merged["follower_checked_at"] == current["follower_checked_at"]


def test_missing_optional_latest_follower_fields_are_not_invented_during_preservation():
    current = row(count=100, at=NOW + timedelta(seconds=1))
    current.pop("follower_source")
    current.pop("official_ge5000")
    result, _ = apply({"games": [current]}, value=batch(row(count=17)))
    merged = result["games"][0]
    assert merged["followers"] == 100
    assert merged["follower_source"] == "official" and merged["official_ge5000"] is False


@pytest.mark.parametrize("qualification,adult", [(False, False), (True, True)])
def test_candidate_invalid_after_concurrent_merge_is_skipped(qualification, adult):
    current = row(count=6000)
    preserve = lambda old, new: {**new, "qualified": qualification, "adult": adult}
    result, _ = apply({"games": [current]}, value=batch(row()), preserve_twitch_admission=preserve)
    assert result["games"] == [current]
    assert "updated_at" not in result


def test_nonempty_records_trigger_sort_even_when_every_candidate_is_skipped():
    first, second = row(9, sexual_content_screened=False), row(7, sexual_content_screened=False)
    master = {"games": [first, second]}
    result, _ = apply(master, value=batch(row(9)))
    assert [game["appid"] for game in result["games"]] == [7, 9]
    assert result["updated_at"] == stamp(NOW)
    unchanged, _ = apply(master, value=batch())
    assert unchanged["games"] == [first, second] and "updated_at" not in unchanged


def test_master_updated_at_changes_only_for_changed_rows():
    original = {"games": [row()], "updated_at": "keep"}
    equal, _ = apply(original, value=batch(row()))
    assert equal["updated_at"] == "keep"
    changed, _ = apply(original, value=batch(row(count=30)))
    assert changed["updated_at"] == stamp(NOW)


def test_empty_batch_initializes_schema_and_games_without_changing_master():
    result, merged = apply(value=batch())
    assert result == {"games": []}
    assert merged == {"schema_version": 1, "games": {}, "updated_at": stamp(NOW)}
    _, again = apply(saved=merged, value={**batch(), "generated_at": "later"})
    assert again == merged


@pytest.mark.parametrize("aid,update", [("bad", {"updated_at": stamp(NOW)}), ("7", []),
                                      ("7", {}), ("7", {"updated_at": "naive"})])
def test_malformed_per_appid_state_updates_are_rejected(aid, update):
    with pytest.raises(ValueError, match="Malformed per-AppID"):
        apply(value=batch(updates={aid: update}))


@pytest.mark.parametrize("delta,expected", [(timedelta(seconds=-1), "incoming"),
    (timedelta(), "incoming"), (timedelta(seconds=1), "latest")])
def test_state_update_timestamp_prevents_older_receipt_overwrite(delta, expected):
    latest = {"updated_at": stamp(NOW + delta), "marker": "latest", "unrelated": {"keep": True}}
    incoming = {"updated_at": stamp(NOW), "marker": "incoming"}
    _, merged = apply(saved={"games": {"7": latest}}, value=batch(updates={"7": incoming}))
    assert merged["games"]["7"]["marker"] == expected
    assert merged["games"]["7"]["unrelated"] == {"keep": True}


@pytest.mark.parametrize("same,explicit", [(False, False), (True, False), (False, True)])
def test_dispatch_receipt_is_invalidated_only_for_changed_signature_without_new_receipt(same, explicit):
    latest = {"updated_at": stamp(NOW), "content_signature": "old", "content_dispatch": {"status": "dispatched"}}
    update = {"updated_at": stamp(NOW), "content_signature": "old" if same else "new"}
    if explicit:
        update["content_dispatch"] = {"status": "pending"}
    _, merged = apply(saved={"games": {"7": latest}}, value=batch(updates={"7": update}))
    assert ("content_dispatch" in merged["games"]["7"]) is (same or explicit)
    if explicit:
        assert merged["games"]["7"]["content_dispatch"] == {"status": "pending"}


@pytest.mark.parametrize("stage,value", [("steam_community", {"retry_at": stamp(NOW), "updated_at": stamp(NOW)}),
    ("steam_appdetails", {"retry_at": "bad", "updated_at": stamp(NOW)}),
    ("steam_store_browse", {"retry_at": stamp(NOW), "updated_at": None})])
def test_malformed_service_cooldowns_fail(stage, value):
    with pytest.raises(ValueError, match="Malformed Steam cooldown"):
        apply(value=batch(cooldowns={stage: value}))


@pytest.mark.parametrize("updated_delta,until_delta,expected_marker,expected_until", [
    (-1, 30, "latest", 20), (0, 5, "incoming", 20), (1, 5, "incoming", 20),
    (0, 30, "incoming", 30), (1, 30, "incoming", 30)])
def test_shared_cooldown_replay_preserves_newer_evidence_and_longer_deadline(updated_delta, until_delta, expected_marker, expected_until):
    latest = {"updated_at": stamp(NOW), "retry_at": stamp(NOW + timedelta(minutes=20)), "marker": "latest"}
    update = {"updated_at": stamp(NOW + timedelta(seconds=updated_delta)),
              "retry_at": stamp(NOW + timedelta(minutes=until_delta)), "marker": "incoming"}
    saved = {"games": {}, "api_cooldowns": {"steam_appdetails": latest}}
    frozen = batch(cooldowns={"steam_appdetails": update})
    before = deepcopy((saved, frozen))
    _, merged = apply(saved=saved, value=frozen)
    receipt = merged["api_cooldowns"]["steam_appdetails"]
    assert receipt["marker"] == expected_marker
    assert receipt["retry_at"] == stamp(NOW + timedelta(minutes=expected_until))
    assert (saved, frozen) == before


def test_copy_and_exclusion_ports_are_used_at_runtime():
    copy = Mock(side_effect=deepcopy)
    excluded = Mock(return_value=set())
    saved = {"schema_version": 1, "games": {}}
    apply({"games": [row()]}, saved, batch(row()), deepcopy_fn=copy, excluded_appids=excluded)
    assert copy.call_count >= 3
    excluded.assert_called_once_with()


@pytest.mark.parametrize("url", ["https://steamcommunity.com/app/7", "https://STEAMCOMMUNITY.COM/app/7",
    "https://sub.steamcommunity.com/profiles/7", "http://deep.sub.steamcommunity.com/"])
def test_community_host_guard_runs_before_network_or_policy(url):
    session, policy = Mock(), Mock()
    with pytest.raises(RuntimeError, match="official Followers queue"):
        http.request(session, url, rate_limit_policy=policy)
    session.get.assert_not_called()
    policy.assert_not_called()


@pytest.mark.parametrize("url", [http.STORE_BROWSE, "https://store.steampowered.com/api/appdetails",
    "https://steamcommunity.com.example.invalid/", "https://example.invalid/steamcommunity.com"])
def test_noncommunity_request_preserves_params_timeout_and_response_identity(url):
    session, policy = Mock(), Mock()
    response = Mock(status_code=200)
    session.get.return_value = response
    params = {"appid": 7}
    assert http.request(session, url, params=params, timeout=0.5, rate_limit_policy=policy) is response
    session.get.assert_called_once_with(url, params=params, timeout=0.5)
    assert session.get.call_args.kwargs["params"] is params
    response.raise_for_status.assert_called_once_with()
    policy.assert_not_called()


@pytest.mark.parametrize("url,stage", [(http.STORE_BROWSE, "steam_store_browse"),
    ("https://store.steampowered.com/api/appdetails", "steam_appdetails"),
    (http.STORE_BROWSE + "?query=1", "steam_appdetails")])
def test_429_uses_exact_endpoint_stage_and_observed_response_clock(url, stage):
    session = Mock()
    session.get.return_value = Mock(status_code=429, headers={"Retry-After": "900"})
    observed = NOW + timedelta(minutes=10)
    prior = {"attempts": 2}
    value = {"retry_seconds": 900, "retry_at": stamp(observed + timedelta(minutes=15))}
    policy = Mock(return_value=value)
    clock = Mock(return_value=observed)
    with pytest.raises(http.RateLimited) as caught:
        http.request(session, url, now=NOW, clock=clock, prior_cooldown=prior, rate_limit_policy=policy)
    error = caught.value
    assert str(error) == "Steam HTTP 429" and error.stage == stage and error.retry_seconds == 900
    assert error.policy is value
    policy.assert_called_once_with(stage, "900", observed, prior)
    clock.assert_called_once_with()
    session.get.return_value.raise_for_status.assert_not_called()


@pytest.mark.parametrize("mode", ["supplied_now", "default_clock"])
def test_429_clock_fallback_keeps_original_explicit_time_precedence(mode):
    session = Mock()
    session.get.return_value = Mock(status_code=429, headers={})
    policy = Mock(return_value={"retry_seconds": 60})
    wall = Mock(return_value=NOW)
    options = {"now": NOW} if mode == "supplied_now" else {}
    with pytest.raises(http.RateLimited):
        http.request(session, http.STORE_BROWSE, rate_limit_policy=policy,
                     datetime_type=SimpleNamespace(now=wall), **options)
    assert policy.call_args.args == ("steam_store_browse", None, NOW, None)
    assert wall.call_count == (0 if mode == "supplied_now" else 1)
    if mode == "default_clock":
        wall.assert_called_once_with(timezone.utc)


def test_falsey_clock_is_still_used_by_transport_when_it_is_not_none():
    class Clock:
        def __bool__(self):
            return False
        def __call__(self):
            return NOW
    session = Mock()
    session.get.return_value = Mock(status_code=429, headers={})
    policy = Mock(return_value={"retry_seconds": 60})
    with pytest.raises(http.RateLimited):
        http.request(session, http.STORE_BROWSE, clock=Clock(), rate_limit_policy=policy,
                     datetime_type=SimpleNamespace(now=Mock(side_effect=AssertionError("clock must win"))))
    assert policy.call_args.args[2] is NOW


def test_transport_propagates_http_status_errors_without_retry_reclassification():
    session = Mock()
    session.get.return_value = Mock(status_code=500)
    session.get.return_value.raise_for_status.side_effect = OSError("HTTP failure")
    policy = Mock()
    with pytest.raises(OSError, match="HTTP failure"):
        http.request(session, http.STORE_BROWSE, rate_limit_policy=policy)
    policy.assert_not_called()


def test_transport_custom_constants_splitter_and_rate_exception_resolve_at_call_time():
    class CustomRateError(RuntimeError):
        def __init__(self, stage, seconds, *, policy):
            self.values = stage, seconds, policy
    session = Mock()
    session.get.return_value = Mock(status_code=429, headers={})
    split = Mock(return_value=SimpleNamespace(hostname="custom.invalid"))
    value = {"retry_seconds": 7}
    with pytest.raises(CustomRateError) as caught:
        http.request(session, "custom", now=NOW, store_browse="custom", urlsplit_fn=split,
                     rate_limited_type=CustomRateError, rate_limit_policy=lambda *args: value)
    split.assert_called_once_with("custom")
    assert caught.value.values == ("steam_store_browse", 7, value)
