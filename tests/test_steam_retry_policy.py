"""Retry behavior for metadata APIs used by the unified Twitch queue."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from scripts.steam_retry_policy import (
    rate_limit_policy,
    transient_retry_policy,
)


NOW = datetime(2026, 10, 2, 14, 32, 37, tzinfo=timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("stage", ["steam_store_browse", "steam_appdetails"])
def test_metadata_rate_limits_back_off_from_five_minutes_and_cap_at_thirty(stage):
    prior = None
    for attempt, seconds in enumerate([300, 600, 1200, 1800, 1800], 1):
        receipt = rate_limit_policy(stage, None, NOW, prior)
        assert receipt["attempts"] == attempt
        assert receipt["retry_seconds"] == seconds
        assert receipt["retry_source"] == "default_backoff"
        assert receipt["retry_at"] == stamp(NOW + timedelta(seconds=seconds))
        assert receipt["observed_at"] == stamp(NOW)
        assert receipt["retry_after"] is None
        prior = receipt


@pytest.mark.parametrize("header,seconds", [("0", 0), ("60", 60), (" 1800 ", 1800), ("86400", 86400)])
def test_explicit_numeric_retry_after_is_preserved_without_fallback_or_cap(header, seconds):
    receipt = rate_limit_policy("steam_appdetails", header, NOW, {"attempts": 99})
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
    receipt = rate_limit_policy("steam_appdetails", header, observed)
    assert receipt["retry_seconds"] == 1800
    assert receipt["retry_at"] == stamp(observed + timedelta(seconds=1800))
    assert datetime.fromisoformat(receipt["retry_at"].replace("Z", "+00:00")) >= deadline


def test_http_date_in_the_past_is_a_valid_zero_delay():
    header = format_datetime(NOW - timedelta(minutes=1), usegmt=True)
    receipt = rate_limit_policy("steam_appdetails", header, NOW)
    assert receipt["retry_seconds"] == 0
    assert receipt["retry_at"] == stamp(NOW)
    assert receipt["retry_source"] == "steam_retry_after"


@pytest.mark.parametrize("header", [None, "", "-1", "Fri, 99 Oct 2026 10:00:00 GMT"])
def test_invalid_retry_after_uses_fallback_but_retains_the_received_header(header):
    receipt = rate_limit_policy("steam_appdetails", header, NOW)
    assert receipt["retry_seconds"] == 300
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
    assert rate_limit_policy("steam_appdetails", None, NOW, prior)["attempts"] == 1


def test_huge_attempt_count_saturates_without_computing_a_huge_exponent():
    assert rate_limit_policy("steam_appdetails", None, NOW, {"attempts": 100000})["retry_seconds"] == 1800
    assert transient_retry_policy({"retry_attempts": 100000}, NOW)["retry_attempts"] == 100001


def test_retired_community_unknown_stage_and_naive_observation_are_rejected():
    for stage in ("steam_community", "other"):
        with pytest.raises(ValueError):
            rate_limit_policy(stage, None, NOW)
    with pytest.raises(ValueError):
        rate_limit_policy("steam_appdetails", None, NOW.replace(tzinfo=None))
