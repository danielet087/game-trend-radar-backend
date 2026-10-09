"""Offline contracts for raw Store titles, display fields and batch transport."""
from copy import deepcopy
import json
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from radar_backend.adapters import steam_localized_titles as adapter
from radar_backend.application import localized_titles as application
from radar_backend.domain import localized_titles as rules


@pytest.mark.parametrize("value,expected", [
    ("  中文 title  ", "中文 title"),
    ("\u3400", "\u3400"), ("\u9fff", "\u9fff"),
    ("\u33ff", None), ("\ua000", None), ("\U00020000", None),
    ("中" * 240, "中" * 240), ("中" * 241, None),
    ("Fable", None), ("  ", None), (None, None), (7, None), (True, None),
])
def test_actual_title_retains_han_range_and_length(value, expected):
    assert rules.actual_zh_tw_title(value) == expected


@pytest.mark.parametrize("value", [None, 0, True, [], {}, "", "  \n"])
def test_display_conversion_skips_non_text_and_blank(value):
    convert = Mock()
    assert rules.display_in_traditional(value, convert=convert) is None
    convert.assert_not_called()


def test_display_conversion_passes_trimmed_raw_once_and_propagates_failure():
    convert = Mock(return_value="繁體")
    assert rules.display_in_traditional("  简体  ", convert=convert) == "繁體"
    convert.assert_called_once_with("简体")
    failure = ValueError("conversion failed")
    with pytest.raises(ValueError, match="conversion failed"):
        rules.display_in_traditional("中", convert=Mock(side_effect=failure))


def test_han_can_be_replaced_without_changing_global_rules():
    latin = re.compile("English")
    assert rules.actual_zh_tw_title("English", han=latin) == "English"
    game = {"name_en": "English"}
    rules.add_traditional_display_names(game, display=lambda raw: "display", han=latin)
    assert game["name_en_traditional"] == "display"
    assert game["display_name"] == "English"


@pytest.mark.parametrize("fields,display,source", [
    ({"name_zh_tw": "针影裁梦", "name_zh_cn": "她在时间之外", "name_en": "English"},
     "針影裁夢", "tchinese"),
    ({"name_zh_cn": "她在时间之外", "name_en": "English"},
     "她在時間之外", "schinese_converted"),
    ({"name_en": "针影裁梦"}, "针影裁梦", "english"),
    ({"name": "Original"}, "Original", "english"),
    ({"appid": 13}, "Steam App 13", "english"),
    ({}, "Steam App None", "english"),
])
def test_display_priority_keeps_raw_names_and_language_flags(fields, display, source):
    game = {**fields, "language_support": {"tchinese": False, "schinese": True}}
    previous = deepcopy(game)
    adapter.add_traditional_display_names(game)
    assert game["display_name"] == display
    assert game["display_name_source"] == source
    assert {key: game[key] for key in previous} == previous


def test_display_removes_stale_converted_fields_and_preserves_other_metadata():
    game = {
        "name_zh_tw": "English", "name_zh_cn": None, "name_en": "English",
        "name_zh_tw_traditional": "stale", "name_zh_cn_traditional": "stale",
        "name_en_traditional": "stale", "extra": {"kept": True},
    }
    rules.add_traditional_display_names(game, display=Mock(side_effect=AssertionError))
    assert not any(key.endswith("_traditional") for key in game)
    assert game["extra"] == {"kept": True}
    assert game["display_name"] == "English"


def test_display_failure_keeps_earlier_converted_field_and_stale_later_field():
    game = {"name_zh_tw": "中文", "name_zh_cn": "简体", "name_en_traditional": "later"}
    convert = Mock(side_effect=["繁體", RuntimeError("second source")])
    with pytest.raises(RuntimeError, match="second source"):
        rules.add_traditional_display_names(game, display=convert)
    assert game["name_zh_tw_traditional"] == "繁體"
    assert game["name_en_traditional"] == "later"
    assert "display_name" not in game


def test_enrichment_counts_missing_store_names_and_preserves_reviewed_titles():
    rows = [
        {"appid": "1", "name": "Original", "name_zh_tw": "previous"},
        {"appid": 2, "name": "Original", "name_zh_tw": "已確認"},
        {"appid": 3, "name": "Original", "name_zh_tw": 42},
        {"appid": 4, "name": "Original"},
    ]
    identities = [id(row) for row in rows]
    stats = adapter.enrich_tw_names(rows, {1: "新的中文名", 2: "English", 4: "English"})
    assert stats == {"total": 4, "official_zh_tw": 3,
                     "english_fallback": 1, "store_not_returned": 1}
    assert [id(row) for row in rows] == identities
    assert rows[0]["name"] == "Original"
    assert rows[0]["name_zh_tw"] == "新的中文名"
    assert rows[1]["name_zh_tw"] == "已確認"
    assert rows[2]["name_zh_tw"] == 42
    assert rows[2]["display_name"] == "Original"


@pytest.mark.parametrize("fields,expected", [
    ({"name_en": "  Chosen  ", "name": "Fallback"}, "Chosen"),
    ({"name_en": "", "name": "  Fallback  "}, "Fallback"),
    ({"name_en": 7}, "7"),
    ({"name": None}, "None"),
    ({"name": 0}, "0"),
])
def test_enrichment_retains_english_selection_and_string_conversion(fields, expected):
    game = {"appid": 1, **fields}
    stats = application.enrich_tw_names([game], {}, display_names=lambda row: None)
    assert game["name_en"] == expected
    assert stats["english_fallback"] == 1


@pytest.mark.parametrize("game,exception", [
    ({"name": "English"}, KeyError),
    ({"appid": "bad", "name": "English"}, ValueError),
    ({"appid": None, "name": "English"}, TypeError),
    ({"appid": 1}, KeyError),
    ({"appid": 1, "name_en": "  ", "name": "Fallback"}, RuntimeError),
])
def test_enrichment_invalid_row_fails_before_display_and_mutation(game, exception):
    prior = deepcopy(game)
    select = Mock()
    display = Mock()
    with pytest.raises(exception):
        application.enrich_tw_names([game], {}, select_title=select, display_names=display)
    assert game == prior
    select.assert_not_called()
    display.assert_not_called()


def test_enrichment_callbacks_observe_order_and_prior_row_stays_mutated_on_failure():
    rows = [{"appid": 1, "name": "  First  "}, {"appid": 2, "name": "  Second  "}]
    events = []

    def select(value):
        events.append(("select", value, rows[len(events) // 2]["name_en"]))
        return "中文"

    def display(row):
        events.append(("display", row["appid"], row["name_zh_tw"]))
        if row["appid"] == 2:
            raise RuntimeError("display failed")
        row["custom_display"] = True

    with pytest.raises(RuntimeError, match="display failed"):
        application.enrich_tw_names(rows, {1: "one", 2: "two"},
                                    select_title=select, display_names=display)
    assert events == [("select", "one", "First"), ("display", 1, "中文"),
                      ("select", "two", "Second"), ("display", 2, "中文")]
    assert rows[0]["custom_display"] is True
    assert rows[1]["name_en"] == "Second"
    assert rows[1]["name_zh_tw"] == "中文"


def test_empty_enrichment_never_calls_operations():
    display = Mock()
    assert application.enrich_tw_names([], {}, display_names=display) == {
        "total": 0, "official_zh_tw": 0, "english_fallback": 0, "store_not_returned": 0,
    }
    display.assert_not_called()


def response(rows=(), *, status=200, data=None, error=None):
    result = Mock(status_code=status)
    result.raise_for_status.side_effect = error
    result.json.return_value = data if data is not None else {"response": {"store_items": rows}}
    return result


def transport(outcomes):
    session = SimpleNamespace(get=Mock(side_effect=outcomes))
    delays = []
    logger = Mock()
    options = {"sleep": delays.append, "monotonic": lambda: 0.0, "logger": logger}
    return session, delays, logger, options


def test_title_transport_uses_normalized_sorted_batches_and_exact_request_contract():
    session, delays, logger, options = transport([
        response([{"appid": 1, "name": "  English  "}, {"appid": 2, "name": "中文"}]),
        response([{"appid": 3, "name": "中" * 241}]),
    ])
    names = adapter.fetch_store_tw_names(session, [3, "2", 1, 2, 0, -1],
                                        batch_size=2, **options)
    assert names == {1: "English", 2: "中文", 3: "中" * 241}
    assert delays == [1.5, 1.5]
    for call, ids in zip(session.get.call_args_list, [[1, 2], [3]]):
        assert call.args == (adapter.STORE_URL,)
        assert call.kwargs == {"params": {"input_json": json.dumps({
            "ids": [{"appid": appid} for appid in ids],
            "context": {"country_code": "TW", "language": "tchinese", "steam_realm": 1},
            "data_request": {"include_basic_info": True},
        }, separators=(",", ":"))}, "timeout": 30}
    assert logger.info.call_args_list[0].args[1:] == (2, 3, 2)
    assert logger.info.call_args_list[1].args[1:] == (3, 3, 3)


def test_default_title_batch_stays_35():
    session, _, _, options = transport([response(), response()])
    adapter.fetch_store_tw_names(session, list(range(1, 37)), **options)
    batches = [json.loads(call.kwargs["params"]["input_json"])["ids"]
               for call in session.get.call_args_list]
    assert [len(batch) for batch in batches] == [35, 1]


def test_title_transport_filters_unrequested_and_invalid_rows_but_accepts_int_subclasses():
    rows = [None, [], "bad", {"appid": 3, "name": "unrequested"},
            {"appid": "1", "name": "wrong type"}, {"appid": 1, "name": None},
            {"appid": 1, "name": "  "}, {"appid": True, "name": "  bool id  "},
            {"appid": 2, "name": "first"}, {"appid": 2, "name": "last"}]
    session, _, _, options = transport([response(rows)])
    assert adapter.fetch_store_tw_names(session, [1, 2], **options) == {1: "bool id", 2: "last"}


def test_title_transport_429_retries_four_times_and_fails_closed():
    session, delays, logger, options = transport([response(status=429) for _ in range(4)])
    with pytest.raises(RuntimeError, match="batch 1 failed; do not publish incomplete"):
        adapter.fetch_store_tw_names(session, [1], **options)
    assert session.get.call_count == 4
    assert delays == [1.5, 20, 1.5, 40, 1.5, 60, 1.5, 80]
    assert logger.warning.call_count == 4
    logger.info.assert_not_called()


@pytest.mark.parametrize("failure", [requests.ConnectionError("offline"),
                                     ValueError("bad JSON"), TypeError("bad value")])
def test_title_transport_retryable_failures_keep_attempt_delays(failure):
    session, delays, logger, options = transport([failure] * 4)
    with pytest.raises(RuntimeError, match="batch 1 failed"):
        adapter.fetch_store_tw_names(session, [1], **options)
    assert session.get.call_count == 4
    assert delays == [1.5, 5, 1.5, 10, 1.5, 15, 1.5]
    assert [call.args[1] for call in logger.warning.call_args_list] == [1, 2, 3, 4]


def test_title_transport_429_then_http_failure_then_success_is_one_batch():
    session, delays, logger, options = transport([
        response(status=429), response(error=requests.HTTPError("503")),
        response([{"appid": 1, "name": "中文"}]),
    ])
    assert adapter.fetch_store_tw_names(session, [1], **options) == {1: "中文"}
    assert delays == [1.5, 20, 1.5, 10, 1.5]
    assert logger.info.call_count == 1


@pytest.mark.parametrize("data", [{}, {"response": None}, {"response": {}}, {"response": []},
                                   {"response": {"store_items": None}},
                                   {"response": {"store_items": []}}])
def test_title_transport_empty_response_remains_success(data):
    session, delays, logger, options = transport([response(data=data)])
    assert adapter.fetch_store_tw_names(session, [1], **options) == {}
    assert delays == [1.5]
    assert logger.info.call_count == 1


@pytest.mark.parametrize("data", [[], 0, {"response": ["bad"]}])
def test_title_transport_attribute_errors_do_not_gain_a_retry(data):
    session, _, logger, options = transport([response(data=data)])
    with pytest.raises(AttributeError):
        adapter.fetch_store_tw_names(session, [1], **options)
    assert session.get.call_count == 1
    logger.warning.assert_not_called()


def test_title_transport_custom_request_exception_and_url_use_injected_ports():
    class CustomRequestError(Exception):
        pass

    session, delays, _, options = transport([
        CustomRequestError("first attempt"), response([{"appid": 1, "name": "中文"}]),
    ])
    assert adapter.fetch_store_tw_names(
        session, [1], requests_module=SimpleNamespace(RequestException=CustomRequestError),
        url="https://example.test/store", **options,
    ) == {1: "中文"}
    assert delays == [1.5, 5, 1.5]
    assert all(call.args == ("https://example.test/store",)
               for call in session.get.call_args_list)


def test_title_transport_type_error_response_retries_and_preserves_prior_collected_rows():
    class PartialRows:
        def __iter__(self):
            yield {"appid": 1, "name": "before error"}
            raise TypeError("iterator broke")

    session, delays, _, options = transport([
        response(data={"response": {"store_items": PartialRows()}}),
        response([{"appid": 2, "name": "after retry"}]),
    ])
    assert adapter.fetch_store_tw_names(session, [1, 2], **options) == {
        1: "before error", 2: "after retry",
    }
    assert delays == [1.5, 5, 1.5]


def test_failed_later_title_batch_returns_no_partial_mapping():
    session, _, logger, options = transport([
        response([{"appid": 1, "name": "first"}]),
        *[requests.ConnectionError("offline") for _ in range(4)],
    ])
    with pytest.raises(RuntimeError, match="batch 2 failed"):
        adapter.fetch_store_tw_names(session, [1, 2], batch_size=1, **options)
    assert session.get.call_count == 5
    assert logger.info.call_count == 1


def test_title_transport_pacing_and_custom_dependencies_are_resolved_at_call_time(monkeypatch):
    session = SimpleNamespace(get=Mock(return_value=response()))
    delays = []
    clock = iter([10.0, 10.0, 10.4, 10.4])
    logger = Mock()
    monkeypatch.setattr(adapter.time, "sleep", delays.append)
    monkeypatch.setattr(adapter.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(adapter, "LOG", logger)
    assert adapter.fetch_store_tw_names(session, [1, 2], batch_size=1) == {}
    assert delays[0] == 0.0
    assert delays[1] == pytest.approx(1.1)
    assert logger.info.call_count == 2


@pytest.mark.parametrize("ids,exception", [(["bad"], ValueError), ([None], TypeError)])
def test_title_transport_id_errors_happen_before_network_and_pacing(ids, exception):
    session, delays, logger, options = transport([])
    with pytest.raises(exception):
        adapter.fetch_store_tw_names(session, ids, **options)
    session.get.assert_not_called()
    assert delays == []
    logger.info.assert_not_called()


def test_title_transport_empty_ids_does_not_request_or_sleep():
    session, delays, _, options = transport([])
    assert adapter.fetch_store_tw_names(session, [0, -1], **options) == {}
    session.get.assert_not_called()
    assert delays == []
