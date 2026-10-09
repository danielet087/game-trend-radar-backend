"""Offline Store metadata retries and candidate screening boundary contracts."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from radar_backend.adapters import steam_metadata as transport
from radar_backend.application import candidate_screening as application
from radar_backend.domain import candidate_screening as domain
from scripts import screen_steam_candidates_before_followers as legacy


NOW = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)


def game(appid=1, **fields):
    return {
        "appid": appid, "release_precision": "day", "release_start": "2026-12-31",
        "name": "Original title", "capsule_image": "Original image", **fields,
    }


def metadata(label="date_full", *, ids=(), tags=(), description=""):
    return {
        "release": {"coming_soon_display": label}, "content_descriptorids": list(ids),
        "tags": [{"tagid": tag} for tag in tags],
        "basic_info": {"short_description": description},
    }


def response(payload=None, *, status=200, error=None):
    result = Mock(status_code=status)
    result.json.return_value = payload
    result.raise_for_status.side_effect = error
    return result


def fetch(replies, ids=None, **options):
    session = Mock()
    session.get.side_effect = replies
    sleep, logger = Mock(), Mock()
    rows = transport.fetch_metadata(
        session, [1] if ids is None else ids, sleep=sleep,
        monotonic=lambda: 0.0, logger=logger, **options,
    )
    return rows, session, sleep, logger


@pytest.mark.parametrize("label", ["date_year", "date_quarter", "date_month", "coming_soon", None, ""])
def test_imprecise_store_display_rejects_before_adult_predicate(label):
    predicate = Mock(side_effect=AssertionError("No adult lookup before exact display"))
    assert domain.classify(game(), metadata(label), explicit_predicate=predicate) == (
        None, str(label or "unknown"),
    )
    predicate.assert_not_called()


@pytest.mark.parametrize("item", [None, {}, {"release": None}, {"release": []}])
def test_unavailable_metadata_rejects_before_reading_candidate(item):
    assert domain.classify(None, item) == (None, "unavailable")


@pytest.mark.parametrize("fields", [
    {"release_precision": "month"}, {"release_precision": None},
    {"release_start": ""}, {"release_start": None},
])
def test_original_day_gate_rejects_before_adult_predicate(fields):
    predicate = Mock(side_effect=AssertionError("No adult lookup for invalid original"))
    assert domain.classify(game(**fields), metadata(), explicit_predicate=predicate) == (
        None, "invalid_original_date",
    )
    predicate.assert_not_called()


def test_eligible_copy_keeps_original_payload_without_new_calendar_validation():
    original = game(release_start="already-recorded-display", enrichment={"language": "zh-TW"})
    before = deepcopy(original)
    selected, reason = domain.classify(original, metadata())
    assert reason == "eligible"
    assert selected is not original
    assert selected["release_start"] == "already-recorded-display"
    assert selected["enrichment"] is original["enrichment"]
    assert selected["capsule_image"] == "Original image"
    assert selected["release_display_provider"] == "Steam IStoreBrowseService/GetItems"
    assert selected["release_display_precision"] == "date_full"
    assert selected["sexual_content_screened"] is True
    assert original == before


@pytest.mark.parametrize("ids,expected", [
    ([3], True), ([4], True), ([1, 3, 5], True), ([1], False),
    ([2], False), ([5], False), ([1, 2, 5], False), (["3"], False),
])
def test_explicit_descriptors_retain_original_numeric_membership(ids, expected):
    assert domain.is_explicit_sex_game(metadata(ids=ids)) is expected


@pytest.mark.parametrize("tags,description,expected", [
    ([12095, 6650], "A romance and dating game", False),
    ([12095, 6650], "An NSFW sex game", True),
    ([12095, 9130], "EROTIC scenes", True),
    ([0, 0, 0, 0, 12095, 9130], "Hentai", True),
    ([0, 0, 0, 0, 0, 12095, 9130], "Hentai", False),
    ([12095] + [0] * 8 + [9130], "Hentai", True),
    ([12095] + [0] * 9 + [9130], "Hentai", False),
    ([12095, 6650], "unsexual scenes and relationship", False),
])
def test_explicit_description_requires_existing_tag_rank_and_whole_word_rule(tags, description, expected):
    assert domain.is_explicit_sex_game(metadata(tags=tags, description=description)) is expected


def test_snapshot_excludes_audited_row_before_metadata_and_preserves_source_order():
    original = {"games": [game("3"), game(1), game(2), game(4)]}
    before = deepcopy(original)
    checked = []

    def classify(existing, item):
        checked.append((existing["appid"], item))
        return domain.classify(existing, item)

    mapped = {1: metadata(), 2: metadata("date_month"), 3: metadata()}
    records = domain.snapshot_records(original, mapped, blocked={3}, classifier=classify)
    assert records["source"] == "data/steam_candidates.json"
    assert records["source_count"] == 4
    assert records["count"] == 1
    assert records["excluded"] == 3
    assert records["reasons"] == {
        "audited_adult_exclusion": 1, "date_month": 1, "eligible": 1, "unavailable": 1,
    }
    assert list(records["reasons"]) == sorted(records["reasons"])
    assert [row["appid"] for row in records["games"]] == [1]
    assert [appid for appid, _ in checked] == [1, 2, 4]
    assert checked[0][1] is mapped[1]
    assert "screened_at" not in records
    assert original == before


def test_snapshot_reads_ledger_then_classifies_then_reads_single_clock():
    observed = []

    def exclusions():
        observed.append("ledger")
        return set()

    def classify(existing, item):
        observed.append("classify")
        return domain.classify(existing, item)

    def clock():
        observed.append("clock")
        return NOW

    snapshot = application.build_snapshot(
        {"games": [game()]}, {1: metadata()}, clock=clock,
        exclusion_loader=exclusions, classifier=classify,
    )
    assert observed == ["ledger", "classify", "clock"]
    assert snapshot["screened_at"] == NOW.isoformat()
    assert list(snapshot)[0] == "screened_at"


@pytest.mark.parametrize("games", [None, {}, "games", 4])
def test_bad_catalog_does_not_read_exclusion_or_clock(games):
    ledger, clock = Mock(), Mock()
    with pytest.raises(ValueError, match="Invalid original candidate games array"):
        application.build_snapshot({"games": games}, {}, clock=clock, exclusion_loader=ledger)
    ledger.assert_not_called()
    clock.assert_not_called()


@pytest.mark.parametrize("original,error", [
    ({"appid": "bad-id"}, ValueError), ({}, KeyError), ({"appid": None}, TypeError),
])
def test_bad_appid_preserves_error_after_ledger_and_before_clock(original, error):
    ledger, clock = Mock(return_value=set()), Mock()
    with pytest.raises(error):
        application.build_snapshot({"games": [original]}, {}, clock=clock, exclusion_loader=ledger)
    ledger.assert_called_once_with()
    clock.assert_not_called()


def test_unreadable_ledger_and_classifier_error_never_stamp_success():
    clock = Mock()
    with pytest.raises(ValueError, match="Malformed ledger"):
        application.build_snapshot(
            {"games": [game()]}, {}, clock=clock,
            exclusion_loader=Mock(side_effect=ValueError("Malformed ledger")),
        )
    with pytest.raises(RuntimeError, match="classifier failed"):
        application.build_snapshot(
            {"games": [game()]}, {}, clock=clock, exclusion_loader=lambda: set(),
            classifier=Mock(side_effect=RuntimeError("classifier failed")),
        )
    clock.assert_not_called()


def test_clock_precedes_final_reason_sort_error_for_custom_classifier():
    clock = Mock(return_value=NOW)
    statuses = iter(["eligible", 1])
    with pytest.raises(TypeError):
        application.build_snapshot(
            {"games": [game(1), game(2)]}, {}, clock=clock,
            exclusion_loader=lambda: set(),
            classifier=lambda existing, item: (None, next(statuses)),
        )
    clock.assert_called_once_with()


def test_transport_batches_exactly_35_and_preserves_ids_request_context_and_timeout():
    ids = list(range(1, 37))
    first = {"appid": 1, "metadata": "first"}
    last = {"appid": 36, "metadata": "last"}
    rows, session, sleep, logger = fetch([
        response({"response": {"store_items": [first]}}),
        response({"response": {"store_items": [last]}}),
    ], ids)
    assert rows == {1: first, 36: last}
    assert rows[1] is first
    assert session.get.call_count == 2
    for index, call in enumerate(session.get.call_args_list):
        assert call.args == (transport.STORE_BROWSE_URL,)
        assert call.kwargs["timeout"] == 30
        encoded = call.kwargs["params"]["input_json"]
        assert ": " not in encoded
        payload = json.loads(encoded)
        assert payload["ids"] == [{"appid": appid} for appid in ids[index * 35:(index + 1) * 35]]
        assert payload["context"] == {"country_code": "TW", "language": "english", "steam_realm": 1}
        assert payload["data_request"] == {
            "include_release": True, "include_basic_info": True, "include_tag_count": 20,
        }
    assert [call.args[0] for call in sleep.call_args_list] == [1.5, 1.5]
    assert logger.info.call_args_list[-1].args == ("DATE_GATE_CHECKED %s/%s resolved=%s", 36, 36, 2)


def test_transport_filters_response_rows_without_appid_normalization_and_last_duplicate_wins():
    first, replacement, boolean = {"appid": 1}, {"appid": 1, "last": True}, {"appid": True}
    rows, *_ = fetch([response({"response": {"store_items": [
        "invalid", None, {"appid": "1"}, {"appid": 1.0}, {"appid": 3},
        first, replacement, boolean,
    ]}})], [1, "2"])
    assert rows == {1: boolean}
    assert rows[1] is boolean
    assert first is not rows[1]
    empty, *_ = fetch([response({"response": {"store_items": [{"appid": 2}]}})], ["2"])
    assert empty == {}


@pytest.mark.parametrize("payload", [{}, {"response": None}, {"response": {}},
    {"response": {"store_items": None}}, {"response": {"store_items": []}},
    {"response": {"store_items": 0}}, {"response": {"store_items": ""}},
])
def test_missing_or_false_response_data_resolves_empty_without_retry(payload):
    rows, session, _, logger = fetch([response(payload)])
    assert rows == {}
    assert session.get.call_count == 1
    logger.warning.assert_not_called()


@pytest.mark.parametrize("payload", [None, [], {"response": [1]}, {"response": "bad"}])
def test_non_dict_response_preserves_uncaught_attribute_error(payload):
    session, logger = Mock(), Mock()
    session.get.return_value = response(payload)
    with pytest.raises(AttributeError):
        transport.fetch_metadata(session, [1], sleep=Mock(), monotonic=lambda: 0, logger=logger)
    assert session.get.call_count == 1
    logger.warning.assert_not_called()


def test_429_cools_down_on_all_four_attempts_and_fails_without_publishing_partial_success():
    session, sleep, logger = Mock(), Mock(), Mock()
    replies = [response(status=429) for _ in range(4)]
    session.get.side_effect = replies
    with pytest.raises(RuntimeError, match="batch 1 failed; refusing to publish an incomplete"):
        transport.fetch_metadata(session, [1], sleep=sleep, monotonic=lambda: 0, logger=logger)
    assert session.get.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [1.5, 20, 1.5, 40, 1.5, 60, 1.5, 80]
    assert logger.warning.call_count == 4
    logger.info.assert_not_called()
    for reply in replies:
        reply.raise_for_status.assert_not_called()
        reply.json.assert_not_called()


@pytest.mark.parametrize("failure", [
    requests.ConnectionError("offline"), ValueError("bad json"), TypeError("bad response"),
])
def test_retryable_failures_use_5_10_15_backoff_without_fourth_backoff(failure):
    session, sleep, logger = Mock(), Mock(), Mock()
    session.get.side_effect = [failure] * 4
    with pytest.raises(RuntimeError, match="batch 1 failed"):
        transport.fetch_metadata(session, [1], sleep=sleep, monotonic=lambda: 0, logger=logger)
    assert [call.args[0] for call in sleep.call_args_list] == [1.5, 5, 1.5, 10, 1.5, 15, 1.5]
    assert logger.warning.call_count == 4


def test_http_error_and_json_parse_failure_can_retry_successfully():
    malformed = response()
    malformed.json.side_effect = ValueError("invalid json")
    valid = {"appid": 1}
    rows, session, sleep, _ = fetch([
        response(error=requests.HTTPError("503")), malformed,
        response({"response": {"store_items": [valid]}}),
    ])
    assert rows[1] is valid
    assert session.get.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.5, 5, 1.5, 10, 1.5]


def test_truthy_non_iterable_items_retries_type_error_then_resolves():
    rows, session, sleep, _ = fetch([
        response({"response": {"store_items": 7}}), response({}),
    ])
    assert rows == {}
    assert session.get.call_count == 2
    assert [call.args[0] for call in sleep.call_args_list] == [1.5, 5, 1.5]


def test_partial_rows_before_iteration_failure_survive_the_retry():
    first = {"appid": 1}

    def interrupted_items():
        yield first
        raise ValueError("iteration stopped")

    rows, session, _, _ = fetch([
        response({"response": {"store_items": interrupted_items()}}), response({}),
    ])
    assert rows == {1: first}
    assert rows[1] is first
    assert session.get.call_count == 2


def test_pacing_reads_clock_before_each_attempt_and_stores_start_before_get():
    observed = []
    readings = iter([0.3, 0.5, 0.9, 1.0])

    def monotonic():
        value = next(readings)
        observed.append(("clock", value))
        return value

    def sleep(seconds):
        observed.append(("sleep", seconds))

    def get(*args, **kwargs):
        observed.append(("get", kwargs["timeout"]))
        if sum(1 for kind, _ in observed if kind == "get") == 1:
            raise requests.ConnectionError("offline")
        return response({})

    transport.fetch_metadata(SimpleNamespace(get=get), [1], sleep=sleep, monotonic=monotonic, logger=Mock())
    assert observed == [
        ("clock", 0.3), ("sleep", 1.2), ("clock", 0.5), ("get", 30),
        ("sleep", 5), ("clock", 0.9), ("sleep", 1.1), ("clock", 1.0), ("get", 30),
    ]


@pytest.mark.parametrize("ids,batch_size", [([], 35), ([1], -1)])
def test_empty_work_and_negative_batch_size_touch_no_ports(ids, batch_size):
    session, sleep, monotonic, logger = Mock(), Mock(), Mock(), Mock()
    assert transport.fetch_metadata(
        session, ids, batch_size=batch_size, sleep=sleep, monotonic=monotonic, logger=logger,
    ) == {}
    session.get.assert_not_called()
    sleep.assert_not_called()
    monotonic.assert_not_called()
    logger.info.assert_not_called()


def test_zero_batch_size_keeps_range_value_error():
    session = Mock()
    with pytest.raises(ValueError, match="range.*must not be zero"):
        transport.fetch_metadata(session, [], batch_size=0, sleep=Mock(), monotonic=Mock())
    session.get.assert_not_called()


def test_legacy_fetch_uses_current_time_log_requests_and_url(monkeypatch):
    class CustomRequestError(Exception):
        pass

    session, sleep, logger = Mock(), Mock(), Mock()
    session.get.side_effect = [CustomRequestError("legacy exception port"), response({})]
    monkeypatch.setattr(legacy, "time", SimpleNamespace(sleep=sleep, monotonic=lambda: 0))
    monkeypatch.setattr(legacy, "LOG", logger)
    monkeypatch.setattr(legacy, "requests", SimpleNamespace(RequestException=CustomRequestError))
    monkeypatch.setattr(legacy, "STORE_BROWSE_URL", "https://offline.invalid/store")
    assert legacy.fetch_metadata(session, [1]) == {}
    assert session.get.call_count == 2
    assert session.get.call_args.args == ("https://offline.invalid/store",)
    assert [call.args[0] for call in sleep.call_args_list] == [1.5, 5, 1.5]
    assert logger.warning.call_count == 1


def test_legacy_classifier_and_snapshot_use_current_predicate_ledger_classifier_and_datetime(monkeypatch):
    observed = []
    predicate = Mock(return_value=True)
    monkeypatch.setattr(legacy, "is_explicit_sex_game", predicate)
    assert legacy.classify(game(), metadata()) == (None, "sexual_content")
    predicate.assert_called_once()

    def classify(existing, item):
        observed.append("patched classifier")
        return {"appid": existing["appid"], "custom": True}, "custom"

    def exclusions():
        observed.append("patched ledger")
        return set()

    def now(tz):
        observed.append(("patched clock", tz))
        return NOW

    monkeypatch.setattr(legacy, "classify", classify)
    monkeypatch.setattr(legacy, "excluded_appids", exclusions)
    monkeypatch.setattr(legacy, "datetime", SimpleNamespace(now=now))
    snapshot = legacy.build_snapshot({"games": [game()]}, {})
    assert observed == ["patched ledger", "patched classifier", ("patched clock", timezone.utc)]
    assert snapshot["games"] == [{"appid": 1, "custom": True}]
    assert snapshot["reasons"] == {"custom": 1}
    assert snapshot["screened_at"] == NOW.isoformat()


def test_legacy_explicit_predicate_reads_current_public_constants(monkeypatch):
    monkeypatch.setattr(legacy, "SEXUAL_CONTENT_IDS", frozenset({99}))
    assert legacy.is_explicit_sex_game(metadata(ids=[99])) is True
    assert legacy.is_explicit_sex_game(metadata(ids=[3])) is False
    monkeypatch.setattr(legacy, "EXPLICIT_DESC", re.compile("patched phrase"))
    assert legacy.is_explicit_sex_game(metadata(tags=[12095, 6650], description="patched phrase")) is True
    assert legacy.is_explicit_sex_game(metadata(tags=[12095, 6650], description="NSFW")) is False
