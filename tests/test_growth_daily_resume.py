"""Exercise overnight coverage with real pacing, shared deadlines and retries."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from radar_backend.application.growth_daily import collect_daily_observations
from radar_backend.domain.official_queue import GROUP_BASE, TAIPEI
from radar_backend.jobs.growth import main, collect
from radar_backend.state.official_followers import CooldownStore, OfficialFollowerCache


class Clock:
    def __init__(self, at=None):
        self.at = at or datetime(2026, 10, 11, 23, 15, tzinfo=timezone.utc)
        self.seconds = 0.0
        self.sleeps = []

    def now(self):
        return self.at + timedelta(seconds=self.seconds)

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.seconds += seconds


def inputs(clock, count=138, cached=120):
    old = clock.now() - timedelta(days=1)
    rows = [{'appid': aid, 'followers': 6000 + aid,
             'release_start': '2026-10-12', 'follower_checked_at': old.isoformat()}
            for aid in range(1, count + 1)]
    checkpoint = {'official_results': {
        str(aid): {'appid': aid, 'group_id64': str(GROUP_BASE + aid),
                   'official_followers': 6000 + aid,
                   'official_checked_at_taipei': (clock.now() - timedelta(hours=1) if aid <= cached else old).isoformat()}
        for aid in range(1, count + 1)}, 'pending_candidates': {},
        'unrelated_queue': {'preserve': True}}
    return rows, checkpoint


def set_deadline(checkpoint, clock, seconds):
    until = clock.now() + timedelta(seconds=seconds)
    checkpoint['next_request_after_taipei'] = until.isoformat()
    checkpoint['community_cooldown'] = {'observed_at': clock.now().isoformat(),
                                      'retry_at': until.isoformat()}
    return until


class Client:
    def __init__(self, clock, checkpoint, fail_once=None, retry_after=10):
        self.clock, self.checkpoint = clock, checkpoint
        self.fail_once, self.retry_after = fail_once, retry_after
        self.calls = []

    def fetch(self, gid):
        aid = int(gid) - GROUP_BASE
        self.calls.append((aid, self.clock.now(), self.clock.monotonic()))
        self.clock.sleep(1)  # HTTP elapsed time is charged to the runner budget.
        if self.fail_once == aid:
            self.fail_once = None
            if self.retry_after is not None:
                set_deadline(self.checkpoint, self.clock, self.retry_after)
            return SimpleNamespace(status='rate_limited', http=429, error_type=None,
                                   observed_at=self.clock.now(), followers=None)
        return SimpleNamespace(status='ok', http=200, error_type=None,
                               observed_at=self.clock.now(), followers=8000 + aid)


def run(clock, rows, checkpoint, client=None, **options):
    client = client or Client(clock, checkpoint)
    saves = []
    result = collect_daily_observations(
        rows, {}, checkpoint, client=client, cache=OfficialFollowerCache(checkpoint),
        cooldown=CooldownStore(checkpoint), clock=clock.now, sleep=clock.sleep,
        monotonic=clock.monotonic, persist=lambda cp, report: saves.append(
            (clock.now(), deepcopy(cp), deepcopy(report))), state_saved=True, **options)
    return result, client, saves


def test_actual_0715_shape_waits_17_minutes_then_measures_remaining_18():
    clock = Clock()
    rows, checkpoint = inputs(clock)
    legacy = deepcopy(checkpoint['official_results'])
    deadline = set_deadline(checkpoint, clock, 17 * 60 + 29)
    result, client, saves = run(clock, rows, checkpoint)
    assert len(client.calls) == result['requests'] == 18
    assert all(at >= deadline for _, at, _ in client.calls)
    assert all(b[2] - a[2] >= 30 for a, b in zip(client.calls, client.calls[1:]))
    assert result['reused'] == 120 and result['eligible'] == 138
    assert result['measurement_complete'] and result['collection_complete']
    assert result['coverage'] == {'eligible': 138, 'measured': 138, 'unavailable': 0, 'pending': 0}
    assert result['daily_budget']['cooldown_wait_seconds'] == 1049
    assert result['daily_budget']['passes'] == 2
    assert checkpoint['official_results'] == legacy
    assert checkpoint['next_request_after_taipei'] == deadline.isoformat()
    assert saves[0][0] < deadline and saves[0][2]['reason'] == 'rate_limited'
    assert all(point['followers'] > 0 for point in result['measurements'])


def test_429_resume_totals_real_attempts_without_calling_new_points_old_reuse():
    clock = Clock()
    rows, checkpoint = inputs(clock, count=3, cached=0)
    result, client, saves = run(clock, rows, checkpoint,
                              Client(clock, checkpoint, fail_once=2))
    assert result['requests'] == 4 and result['reused'] == 0
    assert [event['http'] for event in result['events']] == [200, 429, 200, 200]
    assert result['collection_complete'] and len(result['measurements']) == 3
    assert all(b[2] - a[2] >= 30 for a, b in zip(client.calls, client.calls[1:]))
    assert any(report.get('reason') == 'rate_limited' and report['measurements'] for _, _, report in saves)


@pytest.mark.parametrize('seconds,options,stop', [
    (7200, {}, 'cooldown_exceeds_wait_budget'),
    (1049, {'max_seconds': 700}, 'cooldown_exceeds_runner_budget'),
    (1049, {'max_cooldown_wait': 0}, 'cooldown_exceeds_wait_budget'),
])
def test_long_cooldown_does_not_clear_deadline_or_fake_new_measurements(seconds, options, stop):
    clock = Clock()
    rows, checkpoint = inputs(clock)
    original_deadline = set_deadline(checkpoint, clock, seconds)
    result, client, _ = run(clock, rows, checkpoint, **options)
    assert not client.calls and result['requests'] == 0
    assert len(result['measurements']) == 120 and len(result['pending']) == 18
    assert not result['measurement_complete'] and not result['collection_complete']
    assert result['daily_budget']['stop_reason'] == stop
    assert checkpoint['next_request_after_taipei'] == original_deadline.isoformat()
    assert not clock.sleeps


def test_request_budget_is_shared_across_429_passes():
    clock = Clock()
    rows, checkpoint = inputs(clock, count=3, cached=0)
    result, client, _ = run(clock, rows, checkpoint,
                           Client(clock, checkpoint, fail_once=2), max_requests=3)
    assert len(client.calls) == result['requests'] == 3
    assert len(result['measurements']) == 2 and result['reused'] == 0
    assert len(result['pending']) == 1 and not result['collection_complete']


def test_no_future_cooldown_receipt_does_not_busy_retry_a_429():
    clock = Clock()
    rows, checkpoint = inputs(clock, count=1, cached=0)
    result, client, _ = run(clock, rows, checkpoint,
                           Client(clock, checkpoint, fail_once=1, retry_after=None))
    assert len(client.calls) == 1 and result['requests'] == 1
    assert result['daily_budget']['stop_reason'] == 'cooldown_without_future_deadline'
    assert not result['collection_complete']


def test_cooldown_crossing_taipei_midnight_does_not_merge_different_days():
    clock = Clock(datetime(2026, 10, 12, 15, 58, tzinfo=timezone.utc))
    rows, checkpoint = inputs(clock, count=2, cached=1)
    set_deadline(checkpoint, clock, 300)
    result, client, _ = run(clock, rows, checkpoint)
    assert not client.calls and not result['collection_complete']
    assert result['collection_date_taipei'] == '2026-10-12'
    assert result['daily_budget']['stop_reason'] == 'collection_day_changed'


def test_all_current_points_need_no_wait_even_during_shared_cooldown():
    clock = Clock()
    rows, checkpoint = inputs(clock, count=138, cached=138)
    set_deadline(checkpoint, clock, 7200)
    result, client, _ = run(clock, rows, checkpoint, max_requests=0)
    assert not client.calls and not clock.sleeps
    assert result['reused'] == 138 and result['collection_complete']


def test_actual_late_wakeup_is_charged_to_wait_budget_and_stops_before_requests():
    class LateClock(Clock):
        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.seconds += seconds + 50

    clock = LateClock()
    rows, checkpoint = inputs(clock)
    set_deadline(checkpoint, clock, 1049)
    result, client, _ = run(clock, rows, checkpoint, max_cooldown_wait=1050)
    assert not client.calls and not result['collection_complete']
    assert result['daily_budget']['stop_reason'] == 'cooldown_exceeds_wait_budget'
    assert result['daily_budget']['cooldown_wait_seconds'] == clock.seconds
    assert clock.seconds > sum(clock.sleeps)


def test_deadline_expiring_between_two_clock_reads_resumes_collection():
    clock = Clock()
    rows, checkpoint = inputs(clock, count=1, cached=0)
    deadline = set_deadline(checkpoint, clock, 1)

    class CrossingCooldown(CooldownStore):
        reads = 0

        def blocked(self, now):
            self.reads += 1
            blocked = super().blocked(now)
            if self.reads == 2 and blocked:
                clock.sleep(2)  # Deadline passes after the condition's read.
            return blocked

    client = Client(clock, checkpoint)
    result = collect_daily_observations(
        rows, {}, checkpoint, client=client, cache=OfficialFollowerCache(checkpoint),
        cooldown=CrossingCooldown(checkpoint), clock=clock.now, sleep=clock.sleep,
        monotonic=clock.monotonic, persist=lambda *_: None)
    assert result['collection_complete'] and result['requests'] == 1
    assert client.calls[0][1] >= deadline


@pytest.mark.parametrize('entry', [['-m', 'scripts.collect_public_growth'],
                                  ['scripts/collect_public_growth.py'],
                                  ['-m', 'radar_backend.jobs.growth']])
def test_actual_workflow_cli_forwards_new_budgets_and_reuses_real_cached_points(tmp_path, entry):
    clock = Clock(datetime.now(timezone.utc))
    rows, checkpoint = inputs(clock, count=138, cached=138)
    for row in rows:
        row['release_start'] = clock.now().astimezone(TAIPEI).date().isoformat()
    for cached in checkpoint['official_results'].values():
        cached['official_checked_at_taipei'] = clock.now().isoformat()
    (tmp_path / 'catalog.json').write_text(json.dumps({'count': 138, 'games': rows}))
    state = tmp_path / 'checkpoint.json'
    state.write_text(json.dumps(checkpoint))
    absent = str(tmp_path / 'absent.json')
    output = tmp_path / 'report.json'
    command = [sys.executable, *entry, '--data-dir', str(tmp_path), '--output', str(output),
               '--checkpoint', str(state), '--official-cache', absent,
               '--legacy-checkpoint', absent, '--group-state', absent,
               '--original-checkpoint', absent, '--max-requests', '250',
               '--max-seconds', '9000', '--wait-for-cooldown', '--max-cooldown-wait', '3600']
    done = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True,
                          text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    report = json.loads(output.read_text())
    assert report['reused'] == 138 and report['requests'] == 0
    assert report['collection_complete'] and report['max_seconds'] == 9000
    assert not report['job_result']['state_persisted'] and not report['job_result']['published']


def test_resuming_xml_429_does_not_reset_the_whole_run_group_api_budget(tmp_path):
    clock = Clock()
    rows, checkpoint = inputs(clock, count=25, cached=0)
    for aid in range(1, 25):
        checkpoint['official_results'][str(aid)].pop('group_id64')
    (tmp_path / 'catalog.json').write_text(json.dumps({'count': 25, 'games': rows}))
    state = tmp_path / 'checkpoint.json'
    state.write_text(json.dumps(checkpoint))

    class Session:
        headers = {}

        def __init__(self):
            self.api_calls = 0
            self.xml_calls = 0

        def get(self, url, **kwargs):
            clock.sleep(1)
            if 'ResolveVanityURL' in url:
                self.api_calls += 1
                aid = int(kwargs['params']['vanityurl'])
                return SimpleNamespace(status_code=200, headers={}, json=lambda: {
                    'response': {'success': 1, 'steamid': str(GROUP_BASE + aid)}})
            self.xml_calls += 1
            if self.xml_calls == 1:
                return SimpleNamespace(status_code=429, headers={'Retry-After': '1'})
            gid = url.split('/gid/')[1].split('/')[0]
            return SimpleNamespace(status_code=200, headers={}, content=(
                f'<memberList><groupID64>{gid}</groupID64><memberCount>9000</memberCount></memberList>').encode())

    session = Session()
    result = collect(tmp_path, tmp_path / 'report.json', checkpoint_path=state,
                     session=session, api_key='offline-test-key', clock=clock.now,
                     monotonic=clock.monotonic, sleep=clock.sleep, wait_for_cooldown=True,
                     max_requests=250, max_seconds=9000)
    assert session.api_calls == result['group_resolution_requests'] == 20
    assert result['requests'] == session.xml_calls == 22
    assert result['reused'] == 0 and len(result['measurements']) == 21
    assert len(result['pending']) == 4 and not result['collection_complete']
    assert result['reason'] == 'awaiting_group_resolution'
    assert result['group_resolution_batches'][-1]['stop_reason'] == 'growth_group_budget_exhausted'


@pytest.mark.parametrize('argument,value', [('--interval', 'nan'), ('--interval', 'inf'),
                                         ('--max-seconds', 'nan'), ('--max-seconds', '9001'),
                                         ('--max-cooldown-wait', 'inf'), ('--max-requests', '251')])
def test_cli_rejects_unbounded_or_unpaced_settings(monkeypatch, argument, value):
    monkeypatch.setattr('sys.argv', ['growth', '--data-dir', 'data', '--output', 'report.json', argument, value])
    with pytest.raises(SystemExit) as raised:
        main(collector=lambda *args, **kwargs: pytest.fail('must reject before collection'))
    assert raised.value.code == 2
