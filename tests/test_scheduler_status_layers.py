"""Offline scheduler projection rules and explicit read/save port behavior."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from radar_core.domain.twitch_admission import aware_time
from radar_backend.domain.official_queue import valid_group_id64
from radar_backend.domain import scheduler_status as rules
from radar_backend.application import scheduler_status as application

TZ = ZoneInfo('Asia/Taipei')
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
GROUP = '103582791429521531'
UTC = partial(rules.utc, aware_time=aware_time)
LATEST = partial(rules.latest_time, aware_time=aware_time, utc=UTC)
SLOT = partial(rules.next_official_slot, aware_time=aware_time, utc=UTC, tz=TZ)


def row(aid=123, *, priority=False, group=GROUP, **values):
    return {'appid': aid, 'name': f'Game {aid}', 'release_date': '2026-10-10',
            'group_id64': group, 'queue_source': rules.TWITCH_SOURCE if priority else 'normal', **values}


def checkpoint(*rows):
    return {'pending_candidates': {str(item['appid']): deepcopy(item) for item in rows},
            'official_results': {}, 'attempt_events': [],
            'created_at_taipei': '2026-10-09T03:00:00+08:00'}


def project(cp, *, now=NOW, legacy=None, queue=None, status=None, **ports):
    def make_queue(projected, *inputs, now):
        if queue is None:
            return list(projected['pending_candidates'].values()), status or {'parked_group_xml_appids': []}
        return queue(projected, *inputs, now=now)
    defaults = dict(now=now, make_queue=make_queue, aware_time=aware_time,
                    utc=UTC, latest_time=LATEST, next_official_slot=SLOT,
                    public_run=rules.public_run, valid_group_id=valid_group_id64,
                    tz=TZ, checkpoint_path=Path('data/current-checkpoint.json'))
    defaults.update(ports)
    return rules.build_status(cp, [], legacy or {}, [], {}, {}, {}, {}, **defaults)


@pytest.mark.parametrize('value,expected', [(NOW, '2026-10-09T12:00:00Z'),
                                          (NOW.astimezone(TZ), '2026-10-09T12:00:00Z'),
                                          ('2026-10-09T20:00:00+08:00', '2026-10-09T12:00:00Z'),
                                          ('2026-10-09T12:00:00Z', '2026-10-09T12:00:00Z'),
                                          (None, None), ('bad', None), ('2026-10-09T12:00:00', None),
                                          (NOW.replace(tzinfo=None), None), (True, None), (123, None)])
def test_utc_accepts_aware_values_and_keeps_naive_invalid_values_none(value, expected):
    assert UTC(value) == expected


def test_utc_aware_datetime_skips_parser_and_string_uses_current_parser():
    parser = Mock(side_effect=AssertionError('must not reparse aware datetime'))
    assert rules.utc(NOW, aware_time=parser) == UTC(NOW)
    parser.assert_not_called()
    parser = Mock(return_value=NOW)
    assert rules.utc('custom', aware_time=parser) == UTC(NOW)
    parser.assert_called_once_with('custom')


@pytest.mark.parametrize('values,expected', [([], None), ([None, 'bad', 1], None),
                                           (['2026-10-09T12:00:00Z'], '2026-10-09T12:00:00Z'),
                                           (['2026-10-09T21:00:00+08:00', '2026-10-09T12:00:00Z'], '2026-10-09T13:00:00Z'),
                                           (['bad', '2026-10-09T12:00:00Z', None], '2026-10-09T12:00:00Z')])
def test_latest_time_ignores_bad_values_and_compares_aware_instants(values, expected):
    assert LATEST(values) == expected


@pytest.mark.parametrize('hour', range(24))
@pytest.mark.parametrize('minute', [0, 1])
def test_next_slot_is_first_allowed_hour_03_through_23_taipei(hour, minute):
    observed = datetime(2026, 10, 9, hour, minute, tzinfo=TZ)
    expected = observed.replace(minute=0)
    if minute:
        expected += timedelta(hours=1)
    while not 3 <= expected.hour <= 23:
        expected += timedelta(hours=1)
    assert SLOT(observed) == UTC(expected)


@pytest.mark.parametrize('deadline', [None, 'bad', '2026-10-09T11:00:00Z',
                                     '2026-10-09T12:00:00Z', '2026-10-09T16:30:00Z'])
def test_next_slot_honors_only_later_valid_cooldown_deadline(deadline):
    parsed = aware_time(deadline)
    after = max(NOW, parsed or NOW).astimezone(TZ)
    expected = after.replace(minute=0, second=0, microsecond=0)
    if expected < after:
        expected += timedelta(hours=1)
    while not 3 <= expected.hour <= 23:
        expected += timedelta(hours=1)
    assert SLOT(NOW, deadline) == UTC(expected)


@pytest.mark.parametrize('run_id,expected', [(1, '1'), ('123', '123'), ('00123', '00123'),
                                          ('１２３', '１２３'), (True, None), (False, None),
                                          (0, None), ('0', '0'), (-1, None), (1.0, None),
                                          (None, None), ('', None), ('123x', None),
                                          ('https://evil.test/', None)])
def test_public_run_retains_original_decimal_rules_and_rejects_supplied_urls(run_id, expected):
    result = rules.public_run({'run_id': run_id, 'run_url': 'https://evil.test/'}, repository='example/repo')
    assert result == ({} if expected is None else {'run_id': expected,
                     'run_url': f'https://github.com/example/repo/actions/runs/{expected}'})


def test_public_run_falsy_primary_uses_alternate_but_truthy_invalid_primary_does_not():
    assert rules.public_run({'run_id': 0, 'github_run_id': 123})['run_id'] == '123'
    assert rules.public_run({'run_id': False, 'github_run_id': 123})['run_id'] == '123'
    assert rules.public_run({'run_id': 'invalid', 'github_run_id': 123}) == {}


def test_projection_deepcopies_queue_mutations_parked_groups_and_cooldown_without_source_write():
    cp = checkpoint(row(123), row(124, group=None, priority=True))
    cp['group_resolution_api_cooldown'] = {'status': 'api_rate_limited', 'retry_at': '2026-10-10T12:00:00Z'}
    cp['pending_candidates']['123']['group_resolution'] = {'status': 'resolved', 'checked_at': UTC(NOW)}
    before = deepcopy(cp)
    parked = row(125, group=None, status='official_xml_fallback_returned_html', resolution='retry group')
    def queue(projected, *inputs, now):
        assert projected is not cp and now is NOW
        projected['pending_candidates']['123']['name'] = 'fresh projection'
        projected['unresolved_candidates'] = {'125': parked}
        return [projected['pending_candidates']['124'], projected['pending_candidates']['123']], {'parked_group_xml_appids': ['125']}
    result = project(cp, queue=queue)
    assert cp == before
    assert result['summary'] == {'normal_pending': 1, 'twitch_priority_pending': 1, 'parked': 1,
                                 'ready_pending': 2, 'followers_ready_pending': 1,
                                 'awaiting_group_pending': 1, 'total_pending': 3,
                                 'today_attempts': 0, 'today_successes': 0, 'today_429': 0}
    assert [item['state'] for item in result['queue']] == ['awaiting_group', 'waiting']
    assert [item['position'] for item in result['queue']] == [1, 2]
    assert result['queue'][1]['name'] == 'fresh projection'
    assert result['parked'][0]['state'] == 'parked'
    assert result['parked'][0]['reason'] == 'official_xml_fallback_returned_html'
    result['queue'][1]['group_resolution']['status'] = 'modified'
    result['group_resolution_api_cooldown']['status'] = 'modified'
    assert cp == before


def test_future_invalid_and_non_dict_events_are_omitted_today_counts_use_taipei_day_and_latest_attempt():
    cp = checkpoint(row())
    cp['attempt_events'] = [
        {'appid': 123, 'when_taipei': '2026-10-09T03:00:00+08:00', 'status': 'ok', 'official_followers': 0},
        {'appid': 123, 'when_taipei': '2026-10-09T02:00:00+08:00', 'status': 'http_error', 'http': 429},
        {'appid': 123, 'when_taipei': '2026-10-08T23:59:59+08:00', 'status': 'ok'},
        {'appid': 123, 'when_taipei': '2026-10-09T20:00:01+08:00', 'status': 'future'},
        {'appid': 123, 'when_taipei': 'bad', 'status': 'invalid'}, None,
    ]
    result = project(cp)
    assert [item['at'] for item in result['events']] == ['2026-10-08T15:59:59Z', '2026-10-08T18:00:00Z', '2026-10-08T19:00:00Z']
    assert result['summary']['today_attempts'] == 2
    assert result['summary']['today_successes'] == 1
    assert result['summary']['today_429'] == 1
    assert result['queue'][0]['last_attempt_at'] == '2026-10-08T19:00:00Z'
    assert result['queue'][0]['last_attempt_status'] == 'ok'


@pytest.mark.parametrize('count', [0, 1, 99, 100, 101, 130])
def test_event_limit_is_last_100_after_sort_while_summary_counts_all_today_events(count):
    cp = checkpoint(row())
    cp['attempt_events'] = [{'appid': 123, 'when_taipei': UTC(NOW - timedelta(seconds=i)), 'status': 'ok'} for i in range(count)]
    result = project(cp)
    assert result['events_limit'] == 100
    assert len(result['events']) == min(count, 100)
    assert result['summary']['today_attempts'] == count
    assert result['summary']['today_successes'] == count
    if count:
        assert result['events'][-1]['at'] == UTC(NOW)


def test_event_projection_retains_safe_fields_manual_override_and_derived_run_links():
    cp = checkpoint(row())
    cp['official_results']['123'] = {'appid': 123, 'name': 'official latest name'}
    cp['attempt_events'] = [{'appid': 123, 'when_taipei': UTC(NOW), 'status': 'error', 'http': 429,
                             'queue_source': 'normal', 'official_followers': 0, 'run_id': 123,
                             'run_url': 'https://evil.test/', 'error_type': 'ParseError',
                             'next_request_after_taipei': UTC(NOW + timedelta(minutes=15)),
                             'manual_cooldown_override': True, 'private': 'excluded'}]
    item = project(cp)['events'][0]
    assert item['name'] == 'official latest name'
    assert item['official_followers'] == 0
    assert item['error_type'] == 'ParseError'
    assert item['manual_cooldown_override'] is True
    assert item['run_url'].endswith('/actions/runs/123')
    assert 'private' not in item and 'evil.test' not in str(item)


@pytest.mark.parametrize('override', [None, False, 'true', 1])
def test_event_manual_override_requires_literal_true(override):
    cp = checkpoint(row())
    cp['attempt_events'] = [{'appid': 123, 'when_taipei': UTC(NOW), 'manual_cooldown_override': override}]
    assert 'manual_cooldown_override' not in project(cp)['events'][0]


@pytest.mark.parametrize('newer', ['primary', 'legacy'])
def test_community_cooldown_uses_latest_checkpoint_and_keeps_group_api_cooldown_separate(newer):
    cp = checkpoint(row(), row(124, group=None))
    old = UTC(NOW + timedelta(minutes=15))
    new = UTC(NOW + timedelta(hours=5))
    cp['next_request_after_taipei'] = new if newer == 'primary' else old
    cp['attempt_events'] = [{'appid': 123, 'when_taipei': UTC(NOW - timedelta(minutes=1)), 'http': 429, 'status': 'rate_limited'}]
    cp['group_resolution_api_cooldown'] = {'retry_at': UTC(NOW + timedelta(days=2))}
    legacy = {'next_request_after_taipei': old if newer == 'primary' else new}
    result = project(cp, legacy=legacy)
    assert result['cooldown']['active'] is True
    assert result['cooldown']['until'] == new
    assert result['cooldown']['reason'] == 'steam_http_429'
    assert result['cooldown']['next_eligible_slot'] == '2026-10-09T19:00:00Z'
    assert [item['state'] for item in result['queue']] == ['cooldown', 'awaiting_group']


@pytest.mark.parametrize('deadline', [None, 'bad', UTC(NOW), UTC(NOW - timedelta(seconds=1))])
def test_inactive_cooldown_has_no_reason_and_ready_groups_wait(deadline):
    cp = checkpoint(row())
    cp['next_request_after_taipei'] = deadline
    result = project(cp)
    assert result['cooldown']['active'] is False
    assert result['cooldown']['reason'] is None
    assert result['queue'][0]['state'] == 'waiting'


def test_empty_queue_has_no_next_slot_even_with_active_cooldown():
    cp = checkpoint()
    cp['next_request_after_taipei'] = UTC(NOW + timedelta(hours=1))
    forbidden = Mock(side_effect=AssertionError('must not compute slot for empty queue'))
    result = project(cp, next_official_slot=forbidden)
    assert result['cooldown']['active'] is True
    assert result['cooldown']['next_eligible_slot'] is None
    forbidden.assert_not_called()


def test_source_times_batch_whitelist_and_runtime_constants_are_projected():
    cp = checkpoint(row())
    cp['scheduler_batch'] = {'active': True, 'status': 'working', 'run_id': 123, 'private': 'excluded',
                             'last_updated_at': UTC(NOW - timedelta(minutes=1)), 'request_limit': 17}
    cp['pending_candidates']['123']['group_resolution'] = {'checked_at': UTC(NOW + timedelta(minutes=1))}
    cp['group_resolution_api_cooldown'] = {'observed_at': UTC(NOW + timedelta(minutes=2))}
    candidate = {'updated_at': '2026-10-09T20:00:00+08:00'}
    twitch = {'updated_at': UTC(NOW - timedelta(hours=1))}
    result = project(cp, repository='example/repo', checkpoint_path=Path('runtime/checkpoint.json'),
                     candidate_state=candidate, twitch_state=twitch, max_events=7)
    assert result['source']['repository'] == 'example/repo'
    assert result['source']['checkpoint_path'] == 'runtime/checkpoint.json'
    assert result['source']['checkpoint_updated_at'] == UTC(NOW - timedelta(minutes=1))
    assert result['source']['group_resolution_updated_at'] == UTC(NOW + timedelta(minutes=2))
    assert result['source']['candidate_state_updated_at'] == UTC(NOW)
    assert result['source']['twitch_import_updated_at'] == UTC(NOW - timedelta(hours=1))
    assert result['events_limit'] == 7
    assert result['batch']['request_limit'] == 17
    assert 'private' not in result['batch']


@pytest.mark.parametrize('batch', [None, 'invalid', [], 17])
def test_non_dict_scheduler_batch_projects_none(batch):
    cp = checkpoint()
    cp['scheduler_batch'] = batch
    assert project(cp)['batch'] is None


def test_parked_rows_are_ordered_by_release_then_numeric_appid():
    cp = checkpoint()
    rows = [row(125, release_date='2026-10-11', status='parked'),
            row(124, release_date='2026-10-10', status='parked'),
            row(123, release_date='2026-10-10', status='parked')]
    def queue(projected, *inputs, now):
        projected['unresolved_candidates'] = {str(item['appid']): item for item in rows}
        return [], {'parked_group_xml_appids': ['125', '124', '123']}
    assert [item['appid'] for item in project(cp, queue=queue)['parked']] == [123, 124, 125]


def test_naive_snapshot_fails_before_queue_projection_and_copy():
    forbidden = Mock(side_effect=AssertionError('must not project'))
    with pytest.raises(ValueError, match='aware time'):
        project(checkpoint(row()), now=NOW.replace(tzinfo=None), make_queue=forbidden, deepcopy_fn=forbidden)
    forbidden.assert_not_called()


def export_ports():
    frozen = Path('frozen')
    paths = [Path('current'), frozen / 'source_queue.json', frozen / 'checkpoint.json',
             frozen / 'source_unresolved.json', Path('eligible'), Path('prefilter'),
             Path('cache'), Path('other'), Path('candidate'), Path('twitch')]
    documents = {path: {'path': str(path)} for path in paths}
    ports = dict(output=Path('destination/status.json'), clock=lambda: NOW,
                 read=lambda path: documents[path], is_file=lambda path: True,
                 save=lambda path, value: None, build_status=lambda *args, **kw: {'status': 'projected'},
                 checkpoint_path=paths[0], frozen=frozen, eligible_path=paths[4],
                 prefilter_path=paths[5], official_cache_path=paths[6], original_official_path=paths[7],
                 candidate_state_path=paths[8], twitch_state_path=paths[9])
    return ports, paths, documents


@pytest.mark.parametrize('candidate_exists,twitch_exists', [(True, True), (True, False), (False, True), (False, False)])
def test_export_reads_original_order_optional_inputs_and_writes_only_requested_destination(candidate_exists, twitch_exists):
    ports, paths, documents = export_ports()
    events = []
    exists = {paths[8]: candidate_exists, paths[9]: twitch_exists}
    def read(path):
        events.append(('read', path))
        return documents[path]
    def is_file(path):
        events.append(('exists', path))
        return exists[path]
    build = Mock(return_value={'status': 'projected'})
    save = Mock()
    clock = Mock(return_value=NOW)
    result = application.export_status(**{**ports, 'read': read, 'is_file': is_file, 'build_status': build,
                                         'save': save, 'clock': clock})
    assert events[:8] == [('read', path) for path in paths[:8]]
    expected_tail = [('exists', paths[8])]
    if candidate_exists:
        expected_tail.append(('read', paths[8]))
    expected_tail.append(('exists', paths[9]))
    if twitch_exists:
        expected_tail.append(('read', paths[9]))
    assert events[8:] == expected_tail
    build.assert_called_once_with(*(documents[path] for path in paths[:8]), now=NOW,
                                  candidate_state=documents[paths[8]] if candidate_exists else {},
                                  twitch_state=documents[paths[9]] if twitch_exists else {})
    save.assert_called_once_with(ports['output'], result)
    clock.assert_called_once_with()


def test_export_explicit_now_skips_clock_and_returns_identical_saved_object():
    ports, _, _ = export_ports()
    clock = Mock(side_effect=AssertionError('must not read a new clock'))
    payload = {'nested': {'value': 1}}
    save = Mock()
    result = application.export_status(**{**ports, 'now': NOW, 'clock': clock,
                                         'build_status': lambda *args, **kw: payload, 'save': save})
    assert result is payload
    assert save.call_args.args[1] is payload
    clock.assert_not_called()


@pytest.mark.parametrize('stage', ['read', 'is_file', 'build_status', 'save'])
def test_export_propagates_port_errors_without_extra_writes(stage):
    ports, _, _ = export_ports()
    save = Mock()
    ports['save'] = save
    failed = Mock(side_effect=OSError(stage))
    ports[stage] = failed
    with pytest.raises(OSError, match=stage):
        application.export_status(**ports)
    if stage != 'save':
        save.assert_not_called()
    failed.assert_called()
