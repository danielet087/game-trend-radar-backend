"""Offline contracts for explicit official group rule/collection ports."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest

from radar_core.domain.twitch_admission import aware_time, decimal_id
from radar_backend.domain import official_groups as rules
from radar_backend.domain.official_queue import checked_numeric, valid_group_id64
from radar_backend.application import official_groups as application
from radar_backend.adapters import steam_official_groups as transport

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
GROUP = '103582791429521531'
KEY = 'offline-private-key'
STAMP = rules.stamp
FINGERPRINT = partial(rules.fingerprint, decimal_id=decimal_id)
COMPLETED = partial(rules.completed, checked_numeric=checked_numeric)
COOLDOWN = partial(rules.failure_cooldown, retry_after=rules.retry_after, stamp=STAMP)


class RequestFailure(Exception):
    pass


def row(aid=123, **values):
    return {'appid': aid, 'release_date': '2026-10-09', 'queue_source': 'normal', **values}


def checkpoint(*rows):
    return {'pending_candidates': {str(item['appid']): deepcopy(item) for item in rows},
            'official_results': {}, 'attempt_events': [{'appid': 8, 'http': 429}],
            'cursor': 71, 'next_request_after_taipei': '2026-10-10T01:00:00+08:00',
            'community_cooldown': {'status': 'rate_limited'}, 'rate_limit_count': 8}


def response(http=200, payload=None, headers=None):
    return SimpleNamespace(status_code=http, headers=headers or {}, json=lambda: payload)


def found(aid=123):
    return response(payload={'response': {'success': 1, 'steamid': str(103582791429521408 + aid)}})


def collect(cp, candidates=None, **overrides):
    ports = dict(api_key=KEY, now=NOW, monotonic=lambda: 0, sleep=lambda _: None,
                 default_clock=lambda: NOW, stamp=STAMP, decimal_id=decimal_id,
                 completed=COMPLETED, valid_group_id=valid_group_id64, aware_time=aware_time,
                 fingerprint=FINGERPRINT, failure_cooldown=COOLDOWN,
                 request=lambda session, aid, key, remaining: found(int(aid)),
                 session_factory=lambda: SimpleNamespace(headers={}),
                 configure_session=transport.configure_session, request_exception=RequestFailure)
    ports.update(overrides)
    return application.collect(cp, list(cp['pending_candidates'].values()) if candidates is None else candidates,
                               **ports)


def apply(cp, batch, **overrides):
    ports = dict(completed=COMPLETED, valid_group_id=valid_group_id64,
                 fingerprint=FINGERPRINT, aware_time=aware_time)
    ports.update(overrides)
    return rules.apply_batch(cp, batch, **ports)


@pytest.mark.parametrize('value', [0, 1, 9000])
@pytest.mark.parametrize('field', ['official_followers', 'followers'])
def test_completed_preserves_official_numeric_zero_and_both_count_fields(value, field):
    assert COMPLETED({'official_results': {'123': {field: value}}}, '123')


@pytest.mark.parametrize('value', [None, True, False, -1, 1.0, '0', [], {}])
@pytest.mark.parametrize('field', ['official_followers', 'followers'])
def test_completed_does_not_treat_bad_or_boolean_values_as_official_counts(value, field):
    assert not COMPLETED({'official_results': {'123': {field: value}}}, '123')


def test_fingerprint_ignores_frontend_commit_but_retains_proof_and_identity_fields():
    prior = row(twitch_admission={'source_frontend_commit': 'old', 'valid': True, 'nested': [1]})
    unchanged = deepcopy(prior)
    unchanged['twitch_admission']['source_frontend_commit'] = 'new'
    unchanged['name'] = 'display-only'
    assert FINGERPRINT(prior) == FINGERPRINT(unchanged)
    for key, value in [('appid', 124), ('release_date', '2026-10-10'), ('queue_source', 'replacement')]:
        changed = {**prior, key: value}
        assert FINGERPRINT(prior) != FINGERPRINT(changed)
    unchanged['twitch_admission']['nested'].append(2)
    assert FINGERPRINT(prior) != FINGERPRINT(unchanged)


@pytest.mark.parametrize('proof', [None, 'not-proof', 5, False, []])
def test_non_dict_proof_is_none_in_identity(proof):
    assert FINGERPRINT(row(twitch_admission=proof)) == FINGERPRINT(row())


def test_fingerprint_uses_provided_parser_serializer_and_hash():
    serializer = Mock(return_value='serialized')
    digest = Mock(hexdigest=Mock(return_value='digest'))
    hasher = Mock(return_value=digest)
    parser = Mock(return_value='custom-app-id')
    result = rules.fingerprint(row(), decimal_id=parser,
                               json_module=SimpleNamespace(dumps=serializer),
                               hashlib_module=SimpleNamespace(sha256=hasher))
    assert result == 'digest'
    parser.assert_called_once_with(123)
    serializer.assert_called_once_with({'appid': 'custom-app-id', 'source': 'normal',
                                       'release_date': '2026-10-09', 'twitch_admission': None},
                                      sort_keys=True, separators=(',', ':'))
    hasher.assert_called_once_with(b'serialized')


@pytest.mark.parametrize('header,seconds', [('0', 1), ('1', 1), (' 60 ', 60),
                                         ('Fri, 09 Oct 2026 12:02:00 GMT', 120),
                                         ('Fri, 09 Oct 2026 11:00:00 GMT', 1),
                                         ('Fri, 09 Oct 2026 12:02:00', 120)])
def test_retry_after_honors_ascii_seconds_dates_and_past_minimum(header, seconds):
    assert rules.retry_after(header, NOW) == NOW + timedelta(seconds=seconds)


@pytest.mark.parametrize('header', [None, 5, True, '', 'abc', '６０', '-1', '1.5', '9' * 500])
def test_retry_after_invalid_headers_return_none(header):
    assert rules.retry_after(header, NOW) is None


@pytest.mark.parametrize('status,first,cap', [('network_error', 5, 60),
                                           ('api_rate_limited', 15, 1440),
                                           ('api_error', 15, 1440),
                                           ('api_forbidden', 1440, 1440)])
def test_cooldown_backoff_caps_attempts_and_never_shortens_server_deadline(status, first, cap):
    initial = COOLDOWN({}, status, NOW)
    assert aware_time(initial['retry_at']) == NOW + timedelta(minutes=first)
    final = COOLDOWN({'status': status, 'attempts': 200}, status, NOW)
    assert final['attempts'] == 128
    assert aware_time(final['retry_at']) == NOW + timedelta(minutes=cap)
    extended = COOLDOWN({}, status, NOW, response(429, headers={'Retry-After': '172800'}))
    assert aware_time(extended['retry_at']) == NOW + timedelta(days=2)
    assert extended['retry_source'] == 'steam_retry_after'
    assert extended['http'] == 429


@pytest.mark.parametrize('attempts', [-1, True, False, '5', 1.0, None, {}, []])
def test_cooldown_malformed_attempts_restart_at_one(attempts):
    result = COOLDOWN({'status': 'network_error', 'attempts': attempts}, 'network_error', NOW)
    assert result['attempts'] == 1
    assert aware_time(result['retry_at']) == NOW + timedelta(minutes=5)


@pytest.mark.parametrize('invalid', [{'max_requests': True}, {'max_requests': 0}, {'max_requests': 21},
                                    {'max_requests': 1.0}, {'max_seconds': False}, {'max_seconds': 0},
                                    {'max_seconds': 121}, {'max_seconds': 1.0}, {'interval': float('nan')},
                                    {'interval': float('inf')}, {'interval': -1}, {'interval': .999}])
def test_invalid_limits_fail_before_clock_selection_transport_and_receipt(invalid):
    request = Mock(side_effect=AssertionError('must not reach transport'))
    default_clock = Mock(side_effect=AssertionError('must not read clock'))
    with pytest.raises(ValueError, match='Group resolver requires'):
        collect(checkpoint(row()), request=request, default_clock=default_clock, **invalid)
    request.assert_not_called()
    default_clock.assert_not_called()


def test_naive_time_fails_before_monotonic_or_transport():
    monotonic = Mock(side_effect=AssertionError('must not read deadline'))
    with pytest.raises(ValueError, match='aware timestamps'):
        collect(checkpoint(row()), now=NOW.replace(tzinfo=None), monotonic=monotonic)
    monotonic.assert_not_called()


def test_missing_key_has_receipts_without_session_and_fixed_time_without_new_clock():
    cp = checkpoint(row(123), row(124))
    before = deepcopy(cp)
    forbidden = Mock(side_effect=AssertionError('must not access transport/clock'))
    result = collect(cp, api_key='', session_factory=forbidden, request=forbidden,
                     default_clock=forbidden, max_requests=1)
    assert list(result['results']) == ['123']
    assert result['stop_reason'] == 'missing_api_key'
    assert result['generated_at'] == result['finished_at'] == STAMP(NOW)
    assert result['requests_this_run'] == 0
    assert result['results']['123']['group_resolution']['attempted'] is False
    assert cp == before
    forbidden.assert_not_called()


def test_current_checkpoint_wins_selection_dedup_and_priority_are_stable():
    cp = checkpoint(row(123, group_id64=GROUP), row(124), row(125, queue_source=rules.TWITCH_SOURCE), row(126))
    cp['official_results']['127'] = {'followers': 0}
    request = Mock(side_effect=lambda session, aid, key, remaining: found(int(aid)))
    result = collect(cp, [row(124), row(123), None, row(125), row(124), row(126), row(127), row(True)], request=request)
    assert list(result['results']) == ['125', '124', '126']
    assert [call.args[1] for call in request.call_args_list] == ['125', '124', '126']


def test_pacing_and_time_budget_use_provided_monotonic_sleep_and_remaining():
    elapsed = [0.0]
    starts = []
    def request(session, aid, key, remaining):
        starts.append((elapsed[0], remaining, aid))
        elapsed[0] += .25
        return found(int(aid))
    result = collect(checkpoint(row(123), row(124), row(125)), max_seconds=2,
                     monotonic=lambda: elapsed[0], sleep=lambda delay: elapsed.__setitem__(0, elapsed[0] + delay),
                     request=request)
    assert starts == [(0.0, 2.0, '123'), (1.0, 1.0, '124')]
    assert result['requests_this_run'] == 2
    assert result['stop_reason'] == 'time_budget'
    assert '125' not in result['results']


@pytest.mark.parametrize('http,status', [(429, 'api_rate_limited'), (401, 'api_forbidden'),
                                        (403, 'api_forbidden'), (500, 'api_error'), (599, 'api_error')])
def test_http_global_failure_stops_after_first_and_keeps_key_out_of_receipts(http, status):
    request = Mock(return_value=response(http, {'message': KEY}, {'Retry-After': '3600'}))
    cp = checkpoint(row(123), row(124))
    batch = collect(cp, request=request)
    assert batch['requests_this_run'] == 1
    assert batch['stop_reason'] == status
    assert list(batch['results']) == ['123']
    assert batch['api_cooldown_update']['status'] == status
    assert KEY not in json.dumps(batch)
    merged = apply(cp, batch)
    for field in ('cursor', 'official_results', 'attempt_events', 'community_cooldown', 'next_request_after_taipei', 'rate_limit_count'):
        assert merged[field] == cp[field]


@pytest.mark.parametrize('code,status', [(15, 'api_forbidden'), (84, 'api_rate_limited')])
def test_api_result_global_failure_stops_and_preserves_integer_api_result(code, status):
    batch = collect(checkpoint(row(123), row(124)), request=lambda *args: response(payload={'response': {'success': code}}))
    assert batch['stop_reason'] == status
    assert batch['results']['123']['group_resolution']['api_result'] == code
    assert batch['requests_this_run'] == 1


@pytest.mark.parametrize('payload,status', [({}, 'invalid_response'), ([], 'invalid_response'),
                                          ({'response': None}, 'invalid_response'),
                                          ({'response': {'success': True}}, 'invalid_response'),
                                          ({'response': {'success': '1'}}, 'invalid_response'),
                                          ({'response': {'success': 1, 'steamid': 'bad'}}, 'invalid_response'),
                                          ({'response': {'success': 42}}, 'not_found'),
                                          ({'response': {'success': 7}}, 'api_error')])
def test_per_game_payload_failure_continues_next_candidate(payload, status):
    request = Mock(side_effect=[response(payload=payload), found(124)])
    batch = collect(checkpoint(row(123), row(124)), request=request)
    assert batch['results']['123']['group_resolution']['status'] == status
    assert batch['results']['124']['group_resolution']['status'] == 'resolved'
    assert batch['requests_this_run'] == 2
    assert batch['stop_reason'] == 'complete'


@pytest.mark.parametrize('exception', [ValueError('bad json'), TypeError('bad payload')])
def test_response_decode_errors_remain_per_game(exception):
    first = response()
    first.json = Mock(side_effect=exception)
    request = Mock(side_effect=[first, found(124)])
    batch = collect(checkpoint(row(123), row(124)), request=request)
    assert batch['results']['123']['group_resolution']['status'] == 'invalid_response'
    assert batch['requests_this_run'] == 2


def test_only_injected_transport_exception_is_caught_and_no_credentials_leak():
    request = Mock(side_effect=RequestFailure(KEY))
    batch = collect(checkpoint(row(123), row(124)), request=request)
    assert batch['stop_reason'] == 'network_error'
    assert batch['requests_this_run'] == 1
    assert KEY not in json.dumps(batch)
    with pytest.raises(RuntimeError, match='unexpected'):
        collect(checkpoint(row()), request=Mock(side_effect=RuntimeError('unexpected')))


@pytest.mark.parametrize('remaining,timeout', [(120, 15.0), (20, 10.0), (.1, .1), (0, .1), (-5, .1)])
def test_transport_preserves_key_header_vanity_params_redirect_and_bounded_timeout(remaining, timeout):
    session = Mock()
    transport.configure_session(session)
    transport.request(session, '123', KEY, remaining, api_url='https://api.test/resolver')
    session.headers.update.assert_called_once_with({'User-Agent': 'GameTrendRadarOfficialGroupResolver/1.0'})
    session.get.assert_called_once_with('https://api.test/resolver',
                                       params={'vanityurl': '123', 'url_type': 3, 'format': 'json'},
                                       headers={'x-webapi-key': KEY}, timeout=timeout, allow_redirects=False)


@pytest.mark.parametrize('change', ['withdrawn', 'completed', 'group', 'release', 'source', 'same_timestamp',
                                   'newer_timestamp', 'bad_checked', 'bad_retry', 'bad_status', 'bad_source',
                                   'bad_group', 'bad_fingerprint', 'ineligible'])
def test_fresh_apply_rejects_stale_or_invalid_receipts_without_restoring_progress(change):
    cp = checkpoint(row())
    batch = collect(cp)
    if change == 'withdrawn':
        cp['pending_candidates'].clear()
    elif change == 'completed':
        cp['official_results']['123'] = {'official_followers': 0}
    elif change == 'group':
        cp['pending_candidates']['123']['group_id64'] = str(int(GROUP) + 1)
    elif change in ('release', 'source'):
        cp['pending_candidates']['123']['release_date' if change == 'release' else 'queue_source'] = 'replacement'
    elif change in ('same_timestamp', 'newer_timestamp'):
        cp['pending_candidates']['123']['group_resolution'] = {'checked_at': STAMP(NOW + timedelta(seconds=change == 'newer_timestamp'))}
    elif change == 'bad_group':
        batch['results']['123']['group_id64'] = 'invalid'
    elif change == 'bad_fingerprint':
        batch['results']['123']['candidate_fingerprint'] = 'wrong'
    elif change.startswith('bad_'):
        field = {'bad_checked': 'checked_at', 'bad_retry': 'retry_at', 'bad_status': 'status', 'bad_source': 'source'}[change]
        batch['results']['123']['group_resolution'][field] = 'invalid'
    before = deepcopy(cp)
    merged = apply(cp, batch, eligible_appids=[] if change == 'ineligible' else None)
    assert merged['pending_candidates'] == before['pending_candidates']
    assert cp == before
    assert merged['official_results'] == before['official_results']


@pytest.mark.parametrize('batch', [{}, {'schema_version': 2, 'results': {}}, {'schema_version': 1, 'results': []}])
def test_invalid_batches_fail_before_deepcopy(batch):
    copy = Mock(side_effect=AssertionError('must not copy'))
    with pytest.raises(ValueError, match='Invalid group resolution batch'):
        apply(checkpoint(row()), batch, deepcopy_fn=copy)
    copy.assert_not_called()


def test_apply_strips_unknown_receipt_fields_and_retains_proof_and_rich_state():
    cp = checkpoint(row(twitch_admission={'proof': [1]}, richer={'nested': [2]}))
    before = deepcopy(cp)
    batch = collect(cp)
    batch['results']['123']['group_resolution']['credential'] = KEY
    batch['api_cooldown_update']['credential'] = KEY
    result = apply(cp, batch, eligible_appids=[123])
    assert result['pending_candidates']['123']['group_id64'] == GROUP
    assert result['pending_candidates']['123']['twitch_admission'] == {'proof': [1]}
    assert result['pending_candidates']['123']['richer'] == {'nested': [2]}
    assert KEY not in json.dumps(result)
    result['pending_candidates']['123']['richer']['nested'].append(3)
    assert cp == before


@pytest.mark.parametrize('prior', [None, {}, 'malformed', {'observed_at': 'invalid'},
                                  {'observed_at': STAMP(NOW - timedelta(seconds=1))}])
def test_valid_new_global_cooldown_overrides_missing_or_older_prior(prior):
    cp = checkpoint(row())
    cp['group_resolution_api_cooldown'] = prior
    batch = collect(cp, request=lambda *args: response(429))
    merged = apply(cp, batch)
    assert merged['group_resolution_api_cooldown']['retry_at'] == STAMP(NOW + timedelta(minutes=15))


@pytest.mark.parametrize('age', [0, 1, 60])
def test_global_cooldown_update_requires_strictly_newer_observation(age):
    cp = checkpoint(row())
    batch = collect(cp, request=lambda *args: response(429))
    prior = {'observed_at': STAMP(NOW + timedelta(seconds=age)), 'retry_at': STAMP(NOW + timedelta(days=3))}
    cp['group_resolution_api_cooldown'] = prior
    assert apply(cp, batch)['group_resolution_api_cooldown'] == prior


def test_growth_api_cooldown_apply_only_merges_newer_api_backoff_without_admission_mutation():
    cp = checkpoint(row())
    cp['official_results']['123'] = {'official_followers': 5000}
    before = deepcopy(cp)
    update = {'status': 'api_rate_limited', 'source': rules.SOURCE, 'attempts': 1,
              'observed_at': STAMP(NOW), 'retry_at': STAMP(NOW + timedelta(hours=1))}
    result = rules.apply_api_cooldown(cp, {'api_cooldown_update': update}, aware_time=aware_time)
    assert cp == before
    assert result['group_resolution_api_cooldown'] == update
    assert {key: value for key, value in result.items() if key != 'group_resolution_api_cooldown'} == before
    # Replayed old batches cannot shorten a newer durable API deadline.
    result['group_resolution_api_cooldown'] = {**update, 'observed_at': STAMP(NOW + timedelta(minutes=1)),
                                              'retry_at': STAMP(NOW + timedelta(days=1))}
    assert rules.apply_api_cooldown(result, {'api_cooldown_update': update}, aware_time=aware_time) == result


def test_current_queue_projects_fresh_inputs_in_order_and_appends_parked_without_time_override():
    cp = checkpoint(row())
    root = Path('frozen')
    paths = [root / 'source_queue.json', root / 'checkpoint.json', root / 'source_unresolved.json',
             Path('eligible'), Path('prefilter'), Path('cache'), Path('other')]
    reads = []
    documents = {path: {'path': str(path)} for path in paths}
    parked = row(124)
    def make_queue(projected, *inputs):
        assert projected is not cp and inputs == tuple(documents[path] for path in paths)
        projected['unresolved_candidates'] = {'124': parked}
        return [projected['pending_candidates']['123']], {'parked_group_xml_appids': ['124']}
    projected, rows = application.current_queue(cp, read=lambda path: reads.append(path) or documents[path],
                                               make_queue=make_queue, frozen=root, eligible=paths[3],
                                               prefilter=paths[4], official_cache=paths[5], original_official=paths[6])
    assert reads == paths
    assert rows == [row(), parked]
    assert 'unresolved_candidates' not in cp
    assert projected['unresolved_candidates']['124'] is parked


def test_growth_resolution_repairs_completed_follower_identity_without_changing_admission_queue():
    cp = checkpoint()
    cp['official_results']['123'] = {'official_followers': 7000}
    candidate = {'appid': 123, 'release_start': '2026-10-09', 'followers': 7000}
    before = deepcopy(cp)
    request = Mock(return_value=found(123))
    # Admission resolution keeps its existing completed-follower exclusion.
    assert collect(cp, [candidate], request=request)['requests_this_run'] == 0
    request.assert_not_called()
    batch = collect(cp, [candidate], request=request, resolve_for_growth=True)
    assert batch['requests_this_run'] == 1
    assert batch['results']['123']['group_id64'] == GROUP
    assert batch['results']['123']['candidate_fingerprint'] == FINGERPRINT(
        {**candidate, 'release_date': candidate['release_start'], 'queue_source': 'accepted_steam_growth'})
    assert cp == before


@pytest.mark.parametrize('storage', ['pending_candidates', 'official_growth_group_resolutions'])
def test_growth_resolution_reuses_identity_from_either_shared_checkpoint_store(storage):
    cp = checkpoint()
    cp[storage] = {'123': {'appid': 123, 'group_id64': GROUP}}
    request = Mock(side_effect=AssertionError('known group must not be requested'))
    batch = collect(cp, [row()], request=request, resolve_for_growth=True)
    assert batch['requests_this_run'] == 0
    request.assert_not_called()


def test_growth_resolution_respects_latest_shared_receipt_and_retries_after_its_deadline():
    cp = checkpoint(row(group_resolution={'status': 'not_found', 'checked_at': STAMP(NOW - timedelta(days=1)),
                                         'retry_at': STAMP(NOW + timedelta(days=3))}))
    cp['official_growth_group_resolutions'] = {'123': {'appid': 123, 'group_resolution': {
        'status': 'not_found', 'checked_at': STAMP(NOW - timedelta(hours=1)),
        'retry_at': STAMP(NOW + timedelta(hours=1))}}}
    request = Mock(return_value=found(123))
    assert collect(cp, request=request, resolve_for_growth=True)['requests_this_run'] == 0
    request.assert_not_called()
    batch = collect(cp, request=request, resolve_for_growth=True, now=NOW + timedelta(hours=2))
    assert batch['requests_this_run'] == 1
    assert batch['results']['123']['group_resolution']['status'] == 'resolved'


def test_growth_resolution_ignores_future_dated_no_mapping_evidence():
    cp = checkpoint()
    cp['official_growth_group_resolutions'] = {'123': {'appid': 123, 'group_resolution': {
        'status': 'not_found', 'checked_at': STAMP(NOW + timedelta(hours=1)),
        'retry_at': STAMP(NOW + timedelta(days=1))}}}
    assert collect(cp, [row()], resolve_for_growth=True)['requests_this_run'] == 1


def test_growth_resolution_does_not_bypass_shared_api_cooldown():
    cp = checkpoint()
    cp['group_resolution_api_cooldown'] = {'status': 'api_forbidden', 'retry_at': STAMP(NOW + timedelta(days=1))}
    request = Mock(side_effect=AssertionError('global API cooldown remains active'))
    batch = collect(cp, [row()], request=request, resolve_for_growth=True)
    assert batch['requests_this_run'] == 0
    assert batch['stop_reason'] == 'api_cooldown'
    request.assert_not_called()


@pytest.mark.parametrize('malformed', [None, [], 'invalid', 0])
def test_growth_resolution_rejects_malformed_independent_state(malformed):
    cp = checkpoint()
    cp['official_growth_group_resolutions'] = malformed
    request = Mock(side_effect=AssertionError('must validate state before transport'))
    with pytest.raises(ValueError, match='growth group resolution state'):
        collect(cp, [row()], request=request, resolve_for_growth=True)
    request.assert_not_called()


@pytest.mark.parametrize('steamid', [GROUP, 'malformed', True, False, 0.0, [], {}])
def test_no_match_with_contradictory_steamid_remains_retryable_invalid_response(steamid):
    batch = collect(checkpoint(row()), request=lambda *args: response(
        payload={'response': {'success': 42, 'steamid': steamid}}))
    assert batch['results']['123']['group_resolution']['status'] == 'invalid_response'
    assert 'api_cooldown_update' not in batch


def test_growth_resolution_does_not_reuse_identity_for_a_mismatched_appid():
    cp = checkpoint()
    cp['official_growth_group_resolutions'] = {'123': {'appid': 999, 'group_id64': GROUP}}
    request = Mock(return_value=found(123))
    batch = collect(cp, [row()], request=request, resolve_for_growth=True)
    assert batch['requests_this_run'] == 1
    assert batch['results']['123']['group_id64'] == GROUP


@pytest.mark.parametrize('steamid', [None, '', '0', 0])
def test_only_official_no_match_without_identity_produces_missing_group_evidence(steamid):
    batch = collect(checkpoint(row()), request=lambda *args: response(
        payload={'response': {'success': 42, 'steamid': steamid}}))
    result = batch['results']['123']['group_resolution']
    assert result == {'status': 'not_found', 'checked_at': STAMP(NOW), 'source': rules.SOURCE,
                      'attempted': True, 'http': 200, 'retry_at': STAMP(NOW + timedelta(days=1)),
                      'api_result': 42}


@pytest.mark.parametrize('failure,status', [
    (response(payload={}), 'invalid_response'),
    (response(payload={'response': {'success': 7}}), 'api_error'),
    (response(404), 'api_error'),
])
def test_growth_per_game_transient_is_actually_requested_again_at_next_six_hour_check(failure, status):
    cp = checkpoint()
    candidate = row()
    first = collect(cp, [candidate], request=lambda *args: failure, resolve_for_growth=True)
    resolution = first['results']['123']['group_resolution']
    assert resolution['status'] == status
    assert resolution['retry_at'] == STAMP(NOW + timedelta(hours=6))
    cp['official_growth_group_resolutions'] = {'123': {'appid': 123, 'group_resolution': resolution}}
    request = Mock(return_value=found(123))
    before = collect(cp, [candidate], request=request, resolve_for_growth=True,
                     now=NOW + timedelta(hours=6) - timedelta(seconds=1))
    assert before['requests_this_run'] == 0
    request.assert_not_called()
    retried = collect(cp, [candidate], request=request, resolve_for_growth=True,
                      now=NOW + timedelta(hours=6))
    assert retried['requests_this_run'] == 1
    assert retried['results']['123']['group_resolution']['status'] == 'resolved'


@pytest.mark.parametrize('mode,hours', [(False, 24), (True, 6)])
def test_growth_transient_retry_change_preserves_normal_admission_policy(mode, hours):
    batch = collect(checkpoint(row()), request=lambda *args: response(payload={}), resolve_for_growth=mode)
    assert batch['results']['123']['group_resolution']['retry_at'] == STAMP(NOW + timedelta(hours=hours))


@pytest.mark.parametrize('failure', [response(404, headers={'Retry-After': '172800'}),
                                     response(payload={}, headers={'Retry-After': '172800'}),
                                     response(payload={'response': {'success': 42}}, headers={'Retry-After': '172800'})])
def test_growth_per_game_retry_respects_later_official_retry_after(failure):
    cp = checkpoint()
    first = collect(cp, [row()], request=lambda *args: failure, resolve_for_growth=True)
    resolution = first['results']['123']['group_resolution']
    assert resolution['retry_at'] == STAMP(NOW + timedelta(days=2))
    cp['official_growth_group_resolutions'] = {'123': {'appid': 123, 'group_resolution': resolution}}
    request = Mock(side_effect=AssertionError('Steam deadline must not be shortened for six-hour retry'))
    result = collect(cp, [row()], request=request, resolve_for_growth=True, now=NOW + timedelta(hours=6))
    assert result['requests_this_run'] == 0
    request.assert_not_called()
