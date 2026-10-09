"""Offline content-event contracts across rules, use cases and HTTP transport."""
from copy import deepcopy
from datetime import datetime
from enum import IntEnum
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from radar_backend.adapters import github_content_dispatch as adapter
from radar_backend.application import content_dispatch as application
from radar_backend.domain import content_dispatch as domain

NOW = datetime.fromisoformat("2026-10-09T11:30:00+08:00")
SOURCE = "owner/steam-backend"
TARGET = "owner/content-backend"


def result(appid=123, **changes):
    return {
        "appid": appid, "official_followers": 6200,
        "release_date": "2026-10-29", "store_date_exact": True,
        "release_display_precision": "date_full",
        "official_checked_at_taipei": "2026-10-09T11:25:00+08:00",
        "official_source": "Steam Community XML memberCount",
        **changes,
    }


def environment(**changes):
    return {
        "CONTENT_BACKEND_REPOSITORY": TARGET,
        "CONTENT_BACKEND_TOKEN": "content-secret",
        "GITHUB_REPOSITORY": SOURCE,
        "GITHUB_TOKEN": "runner-secret",
        **changes,
    }


def ports(*, status=204, error=None, environ=None):
    post = Mock(side_effect=error) if error is not None else Mock(return_value=SimpleNamespace(status_code=status))
    clock = Mock(return_value=NOW)
    emit = Mock()
    services = adapter.compose_services(clock, environment() if environ is None else environ, post, emit)
    return services, post, clock, emit


@pytest.mark.parametrize("changes,expected", [
    ({"official_followers": 4999}, "not_qualified"),
    ({"official_followers": 0}, "not_qualified"),
    ({"official_followers": None}, "not_qualified"),
    ({"official_followers": True}, "not_qualified"),
    ({"official_followers": 6200.0}, "not_qualified"),
    ({"official_followers": "6200"}, "not_qualified"),
    ({"release_date": "2026-02-29"}, "invalid_release_date"),
    ({"release_date": "2026-10"}, "invalid_release_date"),
    ({"release_date": None}, "invalid_release_date"),
    ({"store_date_exact": False}, "store_date_not_verified"),
    ({"store_date_exact": 1}, "store_date_not_verified"),
    ({"release_display_precision": "month"}, "store_date_not_verified"),
    ({"release_display_precision": None}, "store_date_not_verified"),
])
def test_unqualified_results_do_not_resolve_secrets_or_mutate_checkpoint(changes, expected):
    checkpoint = {"unknown": {"preserve": True}}
    original = deepcopy(checkpoint)
    forbidden = Mock(side_effect=AssertionError("Gate must stop before configuration, clock or HTTP"))
    services = application.ContentDispatchServices(forbidden, forbidden, forbidden, forbidden)

    assert application.dispatch_content_event(checkpoint, result(**changes), services=services) == expected
    assert checkpoint == original
    forbidden.assert_not_called()


@pytest.mark.parametrize("values,target,token,source", [
    ({}, "", "", None),
    ({"GITHUB_REPOSITORY": SOURCE, "GITHUB_TOKEN": "fallback"}, SOURCE, "fallback", SOURCE),
    (environment(), TARGET, "content-secret", SOURCE),
    (environment(CONTENT_BACKEND_REPOSITORY="", CONTENT_BACKEND_TOKEN=""), SOURCE, "runner-secret", SOURCE),
    (environment(CONTENT_BACKEND_REPOSITORY=" owner/content ", CONTENT_BACKEND_TOKEN=" token "), "owner/content", "token", SOURCE),
    (environment(CONTENT_BACKEND_REPOSITORY=" "), "", "content-secret", SOURCE),
    (environment(CONTENT_BACKEND_TOKEN=" "), TARGET, "", SOURCE),
])
def test_environment_fallback_and_strip_keep_the_existing_contract(values, target, token, source):
    configuration = adapter.resolve_configuration(values)
    assert (configuration.target_repository, configuration.token, configuration.source_repository) == (target, token, source)
    assert configuration.configured is bool(target and token)
    if token:
        assert token not in repr(configuration)


def test_success_sends_the_receiver_contract_and_preserves_unrelated_state():
    services, post, clock, emit = ports()
    observation = result("00123", official_followers=5000)
    original = deepcopy(observation)
    checkpoint = {"unknown": "preserve", "content_dispatches": {"9": {"status": "failed"}}}

    assert application.dispatch_content_event(checkpoint, observation, services=services) == "dispatched"

    signature = "123:5000:2026-10-29:store-v2"
    post.assert_called_once_with(
        f"https://api.github.com/repos/{TARGET}/dispatches",
        headers={
            "Authorization": "Bearer content-secret",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "GameTrendRadarFollowersDispatch/1.0",
        },
        json={
            "event_type": "steam_game_qualified",
            "client_payload": {
                "appid": 123, "official_followers": 5000,
                "release_date": "2026-10-29",
                "official_checked_at_taipei": original["official_checked_at_taipei"],
                "official_source": original["official_source"],
                "source_repository": SOURCE, "signature": signature,
            },
        },
        timeout=20,
    )
    assert checkpoint["content_dispatches"]["123"] == {
        "signature": signature, "status": "dispatched", "target_repository": TARGET,
        "event_type": "steam_game_qualified", "dispatched_at_taipei": NOW.isoformat(),
    }
    assert checkpoint["content_dispatches"]["9"] == {"status": "failed"}
    assert checkpoint["unknown"] == "preserve"
    assert observation == original
    clock.assert_called_once_with()
    emit.assert_called_once_with("CONTENT_DISPATCH_OK", "123", signature, flush=True)


def test_legacy_integer_subclasses_keep_their_existing_numeric_qualification():
    class VerifiedFollowers(IntEnum):
        QUALIFIED = 6200

    services, post, _, _ = ports()
    assert application.dispatch_content_event({}, result(official_followers=VerifiedFollowers.QUALIFIED), services=services) == "dispatched"
    payload = post.call_args.kwargs["json"]["client_payload"]
    assert type(payload["official_followers"]) is int
    assert payload["official_followers"] == 6200


@pytest.mark.parametrize("status", [200, 201, 202, 400, 401, 403, 429, 500])
def test_non_204_is_saved_as_failure_and_remains_retryable(status):
    services, post, clock, emit = ports(status=status)
    checkpoint = {"official_results": {"123": result()}}

    assert application.dispatch_content_event(checkpoint, checkpoint["official_results"]["123"], services=services) == "failed"
    assert checkpoint["content_dispatches"]["123"] == {
        "signature": "123:6200:2026-10-29:store-v2", "status": "failed",
        "target_repository": TARGET, "http": status, "attempted_at_taipei": NOW.isoformat(),
    }
    assert [row["appid"] for row in domain.retry_candidates(checkpoint)] == [123]
    post.assert_called_once()
    clock.assert_called_once()
    emit.assert_called_once_with("CONTENT_DISPATCH_FAILED", "123", status, flush=True)


@pytest.mark.parametrize("error", [requests.Timeout("private token in timeout"), requests.ConnectionError("private URL in error")])
def test_transport_errors_save_only_exception_type_without_secret_text(error):
    services, _, _, emit = ports(error=error)
    checkpoint = {}

    assert application.dispatch_content_event(checkpoint, result(), services=services) == "failed"
    entry = checkpoint["content_dispatches"]["123"]
    assert entry == {
        "signature": "123:6200:2026-10-29:store-v2", "status": "failed",
        "target_repository": TARGET, "error_type": type(error).__name__,
        "attempted_at_taipei": NOW.isoformat(),
    }
    assert "private" not in repr(entry)
    assert "private" not in repr(emit.call_args)


def test_missing_configuration_precedes_idempotency_and_leaves_state_intact():
    observation = result()
    checkpoint = {"content_dispatches": {"123": {
        "signature": domain.content_dispatch_signature(observation), "status": "dispatched",
    }}}
    original = deepcopy(checkpoint)
    services, post, clock, emit = ports(environ={})

    assert application.dispatch_content_event(checkpoint, observation, services=services) == "not_configured"
    assert checkpoint == original
    post.assert_not_called()
    clock.assert_not_called()
    emit.assert_not_called()


def test_only_matching_success_suppresses_delivery_and_new_evidence_is_sent():
    services, post, _, _ = ports()
    checkpoint = {}
    assert application.dispatch_content_event(checkpoint, result(), services=services) == "dispatched"
    first = deepcopy(checkpoint)
    assert application.dispatch_content_event(checkpoint, result(), services=services) == "already_dispatched"
    assert checkpoint == first
    assert post.call_count == 1

    assert application.dispatch_content_event(checkpoint, result(official_followers=6201), services=services) == "dispatched"
    assert post.call_count == 2
    assert checkpoint["content_dispatches"]["123"]["signature"] == "123:6201:2026-10-29:store-v2"
    assert application.dispatch_content_event(checkpoint, result(official_followers=6201, release_date="2026-10-30"), services=services) == "dispatched"
    assert post.call_count == 3


def test_failed_dispatch_is_retried_until_a_real_ack_then_skipped():
    services, post, _, _ = ports(status=429)
    checkpoint = {"official_results": {"123": result()}}
    deliver = lambda cp, row: application.dispatch_content_event(cp, row, services=services)
    assert application.retry_pending_content_dispatches(checkpoint, dispatch=deliver) == 1
    assert checkpoint["content_dispatches"]["123"]["status"] == "failed"
    post.return_value = SimpleNamespace(status_code=204)
    assert application.retry_pending_content_dispatches(checkpoint, dispatch=deliver) == 1
    assert checkpoint["content_dispatches"]["123"]["status"] == "dispatched"
    assert application.retry_pending_content_dispatches(checkpoint, dispatch=deliver) == 0
    assert post.call_count == 2


def test_retry_selection_keeps_sort_order_twitch_exclusion_and_attempt_counts():
    observation = result(4)
    checkpoint = {
        "official_results": {
            "10": result(10, release_date="2026-10-30"),
            "2": result(2, store_date_exact=False),
            "1": result(1, release_date="2026-10-28"),
            "7": result(7, release_date="bad-date"),
            "3": result(3, queue_source="twitch_steam_discovery"),
            "4": observation,
            "5": result(5, official_followers=4999),
            "6": result(6, official_followers=True),
        },
        "content_dispatches": {"4": {"signature": domain.content_dispatch_signature(observation), "status": "dispatched"}},
    }
    services, post, _, _ = ports(environ={})
    attempted = []

    def deliver(cp, row):
        attempted.append((row["appid"], application.dispatch_content_event(cp, row, services=services)))

    assert application.retry_pending_content_dispatches(checkpoint, dispatch=deliver) == 4
    assert attempted == [(1, "not_configured"), (2, "store_date_not_verified"), (10, "not_configured"), (7, "invalid_release_date")]
    post.assert_not_called()


def test_default_limit_stops_before_inspecting_a_26th_signature():
    checkpoint = {"official_results": {str(aid): result(aid) for aid in range(1, 26)}}
    checkpoint["official_results"]["26"] = {"appid": 26, "official_followers": 6000}
    deliver = Mock(return_value="not_configured")
    assert application.retry_pending_content_dispatches(checkpoint, dispatch=deliver) == 25
    assert [call.args[1]["appid"] for call in deliver.call_args_list] == list(range(1, 26))


@pytest.mark.parametrize("limit", [0, -1])
def test_no_retry_budget_avoids_signature_or_delivery(limit):
    checkpoint = {"official_results": {"1": {"appid": 1, "official_followers": 6000}}}
    forbidden = Mock(side_effect=AssertionError("No retry budget"))
    assert application.retry_pending_content_dispatches(checkpoint, limit, dispatch=forbidden, signature=forbidden) == 0
    forbidden.assert_not_called()


def test_explicit_signature_port_drives_payload_registry_and_retry_identity():
    services, post, _, _ = ports()
    signature = Mock(return_value="injected-signature")
    checkpoint = {"official_results": {"123": result()}}
    deliver = lambda cp, row: application.dispatch_content_event(cp, row, services=services, signature=signature)
    assert application.retry_pending_content_dispatches(checkpoint, dispatch=deliver, signature=signature) == 1
    assert post.call_args.kwargs["json"]["client_payload"]["signature"] == "injected-signature"
    assert checkpoint["content_dispatches"]["123"]["signature"] == "injected-signature"
    assert application.retry_pending_content_dispatches(checkpoint, dispatch=deliver, signature=signature) == 0
    assert post.call_count == 1


def test_composed_default_http_and_environment_resolve_at_call_time(monkeypatch):
    for key in ("CONTENT_BACKEND_REPOSITORY", "CONTENT_BACKEND_TOKEN", "GITHUB_REPOSITORY", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    services = adapter.compose_services(lambda: NOW, emit=Mock())
    post = Mock(return_value=SimpleNamespace(status_code=204))
    monkeypatch.setattr(adapter.requests, "post", post)
    monkeypatch.setenv("CONTENT_BACKEND_REPOSITORY", TARGET)
    monkeypatch.setenv("CONTENT_BACKEND_TOKEN", "late-secret")
    monkeypatch.setenv("GITHUB_REPOSITORY", SOURCE)

    assert application.dispatch_content_event({}, result(), services=services) == "dispatched"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer late-secret"
    assert post.call_args.kwargs["json"]["client_payload"]["source_repository"] == SOURCE


def test_unknown_programming_error_does_not_fabricate_a_failed_http_receipt():
    services, _, _, emit = ports(error=RuntimeError("broken local transport"))
    checkpoint = {}
    with pytest.raises(RuntimeError, match="broken local transport"):
        application.dispatch_content_event(checkpoint, result(), services=services)
    assert checkpoint["content_dispatches"] == {}
    emit.assert_not_called()
