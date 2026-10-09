"""Queue policy boundaries use the real pinned Core without legacy entry points."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from radar_core.domain import twitch_admission as core
from radar_backend.domain import twitch_official_queue as rules
from radar_backend.domain.adult_exclusions import is_disallowed


NOW = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
TAIPEI = ZoneInfo("Asia/Taipei")


def proof():
    return {
        "schema_version": 1, "method": "twitch_igdb_external_steam_v1", "appid": 123,
        "twitch_game_id": "22", "igdb_id": "33", "checked_at": "2026-10-02T08:00:00Z",
        "source_frontend_commit": "a" * 40,
        "source_enrollment": {"source": "igdb_first_release_date",
            "observed_at": "2026-10-01T08:00:00Z", "viewer_count": 8000, "min_viewers": 7000},
    }


def queued():
    day = "2026-10-02"
    metadata = {
        "appid": 123, "name": "Original", "store_url": "https://store.steampowered.com/app/123/",
        "release_start": day, "release_end": day, "release_store_date": day,
        "release_precision": "day", "release_display_precision": "date_full",
        "release_date_timezone": "Asia/Taipei", "release_time_utc": "2026-10-02T07:00:00Z",
        "release_date_normalization": "steam_store_date_matches_taipei",
        "release_date_verified_at": "2026-10-02T08:00:00Z",
        "release_timestamp_taipei_date": day, "release_date_conflict": False,
        "sexual_content_screened": True, "steam_type": "game",
        "content_descriptorids": [], "basic_info": {"short_description": "Adventure"},
        "tags": [], "twitch_admission": proof(),
    }
    return {"appid": 123, "name": "Original", "queue_source": rules.TWITCH_QUEUE_SOURCE,
            "release_date": day, "steam_url": metadata["store_url"],
            "twitch_admission": proof(), "steam_candidate": metadata}


def candidate(row, now=NOW, **overrides):
    ports = {
        "decimal_id": core.decimal_id, "normalize_twitch_admission": core.normalize_twitch_admission,
        "exact_day": rules._exact_day,
        "valid_descriptors": lambda meta: rules._valid_descriptors(meta, is_disallowed=is_disallowed),
        "disallowed_current": lambda meta: is_disallowed(meta, set()),
        "is_explicit_sex_game": lambda meta: False,
        "aware_time": core.aware_time, "checked_now": rules._now,
        "resolve_store_release_day": core.resolve_store_release_day,
        "has_taiwan_store_date_authority": core.has_taiwan_store_date_authority,
        "is_twitch_qualified": core.is_twitch_qualified, "taipei": TAIPEI,
    }
    ports.update(overrides)
    return rules.is_twitch_queue_candidate(row, now, **ports)


def restore(pending, aid, row, **overrides):
    return rules._restore_normal(pending, aid, row, decimal_id=core.decimal_id, **overrides)


def sync(checkpoint, batch, now=NOW, **overrides):
    ports = {"checked_now": rules._now, "decimal_id": core.decimal_id,
             "is_twitch_queue_candidate": candidate, "restore_normal": restore,
             "normalize_twitch_admission": core.normalize_twitch_admission}
    ports.update(overrides)
    return rules.sync_twitch_queue(checkpoint, batch, now, **ports)


def batch(rows=None, *, active=None, updates=None):
    return {"follower_candidates": [queued()] if rows is None else rows,
            "active_twitch_appids": [123] if active is None else active,
            "state_updates": {} if updates is None else updates}


def checkpoint():
    return {"pending_candidates": {}, "official_results": {"456": {"official_followers": 0}},
            "attempt_events": [{"appid": 456, "status": 200}], "cursor": 7,
            "next_request_after_taipei": "2026-10-03T12:00:00+08:00",
            "steam_followers_cache": {"games": {"456": {"followers": 0}}}}


@pytest.mark.parametrize("value", [None, True, 0, "2026-10-02", datetime(2026, 10, 2)])
def test_clock_validation_rejects_nonaware_values(value):
    with pytest.raises(ValueError, match="aware time"):
        rules._now(value)


def test_clock_requires_nonempty_utc_offset_and_returns_supplied_identity():
    class NoOffset(tzinfo):
        def utcoffset(self, dt):
            return None
    with pytest.raises(ValueError):
        rules._now(NOW.replace(tzinfo=NoOffset()))
    assert rules._now(NOW) is NOW


@pytest.mark.parametrize("value", [None, 20261002, True, date(2026, 10, 2), "20261002",
    "2026-10", "2026-2-02", "2026-10-2", "2026-02-29", "2026-W40-5", "2026-10-02 ", ""])
def test_exact_day_rejects_noncanonical_or_nondate_input(value):
    assert rules._exact_day(value) is None


@pytest.mark.parametrize("value", ["2026-10-02", "2024-02-29", "0001-01-01", "9999-12-31"])
def test_exact_day_accepts_canonical_dates(value):
    assert rules._exact_day(value).isoformat() == value


def test_date_type_remains_an_explicit_runtime_port_and_nonvalue_errors_propagate():
    parsed = SimpleNamespace(isoformat=lambda: "2026-10-02")
    fromiso = Mock(return_value=parsed)
    assert rules._exact_day("2026-10-02", date_type=SimpleNamespace(fromisoformat=fromiso)) is parsed
    fromiso.assert_called_once_with("2026-10-02")
    with pytest.raises(TypeError, match="parser failure"):
        rules._exact_day("2026-10-02", date_type=SimpleNamespace(
            fromisoformat=Mock(side_effect=TypeError("parser failure"))))


@pytest.mark.parametrize("primary", [None, "", {}, (), [True], [False], ["3"], [3.0], [-1], [None]])
def test_descriptor_primary_shape_rejects_before_alternate_adult_port(primary):
    adult = Mock(side_effect=AssertionError("must not inspect alternate"))
    assert not rules._valid_descriptors({"appid": 123, "content_descriptorids": primary,
                                       "content_descriptors": []}, is_disallowed=adult)
    adult.assert_not_called()


@pytest.mark.parametrize("alternate", ["", (), {}, {"ids": None}, [True], ["3"], [3.0], [-1]])
def test_descriptor_alternate_shape_rejects_without_adult_port(alternate):
    adult = Mock(side_effect=AssertionError("malformed alternate"))
    assert not rules._valid_descriptors({"appid": 123, "content_descriptorids": [],
                                       "content_descriptors": alternate}, is_disallowed=adult)
    adult.assert_not_called()


@pytest.mark.parametrize("alternate", [[], [0, 1, 2, 5], {"ids": []}, {"ids": [1, 5]}])
def test_alternate_descriptors_use_empty_ledger_and_original_list(alternate):
    adult = Mock(return_value=False)
    metadata = {"appid": "123", "content_descriptorids": [3], "content_descriptors": alternate}
    assert rules._valid_descriptors(metadata, is_disallowed=adult)
    actual = alternate["ids"] if isinstance(alternate, dict) else alternate
    args = adult.call_args.args
    assert args == ({"appid": "123", "content_descriptorids": actual}, set())
    assert args[0]["content_descriptorids"] is actual


@pytest.mark.parametrize("alternate", [[3], [4], {"ids": [4]}])
def test_alternate_union_adult_rejection_uses_pure_descriptor_rules(alternate):
    assert not rules._valid_descriptors({"appid": 123, "content_descriptorids": [],
                                       "content_descriptors": alternate}, is_disallowed=is_disallowed)


def test_no_alternate_does_not_call_adult_port_but_missing_appid_with_alternate_raises():
    adult = Mock(side_effect=AssertionError)
    assert rules._valid_descriptors({"content_descriptorids": []}, is_disallowed=adult)
    with pytest.raises(KeyError, match="appid"):
        rules._valid_descriptors({"content_descriptorids": [], "content_descriptors": []},
                                 is_disallowed=adult)


@pytest.mark.parametrize("mutation", ["not_dict", "wrong_source", "bool_appid", "wrong_metadata_id",
    "bad_proof", "wrong_day", "mismatched_release", "wrong_url", "wrong_store_url",
    "bad_descriptors", "alternate_adult", "inner_proof_mismatch", "store_day_conflict"])
def test_identity_date_descriptors_and_inner_proof_fail_before_current_ledger(mutation):
    row = queued()
    if mutation == "not_dict": row = []
    elif mutation == "wrong_source": row["queue_source"] = "normal"
    elif mutation == "bool_appid": row["appid"] = True
    elif mutation == "wrong_metadata_id": row["steam_candidate"]["appid"] = 999
    elif mutation == "bad_proof": row["twitch_admission"]["method"] = "search"
    elif mutation == "wrong_day": row["release_date"] = "20261002"
    elif mutation == "mismatched_release": row["steam_candidate"]["release_start"] = "2026-10-03"
    elif mutation == "wrong_url": row["steam_url"] = "https://example.test/"
    elif mutation == "wrong_store_url": row["steam_candidate"]["store_url"] = "https://example.test/"
    elif mutation == "bad_descriptors": row["steam_candidate"]["content_descriptorids"] = [True]
    elif mutation == "alternate_adult": row["steam_candidate"]["content_descriptors"] = [3]
    elif mutation == "inner_proof_mismatch": row["steam_candidate"]["twitch_admission"]["igdb_id"] = "44"
    elif mutation == "store_day_conflict": row["steam_candidate"]["release_store_date"] = "2026-10-01"
    ledger = Mock(side_effect=AssertionError("ledger must not load"))
    assert not candidate(row, disallowed_current=ledger)
    ledger.assert_not_called()


@pytest.mark.parametrize("field,value", [("basic_info", "bad"), ("basic_info", []),
                                         ("tags", {}), ("tags", "bad")])
def test_current_ledger_is_checked_before_basic_info_and_tag_shapes(field, value):
    row = queued(); row["steam_candidate"][field] = value
    ledger, screen = Mock(return_value=False), Mock(side_effect=AssertionError)
    assert not candidate(row, disallowed_current=ledger, is_explicit_sex_game=screen)
    ledger.assert_called_once_with(row["steam_candidate"])
    screen.assert_not_called()


def test_current_ledger_rejection_stops_before_screening_and_errors_escape():
    row = queued(); screen = Mock(side_effect=AssertionError)
    assert not candidate(row, disallowed_current=Mock(return_value=True), is_explicit_sex_game=screen)
    with pytest.raises(OSError, match="ledger missing"):
        candidate(row, disallowed_current=Mock(side_effect=OSError("ledger missing")))


@pytest.mark.parametrize("error", [TypeError("shape"), ValueError("value")])
def test_explicit_screen_type_and_value_failures_are_rejections(error):
    assert not candidate(queued(), is_explicit_sex_game=Mock(side_effect=error))


def test_explicit_screen_other_errors_propagate():
    with pytest.raises(RuntimeError, match="screen"):
        candidate(queued(), is_explicit_sex_game=Mock(side_effect=RuntimeError("screen")))


@pytest.mark.parametrize("changes", [
    {"steam_type": "dlc"}, {"sexual_content_screened": False}, {"release_precision": "month"},
    {"release_display_precision": "date_month"}, {"release_date_timezone": "UTC"},
    {"release_end": "2026-10-03"}, {"release_time_utc": "2026-10-03T01:00:00Z"},
    {"release_date_verified_at": None}, {"release_date_verified_at": "2026-10-03T00:00:00Z"},
])
def test_real_pinned_core_and_verified_time_still_reject_ineligible_metadata(changes):
    row = queued(); row["steam_candidate"].update(changes)
    assert not candidate(row)


def test_qualification_uses_only_a_temporary_zero_count_and_preserves_nested_references():
    row = queued(); row["steam_candidate"]["followers"] = 9999
    original = deepcopy(row)
    qualified = Mock(return_value=True)
    assert candidate(row, is_twitch_qualified=qualified)
    temporary = qualified.call_args.args[0]
    assert temporary["followers"] == 0
    assert temporary["follower_checked_at"] == proof()["checked_at"]
    assert temporary is not row["steam_candidate"]
    assert temporary["tags"] is row["steam_candidate"]["tags"]
    assert row == original


@pytest.mark.parametrize("offset,expected", [(-31, False), (-30, True), (365, True), (366, False)])
def test_taipei_date_window_is_inclusive_and_not_utc_today(offset, expected):
    now = datetime(2026, 10, 1, 17, tzinfo=timezone.utc)  # Taipei is already October 2.
    row = queued()
    day = now.astimezone(TAIPEI).date() + timedelta(days=offset)
    day_s = day.isoformat()
    meta = row["steam_candidate"]
    meta.update(release_start=day_s, release_end=day_s, release_store_date=day_s,
                release_timestamp_taipei_date=day_s, release_time_utc=f"{day_s}T01:00:00Z",
                release_date_verified_at="2026-10-01T16:00:00Z")
    row["release_date"] = day_s
    row["twitch_admission"]["checked_at"] = "2026-10-01T16:00:00Z"
    meta["twitch_admission"] = deepcopy(row["twitch_admission"])
    assert candidate(row, now) is expected


def test_omitted_now_skips_current_window_clock_port_but_requires_verified_time():
    row = queued(); row["twitch_admission"]["checked_at"] = "2027-10-02T08:00:00Z"
    row["steam_candidate"]["twitch_admission"] = deepcopy(row["twitch_admission"])
    row["steam_candidate"]["release_date_verified_at"] = "2027-10-02T08:00:00Z"
    clock = Mock(side_effect=AssertionError("clock must not validate"))
    assert candidate(row, None, checked_now=clock)
    clock.assert_not_called()


def test_taiwan_authority_skips_normalization_but_real_core_checks_the_audit():
    row = queued(); meta = row["steam_candidate"]
    meta.update(release_time_utc="2026-10-03T07:00:00Z", release_date_conflict=True,
        release_timestamp_taipei_date="2026-10-03",
        release_date_normalization=core.TW_STORE_DATE_AUTHORITY,
        release_display_provider=core.TW_STORE_DATE_PROVIDER)
    normalize = Mock(side_effect=AssertionError("official Taiwan audit wins"))
    assert candidate(row, resolve_store_release_day=normalize)
    normalize.assert_not_called()
    meta["release_display_provider"] = "unverified"
    assert not candidate(row)


@pytest.mark.parametrize("normal", [None, [], {"appid": 999}, {"appid": True},
                                   {"appid": 123, "queue_source": rules.TWITCH_QUEUE_SOURCE}])
def test_restoration_removes_rows_with_no_valid_normal_shadow(normal):
    row = {"normal_candidate": normal}
    pending = {"123": row, "999": {"appid": 999}}
    restore(pending, "123", row)
    assert pending == {"999": {"appid": 999}}


@pytest.mark.parametrize("group", [None, True, 0, "001", "bad", 103582791429999999,
                                  "103582791429999999"])
def test_restoration_overlays_only_valid_group_ids_and_copies_resolution(group):
    normal = {"appid": 123, "queue_source": "normal", "group_id64": "5", "extra": {"nested": []}}
    resolution = {"status": "resolved", "nested": [1]}
    row = {"normal_candidate": normal, "group_id64": group, "group_resolution": resolution}
    pending = {"123": row}
    restore(pending, "123", row)
    expected = group if core.decimal_id(group) is not None else "5"
    assert pending["123"]["group_id64"] == expected
    assert pending["123"]["group_resolution"] == resolution
    assert pending["123"]["group_resolution"] is not resolution
    assert pending["123"]["extra"] is not normal["extra"]
    assert normal["group_id64"] == "5"


def test_duplicate_overlay_last_candidate_wins_and_official_counts_never_leak():
    cp = checkpoint()
    normal = {"appid": 123, "queue_source": "daily", "group_id64": "42", "nested": {"key": [1]}}
    cp["pending_candidates"]["123"] = normal
    first, last = queued(), queued()
    last["name"] = "Last duplicate"
    for field in rules.FOLLOWER_FIELDS:
        last[field] = 17
        last["steam_candidate"][field] = 17
    last["normal_candidate"] = {"appid": 999, "queue_source": "fake"}
    incoming = batch([first, last]); old_cp, old_batch = deepcopy(cp), deepcopy(incoming)
    calls = Mock(side_effect=candidate)
    out = sync(cp, incoming, is_twitch_queue_candidate=calls)
    assert calls.call_count == 2
    row = out["pending_candidates"]["123"]
    assert row["name"] == "Last duplicate"
    assert row["appid"] == 123 and row["priority"] == 100
    assert row["normal_candidate"] == normal
    assert row["normal_candidate"] is not normal
    assert row["group_id64"] == "42"
    assert not rules.FOLLOWER_FIELDS.intersection(row)
    assert not rules.FOLLOWER_FIELDS.intersection(row["steam_candidate"])
    assert cp == old_cp and incoming == old_batch
    assert out["official_results"] == cp["official_results"]
    assert out["official_results"] is not cp["official_results"]


@pytest.mark.parametrize("reason", sorted(rules.WITHDRAW_REASONS))
def test_each_terminal_withdraw_reason_restores_normal_and_preserves_verified_progress(reason):
    cp = checkpoint(); normal = {"appid": 123, "queue_source": "daily", "release_date": "2026-10-20"}
    cp["pending_candidates"]["123"] = normal
    priority = sync(cp, batch())
    priority["pending_candidates"]["123"].update(
        group_id64="777", group_resolution={"status": "resolved", "nested": [1]})
    old = deepcopy(priority)
    out = sync(priority, batch(updates={123: {"status": "pending", "reason": reason}}))
    assert out["pending_candidates"]["123"] == {**normal, "group_id64": "777",
        "group_resolution": {"status": "resolved", "nested": [1]}}
    assert {k: v for k, v in out.items() if k != "pending_candidates"} == {
        k: v for k, v in priority.items() if k != "pending_candidates"}
    assert priority == old


@pytest.mark.parametrize("status", ["accepted", "excluded"])
def test_terminal_status_withdraws_without_a_reason(status):
    cp = sync(checkpoint(), batch())
    out = sync(cp, batch(updates={"123": {"status": status}}))
    assert out["pending_candidates"] == {}


@pytest.mark.parametrize("reason", ["steam_type_unavailable", "official_followers_unavailable",
                                   "HTTP 429", "anything_else", None])
def test_transient_reason_and_active_none_leave_valid_priority_row(reason):
    cp = sync(checkpoint(), batch())
    incoming = batch(updates={"123": {"status": "pending", "reason": reason}})
    incoming["active_twitch_appids"] = None
    assert sync(cp, incoming) == cp


@pytest.mark.parametrize("value", [None, [], 1, "bad"])
def test_invalid_checkpoint_is_rejected_without_deepcopy(value):
    copying = Mock(side_effect=AssertionError("copy must not occur"))
    with pytest.raises(ValueError, match="objects"):
        sync(value, {}, deepcopy_fn=copying)
    copying.assert_not_called()


@pytest.mark.parametrize("cp,batch_value,message", [
    ({"pending_candidates": []}, {}, "Pending candidates"),
    ({}, {"active_twitch_appids": {}}, "Active Twitch"),
    ({}, {"active_twitch_appids": [True]}, "Active Twitch"),
    ({}, {"active_twitch_appids": ["0123"]}, "Active Twitch"),
    ({}, {"state_updates": [1]}, "state updates"),
    ({}, {"follower_candidates": {"123": {}}}, "Follower candidates"),
])
def test_invalid_batch_guard_order_and_inputs_are_unchanged(cp, batch_value, message):
    cp_before, batch_before = deepcopy(cp), deepcopy(batch_value)
    qualified = Mock(side_effect=AssertionError("candidates must not be considered"))
    with pytest.raises(ValueError, match=message):
        sync(cp, batch_value, is_twitch_queue_candidate=qualified)
    assert cp == cp_before and batch_value == batch_before
    qualified.assert_not_called()


@pytest.mark.parametrize("updates,incoming", [(None, None), (False, False), (0, 0), ([], []), ("", "")])
def test_original_falsey_update_and_candidate_fallbacks_remain_accepted(updates, incoming):
    out = sync({}, {"state_updates": updates, "follower_candidates": incoming})
    assert out == {"pending_candidates": {}}


def test_clock_validation_precedes_malformed_checkpoint_and_ignores_clock_return_in_sync():
    with pytest.raises(ValueError, match="aware time"):
        sync([], [], NOW.replace(tzinfo=None))
    observed = Mock(return_value=None)
    called = Mock(side_effect=candidate)
    sync({}, batch(), checked_now=observed, is_twitch_queue_candidate=called)
    observed.assert_called_once_with(NOW)
    called.assert_called_once_with(batch()["follower_candidates"][0], NOW)


def test_pending_short_circuit_avoids_revalidation_for_absent_or_withdrawn_rows():
    cp = sync(checkpoint(), batch())
    called = Mock(side_effect=AssertionError("missing rows short circuit"))
    out = sync(cp, {"follower_candidates": []}, is_twitch_queue_candidate=called)
    assert out["pending_candidates"] == {}
    called.assert_not_called()


def test_existing_candidate_revalidates_after_incoming_and_restores_before_readding():
    cp = checkpoint(); normal = {"appid": 123, "queue_source": "daily", "nested": [1]}
    cp["pending_candidates"]["123"] = normal
    cp = sync(cp, batch())
    current = cp["pending_candidates"]["123"]
    current["steam_candidate"]["release_date_conflict"] = True
    incoming = queued(); calls = []
    def qualify(row, now):
        calls.append(row)
        return candidate(row, now)
    out = sync(cp, batch([incoming]), is_twitch_queue_candidate=qualify)
    assert calls[0] is incoming
    assert calls[1] == current and calls[1] is not current
    assert out["pending_candidates"]["123"]["normal_candidate"] == normal
    assert out["pending_candidates"]["123"]["steam_candidate"]["release_date_conflict"] is False


def test_candidate_port_error_propagates_after_copy_without_mutating_inputs():
    cp, incoming = checkpoint(), batch()
    old_cp, old_batch = deepcopy(cp), deepcopy(incoming)
    with pytest.raises(RuntimeError, match="candidate failure"):
        sync(cp, incoming, is_twitch_queue_candidate=Mock(side_effect=RuntimeError("candidate failure")))
    assert cp == old_cp and incoming == old_batch


def test_noneligible_bad_rows_are_skipped_without_indexing_their_missing_appid():
    incoming = [None, 1, "bad", {}, {"queue_source": "normal"}]
    called = Mock(return_value=False)
    assert sync({}, batch(incoming), is_twitch_queue_candidate=called) == {"pending_candidates": {}}
    assert called.call_count == len(incoming)


def test_custom_normalizer_priority_and_follower_ports_are_honored_without_new_validation():
    row = queued(); row["custom_count"] = 9; row["steam_candidate"]["custom_count"] = 9
    normalizer = Mock(return_value=None)
    out = sync({}, batch([row]), normalize_twitch_admission=normalizer,
               queue_priority=77, follower_fields={"custom_count"})
    actual = out["pending_candidates"]["123"]
    assert actual["priority"] == 77 and actual["twitch_admission"] is None
    assert "custom_count" not in actual and "custom_count" not in actual["steam_candidate"]
    normalizer.assert_called_once_with(row["twitch_admission"], "123")


def test_nonstring_existing_appid_key_keeps_legacy_string_restoration_behavior():
    cp = {"pending_candidates": {123: queued()}}
    out = sync(cp, {"follower_candidates": []})
    assert out == cp  # The old rule pops str(aid), so integer-indexed rows remain.
    assert out is not cp
