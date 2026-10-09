"""Offline contracts for parsing, staged windows, and bounded prescreen HTTP."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.adapters import steam_follower_prefilter as transport
from radar_backend.application import follower_prefilter as application
from radar_backend.domain import follower_prefilter as rules


class Response:
    def __init__(self, code=200, payload=None, *, headers=None, error=None):
        self.status_code = code
        self.payload = payload
        self.headers = {} if headers is None else headers
        self.error = error

    def json(self):
        if self.error is not None:
            raise self.error
        return self.payload


class ScriptedSession:
    def __init__(self, outcomes=()):
        self.outcomes = iter(outcomes)
        self.calls = []
        self.headers = {}

    def _take(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get(self, url, **kwargs):
        return self._take("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._take("POST", url, kwargs)


def rows(*ids):
    return [{"appid": appid, "name": f"Game {appid}"} for appid in ids]


def scan(catalog, state, *, groups=None, counts=None, client=None, **kwargs):
    client = SimpleNamespace(headers={}) if client is None else client
    ports = {
        "session_factory": lambda: client,
        "group_id": lambda _session, _key, appid: (groups or {}).get(appid),
        "bulk_counts": lambda _session, _ids: {} if counts is None else counts,
        "clock_stamp": lambda: "frozen-stamp",
        "sleep": lambda _delay: None,
    }
    ports.update(kwargs)
    return application.scan_batch(
        catalog, state, steam_api_key="private-test", initial_index=0,
        **ports,
    )


@pytest.mark.parametrize("success", [1, True, 1.0])
@pytest.mark.parametrize("steamid, expected", [
    (rules.GROUP_BASE + 7, 7),
    (str(rules.GROUP_BASE + 7), 7),
    (f" {rules.GROUP_BASE + 7} ", 7),
    (rules.GROUP_BASE, None),
    (rules.GROUP_BASE - 1, None),
    (True, None),
])
def test_group_success_numeric_coercion_and_base_boundary(success, steamid, expected):
    assert rules.parse_group_id({"response": {
        "success": success, "steamid": steamid,
    }}) == expected


@pytest.mark.parametrize("success", [0, False, None, "1", 2, [], {}])
def test_absent_group_does_not_require_a_steamid(success):
    assert rules.parse_group_id({"response": {"success": success}}) is None


@pytest.mark.parametrize("payload", [
    None, [], {}, {"response": {"success": 1}},
    {"response": {"success": 1, "steamid": "bad"}},
    {"response": {"success": 1, "steamid": None}},
])
def test_malformed_group_payload_keeps_cursor_failure(payload):
    with pytest.raises(RuntimeError, match="Unexpected Steam vanity response; priority cursor unchanged"):
        rules.parse_group_id(payload)


@pytest.mark.parametrize("item", [None, [], 7, "bad"])
def test_group_unexpected_item_attribute_error_stays_visible(item):
    with pytest.raises(AttributeError):
        rules.parse_group_id({"response": item})


def test_group_custom_base_and_uncaught_overflow():
    assert rules.parse_group_id({"response": {"success": 1, "steamid": "43"}}, group_base=40) == 3
    with pytest.raises(OverflowError):
        rules.parse_group_id({"response": {"success": 1, "steamid": float("inf")}})


@pytest.mark.parametrize("gid", [7, "7", "007", "+7", " 7 ", "７"])
@pytest.mark.parametrize("members", [0, 3999, 4000, 5000])
def test_bulk_measured_numeric_ids_preserve_threshold_counts(gid, members):
    assert rules.parse_bulk_counts({"data": [{"id": gid, "members": members}]}, [7]) == {7: members}


@pytest.mark.parametrize("item", [
    None, 7, [], "text", {},
    {"id": True, "members": 1}, {"id": False, "members": 1},
    {"id": 7.0, "members": 1}, {"id": None, "members": 1},
    {"id": "bad", "members": 1}, {"id": "7.0", "members": 1},
    {"id": 7, "members": True}, {"id": 7, "members": False},
    {"id": 7, "members": "4000"}, {"id": 7, "members": 4000.0},
    {"id": 7, "members": None}, {"id": 7, "members": -1},
    {"id": 8, "members": 4000},
])
def test_bulk_nonempty_without_any_valid_measurement_fails(item):
    with pytest.raises(RuntimeError, match="Third-party bulk response malformed; priority cursor unchanged"):
        rules.parse_bulk_counts({"data": [item]}, [7])


@pytest.mark.parametrize("payload", [None, [], {}, {"data": None}, {"data": {}}, {"data": "bad"}])
def test_bulk_invalid_envelope_fails(payload):
    with pytest.raises(RuntimeError, match="Third-party bulk response malformed"):
        rules.parse_bulk_counts(payload, [7])


def test_bulk_empty_partial_and_duplicate_records_preserve_provider_order():
    assert rules.parse_bulk_counts({"data": [], "notFound": [7]}, [7]) == {}
    assert rules.parse_bulk_counts({"data": [
        None, {"id": True, "members": 99},
        {"id": "7", "members": 4000}, {"id": 7, "members": 0},
    ]}, [7]) == {7: 0}
    assert rules.parse_bulk_counts({"data": [{"id": "1", "members": 4}]}, [True]) == {1: 4}


@pytest.mark.parametrize("maximum, expected", [(0, [1]), (-1, [1]), (1, [1]), (2, [1, 3]), (50, [1, 3])])
def test_pending_queue_limits_keep_catalog_row_identity(maximum, expected):
    catalog = rows(1, 2, 3, 4, 5)
    saved = {"games": {"1": {"priority": True}, "2": {"priority": False},
                       "3": {"priority": "truthy"}, "4": {"priority": True}}}
    cache = {"4": None}
    before = deepcopy((catalog, saved, cache))
    pending = rules.pending_priorities(catalog, saved, cache, min_start_index=0, max_candidates=maximum)
    assert [item["appid"] for item in pending] == expected
    for item in pending:
        assert item is catalog[item["appid"] - 1]
    assert (catalog, saved, cache) == before


@pytest.mark.parametrize("start, expected", [(1, [3]), (-2, [3]), (4, []), (10, [])])
def test_pending_cursor_slice_and_string_cache_keys(start, expected):
    catalog = rows(1, 2, 3, 4)
    saved = {"games": {str(appid): {"priority": True} for appid in range(1, 5)}}
    assert [item["appid"] for item in rules.pending_priorities(
        catalog, saved, {"2": {}, "4": {}, 3: {}}, min_start_index=start,
    )] == expected


@pytest.mark.parametrize("games", [None, {}, []])
def test_pending_empty_saved_returns_no_candidates(games):
    assert rules.pending_priorities(rows(1), {"games": games}, {}, min_start_index=0) == []


@pytest.mark.parametrize("state, key, limit, initial, error, message", [
    ({}, "", 1, 0, RuntimeError, "STEAM_WEB_API_KEY required"),
    ({"version": 2}, "", 0, 0, RuntimeError, "STEAM_WEB_API_KEY required"),
    ({"version": 2}, "key", 0, 0, ValueError, "batch size must be positive"),
    ({"version": 2}, "key", 1, 0, RuntimeError, "Unknown prefilter version"),
    ({"version": "1"}, "key", 1, 0, RuntimeError, "Unknown prefilter version"),
    ({"next_index": -1}, "key", 1, 0, RuntimeError, "cursor out of range"),
    ({"next_index": 2}, "key", 1, 0, RuntimeError, "cursor out of range"),
    ({"next_index": 0}, "key", 1, 1, RuntimeError, "cursor out of range"),
    ({"next_index": "bad"}, "key", 1, 0, ValueError, "invalid literal"),
    ({"next_index": None}, "key", 1, 0, TypeError, "int"),
])
def test_scan_validation_order_and_no_ports_before_valid_window(state, key, limit, initial, error, message):
    before = deepcopy(state)
    forbidden = Mock(side_effect=AssertionError("port must not run"))
    with pytest.raises(error, match=message):
        application.scan_batch(rows(1), state, steam_api_key=key, initial_index=initial, limit=limit,
                               session_factory=forbidden, group_id=forbidden, bulk_counts=forbidden,
                               clock_stamp=forbidden, sleep=forbidden)
    assert state == before
    forbidden.assert_not_called()


def test_scan_empty_window_only_updates_cursor_and_complete_without_session():
    state = {"version": True, "next_index": "1", "games": {"1": {"priority": True}}}
    forbidden = Mock(side_effect=AssertionError("empty window must not use ports"))
    result = application.scan_batch(rows(1), state, steam_api_key="key", initial_index=0,
                                   session_factory=forbidden, group_id=forbidden, bulk_counts=forbidden,
                                   clock_stamp=forbidden, sleep=forbidden)
    assert result == {"start_index": 1, "next_index": 1, "screened": 0, "priority": 0, "missing": 0, "complete": True}
    assert state == {"version": True, "next_index": 1, "complete": True, "games": {"1": {"priority": True}}}
    forbidden.assert_not_called()


@pytest.mark.parametrize("limit, error", [(0.5, ValueError), (1.5, TypeError), ("1", TypeError), (None, TypeError)])
def test_scan_original_limit_type_errors_stay_visible(limit, error):
    state = {}
    with pytest.raises(error):
        scan(rows(1, 2), state, limit=limit)
    assert state == {}


def test_scan_default_window_size_and_boolean_limit_are_unchanged():
    catalog = rows(*range(205))
    state = {"bulk_parser_version": 2}
    result = scan(catalog, state)
    assert result["screened"] == 200
    assert result["next_index"] == 200
    assert result["complete"] is False
    other = {"bulk_parser_version": 2}
    assert scan(catalog, other, limit=True)["screened"] == 1
    # min(stop, catalog length) historically accepts this float if the
    # catalog end supplies an integer slice boundary.
    assert scan(rows(1), {}, limit=1.5)["screened"] == 1


def test_scan_window_deduplicates_appids_and_groups_while_preserving_request_pacing():
    catalog = rows("1", 2, 1, 3, 4)
    state = {"version": 1, "next_index": 0, "games": {}}
    trace = []
    session = SimpleNamespace(headers={"User-Agent": "caller-agent"})
    def group(client, key, appid):
        assert client is session and key == "private-test"
        trace.append(("group", appid))
        return {1: 11, 2: 11, 3: None, 4: 14}[appid]
    def bulk(client, ids):
        assert client is session
        trace.append(("bulk", ids))
        return {11: 4000, 14: 0}
    result = scan(catalog, state, session=session, group_id=group, bulk_counts=bulk,
                  sleep=lambda delay: trace.append(("sleep", delay)))
    assert trace == [("group", 1), ("sleep", 0.5), ("group", 2), ("sleep", 0.5),
                     ("group", 1), ("sleep", 0.5), ("group", 3), ("sleep", 0.5),
                     ("group", 4), ("bulk", [11, 14])]
    assert result == {"start_index": 0, "next_index": 5, "screened": 5,
                      "priority": 2, "missing": 1, "complete": True}
    assert list(state["games"]) == ["1", "2", "3", "4"]
    assert state["games"]["3"] == {"third_party_followers": None, "group_short_id": None,
                                     "priority": False, "scheduling_band": "unresolved", "checked_at": "frozen-stamp"}
    assert state["games"]["4"]["scheduling_band"] == "measured"
    assert state["threshold"] == 4000 and state["bulk_parser_version"] == 2
    assert session.headers == {"User-Agent": "caller-agent"}
    assert catalog == rows("1", 2, 1, 3, 4)


def test_scan_explicit_threshold_current_clock_and_zero_interval_calls():
    state = {}
    delays = []
    clock = Mock(return_value="current-port-stamp")
    result = scan(rows(1, 2), state, groups={1: 1, 2: 2}, counts={1: 5, 2: 4},
                  priority_threshold=5, clock_stamp=clock, request_interval=0, sleep=delays.append)
    assert result["priority"] == 1
    assert state["threshold"] == 5
    assert state["games"]["1"]["checked_at"] == "current-port-stamp"
    assert delays == [0]
    clock.assert_called_once_with()


@pytest.mark.parametrize("provided", [False, True])
def test_scan_falsy_session_uses_factory_truthy_session_preserves_instance(provided):
    class Client:
        headers = {}
        def __bool__(self):
            return provided
    given, fallback = Client(), SimpleNamespace(headers={})
    factory = Mock(return_value=fallback)
    mapper = Mock(return_value=None)
    scan(rows(1), {}, session=given, session_factory=factory, group_id=mapper)
    assert mapper.call_args.args[0] is (given if provided else fallback)
    assert factory.call_count == (0 if provided else 1)


@pytest.mark.parametrize("stage", ["repair", "mapping", "bulk", "clock", "sleep"])
def test_scan_no_saved_mutations_until_repair_and_entire_new_window_succeed(stage):
    state = {"version": 1, "next_index": 0, "games": {
        "old": {"third_party_followers": None, "group_short_id": 99, "priority": True, "custom": []},
    }}
    before = deepcopy(state)
    reference = state["games"]["old"]
    def bulk(_client, ids):
        if (stage == "repair" and ids == [99]) or (stage == "bulk" and ids == [1, 2]):
            raise RuntimeError("selected failure")
        return {99: 8000, 1: 6000, 2: 5000}
    def mapper(_client, _key, appid):
        if stage == "mapping" and appid == 2:
            raise RuntimeError("selected failure")
        return appid
    stamps = iter(["repair-stamp", "window-stamp"])
    def stamp():
        value = next(stamps)
        if stage == "clock" and value == "window-stamp":
            raise RuntimeError("selected failure")
        return value
    def pause(_value):
        if stage == "sleep":
            raise RuntimeError("selected failure")
    with pytest.raises(RuntimeError, match="selected failure"):
        scan(rows(1, 2), state, group_id=mapper, bulk_counts=bulk, clock_stamp=stamp, sleep=pause)
    assert state == before
    assert state["games"]["old"] is reference


def test_scan_repairs_cached_null_groups_before_mapping_with_each_repair_stamp():
    old = {"third_party_followers": None, "group_short_id": 99, "priority": True, "custom": []}
    second = {"third_party_followers": None, "group_short_id": 99, "priority": True}
    unresolved = {"third_party_followers": None, "group_short_id": "not-int", "priority": True}
    measured = {"third_party_followers": 9, "priority": True, "custom": "preserved"}
    state = {"next_index": 1, "games": {"old": old, "copy": second, "missing": unresolved, "measured": measured}}
    trace = []
    times = iter(["repair-first", "repair-second", "window-final"])
    def bulk(_client, ids):
        trace.append(("bulk", ids))
        return {99: 3999} if ids == [99] else {2: 5000}
    def group(_client, _key, appid):
        trace.append(("group", appid))
        return appid
    result = scan(rows(1, 2), state, group_id=group, bulk_counts=bulk, clock_stamp=lambda: next(times))
    assert trace == [("bulk", [99]), ("group", 2), ("bulk", [2])]
    assert result["start_index"] == 1 and result["priority"] == 1
    assert state["games"]["old"]["checked_at"] == "repair-first"
    assert state["games"]["copy"]["checked_at"] == "repair-second"
    assert state["games"]["old"]["third_party_followers"] == 3999
    assert state["games"]["old"]["priority"] is False
    assert state["games"]["old"]["custom"] is old["custom"]
    assert old["priority"] is False and old["scheduling_band"] == "unresolved"
    assert state["games"]["missing"] is unresolved and unresolved["priority"] is False
    assert state["games"]["measured"] is measured and measured["priority"] is True
    assert state["updated_at"] == "window-final"


@pytest.mark.parametrize("count, expected_chunks", [(1, [1]), (200, [200]), (201, [200, 1]), (2000, [200] * 10)])
def test_scan_repair_chunk_size_and_two_thousand_group_cap(count, expected_chunks):
    state = {"games": {str(i): {"third_party_followers": None, "group_short_id": i}
                        for i in range(count)}}
    chunks = []
    def bulk(_client, ids):
        chunks.append(list(ids))
        return {}
    scan(rows(99999), state, bulk_counts=bulk)
    assert [len(chunk) for chunk in chunks[:-1]] == expected_chunks
    assert [gid for chunk in chunks[:-1] for gid in chunk] == list(range(count))
    assert chunks[-1] == []


def test_scan_oversized_unique_repair_blocks_all_requests_and_saved_mutation():
    state = {"games": {str(i): {"third_party_followers": None, "group_short_id": i, "priority": True}
                        for i in range(2001)}}
    before = deepcopy(state)
    forbidden = Mock(side_effect=AssertionError("lookup must not run"))
    with pytest.raises(RuntimeError, match="Unexpected old prescreen size; manual repair required"):
        scan(rows(1), state, group_id=forbidden, bulk_counts=forbidden, clock_stamp=forbidden)
    assert state == before
    forbidden.assert_not_called()


def test_scan_repair_duplicate_group_ids_do_not_count_toward_cap_and_bool_stays_integer():
    state = {"games": {str(i): {"third_party_followers": None, "group_short_id": True}
                        for i in range(2001)}}
    calls = []
    scan(rows(99999), state, bulk_counts=lambda _session, ids: calls.append(ids) or {})
    assert calls == [[True], []]


@pytest.mark.parametrize("parser_version", [2, 3, "2"])
def test_scan_current_parser_skips_legacy_repair(parser_version):
    state = {"bulk_parser_version": parser_version, "games": {
        "old": {"third_party_followers": None, "group_short_id": 99, "priority": True},
    }}
    calls = []
    scan(rows(1), state, bulk_counts=lambda _session, ids: calls.append(ids) or {})
    assert calls == [[]]
    assert state["games"]["old"]["priority"] is False


def test_scan_empty_groups_still_calls_bulk_port_then_records_unresolved():
    bulk = Mock(return_value={})
    state = {}
    result = scan(rows(1), state, bulk_counts=bulk)
    assert bulk.call_args.args[1] == []
    assert result["missing"] == 1
    assert state["games"]["1"]["third_party_followers"] is None


@pytest.mark.parametrize("bad_games, error", [(None, AttributeError), ([], AttributeError), ([1], AttributeError), ({"old": 1}, AttributeError)])
def test_scan_existing_malformed_games_errors_keep_original_commit_order(bad_games, error):
    state = {"bulk_parser_version": 2, "games": bad_games}
    before = deepcopy(state)
    with pytest.raises(error):
        scan(rows(1), state)
    assert state == before


@pytest.mark.parametrize("method, expected", [("GET", "GET"), ("POST", "POST"), ("get", "POST"), ("DELETE", "POST")])
def test_transport_method_dispatch_and_kwargs_preserve_response_identity(method, expected):
    response = Response(200)
    session = ScriptedSession([response])
    sleeper = Mock(side_effect=AssertionError("no retry"))
    result = transport._request_with_retries(session, method, "private-url", sleep=sleeper,
                                            params={"private": "key"}, timeout=15)
    assert result is response
    assert session.calls == [(expected, "private-url", {"params": {"private": "key"}, "timeout": 15})]
    sleeper.assert_not_called()


@pytest.mark.parametrize("name", ["sleep", "request_exception", "retryable_http", "retry_delays", "rate_limit_delays", "request_options"])
def test_transport_request_options_preserve_arbitrary_legacy_kwargs_without_port_collision(name):
    response = Response(200)
    session = ScriptedSession([response])
    value = object()
    options = {name: value, "timeout": 15}
    sleeper = Mock(side_effect=AssertionError("no retry"))
    assert transport._request_with_retries(
        session, "GET", "url", sleep=sleeper, request_options=options,
    ) is response
    assert session.calls == [("GET", "url", options)]
    assert session.calls[0][2][name] is value
    assert options == {name: value, "timeout": 15}
    sleeper.assert_not_called()


@pytest.mark.parametrize("code", [200, 201, 204, 302, 400, 401, 403, 404, 418, 501])
def test_transport_nonretryable_codes_return_immediately(code):
    response = Response(code)
    session = ScriptedSession([response])
    delays = []
    assert transport._request_with_retries(session, "GET", "url", sleep=delays.append) is response
    assert len(session.calls) == 1 and delays == []


@pytest.mark.parametrize("code", sorted(rules.RETRYABLE_HTTP))
def test_transport_temporary_failures_have_five_attempts_and_original_delay_schedule(code):
    session = ScriptedSession([Response(code) for _ in range(5)])
    delays = []
    with pytest.raises(RuntimeError, match=rf"Temporary lookup exhausted retries \(HTTP {code}\); priority cursor unchanged"):
        transport._request_with_retries(session, "GET", "url", sleep=delays.append)
    assert len(session.calls) == 5
    assert delays == list(rules.RATE_LIMIT_DELAYS if code == 429 else rules.RETRY_DELAYS)


@pytest.mark.parametrize("code", [429, 503])
@pytest.mark.parametrize("header, expected", [("1000", 120), ("21", 21), ("-9", None), ("bad", None), (None, None), (2.5, None)])
def test_transport_retry_after_numeric_floor_and_cap(code, header, expected):
    session = ScriptedSession([Response(code, headers={"Retry-After": header}), Response(200)])
    delays = []
    transport._request_with_retries(session, "GET", "url", sleep=delays.append)
    original = 15 if code == 429 else 4
    assert delays == [original if expected is None else max(original, expected)]


def test_transport_custom_exception_policy_and_none_response_use_current_ports():
    class Temporary(Exception):
        pass
    success = Response(200)
    session = ScriptedSession([Temporary("private value"), None, Response(425), success])
    delays = []
    assert transport._request_with_retries(
        session, "GET", "url", sleep=delays.append, request_exception=Temporary,
        retryable_http={425}, retry_delays=(1, 2, 3), rate_limit_delays=(9, 8, 7),
    ) is success
    assert delays == [1, 2, 3]


def test_transport_exhausted_network_error_message_does_not_include_credentials():
    class Temporary(Exception):
        pass
    session = ScriptedSession([Temporary("private-api-key") for _ in range(5)])
    delays = []
    with pytest.raises(RuntimeError) as caught:
        transport._request_with_retries(session, "GET", "https://private-api-key", sleep=delays.append,
                                        request_exception=Temporary, params={"key": "private-api-key"})
    assert str(caught.value) == "Temporary lookup exhausted retries (temporary network failure); priority cursor unchanged"
    assert len(session.calls) == 5 and delays == [4, 12, 30, 60]
    assert caught.value.__cause__ is None


def test_transport_http_error_remains_last_error_after_later_network_failures():
    class Temporary(Exception):
        pass
    session = ScriptedSession([Response(503), Temporary("private"), None])
    with pytest.raises(RuntimeError, match="HTTP 503"):
        transport._request_with_retries(session, "GET", "url", sleep=lambda _delay: None,
                                        request_exception=Temporary, retry_delays=(1, 2))


@pytest.mark.parametrize("failure", [ValueError("program error"), KeyError("key"), TypeError("type")])
def test_transport_only_injected_request_exception_is_caught(failure):
    session = ScriptedSession([failure])
    with pytest.raises(type(failure)) as caught:
        transport._request_with_retries(session, "GET", "url", sleep=Mock())
    assert caught.value is failure


def test_transport_zero_retry_budget_still_makes_one_request_without_sleep():
    session = ScriptedSession([Response(429)])
    sleeper = Mock(side_effect=AssertionError("zero budget"))
    with pytest.raises(RuntimeError, match="HTTP 429"):
        transport._request_with_retries(session, "GET", "url", sleep=sleeper, retry_delays=())
    assert len(session.calls) == 1
    sleeper.assert_not_called()


def test_transport_group_lookup_current_request_url_base_and_parser_ports():
    client = object()
    payload = {"response": {"success": 1, "steamid": "50"}}
    request = Mock(return_value=Response(200, payload))
    parser = Mock(return_value=10)
    assert transport._group_id(client, "private-key", 7, request=request, url="custom-vanity",
                               group_base=40, parser=parser) == 10
    request.assert_called_once_with(client, "GET", "custom-vanity",
                                    params={"key": "private-key", "vanityurl": "7", "url_type": 3}, timeout=15)
    parser.assert_called_once_with(payload, group_base=40)


@pytest.mark.parametrize("code", [400, 401, 403, 429, 500])
def test_transport_group_non200_rejects_before_parsing(code):
    parser = Mock(side_effect=AssertionError("must not parse"))
    with pytest.raises(RuntimeError, match=f"Steam vanity returned HTTP {code}; priority cursor unchanged"):
        transport._group_id(object(), "key", 7, request=Mock(return_value=Response(code)), parser=parser)
    parser.assert_not_called()


@pytest.mark.parametrize("error", [ValueError("json"), KeyError("json"), TypeError("json")])
def test_transport_group_json_errors_keep_exact_public_failure(error):
    with pytest.raises(RuntimeError, match="Unexpected Steam vanity response; priority cursor unchanged") as caught:
        transport._group_id(object(), "key", 7, request=Mock(return_value=Response(error=error)))
    assert caught.value.__cause__ is None


def test_transport_group_attribute_error_remains_uncaught():
    with pytest.raises(AttributeError):
        transport._group_id(object(), "key", 7, request=Mock(return_value=Response(payload={"response": None})))


def test_transport_bulk_empty_short_circuits_and_preserves_passed_ids_list():
    forbidden = Mock(side_effect=AssertionError("empty IDs must not request"))
    assert transport._bulk_counts(object(), [], request=forbidden, parser=forbidden) == {}
    forbidden.assert_not_called()
    client, ids, payload = object(), [7, 8], {"data": [{"id": "7", "members": 4000}]}
    request = Mock(return_value=Response(payload=payload))
    parser = Mock(return_value={7: 4000})
    result = transport._bulk_counts(client, ids, request=request, url="custom-bulk", parser=parser)
    request.assert_called_once_with(client, "POST", "custom-bulk", json={"ids": [7, 8], "limit": 2}, timeout=30)
    assert request.call_args.kwargs["json"]["ids"] is ids
    assert parser.call_args.args[0] is payload and parser.call_args.args[1] is ids
    assert result == {7: 4000}


@pytest.mark.parametrize("code", [400, 401, 403, 429, 500])
def test_transport_bulk_non200_rejects_before_parsing(code):
    parser = Mock(side_effect=AssertionError("must not parse"))
    with pytest.raises(RuntimeError, match=f"Third-party bulk returned HTTP {code}; priority cursor unchanged"):
        transport._bulk_counts(object(), [7], request=Mock(return_value=Response(code)), parser=parser)
    parser.assert_not_called()


@pytest.mark.parametrize("error", [ValueError("json"), TypeError("json")])
def test_transport_bulk_json_error_keeps_exact_failure(error):
    with pytest.raises(RuntimeError, match="Third-party bulk response malformed; priority cursor unchanged") as caught:
        transport._bulk_counts(object(), [7], request=Mock(return_value=Response(error=error)))
    assert caught.value.__cause__ is None


def test_transport_bulk_key_error_stays_uncaught():
    error = KeyError("json")
    with pytest.raises(KeyError) as caught:
        transport._bulk_counts(object(), [7], request=Mock(return_value=Response(error=error)))
    assert caught.value is error
