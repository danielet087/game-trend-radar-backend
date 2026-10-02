"""A successful Steam request ends a consecutive rate-limit failure streak."""
from datetime import timedelta
from unittest.mock import Mock

import pytest

from scripts.import_twitch_steam_discoveries import apply_batch
from tests.test_twitch_retry_flow import run_collect, stamp, xml_response
from tests.test_twitch_steam_admission import (
    NOW, extend_frontend_appids, fake_frontend, metadata_responses,
)


@pytest.mark.parametrize("persist_success_first", [False, True])
def test_community_success_resets_backoff_before_the_next_429(tmp_path, persist_success_first):
    fake_frontend(tmp_path)
    expired_at = NOW - timedelta(minutes=1)
    previous = {"games": {}, "api_cooldowns": {"steam_community": {
        "retry_at": stamp(expired_at),
        "updated_at": stamp(expired_at - timedelta(hours=1)),
        "observed_at": stamp(expired_at - timedelta(hours=1)),
        "retry_seconds": 3600,
        "retry_source": "default_backoff", "retry_after": None,
        "attempts": 3,
    }}}
    master = {"games": []}
    session = Mock()
    if persist_success_first:
        # Persisting a successful recovery must reset the next run, too.
        session.get.side_effect = [*metadata_responses(123), xml_response(19)]
        success = run_collect(tmp_path, master, previous, session)
        master, previous = apply_batch(master, previous, success)
        recovered = previous["api_cooldowns"]["steam_community"]
        assert recovered["attempts"] == 0
        assert recovered["retry_at"] == stamp(expired_at)
        assert recovered["last_success_at"] == stamp(NOW)
        session.reset_mock()
        responses = []
    else:
        # The next game in the same batch must also start a new streak.
        responses = [*metadata_responses(123), xml_response(19)]

    extend_frontend_appids(tmp_path, [123, 124])
    responses.extend([*metadata_responses(124), Mock(status_code=429, headers={})])
    session.get.side_effect = responses

    retry = run_collect(tmp_path, master, previous, session)

    limited = retry["cooldown_updates"]["steam_community"]
    assert limited["attempts"] == 1
    assert limited["retry_source"] == "default_backoff"
    assert limited["retry_at"] == stamp(NOW + timedelta(minutes=15))
    assert retry["state_updates"]["124"]["rate_limit_attempts"] == 1
