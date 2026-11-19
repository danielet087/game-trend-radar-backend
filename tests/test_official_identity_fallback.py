"""zh-TW as the main Community identity, en-US as its backup, and the daily cap."""
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


def test_the_first_run_parks_the_en_us_backoff_and_makes_zh_tw_active():
    checkpoint = blocked_checkpoint()
    store = state.CooldownStore(checkpoint)
    # A checkpoint without the identity block describes en-US.
    assert store.identity() == "en-US" and store.blocked(NOW)
    assert store.prepare_identity(NOW) == "migrated"
    block = checkpoint[state.IDENTITY_FIELD]
    assert store.identity() == "zh-TW" == block["active"] == block["primary"]
    assert block["fallback"] == "en-US"
    assert block["standby_retry_at"] == BLOCKED_UNTIL.isoformat()
    assert block["standby_rate_limit_count"] == 9
    # The top-level fields now describe zh-TW; the last success is either identity's.
    assert checkpoint["rate_limit_count"] == 0
    assert checkpoint["next_request_after_taipei"] is None
    assert checkpoint["community_cooldown"] is None
    assert checkpoint["community_last_success_at"] == "2026-11-17T09:04:06+08:00"
    assert not store.blocked(NOW)
    assert store.prepare_identity(NOW) is None
    assert checkpoint[state.IDENTITY_FIELD] == block


def test_a_checkpoint_without_any_backoff_also_starts_on_zh_tw():
    checkpoint = {"rate_limit_count": 0, "next_request_after_taipei": None}
    assert state.CooldownStore(checkpoint).prepare_identity(NOW) == "migrated"
    block = checkpoint[state.IDENTITY_FIELD]
    assert block["active"] == "zh-TW" and block["standby_retry_at"] is None
    assert block["standby_rate_limit_count"] == 0


def rate_limited_zh_tw(until, count=2):
    checkpoint = blocked_checkpoint()
    store = state.CooldownStore(checkpoint)
    store.prepare_identity(NOW)
    checkpoint.update(rate_limit_count=count, next_request_after_taipei=until.isoformat(),
                      community_cooldown={"retry_at": until.astimezone(timezone.utc).isoformat()})
    return checkpoint, store


def test_a_rate_limited_zh_tw_hands_over_once_en_us_may_be_asked():
    zh_until = BLOCKED_UNTIL + timedelta(hours=3)
    checkpoint, store = rate_limited_zh_tw(zh_until)
    before = deepcopy(checkpoint)
    # Both identities are still backing off: nothing changes.
    assert store.prepare_identity(NOW) is None
    assert checkpoint == before
    assert store.prepare_identity(BLOCKED_UNTIL) == "switched"
    block = checkpoint[state.IDENTITY_FIELD]
    assert store.identity() == "en-US" == block["active"]
    assert block["switched_at"] == BLOCKED_UNTIL.isoformat()
    assert block["standby_retry_at"] == zh_until.isoformat()
    assert block["standby_rate_limit_count"] == 2
    # en-US resumes with its own 429 count, so a new refusal backs off further.
    assert checkpoint["rate_limit_count"] == 9
    assert checkpoint["next_request_after_taipei"] is None and checkpoint["community_cooldown"] is None
    assert not store.blocked(BLOCKED_UNTIL)


@pytest.mark.parametrize("fields", [
    {"rate_limit_count": 0, "next_request_after_taipei": (NOW + timedelta(hours=2)).isoformat()},
    {"rate_limit_count": 1, "next_request_after_taipei": (NOW - timedelta(minutes=1)).isoformat()},
])
def test_switching_needs_an_active_429_backoff(fields):
    checkpoint = {"rate_limit_count": 0, "next_request_after_taipei": None}
    store = state.CooldownStore(checkpoint)
    store.prepare_identity(NOW - timedelta(days=1))
    checkpoint.update(fields)
    before = deepcopy(checkpoint)
    assert store.prepare_identity(NOW) is None
    assert checkpoint == before


def on_en_us(zh_retry, *, zh_count=2):
    """en-US active because zh-TW was refused; zh-TW may be asked at zh_retry."""
    return {"rate_limit_count": 0, "temporary_error_count": 0, "next_request_after_taipei": None,
            "community_cooldown": None, "community_last_success_at": "2026-11-19T21:30:00+08:00",
            state.IDENTITY_FIELD: {
                "active": "en-US", "primary": "zh-TW", "fallback": "en-US",
                "switched_at": "2026-11-19T21:00:00+08:00", "standby_retry_at": zh_retry.isoformat(),
                "standby_rate_limit_count": zh_count, "last_probe_at": None, "last_probe_status": None}}


def test_while_on_en_us_a_refused_zh_tw_probe_backs_off_without_touching_en_us():
    zh_retry = NOW + timedelta(hours=1)
    checkpoint = on_en_us(zh_retry)
    store = state.CooldownStore(checkpoint)
    assert not store.primary_probe_due(NOW)
    assert store.primary_probe_due(zh_retry)
    checkpoint.update(rate_limit_count=1, next_request_after_taipei=(zh_retry + timedelta(minutes=15)).isoformat())
    en = {key: checkpoint[key] for key in ("rate_limit_count", "next_request_after_taipei")}

    until = store.primary_probe_result(outcome("rate_limited", 429), zh_retry)

    block = checkpoint[state.IDENTITY_FIELD]
    assert until == zh_retry + timedelta(minutes=60)
    assert block["standby_retry_at"] == until.isoformat()
    assert block["standby_rate_limit_count"] == 3
    assert block["last_probe_status"] == "rate_limited" and block["last_probe_at"] == zh_retry.isoformat()
    assert block["active"] == "en-US"
    assert {key: checkpoint[key] for key in en} == en
    later = until + timedelta(minutes=5)
    assert store.primary_probe_result(outcome("transport_or_xml_error"), later) == later + timedelta(hours=1)
    assert checkpoint[state.IDENTITY_FIELD]["standby_rate_limit_count"] == 3


def test_a_zh_tw_answer_takes_over_and_parks_en_us():
    zh_retry = NOW + timedelta(hours=1)
    checkpoint = on_en_us(zh_retry)
    en_until = zh_retry + timedelta(minutes=30)
    checkpoint.update(rate_limit_count=4, next_request_after_taipei=en_until.isoformat())
    store = state.CooldownStore(checkpoint)
    assert store.primary_probe_result(outcome("ok", 200), zh_retry) is None
    block = checkpoint[state.IDENTITY_FIELD]
    assert store.identity() == "zh-TW" == block["active"]
    assert block["standby_retry_at"] == en_until.isoformat()
    assert block["standby_rate_limit_count"] == 4
    assert checkpoint["rate_limit_count"] == 0 and checkpoint["next_request_after_taipei"] is None
    assert checkpoint["community_last_success_at"] == zh_retry.isoformat()
    assert not store.primary_probe_due(zh_retry + timedelta(days=2))
    with pytest.raises(ValueError):
        store.primary_probe_result(outcome("ok", 200), zh_retry)


@pytest.mark.parametrize("block", [
    {"active": "ja-JP", "primary": "zh-TW", "fallback": "en-US"},
    # The unmerged en-US-first draft never reached a checkpoint; reject it anyway.
    {"active": "zh-TW", "primary": "en-US", "fallback": "zh-TW"},
    {"active": "zh-TW", "primary": "zh-TW", "fallback": "en-US", "standby_retry_at": "soon"},
    {"active": "zh-TW", "primary": "zh-TW", "fallback": "en-US", "standby_rate_limit_count": -1},
])
def test_malformed_identity_state_is_rejected(block):
    with pytest.raises(ValueError):
        state.CooldownStore({state.IDENTITY_FIELD: block}).identity()


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


def test_client_uses_the_active_identity_and_probes_with_zh_tw():
    checkpoint = blocked_checkpoint()
    store = state.CooldownStore(checkpoint)
    session = Session(200, 200, 429)
    client = adapter.OfficialFollowerClient(session=session, clock=lambda: NOW, cooldown=store)
    gid = domain.group_to_gid(7)
    # Before the first run the stored backoff is en-US's.
    assert client.fetch(gid).status == "cooldown_no_request"
    store.prepare_identity(NOW)
    assert client.fetch(gid).status == "ok"
    checkpoint.update(on_en_us(NOW + timedelta(hours=1)))
    assert client.fetch(gid).status == "ok"
    before = {key: checkpoint[key] for key in ("rate_limit_count", "next_request_after_taipei")}
    assert client.probe_primary(gid).status == "rate_limited"
    assert [language for _, language in session.sent] == [ZH, EN, ZH]
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


def test_hourly_batch_resumes_with_zh_tw_within_the_round_cap(tmp_path):
    session = Session()
    checkpoint, report = run_batch(tmp_path, blocked_checkpoint(), session)
    assert [language for _, language in session.sent] == [ZH] * 5
    assert report["stop_reason"] == "batch_request_limit"
    assert report["requests_this_run"] == 5
    assert report["community_identity"] == "zh-TW"
    assert report["daily_requests_used"] == 5 and report["daily_request_limit"] == 40
    assert checkpoint[state.DAILY_REQUESTS_FIELD] == {"date": "2026-11-19", "requests": 5}
    assert checkpoint[state.IDENTITY_FIELD]["standby_retry_at"] == BLOCKED_UNTIL.isoformat()
    assert len(checkpoint["official_results"]) == 5


def test_an_hour_after_zh_tw_is_refused_the_round_uses_en_us(tmp_path):
    session = Session()
    prior = on_en_us(NOW + timedelta(hours=2))
    block = prior[state.IDENTITY_FIELD]
    # zh-TW active and refused 15 minutes ago; en-US's own backoff has passed.
    block.update(active="zh-TW", standby_retry_at=None, standby_rate_limit_count=3)
    prior.update(rate_limit_count=1, next_request_after_taipei=(NOW + timedelta(minutes=10)).isoformat())
    checkpoint, report = run_batch(tmp_path, prior, session)
    assert [language for _, language in session.sent] == [EN] * 5
    assert report["community_identity"] == "en-US"
    block = checkpoint[state.IDENTITY_FIELD]
    assert block["active"] == "en-US"
    assert block["standby_retry_at"] == (NOW + timedelta(minutes=10)).isoformat()
    assert block["standby_rate_limit_count"] == 1


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


ZH_DUE = NOW + timedelta(hours=1)


def test_a_refused_zh_tw_probe_keeps_en_us_for_the_rest_of_the_round(tmp_path):
    session = Session(429)
    checkpoint, report = run_batch(tmp_path, on_en_us(ZH_DUE), session, now=ZH_DUE)
    gid = domain.group_to_gid(101)
    # The probed game is asked again with en-US; the round keeps its cap.
    assert session.sent == [(gid, ZH), (gid, EN)] + [(domain.group_to_gid(aid), EN) for aid in (102, 103, 104)]
    block = checkpoint[state.IDENTITY_FIELD]
    assert block["active"] == "en-US"
    assert block["standby_retry_at"] == (ZH_DUE + timedelta(minutes=60)).isoformat()
    assert report["identity_probe"] == "rate_limited"
    assert report["daily_requests_used"] == 5
    probe = checkpoint["attempt_events"][0]
    assert probe["identity_probe"] is True and probe["community_identity"] == "zh-TW" and probe["http"] == 429


def test_an_answered_zh_tw_probe_takes_over_without_asking_twice(tmp_path):
    session = Session(200)
    checkpoint, report = run_batch(tmp_path, on_en_us(ZH_DUE), session, now=ZH_DUE)
    assert [language for _, language in session.sent] == [ZH] * 4
    assert len({gid for gid, _ in session.sent}) == 4
    assert checkpoint[state.IDENTITY_FIELD]["active"] == "zh-TW"
    assert report["community_identity"] == "zh-TW"
    assert report["identity_probe"] == "ok"
    assert checkpoint["official_results"]["101"]["official_followers"] == 1234
    assert report["requests_this_run"] == 4
