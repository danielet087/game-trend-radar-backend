"""Offline behavior at the shared Steam Community boundary."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import json
import xml.etree.ElementTree as ET

import pytest
import requests

from scripts.steam_official_followers import (
    GROUP_BASE, CooldownStore, OfficialFollowerCache, OfficialFollowerClient,
    OfficialObservation, parse_official_xml, read_state, steam_429_cooldown,
)

NOW = datetime(2026, 10, 8, 11, 0, tzinfo=timezone.utc)
GID = str(GROUP_BASE + 123)


def response(count=6000, gid=GID, status=200, headers=None):
    body = f'<memberList><groupID64>{gid}</groupID64><memberCount>{count}</memberCount></memberList>'.encode()
    return SimpleNamespace(status_code=status, headers=headers or {}, content=body)


@pytest.mark.parametrize('count', ['0', '6,123'])
def test_xml_requires_matching_official_identity_and_keeps_real_zero(count):
    assert parse_official_xml(response(count).content, GID) == int(count.replace(',', ''))
    with pytest.raises(ValueError):
        parse_official_xml(response(count, str(GROUP_BASE + 999)).content, GID)
    with pytest.raises(ValueError):
        parse_official_xml(b'<memberList><memberCount>6123</memberCount></memberList>', GID)


@pytest.mark.parametrize('gid', [None, True, '76561198000000001', GROUP_BASE, str(GROUP_BASE + 2 ** 32)])
def test_unresolved_group_never_uses_games_endpoint_or_spends_a_request(gid):
    session = SimpleNamespace(get=Mock(side_effect=AssertionError('No Community request')))
    client = OfficialFollowerClient(session=session, clock=lambda: NOW)
    assert client.fetch(gid).status == 'awaiting_group_resolution'
    session.get.assert_not_called()


def test_both_existing_deadline_fields_block_with_no_network():
    state = {'next_request_after_taipei': (NOW + timedelta(hours=1)).isoformat(),
             'community_cooldown': {'retry_at': (NOW + timedelta(hours=2)).isoformat()},
             'pending_candidates': {'123': {'appid': 123}}}
    session = SimpleNamespace(get=Mock())
    client = OfficialFollowerClient(session=session, clock=lambda: NOW, cooldown=CooldownStore(state))
    assert client.fetch(GID).status == 'cooldown_no_request'
    assert CooldownStore(state).deadline() == NOW + timedelta(hours=2)
    session.get.assert_not_called()
    assert state['pending_candidates'] == {'123': {'appid': 123}}


@pytest.mark.parametrize('prior,minutes', [(0, 15), (1, 30), (2, 60), (6, 960), (7, 1440), (30, 1440)])
def test_retry_after_obeys_existing_backoff_policy(prior, minutes):
    state = {'rate_limit_count': prior}
    session = SimpleNamespace(get=Mock(return_value=response(status=429)))
    client = OfficialFollowerClient(session=session, clock=lambda: NOW, cooldown=CooldownStore(state))
    assert client.fetch(GID).status == 'rate_limited'
    assert CooldownStore(state).deadline() == NOW + timedelta(minutes=minutes)
    assert state['rate_limit_count'] == prior + 1
    assert '/gid/' in session.get.call_args.args[0]
    assert session.get.call_args.kwargs['timeout'] == (8, 24)


@pytest.mark.parametrize('header,hours', [('7200', 2), ('Thu, 08 Oct 2026 14:00:00 GMT', 3)])
def test_manual_probe_preserves_longer_deadline_or_server_retry_after(header, hours):
    state = {'rate_limit_count': 1, 'next_request_after_taipei': (NOW + timedelta(hours=1)).isoformat()}
    session = SimpleNamespace(get=Mock(return_value=response(status=429, headers={'Retry-After': header})))
    client = OfficialFollowerClient(session=session, clock=lambda: NOW, cooldown=CooldownStore(state))
    assert client.fetch(GID, manual_override=True).status == 'rate_limited'
    assert CooldownStore(state).deadline() == NOW + timedelta(hours=hours)


def test_only_actual_success_clears_checkpoint_cooldown_not_legacy_or_queue():
    state = {'rate_limit_count': 3, 'temporary_error_count': 4,
             'next_request_after_taipei': (NOW + timedelta(hours=1)).isoformat(),
             'community_cooldown': {'retry_at': (NOW + timedelta(hours=1)).isoformat()},
             'pending_candidates': {'123': {'appid': 123}}, 'content_dispatches': {'123': {'status': 'pending'}}}
    legacy = {'next_request_after_taipei': (NOW + timedelta(hours=3)).isoformat()}
    client = OfficialFollowerClient(session=SimpleNamespace(get=Mock(return_value=response(0))),
                                    clock=lambda: NOW, cooldown=CooldownStore(state, legacy))
    assert client.fetch(GID, manual_override=True).followers == 0
    assert state['rate_limit_count'] == state['temporary_error_count'] == 0
    assert state['community_cooldown'] is state['next_request_after_taipei'] is None
    assert state['pending_candidates']['123']['appid'] == 123
    assert state['content_dispatches']['123']['status'] == 'pending'
    assert legacy['next_request_after_taipei'] == (NOW + timedelta(hours=3)).isoformat()


def test_transport_failure_saves_retry_without_a_fabricated_measurement():
    state = {}
    client = OfficialFollowerClient(session=SimpleNamespace(get=Mock(side_effect=requests.Timeout())),
                                    clock=lambda: NOW, cooldown=CooldownStore(state))
    outcome = client.fetch(GID)
    assert outcome.status == 'transport_or_xml_error'
    assert outcome.followers is None and outcome.error_type == 'Timeout'
    assert CooldownStore(state).deadline() == NOW + timedelta(minutes=15)


def test_html_is_not_valid_xml_and_preserves_diagnostic_and_retry():
    state = {}
    session = SimpleNamespace(get=Mock(return_value=SimpleNamespace(status_code=200,
                            headers={'Content-Type': 'text/html'}, content=b'<html>')))
    outcome = OfficialFollowerClient(session=session, clock=lambda: NOW, cooldown=CooldownStore(state)).fetch(GID)
    assert outcome.status == 'transport_or_xml_error'
    assert outcome.error_type == 'ParseError' and outcome.content_type == 'text/html'
    assert outcome.response_bytes == 6 and outcome.followers is None
    assert state['temporary_error_count'] == 1


@pytest.mark.parametrize('state', [
    {'community_cooldown': 'corrupt'}, {'next_request_after_taipei': 'invalid'},
    {'community_cooldown': {'retry_at': '2026-10-08T11:00:00'}},
])
def test_corrupt_retry_state_fails_closed(state):
    session = SimpleNamespace(get=Mock())
    client = OfficialFollowerClient(session=session, clock=lambda: NOW, cooldown=CooldownStore(state))
    with pytest.raises(ValueError):
        client.fetch(GID)
    session.get.assert_not_called()


def test_invalid_optional_json_is_not_silently_reset(tmp_path):
    path = tmp_path / 'checkpoint.json'
    assert read_state(path, optional=True) == {}
    path.write_text('{broken')
    with pytest.raises(json.JSONDecodeError):
        read_state(path, optional=True)
    path.write_text('[]')
    with pytest.raises(ValueError):
        read_state(path, optional=True)


def test_cache_reuses_only_actual_observation_without_rewriting_time():
    state = {'official_growth_observations': {'123': {'appid': 123, 'group_id64': GID,
              'official_followers': 0, 'official_checked_at_taipei': NOW.isoformat()}}}
    other = {'verified': {'124': {'official_followers': 42,
               'official_group_id64': GID, 'checked_at': (NOW - timedelta(days=1)).isoformat()}}}
    cache = OfficialFollowerCache(state, other)
    assert cache.group_id(124) == GID
    observation = cache.latest(123, NOW, today_only=True)
    assert observation.followers == 0 and observation.checked_at == NOW.isoformat()
    assert cache.latest(124, NOW, today_only=True) is None
    assert cache.latest(123, NOW, expected_group=str(GROUP_BASE + 999)) is None
    cache.remember(OfficialObservation(123, GID, 17, NOW.isoformat()))
    assert state['official_growth_observations']['123']['official_followers'] == 17
