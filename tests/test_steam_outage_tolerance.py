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


# The daily date check reads Store Browse (english and tchinese). With pauses
# a batch that ends on 5xx or a dropped connection waits out the outage.
from unittest.mock import Mock

import requests

from radar_backend.adapters import steam_localized_titles, steam_metadata

ITEM = {"appid": 101, "name": "Game 101"}


class BrowseSession:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def get(self, url, *, params, timeout):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def browse(replies, pauses):
    sleeps = []
    session = BrowseSession(replies)
    result = steam_metadata.fetch_metadata(
        session, [101], interval=0, sleep=sleeps.append, monotonic=lambda: 0.0,
        max_content_details=0, outage_pauses=pauses,
    )
    return result, [delay for delay in sleeps if delay], session


def items(*rows):
    return Reply(200, {"response": {"store_items": list(rows)}})


def test_store_browse_batch_waits_out_an_outage_when_asked():
    result, sleeps, session = browse([*(Reply(502) for _ in range(4)), items(ITEM)], (60, 120))
    assert result == {101: ITEM}
    assert sleeps == [5, 10, 15, 60] and session.calls == 5


def test_store_browse_counts_a_dropped_connection_as_an_outage():
    result, sleeps, _session = browse([*(requests.ConnectionError("reset") for _ in range(4)), items(ITEM)], (60,))
    assert result == {101: ITEM} and sleeps == [5, 10, 15, 60]


def test_store_browse_without_pauses_fails_as_before():
    with pytest.raises(RuntimeError, match="Steam Browse batch 1 failed"):
        browse([Reply(502) for _ in range(4)], ())


@pytest.mark.parametrize("code, expected", [(429, [20, 40, 60, 80]), (403, [5, 10, 15])])
def test_store_browse_rate_limit_or_client_error_is_not_an_outage(code, expected):
    sleeps = []
    session = BrowseSession([Reply(code) for _ in range(4)])
    with pytest.raises(RuntimeError, match="Steam Browse batch 1 failed"):
        steam_metadata.fetch_metadata(session, [101], interval=0, sleep=sleeps.append, monotonic=lambda: 0.0,
                                      max_content_details=0, outage_pauses=(60,))
    assert [delay for delay in sleeps if delay] == expected and session.calls == 4


def test_tw_titles_wait_out_an_outage_when_asked():
    sleeps = []
    session = BrowseSession([*(Reply(503) for _ in range(4)), items({"appid": 101, "name": "遊戲"})])
    names = steam_localized_titles.fetch_store_tw_names(
        session, [101], interval=0, sleep=sleeps.append, monotonic=lambda: 0.0, outage_pauses=(60,))
    assert names == {101: "遊戲"}
    assert [delay for delay in sleeps if delay] == [5, 10, 15, 60]
    with pytest.raises(RuntimeError, match="Steam TW title batch 1 failed"):
        steam_localized_titles.fetch_store_tw_names(
            BrowseSession([Reply(503) for _ in range(4)]), [101], interval=0, sleep=lambda _s: None,
            monotonic=lambda: 0.0)


def test_only_the_daily_candidate_ports_wait_out_maintenance(monkeypatch):
    from scripts import steam_candidate_pipeline as pipeline
    from radar_backend.adapters import steam_store

    metadata = Mock(return_value={})
    monkeypatch.setattr(steam_metadata, "fetch_metadata", metadata)
    candidate_sources.fetch_metadata(object(), [1])
    assert metadata.call_args.kwargs["outage_pauses"] == rules.STEAM_OUTAGE_PAUSES
    # Other Store Browse callers (release-date rechecks, content) keep the short retries.
    steam_store.fetch_metadata(object(), [1], interval=0)
    assert "outage_pauses" not in metadata.call_args.kwargs
    titles = Mock(return_value={})
    monkeypatch.setattr(candidate_sources, "_fetch_store_tw_names", titles)
    candidate_sources.fetch_store_tw_names(object(), [1], interval=0)
    assert titles.call_args.kwargs == {"outage_pauses": rules.STEAM_OUTAGE_PAUSES, "interval": 0}
    defaults = candidate_sources.CandidateSources()
    assert defaults.fetch_metadata is candidate_sources.fetch_metadata
    assert defaults.fetch_store_tw_names is candidate_sources.fetch_store_tw_names
    assert pipeline.fetch_metadata is candidate_sources.fetch_metadata
    assert pipeline.fetch_store_tw_names is candidate_sources.fetch_store_tw_names
