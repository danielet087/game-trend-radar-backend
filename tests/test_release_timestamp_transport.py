"""Offline contracts for the scheduled-timestamp enrichment transport."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
import requests

from radar_backend.adapters import steam_release_timestamps as transport


STAMP = 1790006400


class Clock:
    def __init__(self, now=100.0):
        self.now = now
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


class Logger:
    def __init__(self):
        self.messages = []

    def warning(self, message, *args):
        self.messages.append(("warning", message % args))

    def info(self, message, *args):
        self.messages.append(("info", message % args))


class Response:
    def __init__(self, payload=None, *, status=200, error=None):
        self.status_code = status
        self.payload = payload
        self.error = error
        self.json_calls = 0
        self.status_checks = 0

    def raise_for_status(self):
        self.status_checks += 1
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        self.json_calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


class Session:
    def __init__(self, responses, *, clock=None, durations=()):
        self.responses = list(responses)
        self.clock = clock
        self.durations = iter(durations)
        self.calls = []
        self.starts = []

    def get(self, url, *, params, timeout):
        self.calls.append((url, params, timeout))
        if self.clock is not None:
            self.starts.append(self.clock.now)
            self.clock.now += next(self.durations, 0)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response_with(*items):
    return Response({"response": {"store_items": list(items)}})


def released(appid, stamp=STAMP, **release_fields):
    return {"appid": appid, "release": {"steam_release_date": stamp, **release_fields}}


def lookup(session, appids, **kwargs):
    clock = Clock()
    logger = Logger()
    ports = {"sleep": clock.sleep, "monotonic": clock.monotonic, "logger": logger}
    ports.update(kwargs)
    return transport.fetch_release_timestamps(session, appids, **ports)


def test_payload_sorts_deduplicates_coerces_and_limits_to_positive_ids():
    session = Session([response_with(released(1), released(2), released(3))])
    result = lookup(session, [3, "2", 3, 2.8, -4, 0, True, False])
    assert list(result) == [1, 2, 3]
    url, params, timeout = session.calls[0]
    assert url == transport.STORE_BROWSE_URL
    assert timeout == 25
    assert params == {
        "input_json": '{"ids":[{"appid":1},{"appid":2},{"appid":3}],'
        '"context":{"country_code":"TW","language":"english","steam_realm":1},'
        '"data_request":{"include_release":true}}',
    }


def test_country_and_url_are_preserved_in_request_and_provenance():
    session = Session([response_with(released(9, is_coming_soon=False))])
    result = lookup(session, [9], country="HK", url="https://offline.invalid/browse")
    assert json.loads(session.calls[0][1]["input_json"])["context"]["country_code"] == "HK"
    assert session.calls[0][0] == "https://offline.invalid/browse"
    assert result == {9: {
        "steam_release_date": STAMP,
        "release_time_source": "https://offline.invalid/browse",
        "is_coming_soon": False,
    }}


@pytest.mark.parametrize("bad_id, error", [("bad", ValueError), (None, TypeError), ({}, TypeError)])
def test_invalid_input_id_raises_before_any_http(bad_id, error):
    session = Session([])
    with pytest.raises(error):
        lookup(session, [1, bad_id])
    assert session.calls == []


def test_coercion_preserves_the_original_two_int_calls():
    class Input:
        def __init__(self):
            self.calls = 0

        def __int__(self):
            self.calls += 1
            return 3 if self.calls == 1 else 4

    appid = Input()
    session = Session([response_with(released(4))])
    assert list(lookup(session, [appid])) == [4]
    assert appid.calls == 2


def test_empty_or_nonpositive_inputs_do_not_log_or_use_clock():
    session = Session([])
    logger = Logger()

    def forbidden(*args):
        raise AssertionError("empty lookup used time")

    assert lookup(session, [0, -2, False], logger=logger, sleep=forbidden,
                  monotonic=forbidden) == {}
    assert logger.messages == []
    assert session.calls == []


@pytest.mark.parametrize("batch_size", [0, -8, 1])
def test_nonpositive_batch_sizes_still_process_one_app_per_batch(batch_size):
    session = Session([response_with(released(1)), response_with(released(2))])
    result = lookup(session, [2, 1], batch_size=batch_size)
    assert list(result) == [1, 2]
    assert [json.loads(call[1]["input_json"])["ids"] for call in session.calls] == [
        [{"appid": 1}], [{"appid": 2}],
    ]


def test_default_batch_has_35_apps_and_progress_counts_processed_apps():
    session = Session([response_with(), response_with(released(36))])
    logger = Logger()
    assert list(lookup(session, list(range(1, 37)), logger=logger)) == [36]
    assert [len(json.loads(call[1]["input_json"])["ids"]) for call in session.calls] == [35, 1]
    assert logger.messages == [
        ("info", "Steam Store Browse timestamps: 35/36 apps processed"),
        ("info", "Steam Store Browse timestamps: 36/36 apps processed"),
    ]


def test_first_request_is_immediate_and_later_requests_are_paced_from_start():
    clock = Clock()
    session = Session([response_with(), response_with(), response_with()],
                      clock=clock, durations=[0.75, 3.0])
    lookup(session, [1, 2, 3], batch_size=1, sleep=clock.sleep, monotonic=clock.monotonic)
    assert session.starts == [100.0, 102.0, 105.0]
    assert clock.sleeps == [1.25, 0.0]


def test_first_start_at_zero_still_paces_next_request():
    clock = Clock(now=0)
    session = Session([response_with(), response_with()], clock=clock)
    lookup(session, [1, 2], batch_size=1, sleep=clock.sleep, monotonic=clock.monotonic)
    assert session.starts == [0, 2.0]
    assert clock.sleeps == [2.0]


@pytest.mark.parametrize("interval", [0, -2.5])
def test_nonpositive_interval_produces_only_zero_pacing_sleeps(interval):
    clock = Clock()
    session = Session([response_with(), response_with()], clock=clock)
    lookup(session, [1, 2], batch_size=1, request_interval=interval,
           sleep=clock.sleep, monotonic=clock.monotonic)
    assert clock.sleeps == [0.0]


def test_429_uses_three_attempts_including_final_cooldown_and_returns_partial():
    clock = Clock()
    logger = Logger()
    responses = [Response(status=429), Response(status=429), Response(status=429),
                 response_with(released(2))]
    session = Session(responses, clock=clock)
    assert list(lookup(session, [1, 2], batch_size=1, sleep=clock.sleep,
                       monotonic=clock.monotonic, logger=logger)) == [2]
    assert session.starts == [100.0, 115.0, 145.0, 190.0]
    assert clock.sleeps == [15, 0.0, 30, 0.0, 45, 0.0]
    assert all(response.status_checks == response.json_calls == 0 for response in responses[:3])
    assert logger.messages == [
        ("info", "Steam Store Browse timestamps: 1/2 apps processed"),
        ("info", "Steam Store Browse timestamps: 2/2 apps processed"),
    ]


def test_request_failures_retry_three_times_with_original_log_and_backoff():
    clock = Clock()
    logger = Logger()
    session = Session([requests.ConnectionError("offline")] * 3, clock=clock)
    assert lookup(session, [1], sleep=clock.sleep, monotonic=clock.monotonic, logger=logger) == {}
    assert session.starts == [100.0, 105.0, 115.0]
    assert clock.sleeps == [5, 0.0, 10, 0.0]
    assert logger.messages == [
        ("warning", "Store Browse date lookup failed: offline"),
        ("warning", "Store Browse date lookup failed: offline"),
        ("warning", "Store Browse date lookup failed: offline"),
        ("info", "Steam Store Browse timestamps: 1/1 apps processed"),
    ]


def test_retry_pacing_uses_http_start_after_backoff_not_after_response():
    clock = Clock()
    session = Session([requests.ConnectionError("offline"), response_with(released(1))],
                      clock=clock, durations=[4])
    assert list(lookup(session, [1], request_interval=10, sleep=clock.sleep,
                       monotonic=clock.monotonic)) == [1]
    assert session.starts == [100.0, 110.0]
    assert clock.sleeps == [5, 1.0]


@pytest.mark.parametrize("failure", [
    Response(status=500),
    Response(error=ValueError("invalid JSON")),
    Response(error=TypeError("invalid body")),
    Response(error=AttributeError("invalid response")),
    Response([]),
    Response({"response": [1]}),
    Response({"response": {"store_items": 1}}),
])
def test_http_and_parse_errors_can_recover_on_a_later_attempt(failure):
    session = Session([failure, response_with(released(1))])
    logger = Logger()
    assert list(lookup(session, [1], logger=logger)) == [1]
    assert len(session.calls) == 2
    assert [level for level, _ in logger.messages] == ["warning", "info"]


def test_all_failed_batch_keeps_prior_and_later_successful_batches():
    failures = [requests.ConnectionError("offline")] * 3
    session = Session([response_with(released(1)), *failures, response_with(released(3))])
    assert list(lookup(session, [1, 2, 3], batch_size=1)) == [1, 3]
    assert len(session.calls) == 5


@pytest.mark.parametrize("payload", [{}, {"response": None}, {"response": {}},
                                      {"response": {"store_items": []}},
                                      {"response": {"store_items": "bad"}}])
def test_empty_or_iterable_without_valid_items_is_success_without_retry(payload):
    session = Session([Response(payload)])
    assert lookup(session, [1]) == {}
    assert len(session.calls) == 1


@pytest.mark.parametrize("stamp", [
    None, True, False, [], {}, object(), "", "not a timestamp", "1790006400.0",
    0, -1, 1262303999, 4133980800, 10**100, float("nan"), float("inf"),
])
def test_absent_malformed_and_implausible_timestamps_are_skipped(stamp):
    session = Session([response_with(released(1, stamp))])
    assert lookup(session, [1]) == {}
    assert len(session.calls) == 1


@pytest.mark.parametrize("stamp, expected", [
    (STAMP, STAMP), (float(STAMP) + 0.9, STAMP), (str(STAMP), STAMP),
    (f" {STAMP} ", STAMP), (1262304000, 1262304000), (4133980799, 4133980799),
])
def test_integer_coercion_and_inclusive_2010_through_2100_year_gate(stamp, expected):
    session = Session([response_with(released(1, stamp, is_coming_soon="unknown"))])
    assert lookup(session, [1]) == {1: {
        "steam_release_date": expected,
        "release_time_source": transport.STORE_BROWSE_URL,
        "is_coming_soon": "unknown",
    }}


def test_malformed_rows_and_unrequested_apps_do_not_leak_into_result():
    session = Session([response_with(
        None, "bad", {}, {"appid": "1", "release": {"steam_release_date": STAMP}},
        {"appid": 1, "release": []}, released(2),
        {"appid": 1, "release": {"original_release_date": STAMP}},
        released(1),
    )])
    assert lookup(session, [1]) == {1: {
        "steam_release_date": STAMP, "release_time_source": transport.STORE_BROWSE_URL,
        "is_coming_soon": None,
    }}


def test_response_bool_appid_retains_existing_isinstance_int_behavior():
    session = Session([response_with(released(True))])
    result = lookup(session, [1])
    assert list(result) == [True]
    assert result[1]["steam_release_date"] == STAMP


def test_later_valid_duplicate_overwrites_but_invalid_duplicate_keeps_value():
    session = Session([response_with(released(1), released(1, STAMP + 1), released(1, 0))])
    assert lookup(session, [1])[1]["steam_release_date"] == STAMP + 1


@pytest.mark.parametrize("error", [OverflowError, OSError, ValueError, TypeError])
def test_timestamp_parser_errors_skip_only_the_affected_row(error):
    calls = []

    def fromtimestamp(seconds, *, tz):
        calls.append((seconds, tz))
        if seconds == STAMP:
            raise error("invalid timestamp")
        return datetime.fromtimestamp(seconds, tz=tz)

    session = Session([response_with(released(1), released(2, STAMP + 1))])
    assert list(lookup(session, [1, 2], fromtimestamp=fromtimestamp)) == [2]
    assert calls == [(STAMP, timezone.utc), (STAMP + 1, timezone.utc)]


def test_partial_rows_written_before_parse_failure_survive_exhausted_retries():
    class BrokenItem(dict):
        def get(self, key, default=None):
            raise ValueError("broken item")

    session = Session([response_with(released(1), BrokenItem())] * 3)
    assert list(lookup(session, [1])) == [1]
    assert len(session.calls) == 3


def test_unexpected_error_propagates_without_retry():
    session = Session([RuntimeError("unexpected")])
    with pytest.raises(RuntimeError, match="unexpected"):
        lookup(session, [1])
    assert len(session.calls) == 1


def test_injected_http_exception_type_is_honored():
    class OfflineError(Exception):
        pass

    session = Session([OfflineError("offline"), response_with(released(1))])
    assert list(lookup(session, [1], requests_module=SimpleNamespace(RequestException=OfflineError))) == [1]
    assert len(session.calls) == 2


def test_default_ports_are_resolved_after_import(monkeypatch):
    clock = Clock()
    logger = Logger()
    parser_calls = []

    class OfflineError(Exception):
        pass

    def fromtimestamp(seconds, *, tz):
        parser_calls.append((seconds, tz))
        return datetime.fromtimestamp(seconds, tz=tz)

    monkeypatch.setattr(transport, "time", SimpleNamespace(sleep=clock.sleep, monotonic=clock.monotonic))
    monkeypatch.setattr(transport, "LOG", logger)
    monkeypatch.setattr(transport, "requests", SimpleNamespace(RequestException=OfflineError))
    monkeypatch.setattr(transport, "datetime", SimpleNamespace(fromtimestamp=fromtimestamp))
    session = Session([OfflineError("offline"), response_with(released(1))], clock=clock)
    assert list(transport.fetch_release_timestamps(session, [1])) == [1]
    assert clock.sleeps == [5, 0.0]
    assert parser_calls == [(STAMP, timezone.utc)]
    assert logger.messages[0] == ("warning", "Store Browse date lookup failed: offline")
