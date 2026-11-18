"""Daily candidate refresh rides out Steam's transient failures.

Two real failures on 11-18: the day query's total changed between pages
(2026-12-31), and ResolveVanityURL answered 502 during Steam's weekly
maintenance. Both must be retried before the run gives up.
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from radar_backend.adapters import candidate_sources, follower_prefilter
from radar_backend.adapters import steam_follower_prefilter as transport
from radar_backend.domain import follower_prefilter as rules

DAY = date(2026, 12, 31)
RELEASE = 1798689600  # 2026-12-31 12:00 Taipei


class Reply:
    def __init__(self, code=200, payload=None):
        self.status_code = code
        self.payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self.payload


def page(total, *appids):
    return Reply(200, {"response": {
        "metadata": {"total_matching_records": total},
        "store_items": [{"appid": appid, "name": f"Game {appid}",
                         "release": {"steam_release_date": RELEASE, "is_coming_soon": True}}
                        for appid in appids],
    }})


class QuerySession:
    """Serve scripted replies in order and record each page offset asked for."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.offsets = []

    def get(self, url, *, params, timeout):
        assert url == candidate_sources.QUERY_URL and timeout == 35
        self.offsets.append(json.loads(params["input_json"])["query"]["start"])
        return self.replies.pop(0)


def query(session, sleeps):
    return candidate_sources.query_one_day(
        session, "private-key", DAY, request_interval=0, page_size=1, sleep=sleeps.append,
    )


def test_changed_total_reads_the_whole_day_again():
    # Pass 1 sees the total move from 2 to 3 on its second page.
    session = QuerySession([page(2, 101), page(3, 102), page(2, 101), page(2, 103)])
    sleeps = []
    games, summary = query(session, sleeps)
    assert [game["appid"] for game in games] == [101, 103]
    assert summary["api_matches"] == 2 and summary["pages"] == 2
    assert session.offsets == [0, 1, 0, 1]
    assert sleeps == [candidate_sources.QUERY_RESTART_SECONDS]


def test_total_that_keeps_moving_still_fails_closed():
    replies = []
    for _ in range(candidate_sources.QUERY_DAY_PASSES):
        replies += [page(2, 101), page(3, 102)]
    session = QuerySession(replies)
    sleeps = []
    with pytest.raises(RuntimeError, match=r"changed matching total within 2026-12-31; retry later"):
        query(session, sleeps)
    assert session.offsets == [0, 1] * candidate_sources.QUERY_DAY_PASSES
    assert sleeps == [candidate_sources.QUERY_RESTART_SECONDS] * (candidate_sources.QUERY_DAY_PASSES - 1)


def test_query_waits_out_a_maintenance_burst_of_5xx():
    burst = [Reply(502) for _ in rules.RETRY_DELAYS] + [Reply(502)]
    session = QuerySession([*burst, page(1, 101)])
    sleeps = []
    games, _summary = query(session, sleeps)
    assert [game["appid"] for game in games] == [101]
    assert sleeps == [*rules.RETRY_DELAYS, rules.STEAM_OUTAGE_PAUSES[0]]


def test_query_gives_up_after_the_whole_outage_budget():
    attempts = (len(rules.RETRY_DELAYS) + 1) * (len(rules.STEAM_OUTAGE_PAUSES) + 1)
    session = QuerySession([Reply(503) for _ in range(attempts)])
    sleeps = []
    with pytest.raises(RuntimeError, match=r"Steam Query failed for 2026-12-31 at offset 0: HTTP 503") as caught:
        query(session, sleeps)
    assert len(session.offsets) == attempts
    assert sum(sleeps) == (sum(rules.RETRY_DELAYS) * (len(rules.STEAM_OUTAGE_PAUSES) + 1)
                           + sum(rules.STEAM_OUTAGE_PAUSES))
    assert caught.value.__cause__ is None


def test_query_rate_limit_keeps_its_own_backoff_without_an_outage_wait():
    session = QuerySession([Reply(429) for _ in range(len(rules.RATE_LIMIT_DELAYS) + 1)])
    sleeps = []
    with pytest.raises(RuntimeError, match="HTTP 429"):
        query(session, sleeps)
    assert sleeps == list(rules.RATE_LIMIT_DELAYS)


def test_query_non_retryable_error_is_still_immediate():
    session = QuerySession([Reply(403)])
    sleeps = []
    with pytest.raises(RuntimeError, match=r"Steam Query failed for 2026-12-31 at offset 0: HTTP 403"):
        query(session, sleeps)
    assert sleeps == [] and session.offsets == [0]


def test_outage_wait_keeps_the_exhausted_retry_message():
    calls = []

    def send():
        calls.append(1)
        raise transport.TemporaryLookupError("HTTP 502")

    sleeps = []
    with pytest.raises(RuntimeError, match=r"Temporary lookup exhausted retries \(HTTP 502\); priority cursor unchanged"):
        transport.through_steam_outage(send, sleep=sleeps.append)
    assert len(calls) == len(rules.STEAM_OUTAGE_PAUSES) + 1
    assert sleeps == list(rules.STEAM_OUTAGE_PAUSES)


def test_outage_wait_returns_the_first_good_response():
    good = Reply(200)
    outcomes = [transport.TemporaryLookupError("temporary network failure"),
                transport.TemporaryLookupError("HTTP 504"), good]

    def send():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sleeps = []
    assert transport.through_steam_outage(send, sleep=sleeps.append) is good
    assert sleeps == list(rules.STEAM_OUTAGE_PAUSES[:2])


def test_outage_wait_never_stretches_a_rate_limit():
    def send():
        raise transport.TemporaryLookupError("HTTP 429")

    sleeps = []
    with pytest.raises(transport.TemporaryLookupError, match="HTTP 429"):
        transport.through_steam_outage(send, sleep=sleeps.append)
    assert sleeps == []


class PrefilterSession:
    def __init__(self, replies):
        self.replies = list(replies)
        self.headers = {}
        self.calls = 0

    def get(self, url, **options):
        self.calls += 1
        return self.replies.pop(0)

    post = get


def test_vanity_lookup_waits_out_maintenance(monkeypatch):
    sleeps = []
    monkeypatch.setattr(follower_prefilter, "time", SimpleNamespace(sleep=sleeps.append))
    burst = [Reply(502) for _ in range(len(rules.RETRY_DELAYS) + 1)]
    found = Reply(200, {"response": {"success": 1, "steamid": str(rules.GROUP_BASE + 42)}})
    session = PrefilterSession([*burst, found])
    assert follower_prefilter._group_id(session, "private-key", 7) == 42
    assert sleeps == [*rules.RETRY_DELAYS, rules.STEAM_OUTAGE_PAUSES[0]]


def test_third_party_bulk_keeps_the_short_retry_burst(monkeypatch):
    sleeps = []
    monkeypatch.setattr(follower_prefilter, "time", SimpleNamespace(sleep=sleeps.append))
    session = PrefilterSession([Reply(502) for _ in range(len(rules.RETRY_DELAYS) + 1)])
    with pytest.raises(RuntimeError, match=r"Temporary lookup exhausted retries \(HTTP 502\)"):
        follower_prefilter._bulk_counts(session, [42])
    assert sleeps == list(rules.RETRY_DELAYS)
