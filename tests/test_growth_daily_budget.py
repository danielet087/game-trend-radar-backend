"""A fresh growth day needs enough bounded time for the full accepted cohort."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from radar_backend.application.growth import collect_observations
from radar_backend.domain.growth import growth_collection_complete
from radar_backend.domain.official_queue import GROUP_BASE
from radar_backend.publication.growth import stamp_growth_publication
from radar_backend.state.official_followers import CooldownStore, OfficialFollowerCache

START = datetime(2026, 10, 11, 17, 15, tzinfo=timezone.utc)  # Taiwan October 12, 01:15.


class SimulatedTime:
    def __init__(self):
        self.seconds = 0
        self.sleeps = []

    def clock(self):
        return START + timedelta(seconds=self.seconds)

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.seconds += seconds


class OfficialSource:
    def __init__(self, timer, request_seconds=3):
        self.timer = timer
        self.request_seconds = request_seconds
        self.calls = []

    def fetch(self, gid):
        start = self.timer.monotonic()
        self.timer.seconds += self.request_seconds
        observed_at = self.timer.clock()
        followers = 7000 + int(gid) - GROUP_BASE
        self.calls.append((gid, start, observed_at, followers))
        return SimpleNamespace(status="ok", http=200, error_type=None,
                               observed_at=observed_at, followers=followers)


def cohort(size, *, checked_at=None):
    old = checked_at or (START - timedelta(days=1)).isoformat()
    rows, checkpoint, history = [], {"official_growth_observations": {}}, {}
    for aid in range(1, size + 1):
        gid = str(GROUP_BASE + aid)
        rows.append({"appid": aid, "followers": 6000,
                     "release_start": "2026-10-12", "follower_checked_at": old})
        checkpoint["official_growth_observations"][str(aid)] = {
            "appid": aid, "group_id64": gid, "official_followers": 6000,
            "official_checked_at_taipei": old,
            "official_source": "Steam Community XML memberCount",
        }
        history[str(aid)] = {"history": [{"at": old, "followers": 6000,
                                        "source": "steam_community"}]}
    return rows, checkpoint, history


def run(size, *, timer=None, source=None, checked_at=None, **kwargs):
    timer = timer or SimulatedTime()
    source = source or OfficialSource(timer)
    rows, checkpoint, history = cohort(size, checked_at=checked_at)
    report = collect_observations(
        rows, history, checkpoint, client=source, cache=OfficialFollowerCache(checkpoint),
        cooldown=CooldownStore(checkpoint), clock=timer.clock, monotonic=timer.monotonic,
        sleep=timer.sleep, persist=lambda *_: None, **kwargs,
    )
    return report, timer, source, checkpoint, history


@pytest.mark.parametrize("size", [136, 138])
def test_fresh_day_complete_cohort_preserves_pacing_and_real_measurement_times(size):
    result, timer, source, checkpoint, history = run(size, max_requests=250, max_seconds=9000)
    assert result["eligible"] == result["requests"] == len(source.calls) == size
    assert result["reused"] == 0  # Yesterday's observations never cover today.
    assert result["pending"] == result["unavailable"] == result["errors"] == []
    assert result["reason"] == "completed"
    assert result["measurement_complete"] is result["collection_complete"] is True
    assert result["max_requests"] == 250
    assert result["max_seconds"] == 9000
    assert result["elapsed_seconds"] == (size - 1) * 30 + 3
    assert result["elapsed_seconds"] == timer.monotonic() < result["max_seconds"]
    assert all(later[1] - earlier[1] >= 30 for earlier, later in zip(source.calls, source.calls[1:]))
    for measurement, call in zip(result["measurements"], source.calls):
        assert measurement["at"] == call[2].isoformat().replace("+00:00", "Z")
        assert measurement["followers"] == call[3]
        saved = checkpoint["official_growth_observations"][str(measurement["appid"])]
        assert saved["official_checked_at_taipei"] == measurement["at"]
        assert saved["official_followers"] == measurement["followers"]
        assert history[str(measurement["appid"])]["history"][0]["followers"] == 6000
    delivered = stamp_growth_publication(result, timer.clock(), state_persisted=True, published=True)
    assert growth_collection_complete(delivered, timer.clock())


@pytest.mark.parametrize("size", [136, 138])
def test_legacy_default_budget_stays_partial_and_retains_exact_pending_games(size):
    result, timer, source, checkpoint, _ = run(size)
    assert result["max_requests"] == 120
    assert result["max_seconds"] == 3600
    assert result["requests"] == len(source.calls) == 120
    assert result["reused"] == 0
    assert result["reason"] == "bounded_run"
    assert result["pending"] == [{"appid": aid, "reason": "bounded_run"}
                                  for aid in range(121, size + 1)]
    assert not result["measurement_complete"]
    assert not result["collection_complete"]
    assert checkpoint["official_growth_observations"]["121"]["official_followers"] == 6000
    assert checkpoint["official_growth_observations"]["121"]["official_checked_at_taipei"] == (
        START - timedelta(days=1)).isoformat()
    delivered = stamp_growth_publication(result, timer.clock(), state_persisted=True, published=True)
    assert not growth_collection_complete(delivered, timer.clock())


def test_increasing_request_limit_alone_does_not_bypass_the_seconds_budget():
    result, timer, source, _, _ = run(138, max_requests=250)
    assert result["requests"] == len(source.calls) == 120
    assert result["reason"] == "bounded_run"
    assert result["elapsed_seconds"] == timer.monotonic() < 3600
    assert all(start < 3600 for _, start, _, _ in source.calls)
    assert len(result["pending"]) == 18


def test_pacing_delay_is_checked_before_waiting_or_requesting_outside_budget():
    result, timer, source, _, _ = run(3, max_requests=250, max_seconds=59)
    assert [start for _, start, _, _ in source.calls] == [0, 30]
    assert timer.sleeps == [27]
    assert result["elapsed_seconds"] == 33
    assert result["pending"] == [{"appid": 3, "reason": "bounded_run"}]


def test_late_sleep_wakeup_cannot_start_a_request_after_the_deadline():
    timer = SimulatedTime()
    original_sleep = timer.sleep

    def late_sleep(seconds):
        original_sleep(seconds + 40)

    timer.sleep = late_sleep
    result, _, source, _, _ = run(3, timer=timer, max_seconds=60)
    assert len(source.calls) == 1
    assert result["requests"] == 1
    assert result["reason"] == "bounded_run"
    assert result["elapsed_seconds"] == 70
    assert len(result["pending"]) == 2


def test_existing_same_day_measurements_need_no_extra_request_or_elapsed_budget():
    result, timer, source, _, _ = run(138, checked_at=START.isoformat(),
                                     max_requests=0, max_seconds=1)
    assert result["reused"] == 138
    assert result["requests"] == 0
    assert source.calls == timer.sleeps == []
    assert result["elapsed_seconds"] == 0
    assert result["collection_complete"]
    assert {point["at"] for point in result["measurements"]} == {START.isoformat()}


@pytest.mark.parametrize("budget", [0, -1, True, False, None, "9000", float("inf"), float("nan")])
def test_invalid_seconds_budget_fails_before_any_official_request(budget):
    timer = SimulatedTime()
    source = OfficialSource(timer)
    with pytest.raises(ValueError, match="positive finite seconds budget"):
        run(1, timer=timer, source=source, max_seconds=budget)
    assert source.calls == timer.sleeps == []


@pytest.mark.parametrize("budget", [-1, True, 1.5, None])
def test_invalid_request_budget_fails_before_any_official_request(budget):
    timer = SimulatedTime()
    source = OfficialSource(timer)
    with pytest.raises(ValueError, match="non-negative integer request budget"):
        run(1, timer=timer, source=source, max_requests=budget)
    assert source.calls == []


@pytest.mark.parametrize("tick", [True, float("nan"), float("inf"), None])
def test_invalid_monotonic_clock_cannot_disable_the_budget(tick):
    timer = SimulatedTime()
    source = OfficialSource(timer)
    timer.monotonic = lambda: tick
    with pytest.raises(ValueError, match="finite non-decreasing monotonic clock"):
        run(1, timer=timer, source=source)
    assert source.calls == []


def test_backwards_monotonic_clock_stops_before_a_second_official_request():
    timer = SimulatedTime()
    source = OfficialSource(timer)
    readings = iter((100, 100, 100, 99))
    timer.monotonic = lambda: next(readings)
    # The source uses the wall time, leaving this sequence exclusively to the
    # budget checks. One actual observation is saved before the bad next tick.
    source.fetch = lambda gid: SimpleNamespace(
        status="ok", http=200, error_type=None, observed_at=timer.clock(), followers=7001)
    rows, checkpoint, history = cohort(2)
    saved = []
    with pytest.raises(ValueError, match="finite non-decreasing monotonic clock"):
        collect_observations(rows, history, checkpoint, client=source,
            cache=OfficialFollowerCache(checkpoint), cooldown=CooldownStore(checkpoint),
            clock=timer.clock, monotonic=timer.monotonic, sleep=timer.sleep,
            persist=lambda state, _: saved.append(state["official_growth_observations"]["1"]["official_followers"]))
    assert saved == [7001]
