"""The owner's temporary zh-TW Community identity, its daily en-US probe and the daily cap."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.adapters import official_followers as adapter
from radar_backend.application.official_followers import (
    OfficialBatchServices, OfficialPaths, run_official_batch,
)
from radar_backend.domain import official_queue as domain
from radar_backend.state import official_followers as state

TAIPEI = domain.TAIPEI
NOW = datetime(2026, 11, 19, 22, 0, tzinfo=TAIPEI)
BLOCKED_UNTIL = datetime(2026, 11, 20, 19, 3, 4, tzinfo=TAIPEI)
EN, ZH = "en-US,en;q=0.9", "zh-TW,zh;q=0.9"


def blocked_checkpoint(**extra):
    return {
        "rate_limit_count": 9, "temporary_error_count": 0,
        "next_request_after_taipei": BLOCKED_UNTIL.isoformat(),
        "community_cooldown": {"attempts": 9, "retry_at": BLOCKED_UNTIL.astimezone(timezone.utc).isoformat()},
        "community_last_success_at": "2026-11-17T09:04:06+08:00",
        **extra,
    }


def outcome(status, http=None, retry_after=None):
    return domain.FollowerOutcome(status, NOW, http=http, retry_after=retry_after)


def test_fallback_moves_the_primary_backoff_aside_once():
    checkpoint = blocked_checkpoint()
    store = state.CooldownStore(checkpoint)
    assert store.identity() == state.PRIMARY_IDENTITY
    assert store.begin_fallback(NOW) is True
    block = checkpoint[state.IDENTITY_FIELD]
    assert store.identity() == state.FALLBACK_IDENTITY == block["active"]
    assert block["primary_retry_at"] == BLOCKED_UNTIL.isoformat()
    assert block["primary_rate_limit_count"] == 9
    assert block["primary_last_success_at"] == "2026-11-17T09:04:06+08:00"
    # The top-level fields now describe the fresh fallback identity.
    assert checkpoint["rate_limit_count"] == 0
    assert checkpoint["next_request_after_taipei"] is None
    assert checkpoint["community_cooldown"] is None
    assert not store.blocked(NOW)
    assert store.begin_fallback(NOW) is False
    assert checkpoint[state.IDENTITY_FIELD] == block


@pytest.mark.parametrize("checkpoint", [
    {"rate_limit_count": 0, "next_request_after_taipei": None},
    blocked_checkpoint(next_request_after_taipei="2026-11-19T21:00:00+08:00", community_cooldown=None),
    {"rate_limit_count": 0, "next_request_after_taipei": BLOCKED_UNTIL.isoformat()},
])
def test_fallback_needs_an_active_429_backoff(checkpoint):
    before = deepcopy(checkpoint)
    assert state.CooldownStore(checkpoint).begin_fallback(NOW) is False
    assert checkpoint == before


def test_failed_primary_probes_back_off_without_touching_the_fallback():
    checkpoint = blocked_checkpoint()
    store = state.CooldownStore(checkpoint)
    store.begin_fallback(NOW)
    assert not store.primary_probe_due(NOW)
    assert store.primary_probe_due(BLOCKED_UNTIL)
    checkpoint.update(rate_limit_count=1, next_request_after_taipei=(NOW + timedelta(minutes=15)).isoformat())
    fallback = {key: checkpoint[key] for key in ("rate_limit_count", "next_request_after_taipei")}

    until = store.primary_probe_result(outcome("rate_limited", 429), BLOCKED_UNTIL)

    block = checkpoint[state.IDENTITY_FIELD]
    assert until == BLOCKED_UNTIL + timedelta(hours=24)
    assert block["primary_retry_at"] == until.isoformat()
    assert block["primary_rate_limit_count"] == 10
    assert block["last_probe_status"] == "rate_limited"
    assert block["active"] == state.FALLBACK_IDENTITY
    assert {key: checkpoint[key] for key in fallback} == fallback
    later = BLOCKED_UNTIL + timedelta(hours=24)
    assert store.primary_probe_result(outcome("transport_or_xml_error"), later) == later + timedelta(hours=1)
    assert checkpoint[state.IDENTITY_FIELD]["primary_rate_limit_count"] == 10


def test_a_primary_answer_returns_for_good():
    checkpoint = blocked_checkpoint()
    store = state.CooldownStore(checkpoint)
    store.begin_fallback(NOW)
    store.primary_probe_result(outcome("ok", 200), BLOCKED_UNTIL)
    block = checkpoint[state.IDENTITY_FIELD]
    assert store.identity() == state.PRIMARY_IDENTITY
    assert block["returned_at"] == BLOCKED_UNTIL.isoformat()
    assert checkpoint["rate_limit_count"] == 0
    assert checkpoint["community_last_success_at"] == BLOCKED_UNTIL.isoformat()
    assert not store.primary_probe_due(BLOCKED_UNTIL + timedelta(days=2))
    # A later block of the primary uses the ordinary cooldown; no third identity.
    checkpoint.update(rate_limit_count=1, next_request_after_taipei=(BLOCKED_UNTIL + timedelta(hours=1)).isoformat())
    assert store.begin_fallback(BLOCKED_UNTIL) is False
    with pytest.raises(ValueError):
        store.primary_probe_result(outcome("ok", 200), BLOCKED_UNTIL)


def test_malformed_identity_state_is_rejected():
    with pytest.raises(ValueError):
        state.CooldownStore({state.IDENTITY_FIELD: {"active": "ja-JP"}}).identity()


class Session:
    def __init__(self, *statuses):
        self.statuses, self.headers, self.sent = list(statuses), {}, []

    def get(self, url, *, headers, timeout):
        gid = url.split("/gid/")[1].split("/")[0]
        status = self.statuses.pop(0) if self.statuses else 200
        self.sent.append((gid, headers["Accept-Language"]))
        body = (f"<memberList><groupID64>{gid}</groupID64><groupDetails>"
                f"<memberCount>1234</memberCount></groupDetails></memberList>").encode()
        return SimpleNamespace(status_code=status, content=body, headers={})


def test_client_uses_the_active_identity_and_probes_with_the_primary():
    checkpoint = blocked_checkpoint()
    store = state.CooldownStore(checkpoint)
    session = Session(200, 429)
    client = adapter.OfficialFollowerClient(session=session, clock=lambda: NOW, cooldown=store)
    gid = domain.group_to_gid(7)
    assert client.fetch(gid).status == "cooldown_no_request"
    store.begin_fallback(NOW)
    assert client.fetch(gid).status == "ok"
    before = {key: checkpoint[key] for key in ("rate_limit_count", "next_request_after_taipei")}
    assert client.probe_primary(gid).status == "rate_limited"
    assert [language for _, language in session.sent] == [ZH, EN]
    assert {key: checkpoint[key] for key in before} == before


def run_batch(tmp_path, checkpoint, session, *, rows=8, max_requests=5, daily_limit=40, now=NOW):
    paths = OfficialPaths(
        frozen=tmp_path / "frozen", eligible=tmp_path / "eligible.json",
        prefilter=tmp_path / "prefilter.json", official_cache=tmp_path / "cache.json",
        original_official=tmp_path / "original.json", checkpoint=tmp_path / "queue.json",
        master=tmp_path / "master.json", output=tmp_path / "out", cohort="test-cohort",
    )
    paths.checkpoint.touch()
    paths.master.touch()
    queue = [{"appid": aid, "name": f"Game {aid}", "group_id64": domain.group_to_gid(aid),
              "release_date": "2027-01-15", "queue_source": "fresh_daily_prefilter_unresolved"}
             for aid in range(101, 101 + rows)]
    checkpoint = {"cohort": paths.cohort, "pending_candidates": {str(row["appid"]): row for row in queue},
                  "official_results": {}, "attempt_events": [], **checkpoint}
    inputs = {
        paths.frozen / "source_queue.json": [], paths.frozen / "source_unresolved.json": [],
        paths.frozen / "checkpoint.json": {"official_results": {}},
        paths.eligible: {"games": []}, paths.prefilter: {},
        paths.official_cache: {"games": {}}, paths.original_official: {},
        paths.checkpoint: checkpoint, paths.master: {"games": []},
    }
    saved = {}
    unused = Mock(side_effect=AssertionError("Counts below 5,000 never qualify"))
    services = OfficialBatchServices(
        read=lambda path: deepcopy(saved.get(path, inputs.get(path))),
        save=lambda path, value: saved.__setitem__(path, deepcopy(value)),
        exists=lambda path: path.exists(),
        clock=lambda: now, monotonic=lambda: 100.0, sleep=Mock(),
        session_factory=lambda: session,
        make_queue=lambda *args: (deepcopy(queue), {"status": "current_day_prefilter_complete"}),
        git_push=Mock(return_value=True),
        reverify_pending_store_dates=Mock(return_value=0),
        retry_pending_content_dispatches=Mock(return_value=0),
        verify_store_date_for_result=unused, upsert_qualified_master=unused, dispatch_content_event=unused,
        follower_client_factory=adapter.OfficialFollowerClient,
        follower_cache_factory=state.OfficialFollowerCache, cooldown_factory=state.CooldownStore,
    )
    args = SimpleNamespace(max_requests=max_requests, interval=30.0, max_seconds=2100, save_every=10,
                           daily_limit=daily_limit, identity_fallback=True)
    run_official_batch(args, paths=paths, services=services)
    return saved[paths.checkpoint], saved[paths.output / "report.json"]


def test_hourly_batch_resumes_on_the_fallback_within_the_round_cap(tmp_path):
    session = Session()
    checkpoint, report = run_batch(tmp_path, blocked_checkpoint(), session)
    assert [language for _, language in session.sent] == [ZH] * 5
    assert report["stop_reason"] == "batch_request_limit"
    assert report["requests_this_run"] == 5
    assert report["community_identity"] == state.FALLBACK_IDENTITY
    assert report["daily_requests_used"] == 5 and report["daily_request_limit"] == 40
    assert checkpoint[state.DAILY_REQUESTS_FIELD] == {"date": "2026-11-19", "requests": 5}
    assert checkpoint[state.IDENTITY_FIELD]["primary_retry_at"] == BLOCKED_UNTIL.isoformat()
    assert len(checkpoint["official_results"]) == 5


@pytest.mark.parametrize("used,expected,stop", [
    (38, 2, "batch_request_limit"), (40, 0, "daily_request_budget"),
])
def test_hourly_batch_stops_at_the_daily_limit(tmp_path, used, expected, stop):
    session = Session()
    prior = blocked_checkpoint(**{state.DAILY_REQUESTS_FIELD: {"date": "2026-11-19", "requests": used}})
    checkpoint, report = run_batch(tmp_path, prior, session)
    assert len(session.sent) == expected
    assert report["stop_reason"] == stop
    assert checkpoint[state.DAILY_REQUESTS_FIELD] == {"date": "2026-11-19", "requests": used + expected}


def test_the_daily_counter_starts_over_on_a_new_taipei_date(tmp_path):
    session = Session()
    prior = blocked_checkpoint(**{state.DAILY_REQUESTS_FIELD: {"date": "2026-11-18", "requests": 40}})
    checkpoint, _ = run_batch(tmp_path, prior, session)
    assert checkpoint[state.DAILY_REQUESTS_FIELD] == {"date": "2026-11-19", "requests": 5}


def fallback_due():
    checkpoint = blocked_checkpoint()
    state.CooldownStore(checkpoint).begin_fallback(NOW - timedelta(days=1))
    return checkpoint


def test_a_refused_probe_keeps_the_fallback_for_the_rest_of_the_round(tmp_path):
    session = Session(429)
    checkpoint, report = run_batch(tmp_path, fallback_due(), session, now=BLOCKED_UNTIL)
    gid = domain.group_to_gid(101)
    # The probed game is asked again with the fallback; the round keeps its cap.
    assert session.sent == [(gid, EN), (gid, ZH)] + [(domain.group_to_gid(aid), ZH) for aid in (102, 103, 104)]
    block = checkpoint[state.IDENTITY_FIELD]
    assert block["active"] == state.FALLBACK_IDENTITY
    assert block["primary_retry_at"] == (BLOCKED_UNTIL + timedelta(hours=24)).isoformat()
    assert report["identity_probe"] == "rate_limited"
    assert report["daily_requests_used"] == 5
    probe = checkpoint["attempt_events"][0]
    assert probe["identity_probe"] is True and probe["community_identity"] == "en-US" and probe["http"] == 429


def test_an_answered_probe_returns_to_the_primary_without_asking_twice(tmp_path):
    session = Session(200)
    checkpoint, report = run_batch(tmp_path, fallback_due(), session, now=BLOCKED_UNTIL)
    assert [language for _, language in session.sent] == [EN] * 4
    assert len({gid for gid, _ in session.sent}) == 4
    assert checkpoint[state.IDENTITY_FIELD]["active"] == state.PRIMARY_IDENTITY
    assert report["community_identity"] == state.PRIMARY_IDENTITY
    assert report["identity_probe"] == "ok"
    assert checkpoint["official_results"]["101"]["official_followers"] == 1234
    assert report["requests_this_run"] == 4
