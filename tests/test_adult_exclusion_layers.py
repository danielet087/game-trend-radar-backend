"""Characterize ledger failures and metadata coercion across the adult boundary."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from radar_backend.domain import adult_exclusions as domain
from radar_backend.state import adult_exclusions as state
from scripts import steam_adult_exclusions as legacy


def document(rows=None, *, criteria=None):
    return {
        "criteria": {"exclude_content_descriptor_ids": [3, 4] if criteria is None else criteria},
        "games": [] if rows is None else rows,
    }


@pytest.mark.parametrize("doc, expected", [
    (document(), set()),
    (document(criteria=[3, 4, 3]), set()),
    (document(criteria=[3.0, 4.0]), set()),
    (document([{"appid": "10", "excluded_descriptor_ids": [3]}]), {10}),
    (document([{"appid": 10.9, "excluded_descriptor_ids": [4]}]), {10}),
    (document([{"appid": True, "excluded_descriptor_ids": [3]}]), {1}),
    (document([{"appid": -10, "excluded_descriptor_ids": [4]}]), {-10}),
    (document([{"appid": "10", "excluded_descriptor_ids": [3.0]}]), {10}),
    (document([{"appid": "10", "excluded_descriptor_ids": ["3", "4"]}]), set()),
    (document([{"appid": "10", "excluded_descriptor_ids": "34"}]), set()),
    (document([{"excluded_descriptor_ids": [1]}, {}]), set()),
    (document([
        {"appid": 10, "excluded_descriptor_ids": [3]},
        {"appid": "10", "excluded_descriptor_ids": [4]},
        {"appid": 20, "excluded_descriptor_ids": [1]},
    ]), {10}),
])
def test_ledger_evidence_preserves_raw_descriptor_comparison_and_appid_coercion(doc, expected, tmp_path):
    before = copy.deepcopy(doc)
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert domain.excluded_appids_from_document(doc) == expected
    assert state.excluded_appids(path) == expected
    assert legacy.excluded_appids(path) == expected
    assert doc == before


@pytest.mark.parametrize("doc, error", [
    ([], TypeError),
    ({}, KeyError),
    ({"criteria": None}, TypeError),
    ({"criteria": {}}, KeyError),
    (document(criteria=["3", "4"]), RuntimeError),
    (document(criteria=[3]), RuntimeError),
    (document(criteria=[3, 4, True]), RuntimeError),
    (document(criteria=[[3], 4]), TypeError),
    ({"criteria": {"exclude_content_descriptor_ids": [3, 4]}}, RuntimeError),
    ({"criteria": {"exclude_content_descriptor_ids": [3, 4]}, "games": None}, RuntimeError),
    ({"criteria": {"exclude_content_descriptor_ids": [3, 4]}, "games": {}}, RuntimeError),
    (document([None]), AttributeError),
    (document([{"appid": 10, "excluded_descriptor_ids": None}]), TypeError),
    (document([{"appid": 10, "excluded_descriptor_ids": [[3]]}]), TypeError),
    (document([{"excluded_descriptor_ids": [3]}]), KeyError),
    (document([{"appid": "bad", "excluded_descriptor_ids": [3]}]), ValueError),
])
def test_malformed_ledger_never_becomes_an_empty_exclusion_set(doc, error, tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    for read in (lambda: domain.excluded_appids_from_document(doc),
                 lambda: state.excluded_appids(path), lambda: legacy.excluded_appids(path)):
        with pytest.raises(error):
            read()


@pytest.mark.parametrize("row, blocked, expected", [
    ({"appid": 10}, {10}, True),
    ({"appid": "10"}, {10}, True),
    ({"appid": "10"}, {"10"}, False),
    ({"appid": 10.9}, {10}, True),
    ({"appid": True}, set(), False),
    ({"appid": 0}, set(), False),
    ({"appid": -10}, set(), False),
    ({"appid": 10, "content_descriptorids": [3]}, set(), True),
    ({"appid": 10, "content_descriptorids": ["4"]}, set(), True),
    ({"appid": 10, "content_descriptorids": "34"}, set(), True),
    ({"appid": 10, "content_descriptorids": [3.9]}, set(), True),
    ({"appid": 10, "content_descriptorids": [True, 1, 2]}, set(), False),
    ({"appid": 10, "content_descriptorids": [3, "bad"]}, set(), False),
    ({"appid": 10, "content_descriptorids": [3, "bad"]}, {10}, True),
    ({"appid": 10, "content_descriptorids": 3}, set(), False),
    ({"appid": 10, "content_descriptors": {"ids": [4]}}, set(), True),
    ({"appid": 10, "content_descriptors": {"ids": "34"}}, set(), True),
    ({"appid": 10, "content_descriptors": {"ids": None}}, set(), False),
    ({"appid": 10, "content_descriptorids": [], "content_descriptors": [3]}, set(), True),
    ({"appid": 10, "content_descriptorids": ["bad"], "content_descriptors": [3]}, set(), False),
    ({"appid": 10, "content_descriptorids": {"ids": []}, "content_descriptors": [3]}, set(), False),
    ({"appid": 10, "content_descriptorids": None, "content_descriptors": [3]}, set(), True),
])
def test_candidate_metadata_keeps_integer_coercion_and_whole_value_fallback(row, blocked, expected):
    before = copy.deepcopy((row, blocked))
    assert domain.is_disallowed(row, blocked) is expected
    assert legacy.is_disallowed(row, blocked) is expected
    assert (row, blocked) == before


@pytest.mark.parametrize("row", [None, {}, {"appid": None}, {"appid": "bad"}, {"appid": []}])
def test_unreadable_appid_is_disallowed(row):
    assert domain.is_disallowed(row, set()) is True
    assert legacy.is_disallowed(row, set()) is True


def test_uncaught_appid_overflow_remains_visible():
    for check in (domain.is_disallowed, legacy.is_disallowed):
        with pytest.raises(OverflowError):
            check({"appid": float("inf")}, set())


@pytest.mark.parametrize("kind", ["missing", "directory", "broken_symlink"])
def test_missing_ledger_forms_keep_original_error(kind, tmp_path):
    path = tmp_path / "ledger.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "broken_symlink":
        path.symlink_to(tmp_path / "missing.json")
    for read in (state.excluded_appids, legacy.excluded_appids):
        with pytest.raises(RuntimeError, match="Steam adult exclusion ledger missing") as failure:
            read(path)
        assert str(path) in str(failure.value)


def test_existing_ledger_symlink_is_read_without_an_added_path_policy(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps(document([{"appid": 10, "excluded_descriptor_ids": [3]}])), encoding="utf-8")
    link = tmp_path / "ledger.json"
    link.symlink_to(target)
    assert state.excluded_appids(link) == {10}
    assert legacy.excluded_appids(link) == {10}


@pytest.mark.parametrize("read", [state.excluded_appids, legacy.excluded_appids])
def test_malformed_json_keeps_decoder_failure(read, tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read(path)


def test_ledger_read_errors_are_not_converted_to_default_state(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=OSError("ledger unreadable")))
    for read in (state.excluded_appids, legacy.excluded_appids):
        with pytest.raises(OSError, match="ledger unreadable"):
            read(path)


def test_legacy_wrapper_resolves_layer_functions_and_rule_constant_at_call_time(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    read = Mock(return_value={10})
    check = Mock(return_value=True)
    monkeypatch.setattr(state, "excluded_appids", read)
    monkeypatch.setattr(domain, "is_disallowed", check)
    monkeypatch.setattr(legacy, "EXCLUDED_DESCRIPTORS", frozenset({7}))
    assert legacy.excluded_appids(path) == {10}
    read.assert_called_once_with(path, excluded_descriptors=frozenset({7}))
    row = {"appid": 10}
    blocked = set()
    assert legacy.is_disallowed(row, blocked) is True
    check.assert_called_once_with(row, blocked, excluded_descriptors=frozenset({7}))


def test_legacy_default_path_stays_bound_when_exported_path_constant_changes(tmp_path, monkeypatch):
    original_path = legacy.excluded_appids.__defaults__[0]
    assert original_path == Path(__file__).resolve().parents[1] / "data" / "steam_adult_exclusion.json"
    assert state.EXCLUSION_PATH == original_path
    read = Mock(return_value=set())
    monkeypatch.setattr(state, "excluded_appids", read)
    monkeypatch.setattr(legacy, "EXCLUSION_PATH", tmp_path / "replacement.json")
    assert legacy.excluded_appids() == set()
    read.assert_called_once_with(original_path, excluded_descriptors=domain.EXCLUDED_DESCRIPTORS)


def test_legacy_rule_constant_can_still_be_replaced_for_both_decisions(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(document([{"appid": 10, "excluded_descriptor_ids": [7]}], criteria=[7])), encoding="utf-8")
    monkeypatch.setattr(legacy, "EXCLUDED_DESCRIPTORS", frozenset({7}))
    assert legacy.excluded_appids(path) == {10}
    assert legacy.is_disallowed({"appid": 20, "content_descriptorids": [7]}, set()) is True
    assert legacy.is_disallowed({"appid": 20, "content_descriptorids": [3]}, set()) is False
