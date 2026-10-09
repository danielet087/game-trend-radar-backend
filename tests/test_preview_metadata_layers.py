from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from radar_backend.adapters import steam_preview_metadata as adapter
from radar_backend.application import preview_metadata as application
from radar_backend.domain import preview_metadata as rules


class Response:
    def __init__(self, body=None, *, status=200, error=None):
        self.status_code = status
        self.body = body
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Session:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.parametrize("body", [{}, {"response": {"x": 1}}])
def test_transport_returns_dict_by_identity_with_original_request(body):
    session = Session([Response(body)])
    params = {"appids": 12, "cc": "TW", "l": "english"}
    sleep = Mock()
    assert adapter.steam_get(session, "endpoint", params, sleep=sleep) is body
    assert session.calls == [("endpoint", {"params": params, "timeout": 25})]
    assert session.calls[0][1]["params"] is params
    sleep.assert_not_called()


@pytest.mark.parametrize("body", [None, [], [1], "json", 0, True])
def test_transport_nonobject_success_returns_none_without_retry(body):
    session = Session([Response(body)])
    sleep = Mock()
    assert adapter.steam_get(session, "endpoint", {}, sleep=sleep) is None
    assert len(session.calls) == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("failure", [requests.ConnectionError("offline"), ValueError("bad json")])
def test_transport_retries_request_and_value_errors_without_final_sleep(failure):
    session = Session([failure, failure, failure])
    sleep = Mock()
    logger = Mock()
    assert adapter.steam_get(session, "endpoint", {}, sleep=sleep, logger=logger) is None
    assert len(session.calls) == 3
    assert [call.args[0] for call in sleep.call_args_list] == [5, 10]
    assert logger.warning.call_count == 3


def test_transport_429_includes_final_cooldown_without_reading_response():
    responses = [Mock(status_code=429) for _ in range(3)]
    session = Session(responses)
    sleep = Mock()
    assert adapter.steam_get(session, "endpoint", {}, sleep=sleep, logger=Mock()) is None
    assert [call.args[0] for call in sleep.call_args_list] == [60, 120, 180]
    for response in responses:
        response.raise_for_status.assert_not_called()
        response.json.assert_not_called()


@pytest.mark.parametrize("outcomes,expected", [
    ([Response(status=429), Response({"ok": True})], [60]),
    ([requests.Timeout("timeout"), Response({"ok": True})], [5]),
    ([Response(ValueError("bad")), Response({"ok": True})], [5]),
    ([Response(status=429), Response(ValueError("bad")), Response({"ok": True})], [60, 10]),
    ([Response(error=requests.HTTPError("500")), Response(status=429), Response({"ok": True})], [5, 120]),
])
def test_transport_mixed_failures_preserve_attempt_based_delays(outcomes, expected):
    sleep = Mock()
    result = adapter.steam_get(Session(outcomes), "endpoint", {}, sleep=sleep, logger=Mock())
    assert result == {"ok": True}
    assert [call.args[0] for call in sleep.call_args_list] == expected


@pytest.mark.parametrize("failure", [TypeError("wrong body"), AttributeError("shape"), RuntimeError("unknown")])
def test_transport_does_not_broaden_retryable_errors(failure):
    session = Session([Response(failure)])
    sleep = Mock()
    with pytest.raises(type(failure), match=str(failure)):
        adapter.steam_get(session, "endpoint", {}, sleep=sleep)
    assert len(session.calls) == 1
    sleep.assert_not_called()


def test_transport_uses_explicit_request_exception_type():
    class CustomFailure(Exception):
        pass

    session = Session([CustomFailure(), Response({"ok": 1})])
    sleep = Mock()
    assert adapter.steam_get(
        session, "endpoint", {}, sleep=sleep, logger=Mock(),
        requests_module=SimpleNamespace(RequestException=CustomFailure),
    ) == {"ok": 1}
    sleep.assert_called_once_with(5)


def test_transport_default_ports_are_read_at_call_time(monkeypatch):
    sleep = Mock()
    logger = Mock()
    monkeypatch.setattr(adapter.time, "sleep", sleep)
    monkeypatch.setattr(adapter, "LOGGER", logger)
    assert adapter.steam_get(Session([Response(status=429), Response({})]), "url", {}) == {}
    sleep.assert_called_once_with(60)
    logger.warning.assert_called_once_with("Steam Store returned 429; sleeping %ds", 60)


@pytest.mark.parametrize("data", [
    None, {}, {12: {"success": True, "data": {}}}, {"12": None},
    {"12": []}, {"12": {"success": False, "data": {}}},
    {"12": {"success": True}}, {"12": {"success": True, "data": []}},
])
def test_response_extract_rejects_absent_or_unsuccessful_details(data):
    assert rules.app_details_from_response(12, data) is None


@pytest.mark.parametrize("success", [True, 1, "yes", [1]])
def test_response_extract_truthy_success_preserves_data_identity(success):
    details = {"name": "Game"}
    assert rules.app_details_from_response(12, {"12": {"success": success, "data": details}}) is details


def test_app_details_keeps_country_language_url_and_fetch_callback(monkeypatch):
    details = {}
    fetch = Mock(return_value={"12": {"success": True, "data": details}})
    session = object()
    assert adapter.app_details(session, 12, language="schinese", fetch=fetch, url="custom") is details
    fetch.assert_called_once_with(session, "custom", {"appids": 12, "cc": "TW", "l": "schinese"})
    monkeypatch.setattr(adapter, "steam_get", fetch)
    assert adapter.app_details(session, 12) is details
    assert fetch.call_args.args == (session, adapter.APP_DETAILS, {"appids": 12, "cc": "TW", "l": "english"})


@pytest.mark.parametrize("en,tw,expected", [
    (" Game ", " 遊戲 ", ("Game", "遊戲")),
    ("Game", "Game", ("Game", None)),
    ("Game", "game", ("Game", "game")),
    ("Game", " Different ", ("Game", "Different")),
    ("Game", "x" * 241, ("Game", "x" * 241)),
    ("Game", "", ("Game", None)),
    ("Game", "  ", ("Game", None)),
    (None, "中文", ("", "中文")),
    (False, None, ("", None)),
    (123, 456, ("123", "456")),
    ([1], [2], ("[1]", "[2]")),
])
def test_preview_name_policy_is_exact_unequal_nonblank_without_han_gate(en, tw, expected):
    assert rules.localized_names({"name": en}, {"name": tw}) == expected


def test_name_selection_missing_traditional_details():
    assert rules.localized_names({"name": " Game "}, None) == ("Game", None)


def exact_release(**changes):
    return {"release_precision": "day", "release_start": "2026-09-19", **changes}


def normalize(details, *, release=None, clock=None, preserve=None, browse=None):
    return application.normalized_metadata(
        12, details, 7000, "followers-time", browse_release=browse,
        resolved_store_date=Mock(return_value=exact_release() if release is None else release),
        preserve_player_categories=preserve or (lambda old, incoming: incoming),
        clock=clock or Mock(return_value="category-time"),
    )


@pytest.mark.parametrize("release", [
    {"release_precision": "month", "release_start": "2026-09-01"},
    {"release_precision": "day", "release_start": None},
    {"release_precision": "day", "release_start": ""},
])
def test_precision_gate_precedes_name_projection_clock_and_preservation(release):
    class Unstringable:
        def __str__(self):
            raise AssertionError("Name must not be read")

    clock, preserve = Mock(), Mock()
    assert normalize({"name": Unstringable()}, release=release, clock=clock, preserve=preserve) is None
    clock.assert_not_called()
    preserve.assert_not_called()


def test_normalization_resolver_receives_original_release_and_browse_by_identity():
    release_details = {"date": "raw", "coming_soon": False}
    browse = {"timestamp": 123}
    resolve = Mock(return_value=exact_release())
    preserve = Mock(side_effect=lambda old, incoming: incoming)
    row = application.normalized_metadata(
        12, {"release_date": release_details}, None, None, browse_release=browse,
        resolved_store_date=resolve, preserve_player_categories=preserve, clock=Mock(),
    )
    resolve.assert_called_once_with(12, "raw", browse, fallback_detail=release_details)
    assert resolve.call_args.args[2] is browse
    assert resolve.call_args.kwargs["fallback_detail"] is release_details
    assert row["name"] == row["name_en"] == "Steam App 12"
    assert row["followers"] is None and row["follower_checked_at"] is None
    preserve.assert_called_once_with({}, row)


@pytest.mark.parametrize("details", [
    {}, {"type": "game"},
    {"type": "game", "steam_appid": True, "categories": []},
    {"type": "game", "steam_appid": 13, "categories": []},
    {"type": "game", "steam_appid": "12", "categories": []},
    {"type": "game", "steam_appid": 12.0, "categories": []},
    {"type": "dlc", "steam_appid": 12, "categories": []},
    {"type": "game", "steam_appid": 12, "categories": None},
    {"type": "game", "steam_appid": 12, "categories": {}},
    {"type": "game", "steam_appid": 12, "categories": ()},
])
def test_unverified_categories_never_read_clock(details):
    clock = Mock()
    row = normalize(details, clock=clock)
    assert not rules.has_official_player_categories(12, details)
    assert "categories" not in row and "categories_checked_at" not in row
    clock.assert_not_called()


@pytest.mark.parametrize("categories", [[], [{"id": 1, "description": "Multi-player"}], [None]])
def test_verified_category_list_is_forwarded_without_new_validation(categories):
    details = {"type": "game", "steam_appid": 12, "categories": categories}
    clock = Mock(return_value="category-time")
    row = normalize(details, clock=clock)
    assert rules.has_official_player_categories(12, details)
    assert row["categories"] is categories
    assert row["categories_source"] == rules.PLAYER_CATEGORY_SOURCE
    assert row["categories_checked_at"] == "category-time"
    clock.assert_called_once_with()


def test_normalized_field_order_keeps_release_overrides_and_followers_owner():
    nested = {"extra": [1]}
    release = exact_release(appid=99, name="resolved", name_en="resolved-en",
                            followers=2, follower_checked_at="wrong", unknown=nested,
                            header_image="wrong", capsule_image="wrong")
    row = normalize({"name": 23, "capsule_image": "", "header_image": "header"}, release=release)
    assert row["appid"] == 99 and row["name"] == "resolved" and row["name_en"] == "resolved-en"
    assert row["followers"] == 7000 and row["follower_checked_at"] == "followers-time"
    assert row["header_image"] == row["capsule_image"] == "header"
    assert row["unknown"] is nested
    assert row["store_url"] == "https://store.steampowered.com/app/12/"


def test_category_clock_runs_after_fields_and_keeps_preclock_category_reference():
    trace = []

    class Name:
        def __str__(self):
            trace.append("name")
            return "Game"

    categories = []
    details = {"name": Name(), "type": "game", "steam_appid": 12, "categories": categories}

    def clock():
        trace.append("clock")
        details["categories"] = [{"id": 2}]
        return "time"

    def preserve(old, row):
        trace.append("preserve")
        assert row["categories"] is categories
        return row

    normalize(details, clock=clock, preserve=preserve)
    assert trace == ["name", "name", "clock", "preserve"]


def test_category_preservation_callback_owns_final_result():
    final = {"custom": True}
    preserve = Mock(return_value=final)
    assert normalize({"name": "Game"}, preserve=preserve) is final


def test_metadata_resolver_error_precedes_clock_and_preservation():
    clock, preserve = Mock(), Mock()
    with pytest.raises(ValueError, match="resolver"):
        application.normalized_metadata(
            12, {}, 7000, None, resolved_store_date=Mock(side_effect=ValueError("resolver")),
            preserve_player_categories=preserve, clock=clock,
        )
    clock.assert_not_called()
    preserve.assert_not_called()


def test_locale_application_preserves_sequence_and_mutates_original_game():
    trace = []
    session, english, traditional = object(), {"name": "English"}, {"name": "中文"}
    game = {"name": "Old", "release_start": "2026-09-19", "language_support": {"tw": False}}

    def sleep(value):
        trace.append(("sleep", value))

    def fetch(got_session, appid, **kwargs):
        trace.append(("fetch", got_session, appid, kwargs))
        return traditional

    def select(en, tw):
        assert en is english and tw is traditional
        trace.append("select")
        return "English", "中文"

    def display(row):
        assert row is game and row["name"] == row["name_en"] == "English"
        assert row["name_zh_tw"] == "中文"
        trace.append("display")

    assert application.add_traditional_name(
        session, 12, game, english, delay_seconds=0,
        sleep=sleep, app_details=fetch, localized_names=select,
        add_traditional_display_names=display,
    ) is None
    assert trace == [("sleep", 0), ("fetch", session, 12, {"language": "tchinese"}), "select", "display"]
    assert game["release_start"] == "2026-09-19" and game["language_support"] == {"tw": False}


@pytest.mark.parametrize("english,initial,expected", [
    ({"name": ""}, {"name": "Old", "name_en": "Earlier"}, "Old"),
    ({"name": " English "}, {"name": "Old"}, "English"),
])
def test_locale_application_english_fallback_keeps_existing_assignment_order(english, initial, expected):
    application.add_traditional_name(
        None, 12, initial, english, delay_seconds=0, sleep=Mock(),
        app_details=Mock(return_value=None), localized_names=rules.localized_names,
        add_traditional_display_names=Mock(),
    )
    assert initial["name"] == initial["name_en"] == expected
    assert initial["name_zh_tw"] is None


def test_locale_application_display_failure_retains_completed_name_mutations():
    game = {"name": "Old", "name_zh_tw": "Old CN"}
    with pytest.raises(RuntimeError, match="display"):
        application.add_traditional_name(
            None, 12, game, {"name": "English"}, delay_seconds=0, sleep=Mock(),
            app_details=Mock(return_value={"name": "新名"}), localized_names=rules.localized_names,
            add_traditional_display_names=Mock(side_effect=RuntimeError("display")),
        )
    assert game == {"name": "English", "name_en": "English", "name_zh_tw": "新名"}


def test_locale_application_lookup_failure_leaves_game_untouched():
    game = {"name": "Old"}
    sleep, select, display = Mock(), Mock(), Mock()
    with pytest.raises(ValueError, match="lookup"):
        application.add_traditional_name(
            None, 12, game, {}, delay_seconds=2.5, sleep=sleep,
            app_details=Mock(side_effect=ValueError("lookup")), localized_names=select,
            add_traditional_display_names=display,
        )
    sleep.assert_called_once_with(2.5)
    select.assert_not_called()
    display.assert_not_called()
    assert game == {"name": "Old"}


def test_canonical_composition_uses_new_ports_without_legacy_preview(monkeypatch):
    import scripts.publish_steam_preview as legacy

    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy preview is not a canonical dependency")

    for name in ("steam_get", "app_details", "localized_names", "normalized_metadata", "add_traditional_name"):
        monkeypatch.setattr(legacy, name, forbidden)
    sleep = Mock()
    monkeypatch.setattr(adapter.time, "sleep", sleep)
    session = Session([Response({"12": {"success": True, "data": {"name": "中文"}}})])
    game = adapter.normalized_metadata(
        12, {"name": "English", "release_date": {"date": "19 Sep, 2026"}},
        7000, "followers-time", preserve_player_categories=lambda old, incoming: incoming,
    )
    adapter.add_traditional_name(session, 12, game, {"name": "English"}, delay_seconds=0)
    assert game["name_en"] == "English" and game["name_zh_tw"] == "中文"
    assert game["release_start"] == "2026-09-19"
    assert game["display_name"] == "中文"
    sleep.assert_called_once_with(0)
    assert session.calls[0][1]["params"]["l"] == "tchinese"


def test_canonical_metadata_default_resolver_is_late_bound(monkeypatch):
    resolve = Mock(return_value=exact_release())
    monkeypatch.setattr(adapter, "resolved_store_date", resolve)
    row = adapter.normalized_metadata(12, {}, None, None,
                                      preserve_player_categories=lambda old, incoming: incoming)
    assert row["release_start"] == "2026-09-19"
    resolve.assert_called_once_with(12, None, None, fallback_detail={})


@pytest.mark.parametrize("module", [rules, application])
def test_pure_layers_have_no_transport_or_legacy_imports(module):
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module or ""]
        else:
            continue
        assert not any(name.startswith(("requests", "opencc", "scripts", "collectors", "radar_backend.adapters"))
                       for name in imports)
