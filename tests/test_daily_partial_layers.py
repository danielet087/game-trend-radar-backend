"""Offline contracts for explicit daily slot and partial-catalog ports."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_core.domain import twitch_admission as core
from radar_backend.application import partial_catalog as application
from radar_backend.domain import daily_schedule as schedule
from radar_backend.domain import partial_catalog as partial
from radar_backend.domain import catalog_metadata
from radar_backend.domain.adult_exclusions import is_disallowed


DAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)


def row(appid=1, followers=6000, release_start="2026-10-09", **extras):
    return {"appid": appid, "followers": followers, "release_start": release_start, **extras}


def player_categories(existing, incoming):
    return catalog_metadata.preserve_player_categories(
        existing, incoming,
        snapshot=lambda value: catalog_metadata.player_category_snapshot(value, now=lambda: NOW),
    )


def merge(existing, incoming, **overrides):
    ports = {
        "today": DAY,
        "blocked": set(),
        "prune_released": partial.prune_released,
        "is_disallowed": is_disallowed,
        "preserve_twitch_admission": core.preserve_twitch_admission,
        "preserve_player_categories": player_categories,
    }
    ports.update(overrides)
    return partial.merge_partial_segment(existing, incoming, **ports)


def decision(workflow, slot, *, now=NOW, refresh=True, **overrides):
    return schedule.schedule_decision(
        workflow, "workflow_dispatch", "cloudflare", slot, refresh, now, **overrides,
    )


def utc_slot(hour, minute=0, *, day=DAY):
    return datetime.combine(day, datetime.min.time(), schedule.TAIPEI).replace(
        hour=hour, minute=minute,
    ).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("day,expected", [
    (date(2026, 10, 9), "2026-10-08T16:00:00Z"),
    (date(2024, 2, 29), "2024-02-28T16:00:00Z"),
    (date(2027, 1, 1), "2026-12-31T16:00:00Z"),
    (date(2000, 1, 1), "1999-12-31T16:00:00Z"),
])
def test_daily_slot_owns_the_entire_taiwan_day(day, expected):
    assert schedule.daily_slot(day) == expected


def test_daily_slot_forwards_clock_types_time_zone_and_midnight_identity():
    midnight, taipei, utc = object(), object(), object()
    combined = Mock()
    converted = Mock()
    combined.astimezone.return_value = converted
    converted.isoformat.return_value = "2026-10-08T16:00:00+00:00"
    combine = Mock(return_value=combined)
    time_type = Mock(return_value=midnight)
    result = schedule.daily_slot(
        DAY, datetime_type=SimpleNamespace(combine=combine), time_type=time_type,
        taipei=taipei, timezone_type=SimpleNamespace(utc=utc),
    )
    assert result == "2026-10-08T16:00:00Z"
    combine.assert_called_once_with(DAY, midnight, taipei)
    combined.astimezone.assert_called_once_with(utc)
    time_type.assert_called_once_with()


def state(**updates):
    return {
        "anchor_date": DAY.isoformat(), "mode": "two_phase_steam_year",
        "daily_refresh_slot": schedule.daily_slot(DAY),
        "last_reset_date_taipei": DAY.isoformat(),
        "phase": "prefilter", "prefilter_next_index": 129,
        **updates,
    }


@pytest.mark.parametrize("updates,expected", [
    ({}, False),
    ({"anchor_date": "2026-10-08"}, True),
    ({"anchor_date": None}, True),
    ({"mode": "other"}, True),
    ({"daily_refresh_slot": "2026-10-09T00:00:00Z"}, True),
    ({"last_reset_date_taipei": "2026-10-08"}, True),
    ({"daily_refresh_slot": None, "last_reset_date_taipei": None}, False),
    ({"daily_refresh_slot": "", "last_reset_date_taipei": ""}, False),
    ({"daily_refresh_slot": False, "last_reset_date_taipei": 0}, False),
    ({"daily_refresh_slot": None}, True),
    ({"last_reset_date_taipei": None}, True),
])
def test_daily_reset_retains_legacy_progress_only_under_the_same_anchor(updates, expected):
    value = state(**updates)
    before = deepcopy(value)
    assert schedule.daily_reset_required(value, DAY) is expected
    assert value == before


def test_forced_reset_and_missing_markers_do_not_evaluate_the_slot_port():
    slot = Mock(side_effect=AssertionError("must not evaluate slot"))
    assert schedule.daily_reset_required(object(), object(), force=True, daily_slot_fn=slot)
    assert schedule.daily_reset_required(state(mode="old"), DAY, daily_slot_fn=slot)
    assert not schedule.daily_reset_required(
        state(daily_refresh_slot=None, last_reset_date_taipei=None), DAY, daily_slot_fn=slot,
    )
    slot.assert_not_called()


def test_daily_reset_uses_supplied_falsy_callback_without_rebinding_it():
    class Slot:
        def __bool__(self):
            return False

        def __call__(self, day):
            assert day is DAY
            return "patched slot"

    assert not schedule.daily_reset_required(
        state(daily_refresh_slot="patched slot"), DAY, daily_slot_fn=Slot(),
    )


def test_daily_reset_does_not_wrap_callback_failures():
    slot = Mock(side_effect=OSError("slot unavailable"))
    with pytest.raises(OSError, match="slot unavailable"):
        schedule.daily_reset_required(state(), DAY, daily_slot_fn=slot)
    slot.assert_called_once_with(DAY)


@pytest.mark.parametrize("workflow,hour", [
    (workflow, hour)
    for workflow in ("daily-discovery", "public-growth", "official-followers")
    for hour in range(24)
])
def test_external_hours_preserve_each_distinct_workflow_window(workflow, hour):
    minute = 15 if workflow == "public-growth" else 0
    valid = hour in ({0, 6, 12, 18} if workflow == "daily-discovery" else
                     {1, 7, 13, 19} if workflow == "public-growth" else range(3, 24))
    slot = utc_slot(hour, minute)
    now = datetime.fromisoformat(slot.replace("Z", "+00:00")) + timedelta(minutes=20)
    if valid:
        assert decision(workflow, slot, now=now) == (True, "External scheduled slot is current")
    else:
        with pytest.raises(ValueError):
            decision(workflow, slot, now=now)


@pytest.mark.parametrize("hour", range(24))
def test_github_followers_window_has_no_external_slot_or_refresh_requirement(hour):
    now = datetime(2026, 10, 9, hour, tzinfo=schedule.TAIPEI)
    assert schedule.schedule_decision(
        "official-followers", "schedule", "", "not an ISO timestamp", False, now,
    ) == (3 <= hour <= 23, "GitHub schedule Taiwan execution window")


@pytest.mark.parametrize("workflow", ["daily-discovery", "public-growth", "official-followers"])
@pytest.mark.parametrize("source", ["", "manual"])
@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule", "push"])
def test_manual_and_existing_github_triggers_keep_their_original_bypass(workflow, source, event):
    now = datetime(2026, 10, 9, 0, tzinfo=schedule.TAIPEI)
    result = schedule.schedule_decision(workflow, event, source, "ignored", False, now)
    expected = ((False, "GitHub schedule Taiwan execution window")
                if workflow == "official-followers" and event != "workflow_dispatch"
                else (True, "Manual or existing GitHub trigger"))
    assert result == expected


@pytest.mark.parametrize("workflow,slot", [
    ("daily-discovery", "2026-10-08T16:00:00Z"),
    ("public-growth", "2026-10-08T17:15:00Z"),
    ("official-followers", "2026-10-08T19:00:00Z"),
])
def test_future_slots_skip_before_the_cross_day_check(workflow, slot):
    now = datetime(2026, 10, 8, 15, 59, tzinfo=timezone.utc)
    assert decision(workflow, slot, now=now) == (False, "External scheduled slot is in the future")


@pytest.mark.parametrize("workflow,slot", [
    ("daily-discovery", "2026-10-08T16:00:00Z"),
    ("public-growth", "2026-10-08T17:15:00Z"),
    ("official-followers", "2026-10-08T19:00:00Z"),
])
def test_old_slots_skip_on_taiwan_midnight_before_followers_hour_check(workflow, slot):
    now = datetime(2026, 10, 9, 16, tzinfo=timezone.utc)
    assert decision(workflow, slot, now=now) == (False, "External scheduled slot belongs to another Taiwan day")


def test_only_followers_slots_expire_at_the_next_execution_hour():
    now = datetime(2026, 10, 9, 4, tzinfo=schedule.TAIPEI)
    assert decision("official-followers", utc_slot(3), now=now) == (
        False, "External Followers slot crossed its Taiwan execution hour",
    )
    assert decision("daily-discovery", utc_slot(0), now=now)[0]
    assert decision("public-growth", utc_slot(1, 15), now=now)[0]


@pytest.mark.parametrize("workflow,event,source,slot,refresh,message", [
    ("unknown", "schedule", "unknown", "", False, "Unknown scheduled workflow"),
    ("official-followers", "schedule", "unknown", "", False, "Unknown trigger_source"),
    ("daily-discovery", "schedule", "cloudflare", "", False, "Cloudflare must use workflow_dispatch"),
    ("daily-discovery", "workflow_dispatch", "cloudflare", "", False, "Cloudflare target_slot is required"),
    ("daily-discovery", "workflow_dispatch", "cloudflare", "bad", False, "target_slot must be an ISO UTC timestamp"),
    ("daily-discovery", "workflow_dispatch", "cloudflare", "2026-10-09T00:00:00", False, "target_slot must include the UTC timezone"),
    ("daily-discovery", "workflow_dispatch", "cloudflare", "2026-10-09T00:00:00+08:00", False, "target_slot must include the UTC timezone"),
    ("daily-discovery", "workflow_dispatch", "cloudflare", "2026-10-08T16:00:01Z", False, "target_slot must use a whole scheduled minute"),
    ("daily-discovery", "workflow_dispatch", "cloudflare", "2026-10-08T16:00:00.000001Z", False, "target_slot must use a whole scheduled minute"),
    ("daily-discovery", "workflow_dispatch", "cloudflare", "2026-10-08T16:00:00Z", False, "Cloudflare daily discovery requires refresh_today=true"),
])
def test_validation_order_and_error_messages_remain_public_contracts(workflow, event, source, slot, refresh, message):
    with pytest.raises(ValueError) as error:
        schedule.schedule_decision(workflow, event, source, slot, refresh, NOW)
    assert str(error.value) == message


def test_parse_value_error_keeps_original_cause_while_other_errors_escape():
    cause = ValueError("parser cause")
    parse = Mock(side_effect=cause)
    with pytest.raises(ValueError) as error:
        decision("daily-discovery", utc_slot(0), datetime_type=SimpleNamespace(fromisoformat=parse))
    assert error.value.__cause__ is cause
    parse.assert_called_once_with(utc_slot(0).replace("Z", "+00:00"))
    parse.side_effect = TypeError("parser type")
    with pytest.raises(TypeError, match="parser type"):
        decision("daily-discovery", utc_slot(0), datetime_type=SimpleNamespace(fromisoformat=parse))


def test_slot_parser_timezone_and_hour_sets_are_explicit_ports():
    taipei = timezone(timedelta(hours=7))
    now = datetime(2026, 10, 9, 4, 20, tzinfo=taipei)
    slot = "2026-10-08T21:00:00Z"
    assert decision("daily-discovery", slot, now=now, taipei=taipei, daily_discovery_hours={4})[0]
    assert decision("public-growth", "2026-10-08T21:15:00Z", now=now,
                    taipei=taipei, public_growth_hours={4})[0]
    assert schedule.schedule_decision("added", "push", "manual", "", False, now,
                                      workflows={"added"}, taipei=taipei)[0]


def test_naive_now_keeps_existing_astimezone_behavior_and_aware_comparison_error():
    naive = datetime(2026, 10, 9, 0)
    expected = 3 <= naive.astimezone(schedule.TAIPEI).hour <= 23
    assert schedule.schedule_decision("official-followers", "schedule", "", "", False, naive)[0] is expected
    with pytest.raises(TypeError):
        decision("daily-discovery", utc_slot(0), now=naive)


def test_slot_with_tzinfo_but_no_offset_preserves_attribute_error():
    class EmptyOffset(tzinfo):
        def utcoffset(self, value):
            return None

    parsed = datetime(2026, 10, 9, tzinfo=EmptyOffset())
    with pytest.raises(AttributeError):
        decision("daily-discovery", "valid parser input",
                 datetime_type=SimpleNamespace(fromisoformat=lambda value: parsed))


@pytest.mark.parametrize("value", [None, 123, [], {}])
def test_target_slot_nonstring_errors_are_not_normalized(value):
    if not value:
        with pytest.raises(ValueError, match="Cloudflare target_slot is required"):
            decision("daily-discovery", value)
    else:
        with pytest.raises(AttributeError):
            decision("daily-discovery", value)


@pytest.mark.parametrize("release", ["1990-01-01", "2026-10-08", "2026-10-09", "2027-10-09", "bad", None])
def test_legacy_prune_keeps_every_dict_without_date_gates_or_copying(release):
    game = row(release_start=release)
    result = partial.prune_released([None, game, [], "bad", 0], object())
    assert result == [game]
    assert result[0] is game


def test_prune_preserves_dict_subclasses_and_input_list():
    class Game(dict):
        pass

    game = Game(appid=1)
    values = [game, {}]
    result = partial.prune_released(values, DAY)
    assert result is not values
    assert result[0] is game
    assert result[1] is values[1]
    assert values == [game, {}]


def test_partial_merge_keeps_history_and_unchecked_future_titles():
    history = row(1, 7000, "2000-01-01")
    unchecked = row(2, 8000, "2027-01-01")
    incoming = row(3, 9000, "2026-11-01")
    result = merge([history, unchecked], [incoming])
    assert [game["appid"] for game in result] == [3, 2, 1]
    assert result[-1]["release_start"] == "2000-01-01"


@pytest.mark.parametrize("key,descriptor", [
    ("content_descriptorids", [3]), ("content_descriptorids", [4]),
    ("content_descriptors", [3]), ("content_descriptors", {"ids": [4]}),
])
def test_adult_predicate_runs_for_existing_and_incoming_before_merge(key, descriptor):
    adult_old = row(1, **{key: descriptor})
    adult_new = row(2, **{key: descriptor})
    assert merge([adult_old, row(3)], [adult_new, row(4)], blocked={3}) == [row(4)]


def test_partial_merge_does_not_impose_an_additional_followers_or_release_gate():
    result = merge([], [row(1, 0, None), row(2, 2999, "bad"), row(3, 1, "1990-01-01")])
    assert [game["appid"] for game in result] == [2, 3, 1]


@pytest.mark.parametrize("appid", [None, "bad", [], {}, object()])
def test_only_key_value_and_type_appid_errors_are_skipped(appid):
    assert merge([], [{"appid": appid}], is_disallowed=lambda game, blocked: False) == []


def test_missing_appid_is_skipped_after_the_adult_predicate():
    adult = Mock(return_value=False)
    game = {"followers": 5}
    assert merge([], [game], is_disallowed=adult) == []
    adult.assert_called_once_with(game, set())


def test_partial_merge_keeps_existing_bool_and_float_integer_coercion():
    assert [game["appid"] for game in merge([], [row(True), row(2.9)])] == [True, 2.9]


def test_overflow_and_adult_callback_errors_are_not_hidden():
    with pytest.raises(OverflowError):
        merge([], [row(float("inf"))], is_disallowed=lambda game, blocked: False)
    with pytest.raises(OSError, match="adult check unavailable"):
        merge([], [row()], is_disallowed=Mock(side_effect=OSError("adult check unavailable")))


def test_sort_uses_numeric_appid_after_followers_and_date_not_name():
    result = merge([], [row("10", name="A"), row("2", name="Z"), row(3, 7000),
                        row(5, 6000, "2026-01-01"), row(4, 6000, None)])
    assert [game["appid"] for game in result] == [3, 5, "2", "10", 4]


@pytest.mark.parametrize("followers", ["bad", [], {}, object()])
def test_invalid_sort_followers_keep_original_conversion_failures(followers):
    if not followers:
        assert merge([], [row(followers=followers)])[0]["followers"] is followers
    else:
        with pytest.raises((ValueError, TypeError)):
            merge([], [row(followers=followers)])


def test_partial_duplicate_rows_use_the_previous_result_and_exact_callback_order():
    old, first, final = row(1, 5000), row(1, 6000), row(1, 7000)
    trace = []
    produced = []

    def adult(game, blocked):
        trace.append(("adult", game, blocked))
        return False

    def twitch(prior, game):
        trace.append(("twitch", prior, game))
        return {**game, "nested": game}

    def categories(prior, game):
        trace.append(("categories", prior, game))
        result = {**game, "merged": len(produced)}
        produced.append(result)
        return result

    result = merge([old], [first, final], is_disallowed=adult,
                   preserve_twitch_admission=twitch, preserve_player_categories=categories)
    assert [step[0] for step in trace] == ["adult", "twitch", "categories"] * 3
    assert trace[1][1] == {}
    assert trace[4][1] is produced[0]
    assert trace[7][1] is produced[1]
    assert trace[2][1] is trace[1][1]
    assert trace[5][1] is trace[4][1]
    assert result[0] is produced[2]
    assert result[0]["nested"] is final


def test_real_core_proof_and_newer_official_categories_survive_followers_only_refresh():
    proof = {
        "schema_version": 1, "method": "twitch_igdb_external_steam_v1", "appid": 1,
        "twitch_game_id": "22", "igdb_id": "33", "checked_at": "2026-10-09T08:00:00Z",
        "source_frontend_commit": "a" * 40,
        "source_enrollment": {"source": "igdb_first_release_date", "observed_at": "2026-10-08T08:00:00Z",
                              "viewer_count": 8000, "min_viewers": 7000},
    }
    old = row(
        twitch_admission=proof, steam_type="game", sexual_content_screened=True,
        categories=[{"id": 1, "description": "Multi-player"}],
        categories_source="Steam IStoreBrowseService/GetItems supported_player_categoryids",
        categories_checked_at="2026-10-09T09:00:00Z",
    )
    new = row(followers=7000)
    before = deepcopy((old, new))
    result = merge([old], [new])[0]
    assert result["followers"] == 7000
    assert result["twitch_admission"] == proof
    assert result["steam_type"] == "game"
    assert result["sexual_content_screened"] is True
    assert result["categories"] == old["categories"]
    assert result["categories_checked_at"] == old["categories_checked_at"]
    assert (old, new) == before
    assert result is not old and result is not new
    assert result["categories"] is old["categories"]


def test_explicit_newer_empty_player_categories_replace_the_previous_modes():
    old = row(categories=[{"id": 1, "description": "Multiplayer"}],
              categories_source="Steam Store appdetails cc=TW categories",
              categories_checked_at="2026-10-09T08:00:00Z")
    new = row(followers=7000, categories=[],
              categories_source="Steam Store appdetails cc=TW categories",
              categories_checked_at="2026-10-09T09:00:00Z")
    result = merge([old], [new])[0]
    assert result["categories"] == []
    assert result["categories"] is new["categories"]


def test_application_loads_exclusions_before_pruning_then_forwards_same_objects():
    old, new, blocked = [row(1)], [row(2)], {3}
    trace = []

    def load():
        trace.append("ledger")
        return blocked

    def prune(games, today):
        assert games is old and today is DAY
        trace.append("prune")
        return games

    def adult(game, excluded):
        assert excluded is blocked
        trace.append("adult")
        return False

    def twitch(prior, game):
        trace.append("twitch")
        return game

    def categories(prior, game):
        trace.append("categories")
        return game

    result = application.merge_partial_segment(
        old, new, today=DAY, excluded_appids=load, prune_released=prune,
        is_disallowed=adult, preserve_twitch_admission=twitch,
        preserve_player_categories=categories,
    )
    assert trace == ["ledger", "prune", "adult", "twitch", "categories", "adult", "twitch", "categories"]
    assert result[0] is old[0]
    assert result[1] is new[0]


def test_application_ledger_failure_prevents_pruning_or_merging():
    forbidden = Mock(side_effect=AssertionError("must not evaluate"))
    with pytest.raises(OSError, match="ledger unavailable"):
        application.merge_partial_segment(
            [], [], today=DAY, excluded_appids=Mock(side_effect=OSError("ledger unavailable")),
            prune_released=forbidden, is_disallowed=forbidden,
            preserve_twitch_admission=forbidden, preserve_player_categories=forbidden,
        )
    forbidden.assert_not_called()


@pytest.mark.parametrize("failing_port", ["prune_released", "is_disallowed", "preserve_twitch_admission", "preserve_player_categories"])
def test_application_preserves_nonledger_port_failures(failing_port):
    ports = {
        "excluded_appids": lambda: set(), "prune_released": partial.prune_released,
        "is_disallowed": is_disallowed, "preserve_twitch_admission": core.preserve_twitch_admission,
        "preserve_player_categories": player_categories,
    }
    ports[failing_port] = Mock(side_effect=RuntimeError(failing_port))
    with pytest.raises(RuntimeError, match=failing_port):
        application.merge_partial_segment([], [row()], today=DAY, **ports)
