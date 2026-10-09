"""The public intake composes its clock before entering the IO-free use case."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.adapters import twitch_intake as canonical
from radar_backend.application import twitch_intake as application
from scripts import import_twitch_steam_discoveries as legacy


NOW = datetime(2026, 10, 9, 15, 59, 59, tzinfo=timezone.utc)


@pytest.fixture(params=[canonical, legacy], ids=["canonical", "legacy"])
def intake(request, monkeypatch):
    entry = request.param
    calls = []
    application_collect = application.collect
    forbidden_clock = Mock(side_effect=AssertionError("Application read a wall clock"))

    def application_port(*args, **kwargs):
        calls.append(kwargs)
        # A composed clock must work even when the use case's datetime default
        # cannot provide a wall clock. Run the actual application body.
        kwargs["datetime_type"] = SimpleNamespace(now=forbidden_clock)
        return application_collect(*args, **kwargs)

    monkeypatch.setattr(application, "collect", application_port)
    reader = Mock(return_value={})
    validator = Mock(return_value=[])
    monkeypatch.setattr(entry, "read_json", reader)
    monkeypatch.setattr(entry, "validate_snapshot", validator)

    def run(**options):
        result = entry.collect(
            Path("immutable-frontend"), "a" * 40, {"games": []}, {},
            session=SimpleNamespace(headers={}), blocked=set(),
            monotonic=lambda: 0, sleep=lambda seconds: None, **options,
        )
        forbidden_clock.assert_not_called()
        assert [call.args[0].name for call in reader.call_args_list] == [
            "twitch_steam_discovery.json", "twitch_tracking.json", "steam_upcoming.json",
        ]
        assert result["records"] == result["follower_candidates"] == []
        return result, calls[-1]["clock"], validator.call_args.args[-1]

    return entry, run


def test_default_clock_keeps_initial_observation_and_later_service_time(intake, monkeypatch):
    entry, run = intake
    later = NOW + timedelta(seconds=2)
    wall = Mock(side_effect=[NOW, later])
    monkeypatch.setattr(entry, "datetime", SimpleNamespace(now=wall))
    result, clock, validated_at = run()
    assert result["generated_at"] == "2026-10-09T15:59:59Z"
    assert validated_at is NOW
    wall.assert_called_once_with(timezone.utc)
    assert clock() is later
    assert wall.call_count == 2


def test_explicit_observation_freezes_default_clock_without_reading_wall_time(intake, monkeypatch):
    entry, run = intake
    wall = Mock(side_effect=AssertionError("Explicit observation must win"))
    monkeypatch.setattr(entry, "datetime", SimpleNamespace(now=wall))
    result, clock, validated_at = run(now=NOW)
    assert result["generated_at"] == "2026-10-09T15:59:59Z"
    assert validated_at is NOW and clock() is NOW and clock() is NOW
    wall.assert_not_called()


def test_explicit_clock_is_forwarded_and_read_once_for_the_observation(intake, monkeypatch):
    entry, run = intake
    wall = Mock(side_effect=AssertionError("Explicit clock must win"))
    monkeypatch.setattr(entry, "datetime", SimpleNamespace(now=wall))
    later = NOW + timedelta(seconds=2)
    supplied = Mock(side_effect=[NOW, later])
    result, clock, validated_at = run(clock=supplied)
    assert result["generated_at"] == "2026-10-09T15:59:59Z"
    assert validated_at is NOW and clock is supplied
    supplied.assert_called_once_with()
    assert clock() is later
    wall.assert_not_called()


def test_explicit_observation_does_not_consume_a_supplied_service_clock(intake, monkeypatch):
    entry, run = intake
    wall = Mock(side_effect=AssertionError("Neither explicit port reads wall time"))
    monkeypatch.setattr(entry, "datetime", SimpleNamespace(now=wall))
    later = NOW + timedelta(seconds=2)
    supplied = Mock(return_value=later)
    result, clock, validated_at = run(now=NOW, clock=supplied)
    assert result["generated_at"] == "2026-10-09T15:59:59Z"
    assert validated_at is NOW and clock is supplied
    supplied.assert_not_called()
    assert clock() is later
    wall.assert_not_called()


def test_false_clock_keeps_original_fallback_truthiness_and_evaluation_count(intake, monkeypatch):
    entry, run = intake
    events = []

    class FalseClock:
        def __bool__(self):
            events.append("bool")
            return False

        def __call__(self):
            raise AssertionError("False clock must fall back to the public wall clock")

    wall = Mock(return_value=NOW)
    monkeypatch.setattr(entry, "datetime", SimpleNamespace(now=wall))
    result, clock, validated_at = run(clock=FalseClock())
    assert events == ["bool"]
    assert result["generated_at"] == "2026-10-09T15:59:59Z" and validated_at is NOW
    wall.assert_called_once_with(timezone.utc)


def test_default_clock_retains_the_datetime_dependency_selected_for_this_call(intake, monkeypatch):
    entry, run = intake
    first = Mock(return_value=NOW)
    monkeypatch.setattr(entry, "datetime", SimpleNamespace(now=first))
    _, clock, _ = run()
    later = NOW + timedelta(seconds=2)
    replacement = Mock(return_value=later)
    monkeypatch.setattr(entry, "datetime", SimpleNamespace(now=replacement))
    assert clock() is NOW
    assert first.call_count == 2
    replacement.assert_not_called()


@pytest.mark.parametrize("dependency", ["reader", "validator", "datetime", "application"])
def test_clock_truthiness_does_not_rebind_already_selected_ports(intake, monkeypatch, dependency):
    entry, run = intake
    wall = Mock(return_value=NOW)
    monkeypatch.setattr(entry, "datetime", SimpleNamespace(now=wall))
    rebound = Mock(side_effect=AssertionError("A selected dependency was rebound"))
    evaluated = []

    class RebindingClock:
        def __bool__(self):
            evaluated.append("bool")
            if dependency == "reader":
                monkeypatch.setattr(entry, "read_json", rebound)
            elif dependency == "validator":
                monkeypatch.setattr(entry, "validate_snapshot", rebound)
            elif dependency == "datetime":
                monkeypatch.setattr(entry, "datetime", SimpleNamespace(now=rebound))
            else:
                monkeypatch.setattr(application, "collect", rebound)
            return False

        def __call__(self):
            raise AssertionError("False clock must use its selected fallback")

    result, _, validated_at = run(clock=RebindingClock())
    assert result["generated_at"] == "2026-10-09T15:59:59Z" and validated_at is NOW
    assert evaluated == ["bool"]
    wall.assert_called_once_with(timezone.utc)
    rebound.assert_not_called()
