"""Real SQLite primary checkpoints and the external-dispatch durability gate."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from radar_backend.application.content_dispatch import ContentDispatchServices, dispatch_content_event
from radar_backend.domain.content_dispatch import DispatchConfiguration, DispatchDelivery
from radar_backend.publication.database_intents import REPOSITORY, persist_dispatch_state
from radar_backend.publication.official_checkpoint import CHECKPOINT, MASTER, COHORT
from radar_backend.publication.steam import write_json
from radar_backend.state.growth import GrowthProgressWriter
from radar_core.storage import documents, primary, primary_store
from radar_core.storage.sql import SqliteClient, StorageError

NOW = datetime(2026, 10, 15, 10, tzinfo=timezone.utc)
SOURCE = REPOSITORY + '@main'


def checkpoint():
    return {'version': 1, 'cohort': COHORT, 'official_results': {},
            'pending_candidates': {}, 'attempt_events': [], 'content_dispatches': {},
            'rate_limit_count': 0, 'next_request_after_taipei': None}


def raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()


@pytest.fixture
def db(tmp_path, monkeypatch):
    database = SqliteClient()
    documents.initialize(database)
    cp, master = checkpoint(), {'version': 1, 'games': [], 'count': 0}
    documents.publish_snapshot(database, SOURCE, 'a' * 40,
        {CHECKPOINT: raw(cp), MASTER: raw(master), 'data/other.json': b'{ "keep": 42 }\n'}, NOW.isoformat())
    primary_store.activate_primary(database, SOURCE)
    monkeypatch.setenv('RADAR_DATABASE_PRIMARY', '1')
    monkeypatch.setattr(primary, 'client', lambda: database)
    write_json(tmp_path / CHECKPOINT, cp)
    write_json(tmp_path / MASTER, master)
    yield database
    database.close()


def publisher(root):
    cp = json.loads((root / CHECKPOINT).read_bytes())
    master = json.loads((root / MASTER).read_bytes())
    return SimpleNamespace(root=root, baseline={'checkpoint': deepcopy(cp), 'master': deepcopy(master)},
        baseline_present={'checkpoint': True, 'master': True}, clock=lambda: NOW,
        pending_path=root / 'output/official-pending.json'), cp, master


def result():
    return {'appid': 123, 'official_followers': 6000, 'release_date': '2026-10-25',
            'store_date_exact': True, 'release_display_precision': 'date_full',
            'official_checked_at_taipei': NOW.isoformat(),
            'group_id64': '103582791429521531', 'official_source': 'Steam Community XML memberCount'}


def test_dispatch_sees_acknowledged_master_and_pending_intent_before_post(db, tmp_path):
    owner, cp, master = publisher(tmp_path)
    observation = result()
    cp['official_results']['123'] = observation
    master['games'].append({'appid': 123, 'followers': 6000, 'follower_checked_at': NOW.isoformat()})
    master['count'] = 1
    called = []
    def send(config, payload):
        current = primary.read_json(REPOSITORY, CHECKPOINT)
        assert current['content_dispatches']['123']['status'] == 'pending'
        assert primary.read_json(REPOSITORY, MASTER)['games'][0]['appid'] == 123
        called.append(payload)
        return DispatchDelivery(http=204)
    services = ContentDispatchServices(lambda: NOW,
        lambda: DispatchConfiguration('owner/content', 'secret', REPOSITORY), send, lambda *a, **k: None)
    assert dispatch_content_event(cp, observation, services=services,
        persist=lambda state: persist_dispatch_state(owner, state, master)) == 'dispatched'
    assert len(called) == 1
    assert primary.read_json(REPOSITORY, CHECKPOINT)['content_dispatches']['123']['status'] == 'dispatched'
    assert cp['official_results']['123'] is observation
    assert primary.read_files(REPOSITORY, ['data/other.json'])[1]['data/other.json'] == b'{ "keep": 42 }\n'
    assert db.execute('SELECT COUNT(*) AS n FROM radar_operations')[0]['n'] == 2


def test_database_failure_never_sends_and_preserves_replayable_intent(db, tmp_path, monkeypatch):
    owner, cp, master = publisher(tmp_path)
    cp['official_results']['123'] = result()
    def fail(*args, **kwargs):
        raise StorageError('PRIMARY_UNAVAILABLE', 'Primary unavailable')
    monkeypatch.setattr(primary, 'write_files', fail)
    services = ContentDispatchServices(lambda: NOW,
        lambda: DispatchConfiguration('owner/content', 'secret'),
        lambda *args: pytest.fail('POST cannot precede DB ACK'), lambda *a, **k: None)
    with pytest.raises(StorageError):
        dispatch_content_event(cp, result(), services=services,
            persist=lambda state: persist_dispatch_state(owner, state, master))
    pending = json.loads(owner.pending_path.read_bytes())
    assert pending['recovery_eligible'] is True
    assert pending['observed']['checkpoint']['content_dispatches']['123']['status'] == 'pending'


def test_intent_cas_conflict_replays_frozen_changes_and_preserves_concurrent_queue(db, tmp_path, monkeypatch):
    owner, cp, master = publisher(tmp_path)
    cp['official_results']['123'] = result()
    original = primary.write_files
    attempts = []
    def write(repository, files, **kwargs):
        attempts.append(kwargs['operation_id'])
        if len(attempts) == 1:
            head, remote = primary.read_files(REPOSITORY, [CHECKPOINT])
            value = json.loads(remote[CHECKPOINT])
            value['pending_candidates']['999'] = {'appid': 999, 'name': 'Concurrent Twitch'}
            original(repository, {CHECKPOINT: raw(value)}, base_generation=head['generation'],
                     operation_id='competing-queue-write')
        return original(repository, files, **kwargs)
    monkeypatch.setattr(primary, 'write_files', write)
    persist_dispatch_state(owner, cp, master)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    saved = primary.read_json(REPOSITORY, CHECKPOINT)
    assert saved['pending_candidates']['999']['appid'] == 999
    assert saved['official_results']['123']['official_followers'] == 6000


def test_dispatch_never_sends_an_observation_superseded_during_db_merge(db, tmp_path):
    owner, cp, master = publisher(tmp_path)
    observation = result()
    cp['official_results']['123'] = observation
    head, files = primary.read_files(REPOSITORY, [CHECKPOINT])
    newer = json.loads(files[CHECKPOINT])
    newer['official_results']['123'] = {**observation, 'official_followers': 9000,
                                       'official_checked_at_taipei': '2026-10-15T11:00:00Z'}
    primary.write_files(REPOSITORY, {CHECKPOINT: raw(newer)},
                        base_generation=head['generation'], operation_id='newer-count')
    services = ContentDispatchServices(lambda: NOW,
        lambda: DispatchConfiguration('owner/content', 'secret'),
        lambda *args: pytest.fail('A superseded event must not be sent'), lambda *a, **k: None)
    assert dispatch_content_event(cp, observation, services=services,
        persist=lambda state: persist_dispatch_state(owner, state, master)) == 'superseded'
    assert cp['official_results']['123']['official_followers'] == 9000


def test_growth_observation_is_durable_before_return_and_db_failure_stops(db, tmp_path, monkeypatch):
    output = tmp_path / 'output/growth.json'
    path = tmp_path / CHECKPOINT
    writer = GrowthProgressWriter(output, path)
    cp = json.loads(path.read_bytes())
    cp['official_growth_observations'] = {'123': result()}
    report = {'measurements': [{'appid': 123, 'followers': 6000, 'at': NOW.isoformat()}], 'events': []}
    writer.save(cp, report)
    assert primary.read_json(REPOSITORY, CHECKPOINT)['official_growth_observations']['123']['official_followers'] == 6000
    assert json.loads(output.with_name('growth-database-pending.json').read_bytes())['recovery_eligible'] is False
    cp['official_growth_observations']['123']['official_followers'] = 7000
    cp['official_growth_observations']['123']['official_checked_at_taipei'] = '2026-10-15T11:00:00+00:00'
    def fail(*a, **k):
        raise StorageError('PRIMARY_UNAVAILABLE', 'Primary unavailable')
    monkeypatch.setattr(primary, 'write_files', fail)
    with pytest.raises(StorageError):
        writer.save(cp, report)
    pending = json.loads(output.with_name('growth-database-pending.json').read_bytes())
    assert pending['recovery_eligible'] is True
    assert pending['observed']['official_growth_observations']['123']['official_followers'] == 7000


def test_twitch_collection_uses_and_pins_database_bytes_before_collecting(db, monkeypatch):
    from radar_backend.jobs import reconcile_twitch_official_queue as job
    files = {name: raw({'database_path': name}) for name in job.TWITCH_INPUTS}
    head = documents.publish_snapshot(db, job.FRONTEND_SOURCE, 'b' * 40, files, NOW.isoformat())
    primary_store.activate_primary(db, job.FRONTEND_SOURCE)
    sequence = []
    original_pin = primary_store.pin_evidence
    def pin(*args, **kwargs):
        sequence.append('pin')
        return original_pin(*args, **kwargs)
    def collect(root, proof, master, state, **kwargs):
        sequence.append('collect')
        assert proof == {'source_database_revision': head['manifest_digest'],
            'source_database_generation': head['generation'], 'source_database_namespace': job.FRONTEND_SOURCE}
        assert {name: (root / name).read_bytes() for name in files} == files
        return {'database': True}
    monkeypatch.setattr(primary_store, 'pin_evidence', pin)
    monkeypatch.setattr(job, 'collect', collect)
    assert job.collect_from_database({'games': []}, {}, caches=[], max_seconds=1) == {'database': True}
    assert sequence == ['pin', 'collect']


def test_production_workflows_use_primary_entries_and_fence_old_writers():
    root = Path(__file__).resolve().parents[1] / '.github/workflows'
    official = yaml.safe_load((root / 'steam-official-daily-catchup-250.yml').read_text())
    job = official['jobs']['follower-catchup']
    assert "RADAR_DATABASE_MIGRATION_PAUSE != '1'" in job['if']
    commands = '\n'.join(step.get('run', '') for step in job['steps'])
    assert 'python -u -m radar_backend.jobs.official_followers' in commands
    assert 'python -u -m radar_backend.jobs.reconcile_twitch_official_queue' in commands
    assert 'scripts.reconcile_twitch_official_queue' not in commands
    assert 'git reset --hard origin/main\npython -m radar_core.storage.primary hydrate' in commands
    tests = next(step for step in job['steps'] if 'pytest' in step.get('run', ''))
    assert tests['env']['RADAR_DATABASE_PRIMARY'] == '0'
    for filename in ('pilot-steam-prefilter-4000.yml', 'verify-steam-4000-shortlist-15s.yml',
                     'steam-official-backlog-oneoff-20260923.yml', 'publish-steam-preview.yml'):
        legacy = yaml.safe_load((root / filename).read_text())
        for value in legacy['jobs'].values():
            assert "vars.RADAR_DATABASE_PRIMARY != '1'" in value['if']
