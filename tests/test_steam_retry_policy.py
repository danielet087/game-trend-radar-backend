from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from scripts.steam_retry_policy import (
    apply_legacy_retry_migrations,
    legacy_retry_migrations,
    rate_limit_policy,
    transient_retry_policy,
)


NOW = datetime(2026, 10, 2, 14, 32, 37, tzinfo=timezone.utc)
FAILURE = datetime(2026, 10, 2, 10, 14, 27, 239399, tzinfo=timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def legacy_state():
    deadline = stamp(FAILURE + timedelta(hours=6))
    return {
        "schema_version": 1,
        "games": {
            "892970": {
                "status": "pending", "reason": "RateLimited", "validation_version": 2,
                "updated_at": stamp(FAILURE), "retry_at": deadline,
                "twitch_admission": {"appid": 892970, "twitch_game_id": "23"},
            },
            "1867240": {
                "status": "pending", "reason": "steam_community_cooldown", "validation_version": 2,
                "rate_limit_stage": "steam_community", "retry_at": deadline,
                "updated_at": stamp(FAILURE + timedelta(minutes=11)),
            },
            "3219630": {
                "status": "pending", "reason": "steam_date_conflict", "validation_version": 2,
                "updated_at": stamp(FAILURE), "retry_at": stamp(FAILURE + timedelta(days=1)),
            },
        },
        "api_cooldowns": {
            "steam_community": {
                "retry_at": deadline, "updated_at": stamp(FAILURE + timedelta(minutes=11)),
            },
        },
    }


@pytest.mark.parametrize("stage,expected", [
    ("steam_community", [900, 1800, 3600, 3600, 3600]),
    ("steam_store_browse", [300, 600, 1200, 1800, 1800]),
    ("steam_appdetails", [300, 600, 1200, 1800, 1800]),
])
def test_default_rate_limits_back_off_with_stage_specific_caps(stage, expected):
    prior = None
    for attempt, seconds in enumerate(expected, 1):
        receipt = rate_limit_policy(stage, None, NOW, prior)
        assert receipt["attempts"] == attempt
        assert receipt["retry_seconds"] == seconds
        assert receipt["retry_source"] == "default_backoff"
        assert receipt["retry_at"] == stamp(NOW + timedelta(seconds=seconds))
        assert receipt["observed_at"] == stamp(NOW)
        assert receipt["retry_after"] is None
        prior = receipt


@pytest.mark.parametrize("header,seconds", [("0", 0), ("60", 60), (" 1800 ", 1800), ("86400", 86400)])
@pytest.mark.parametrize("stage", ["steam_community", "steam_appdetails"])
def test_explicit_numeric_retry_after_is_preserved_without_fallback_or_cap(header, seconds, stage):
    receipt = rate_limit_policy(stage, header, NOW, {"attempts": 99})
    assert receipt["retry_seconds"] == seconds
    assert receipt["retry_at"] == stamp(NOW + timedelta(seconds=seconds))
    assert receipt["retry_source"] == "steam_retry_after"
    assert receipt["retry_after"] == header


def test_http_date_is_absolute_and_explicit_long_delay_is_not_capped():
    deadline = NOW + timedelta(days=2, minutes=3)
    header = format_datetime(deadline, usegmt=True)
    receipt = rate_limit_policy("steam_store_browse", header, NOW)
    assert receipt["retry_seconds"] == 2 * 86400 + 180
    assert receipt["retry_at"] == stamp(deadline)
    assert receipt["retry_source"] == "steam_retry_after"
    assert receipt["retry_after"] == header


def test_http_date_converts_offsets_and_rounds_up_partial_seconds():
    observed = NOW.replace(microsecond=400000)
    deadline = NOW + timedelta(minutes=30)
    header = format_datetime(deadline.astimezone(timezone(timedelta(hours=8))))
    receipt = rate_limit_policy("steam_community", header, observed)
    assert receipt["retry_seconds"] == 1800
    assert receipt["retry_at"] == stamp(observed + timedelta(seconds=1800))
    assert datetime.fromisoformat(receipt["retry_at"].replace("Z", "+00:00")) >= deadline


def test_http_date_in_the_past_is_a_valid_zero_delay():
    header = format_datetime(NOW - timedelta(minutes=1), usegmt=True)
    receipt = rate_limit_policy("steam_appdetails", header, NOW)
    assert receipt["retry_seconds"] == 0
    assert receipt["retry_at"] == stamp(NOW)
    assert receipt["retry_source"] == "steam_retry_after"


@pytest.mark.parametrize("header", [None, "", "invalid", "-1", "1.5", "Fri, 99 Oct 2026 10:00:00 GMT"])
def test_invalid_retry_after_uses_fallback_but_retains_the_received_header(header):
    receipt = rate_limit_policy("steam_community", header, NOW)
    assert receipt["retry_seconds"] == 900
    assert receipt["retry_source"] == "default_backoff"
    assert receipt["retry_after"] == header


def test_observation_is_the_response_time_so_delayed_429_still_waits_full_interval():
    response_time = NOW + timedelta(minutes=12)
    receipt = rate_limit_policy("steam_appdetails", "900", response_time)
    assert receipt["retry_at"] == stamp(response_time + timedelta(minutes=15))


def test_transient_retries_back_off_from_five_minutes_and_reset_with_no_prior():
    prior = None
    for attempt, seconds in enumerate([300, 600, 1200, 2400, 3600, 3600], 1):
        receipt = transient_retry_policy(prior, NOW)
        assert receipt["retry_attempts"] == attempt
        assert receipt["retry_at"] == stamp(NOW + timedelta(seconds=seconds))
        assert receipt["retry_source"] == "transient_backoff"
        prior = receipt
    assert transient_retry_policy({}, NOW)["retry_attempts"] == 1


@pytest.mark.parametrize("prior", [{"attempts": -1}, {"attempts": True}, {"attempts": "3"}])
def test_malformed_attempt_counts_restart_at_first_fallback(prior):
    assert rate_limit_policy("steam_community", None, NOW, prior)["attempts"] == 1


def test_huge_attempt_count_saturates_without_computing_a_huge_exponent():
    assert rate_limit_policy("steam_community", None, NOW, {"attempts": 100000})["retry_seconds"] == 3600
    assert transient_retry_policy({"retry_attempts": 100000}, NOW)["retry_attempts"] == 100001


def test_invalid_stage_and_naive_observation_are_rejected():
    with pytest.raises(ValueError):
        rate_limit_policy("other", None, NOW)
    with pytest.raises(ValueError):
        rate_limit_policy("steam_community", None, NOW.replace(tzinfo=None))


def test_recognized_old_defaults_migrate_original_global_and_inherited_receipts_once():
    state = legacy_state()
    original = deepcopy(state)
    normalized, migrations = legacy_retry_migrations(state, NOW)
    assert state == original
    assert set(migrations["games"]) == {"892970", "1867240"}
    assert set(migrations["cooldowns"]) == {"steam_community"}
    for receipt in [normalized["games"]["892970"], normalized["games"]["1867240"],
                    normalized["api_cooldowns"]["steam_community"]]:
        assert receipt["retry_at"] == stamp(FAILURE + timedelta(hours=1))
        assert receipt["observed_at"] == stamp(FAILURE)
        assert receipt["retry_source"] == "legacy_default"
        assert receipt["retry_migration"]["policy_version"] == 1
        assert receipt["retry_migration"]["original_retry_at"] == stamp(FAILURE + timedelta(hours=6))
    assert normalized["games"]["3219630"] == original["games"]["3219630"]
    assert migrations["games"]["892970"]["before"] == original["games"]["892970"]
    repeated, next_migrations = legacy_retry_migrations(normalized, NOW + timedelta(minutes=5))
    assert repeated == normalized
    assert next_migrations == {"games": {}, "cooldowns": {}}


def test_parser_version_three_receipts_inheriting_the_old_limit_also_migrate():
    state = legacy_state()
    for aid in ("3219630", "2776270", "3640200"):
        state["games"][aid] = {
            "status": "pending", "reason": "steam_community_cooldown", "validation_version": 3,
            "rate_limit_stage": "steam_community", "retry_at": stamp(FAILURE + timedelta(hours=6)),
            "updated_at": stamp(FAILURE + timedelta(minutes=20)),
        }
    normalized, migrations = legacy_retry_migrations(state, NOW)
    for aid in ("3219630", "2776270", "3640200"):
        assert normalized["games"][aid]["validation_version"] == 3
        assert normalized["games"][aid]["retry_at"] == stamp(FAILURE + timedelta(hours=1))
        assert aid in migrations["games"]


def test_version_three_inherited_receipt_with_new_server_evidence_is_preserved():
    state = legacy_state()
    state["games"]["1867240"].update(validation_version=3, retry_after="21600",
                                    retry_source="steam_retry_after")
    normalized, migrations = legacy_retry_migrations(state, NOW)
    assert normalized["games"]["1867240"] == state["games"]["1867240"]
    assert "1867240" not in migrations["games"]


@pytest.mark.parametrize("changes", [
    {"rate_limit_stage": "steam_community"},
    {"retry_source": "steam_retry_after"},
    {"source": "server"},
    {"stage": "steam_community"},
    {"retry_after": "21600"},
    {"retry_after": "0"},
    {"observed_at": stamp(FAILURE)},
    {"validation_version": 3},
    {"validation_version": True},
    {"updated_at": "malformed"},
    {"updated_at": FAILURE.replace(tzinfo=None).isoformat()},
    {"retry_at": stamp(FAILURE + timedelta(hours=5, minutes=59))},
    {"updated_at": stamp(NOW + timedelta(hours=1)), "retry_at": stamp(NOW + timedelta(hours=7))},
])
def test_unknown_or_evidenced_rate_limit_receipts_are_not_migrated(changes):
    state = legacy_state()
    state["games"]["892970"].update(changes)
    normalized, migrations = legacy_retry_migrations(state, NOW)
    assert normalized == state
    assert migrations == {"games": {}, "cooldowns": {}}


@pytest.mark.parametrize("field,value", [("retry_source", "steam_retry_after"), ("retry_after", "21600")])
def test_matching_global_deadline_with_server_evidence_is_preserved(field, value):
    state = legacy_state()
    state["api_cooldowns"]["steam_community"][field] = value
    normalized, migrations = legacy_retry_migrations(state, NOW)
    assert normalized["api_cooldowns"] == state["api_cooldowns"]
    assert migrations["cooldowns"] == {}


def test_unrelated_global_and_inherited_deadlines_are_preserved():
    state = legacy_state()
    state["api_cooldowns"]["steam_community"]["retry_at"] = stamp(NOW + timedelta(hours=12))
    state["games"]["1867240"]["retry_at"] = stamp(NOW + timedelta(hours=12))
    normalized, migrations = legacy_retry_migrations(state, NOW)
    assert normalized["api_cooldowns"] == state["api_cooldowns"]
    assert normalized["games"]["1867240"] == state["games"]["1867240"]
    assert set(migrations["games"]) == {"892970"}


def test_metadata_cooldowns_do_not_inherit_the_unknown_stage_legacy_bug():
    state = legacy_state()
    state["api_cooldowns"]["steam_appdetails"] = deepcopy(state["api_cooldowns"]["steam_community"])
    normalized, migrations = legacy_retry_migrations(state, NOW)
    assert normalized["api_cooldowns"]["steam_appdetails"] == state["api_cooldowns"]["steam_appdetails"]
    assert "steam_appdetails" not in migrations["cooldowns"]


def test_receipts_written_before_original_failure_are_not_proven_inherited_limits():
    state = legacy_state()
    state["api_cooldowns"]["steam_community"]["updated_at"] = stamp(FAILURE - timedelta(minutes=1))
    state["games"]["1867240"]["updated_at"] = stamp(FAILURE - timedelta(minutes=1))
    normalized, migrations = legacy_retry_migrations(state, NOW)
    assert normalized["api_cooldowns"] == state["api_cooldowns"]
    assert normalized["games"]["1867240"] == state["games"]["1867240"]
    assert set(migrations["games"]) == {"892970"}


def test_apply_recomputes_after_from_latest_state_and_preserves_batch_and_input():
    state = legacy_state()
    _, migrations = legacy_retry_migrations(state, NOW)
    state_before, batch_before = deepcopy(state), deepcopy(migrations)
    applied_at = NOW + timedelta(minutes=1)
    saved = apply_legacy_retry_migrations(state, migrations, applied_at)
    assert state == state_before and migrations == batch_before
    assert saved["games"]["892970"]["retry_migration"]["migrated_at"] == stamp(applied_at)
    assert saved["api_cooldowns"]["steam_community"]["retry_at"] == stamp(FAILURE + timedelta(hours=1))


def test_concurrent_explicit_long_429_cannot_be_shortened_by_old_migration_batch():
    state = legacy_state()
    _, migrations = legacy_retry_migrations(state, NOW)
    latest = deepcopy(state)
    longer = rate_limit_policy("steam_community", "86400", NOW + timedelta(seconds=1))
    longer["updated_at"] = stamp(NOW + timedelta(seconds=1))
    latest["api_cooldowns"]["steam_community"] = longer
    latest["games"]["892970"].update(longer)
    latest["games"]["892970"]["rate_limit_stage"] = "steam_community"
    saved = apply_legacy_retry_migrations(latest, migrations, NOW + timedelta(minutes=1))
    assert saved == latest


def test_same_deadline_changed_cooldown_timestamp_fails_full_before_compare():
    state = legacy_state()
    _, migrations = legacy_retry_migrations(state, NOW)
    latest = deepcopy(state)
    latest["api_cooldowns"]["steam_community"]["updated_at"] = stamp(NOW)
    saved = apply_legacy_retry_migrations(latest, migrations, NOW)
    assert saved["api_cooldowns"] == latest["api_cooldowns"]
    assert saved["games"]["892970"]["retry_source"] == "legacy_default"


def test_changed_game_identity_or_updated_time_is_not_overwritten():
    state = legacy_state()
    _, migrations = legacy_retry_migrations(state, NOW)
    latest = deepcopy(state)
    latest["games"]["892970"]["twitch_admission"]["twitch_game_id"] = "new-category"
    latest["games"]["1867240"]["updated_at"] = stamp(NOW)
    saved = apply_legacy_retry_migrations(latest, migrations, NOW)
    assert saved["games"]["892970"] == latest["games"]["892970"]
    assert saved["games"]["1867240"] == latest["games"]["1867240"]


@pytest.mark.parametrize("malformed", [None, [], {}, {"before": []}, {"before": {}, "after": {}}])
def test_malformed_or_unknown_before_pairs_are_ignored(malformed):
    state = legacy_state()
    saved = apply_legacy_retry_migrations(state, {"games": {"892970": malformed}}, NOW)
    assert saved == state


@pytest.mark.parametrize("invalid_time", [None, "malformed", NOW.replace(tzinfo=None)])
def test_apply_without_a_valid_batch_clock_preserves_all_retry_state(invalid_time):
    state = legacy_state()
    _, migrations = legacy_retry_migrations(state, NOW)
    assert apply_legacy_retry_migrations(state, migrations, invalid_time) == state
    assert apply_legacy_retry_migrations(state, {}, invalid_time) == state


def test_proposed_after_is_not_trusted_to_choose_a_shorter_deadline():
    state = legacy_state()
    _, migrations = legacy_retry_migrations(state, NOW)
    migrations["games"]["892970"]["after"]["retry_at"] = stamp(FAILURE)
    saved = apply_legacy_retry_migrations(state, migrations, NOW)
    assert saved["games"]["892970"]["retry_at"] == stamp(FAILURE + timedelta(hours=1))
