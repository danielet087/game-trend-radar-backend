"""Offline HTTP contracts for refreshing already published Chinese titles."""
import json
from types import SimpleNamespace

import pytest
import requests

from radar_backend.adapters import steam_published_titles as transport


class Logger:
    def __init__(self):
        self.messages = []

    def warning(self, message, *args):
        self.messages.append((message, args))


class Response:
    def __init__(self, payload=None, *, status=200, error=None):
        self.status_code = status
        self.payload = payload
        self.error = error
        self.checks = 0
        self.reads = 0

    def raise_for_status(self):
        self.checks += 1
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params, timeout):
        self.calls.append((url, params, timeout))
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(*rows):
    return Response({"response": {"store_items": list(rows)}})


def lookup(session, appids=(1,), language="tchinese", interval=2.0, **kwargs):
    sleeps = []
    logger = Logger()
    ports = {"sleep": sleeps.append, "logger": logger}
    ports.update(kwargs)
    result = transport.localized_batch(session, list(appids), language, interval, **ports)
    return result, sleeps, logger.messages


def test_request_retains_order_duplicates_types_and_exact_wire_body():
    ids = [3, 1, 3, "2", True, -9, 0, 2.5]
    session = Session([reply({"appid": 3, "name": "  名稱  "})])
    assert lookup(session, ids, language="schinese")[0] == {3: "名稱"}
    assert session.calls == [(
        transport.STORE,
        {"input_json": '{"ids":[{"appid":3},{"appid":1},{"appid":3},{"appid":"2"},'
         '{"appid":true},{"appid":-9},{"appid":0},{"appid":2.5}],'
         '"context":{"country_code":"TW","language":"schinese","steam_realm":1},'
         '"data_request":{"include_basic_info":true}}'},
        45,
    )]
    assert ids == [3, 1, 3, "2", True, -9, 0, 2.5]


def test_empty_batch_still_makes_one_lookup_without_sleep():
    session = Session([reply({"appid": 1, "name": "一"})])
    result, sleeps, messages = lookup(session, [])
    assert result == {} and sleeps == [] and messages == []
    assert json.loads(session.calls[0][1]["input_json"])["ids"] == []


@pytest.mark.parametrize("interval", [0, -1, 1.5, 100, None, object()])
def test_interval_is_not_used_inside_batch(interval):
    session = Session([reply({"appid": 1, "name": "名稱"})])
    assert lookup(session, interval=interval) == ({1: "名稱"}, [], [])


def test_custom_url_and_language_are_forwarded():
    session = Session([reply()])
    lookup(session, language="custom", url="https://offline.invalid/items")
    assert session.calls[0][0] == "https://offline.invalid/items"
    assert json.loads(session.calls[0][1]["input_json"])["context"] == {
        "country_code": "TW", "language": "custom", "steam_realm": 1,
    }


def test_nonserializable_requested_id_retries_without_any_http_then_chains_error():
    session = Session([])
    sleeps = []
    logger = Logger()
    with pytest.raises(RuntimeError, match="Failed Steam tchinese Store lookup") as failure:
        lookup(session, [object()], sleep=sleeps.append, logger=logger)
    assert session.calls == []
    assert isinstance(failure.value.__cause__, TypeError)
    assert sleeps == [5, 10, 15, 20]
    assert [entry[1][:2] for entry in logger.messages] == [("tchinese", n) for n in range(1, 6)]


@pytest.mark.parametrize("appid", [True, False, 1.0, "1", None])
def test_response_requires_exact_integer_type(appid):
    assert lookup(Session([reply({"appid": appid, "name": "一"})]), [1, 0])[0] == {}


def test_integer_subclass_response_id_is_rejected():
    class Subclass(int):
        pass

    assert lookup(Session([reply({"appid": Subclass(1), "name": "一"})]))[0] == {}


@pytest.mark.parametrize("requested", [[True], [1.0]])
def test_request_membership_retains_original_python_equality(requested):
    assert lookup(Session([reply({"appid": 1, "name": "一"})]), requested)[0] == {1: "一"}


@pytest.mark.parametrize("row", [None, [], "text", 7, {}, {"name": "一"},
                                 {"appid": 2, "name": "二"}])
def test_unrequested_and_nonobject_rows_are_skipped(row):
    assert lookup(Session([reply(row)]))[0] == {}


@pytest.mark.parametrize("name", [None, 1, True, [], {}, "", " \t\n "])
def test_name_must_be_a_nonblank_string(name):
    assert lookup(Session([reply({"appid": 1, "name": name})]))[0] == {}


@pytest.mark.parametrize("name", ["English fallback", "名" * 241, " \u3000簡體名稱\t "])
def test_transport_does_not_apply_title_selection_or_traditional_conversion(name):
    assert lookup(Session([reply({"appid": 1, "name": name})]))[0] == {1: name.strip()}


def test_duplicate_valid_rows_overwrite_without_reordering_keys_or_erasing_for_invalid_rows():
    rows = [{"appid": 2, "name": "二"}, {"appid": 1, "name": "初名"},
            {"appid": 2, "name": "後名"}, {"appid": 1, "name": " "},
            {"appid": 2, "name": None}]
    result = lookup(Session([reply(*rows)]), [1, 2])[0]
    assert list(result.items()) == [(2, "後名"), (1, "初名")]


@pytest.mark.parametrize("payload", [{}, {"response": None}, {"response": False},
                                     {"response": []}, {"response": {}},
                                     {"response": {"store_items": None}},
                                     {"response": {"store_items": False}},
                                     {"response": {"store_items": {}}},
                                     {"response": {"store_items": ""}}])
def test_falsy_response_and_rows_produce_empty_success(payload):
    assert lookup(Session([Response(payload)])) == ({}, [], [])


@pytest.mark.parametrize("rows", [{"appid": 1}, "not-list", 3, (1,)])
def test_truthy_nonlist_rows_retry_then_succeed(rows):
    session = Session([Response({"response": {"store_items": rows}}), reply()])
    result, sleeps, messages = lookup(session)
    assert result == {} and sleeps == [5]
    assert messages[0][0] == "Store lookup %s attempt %d/5: %s"
    assert messages[0][1][:2] == ("tchinese", 1)
    assert isinstance(messages[0][1][2], ValueError)
    assert str(messages[0][1][2]) == "Invalid Steam store_items"


@pytest.mark.parametrize("payload", [None, [], "text", 4, {"response": [1]},
                                     {"response": "text"}])
def test_attribute_errors_escape_without_retry_log_or_backoff(payload):
    session = Session([Response(payload)])
    sleeps = []
    logger = Logger()
    with pytest.raises(AttributeError):
        lookup(session, sleep=sleeps.append, logger=logger)
    assert len(session.calls) == 1
    assert sleeps == [] and logger.messages == []


def test_429_cools_down_after_every_attempt_including_fifth_and_fails_closed():
    responses = [Response(status=429) for _ in range(5)]
    session = Session(responses)
    sleeps = []
    logger = Logger()
    with pytest.raises(RuntimeError, match="Steam schinese rate limit persisted; no partial publish") as error:
        lookup(session, language="schinese", sleep=sleeps.append, logger=logger)
    assert error.value.__cause__ is None
    assert sleeps == [15, 30, 45, 60, 75]
    assert logger.messages == [("%s throttled; attempt %d/5", ("schinese", n)) for n in range(1, 6)]
    assert len(session.calls) == 5
    assert all(response.checks == response.reads == 0 for response in responses)
    assert len({call[1]["input_json"] for call in session.calls}) == 1


def test_success_after_throttle_returns_complete_batch():
    session = Session([Response(status=429), reply({"appid": 1, "name": "名稱"})])
    assert lookup(session) == ({1: "名稱"}, [15], [
        ("%s throttled; attempt %d/5", ("tchinese", 1)),
    ])


@pytest.mark.parametrize("error", [requests.ConnectionError("offline"),
                                  requests.Timeout("deadline"),
                                  ValueError("invalid-json"), TypeError("invalid-body")])
def test_caught_failures_have_five_attempts_original_error_logs_and_chained_failure(error):
    session = Session([error] * 5)
    sleeps = []
    logger = Logger()
    with pytest.raises(RuntimeError, match="Failed Steam tchinese Store lookup") as failure:
        lookup(session, sleep=sleeps.append, logger=logger)
    assert failure.value.__cause__ is error
    assert sleeps == [5, 10, 15, 20]
    assert len(session.calls) == 5
    assert logger.messages == [("Store lookup %s attempt %d/5: %s", ("tchinese", n, error))
                               for n in range(1, 6)]


def test_http_error_checks_status_and_does_not_parse_error_response():
    response = Response(status=503)
    session = Session([response, reply()])
    result, sleeps, messages = lookup(session)
    assert result == {} and sleeps == [5]
    assert response.checks == 1 and response.reads == 0
    assert isinstance(messages[0][1][2], requests.HTTPError)


def test_json_error_is_caught_after_status_check():
    error = ValueError("not-json")
    response = Response(error=error)
    session = Session([response, reply()])
    result, sleeps, messages = lookup(session)
    assert result == {} and sleeps == [5]
    assert response.checks == response.reads == 1
    assert messages[0][1][2] is error


def test_mixed_failures_count_attempts_together_and_final_throttle_uses_rate_limit_failure():
    error = requests.ConnectionError("offline")
    session = Session([error, Response(status=429), ValueError("bad"),
                       Response(status=429), Response(status=429)])
    sleeps = []
    with pytest.raises(RuntimeError, match="rate limit persisted") as failure:
        lookup(session, sleep=sleeps.append)
    assert failure.value.__cause__ is None
    assert sleeps == [5, 30, 15, 60, 75]


def test_final_exception_after_throttles_uses_chained_lookup_failure():
    error = TypeError("last")
    session = Session([Response(status=429) for _ in range(4)] + [error])
    sleeps = []
    with pytest.raises(RuntimeError, match="Failed Steam tchinese Store lookup") as failure:
        lookup(session, sleep=sleeps.append)
    assert failure.value.__cause__ is error
    assert sleeps == [15, 30, 45, 60]


def test_custom_requests_exception_port_is_used():
    class CustomRequestError(Exception):
        pass

    error = CustomRequestError("custom")
    session = Session([error, reply()])
    result, sleeps, messages = lookup(session, requests_module=SimpleNamespace(RequestException=CustomRequestError))
    assert result == {} and sleeps == [5]
    assert messages[0][1][2] is error


def test_defaults_are_resolved_at_call_time(monkeypatch):
    class CustomRequestError(Exception):
        pass

    sleeps = []
    logger = Logger()
    monkeypatch.setattr(transport.time, "sleep", sleeps.append)
    monkeypatch.setattr(transport, "LOG", logger)
    monkeypatch.setattr(transport, "requests", SimpleNamespace(RequestException=CustomRequestError))
    error = CustomRequestError("runtime-default")
    session = Session([error, reply()])
    assert transport.localized_batch(session, [1], "tchinese", 2) == {}
    assert sleeps == [5]
    assert logger.messages == [("Store lookup %s attempt %d/5: %s", ("tchinese", 1, error))]


def test_uncaught_request_error_propagates_without_sleep_or_log():
    error = AttributeError("missing method")
    session = Session([error])
    sleeps = []
    logger = Logger()
    with pytest.raises(AttributeError) as failure:
        lookup(session, sleep=sleeps.append, logger=logger)
    assert failure.value is error
    assert sleeps == [] and logger.messages == []


def test_payload_is_frozen_while_row_membership_reads_current_requested_list():
    ids = [1]

    class MutatingSession(Session):
        def get(self, url, *, params, timeout):
            ids.append(2)
            return super().get(url, params=params, timeout=timeout)

    session = MutatingSession([Response(status=429), reply({"appid": 2, "name": "二"})])
    sleeps = []
    assert transport.localized_batch(session, ids, "tchinese", 2, sleep=sleeps.append, logger=Logger()) == {2: "二"}
    # The request payload is built once, while row admission reads the caller's list.
    assert [json.loads(call[1]["input_json"])["ids"] for call in session.calls] == [[{"appid": 1}]] * 2
    assert sleeps == [15]
