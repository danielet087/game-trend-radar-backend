"""Regression contracts for the version-3 public catalog projection."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from radar_backend.application.catalog_projection import write_catalog_projection
from radar_backend.domain.catalog_projection import FIELDS, catalog_payload, catalog_revision
from radar_backend.state import catalog_projection as state


@pytest.mark.parametrize(('rows', 'expected'), [
    ([], '4f53cda18c2baa0c0354'),
    ([{'appid': 7, 'name': '遊戲', 'private': {'x': None}}], '19559cd4f857239f4266'),
    ([{'appid': 1, 'score': float('nan')}], '412ff9e12d533d2b195c'),
    ([{'appid': 1, 'score': float('inf')}], '4e996fc31d8e65694809'),
    ([{'appid': 1, 'score': float('-inf')}], '29fdbae9c42a890f7a79'),
])
def test_full_row_utf8_revision_known_vectors(rows, expected):
    assert catalog_revision(rows) == expected


def test_revision_ignores_dictionary_order_but_preserves_row_order():
    first = [{'appid': 1, 'private': {'b': 2, 'a': 1}}, {'appid': 2}]
    reordered_keys = [{'private': {'a': 1, 'b': 2}, 'appid': 1}, {'appid': 2}]
    assert catalog_revision(first) == catalog_revision(reordered_keys)
    assert catalog_revision(first) != catalog_revision(list(reversed(first)))


@pytest.mark.parametrize('replacement', [None, False, 0, '', [], {}, 'new metadata'])
def test_unprojected_metadata_still_changes_revision(replacement):
    old = [{'appid': 1, 'unknown_private_field': 'original'}]
    new = [{'appid': 1, 'unknown_private_field': replacement}]
    assert catalog_revision(old) != catalog_revision(new)
    assert catalog_payload(old, 'now', 'r')['games'] == catalog_payload(new, 'now', 'r')['games']


@pytest.mark.parametrize('rows', [[{'x': object()}], [{'x': {1, 2}}], [{1: 'a', 'b': 'c'}]])
def test_revision_serialization_errors_are_not_tolerated(rows):
    with pytest.raises(TypeError):
        catalog_revision(rows)


def test_revision_circular_rows_error_is_not_tolerated():
    row = {'appid': 1}
    row['cycle'] = row
    with pytest.raises(ValueError, match='Circular'):
        catalog_revision([row])


def test_revision_injected_modules_observe_exact_hash_input():
    events = []
    rows = [{'appid': 3}]

    def dumps(value, **kwargs):
        events.append(('dumps', value, kwargs))
        return '中文'

    def sha256(value):
        events.append(('hash', value))
        return SimpleNamespace(hexdigest=lambda: '12345678901234567890trailing')

    assert catalog_revision(rows, json_module=SimpleNamespace(dumps=dumps),
                            hashlib_module=SimpleNamespace(sha256=sha256)) == '12345678901234567890'
    assert events == [('dumps', rows, {'ensure_ascii': False, 'sort_keys': True,
                                     'separators': (',', ':')}), ('hash', '中文'.encode())]


def test_projection_keeps_order_none_and_nested_references_without_unknown_fields():
    tags = ['Capitalism']
    categories = [{'id': 1, 'description': 'Multi-player'}]
    row = {'unknown': 'hidden', 'categories': categories, 'tags': tags, 'name': None, 'appid': 7}
    payload = catalog_payload([row, {'appid': 9}], '2026-10-09', 'revision')
    assert list(payload) == ['version', 'revision', 'generated_at', 'count', 'games']
    assert payload['version'] == 3
    assert payload['count'] == 2
    assert list(payload['games'][0]) == ['appid', 'name', 'tags', 'categories']
    assert payload['games'][0]['name'] is None
    assert payload['games'][0]['tags'] is tags
    assert payload['games'][0]['categories'] is categories
    assert payload['games'][1] == {'appid': 9}
    assert row['unknown'] == 'hidden'


def test_projection_fields_are_the_existing_45_fields_with_player_category_evidence():
    assert len(FIELDS) == 45
    assert len(set(FIELDS)) == 45
    assert FIELDS[32:35] == ('categories', 'categories_source', 'categories_checked_at')
    row = {key: None for key in reversed(FIELDS)}
    assert list(catalog_payload([row], 'now', 'r')['games'][0]) == list(FIELDS)


def test_projection_custom_fields_and_timestamp_do_not_change_revision():
    rows = [{'appid': 8, 'other': {'nested': True}}]
    revision = catalog_revision(rows)
    one = catalog_payload(rows, 'old', revision, fields=('other', 'absent', 'appid'))
    two = catalog_payload(rows, 'new', revision)
    assert one['revision'] == two['revision'] == revision
    assert one['generated_at'] != two['generated_at']
    assert list(one['games'][0]) == ['other', 'appid']
    assert one['games'][0]['other'] is rows[0]['other']


class DirectoryPort:
    def __init__(self, events, *, error=None):
        self.events = events
        self.error = error

    def __truediv__(self, name):
        self.events.append(('path', name))
        if self.error is not None:
            raise self.error
        return Path('/data') / name


def application_ports(events, *, present=False, prior=None, fail_at=None, error=None):
    def callback(name, result):
        def invoke(*args):
            events.append((name, *args))
            if fail_at == name:
                raise error
            return result
        return invoke

    return {
        'revision_for_rows': callback('revision', 'r'),
        'payload_for_rows': callback('payload', {'version': 3}),
        'exists': callback('exists', present),
        'read_revision': callback('read', prior),
        'write_payload': callback('write', None),
    }


@pytest.mark.parametrize(('present', 'prior', 'expected'), [
    (False, 'r', ['revision', 'path', 'payload', 'exists', 'write']),
    (True, 'r', ['revision', 'path', 'payload', 'exists', 'read']),
    (True, 'other', ['revision', 'path', 'payload', 'exists', 'read', 'write']),
    (True, None, ['revision', 'path', 'payload', 'exists', 'read', 'write']),
    (True, 3, ['revision', 'path', 'payload', 'exists', 'read', 'write']),
    (True, [], ['revision', 'path', 'payload', 'exists', 'read', 'write']),
    (True, {}, ['revision', 'path', 'payload', 'exists', 'read', 'write']),
])
def test_application_keeps_original_sequence_and_manifest(present, prior, expected):
    events = []
    rows = [{'appid': 7}]
    result = write_catalog_projection(DirectoryPort(events), rows, 'now',
                                      **application_ports(events, present=present, prior=prior))
    assert [event[0] for event in events] == expected
    assert events[0][1] is rows
    assert events[2] == ('payload', rows, 'now', 'r')
    assert result == {'catalog_path': 'catalog.json', 'catalog_revision': 'r'}


@pytest.mark.parametrize(('fail_at', 'last'), [
    ('revision', 'revision'), ('payload', 'payload'), ('exists', 'exists'),
    ('read', 'read'), ('write', 'write'),
])
def test_application_port_failures_stop_at_original_operation(fail_at, last):
    events = []
    with pytest.raises(RuntimeError, match='port failure'):
        write_catalog_projection(DirectoryPort(events), [], 'now', **application_ports(
            events, present=True, fail_at=fail_at, error=RuntimeError('port failure'),
        ))
    assert events[-1][0] == last


def test_path_failure_occurs_after_hashing_before_projecting():
    events = []
    with pytest.raises(OSError, match='path failure'):
        write_catalog_projection(DirectoryPort(events, error=OSError('path failure')), [], 'now',
                                 **application_ports(events))
    assert [event[0] for event in events] == ['revision', 'path']


@pytest.mark.parametrize('error', [OSError('read failure'), ValueError('read failure'), TypeError('read failure')])
def test_application_preserves_read_exception_scope_and_then_writes(error):
    events = []
    assert write_catalog_projection(DirectoryPort(events), [], 'now', **application_ports(
        events, present=True, fail_at='read', error=error,
    )) == {'catalog_path': 'catalog.json', 'catalog_revision': 'r'}
    assert [event[0] for event in events] == ['revision', 'path', 'payload', 'exists', 'read', 'write']


@pytest.mark.parametrize('error', [OSError('equality failure'), ValueError('equality failure'),
                                  TypeError('equality failure'), AttributeError('equality failure')])
def test_application_existing_revision_comparison_keeps_original_exception_scope(error):
    class Revision:
        def __eq__(self, other):
            raise error

    events = []
    ports = application_ports(events, present=True, prior=Revision())
    if isinstance(error, AttributeError):
        with pytest.raises(AttributeError, match='equality failure'):
            write_catalog_projection(DirectoryPort(events), [], 'now', **ports)
        assert events[-1][0] == 'read'
    else:
        assert write_catalog_projection(DirectoryPort(events), [], 'now', **ports)['catalog_revision'] == 'r'
        assert events[-1][0] == 'write'


class FilePort:
    def __init__(self, events, *, text='{}', present=True, error_at=None, error=None):
        self.events = events
        self.text = text
        self.present = present
        self.error_at = error_at
        self.error = error
        self.parent = self

    def operation(self, name, *args, **kwargs):
        self.events.append((name, args, kwargs))
        if self.error_at == name:
            raise self.error

    def exists(self):
        self.operation('exists')
        return self.present

    def read_text(self, **kwargs):
        self.operation('read', **kwargs)
        return self.text

    def mkdir(self, **kwargs):
        self.operation('mkdir', **kwargs)

    def write_text(self, output, **kwargs):
        self.operation('write', output, **kwargs)


@pytest.mark.parametrize(('text', 'revision'), [
    ('{}', None), ('{"revision":"r"}', 'r'), ('{"revision":null}', None),
    ('{"revision":[]}', []), ('{"revision":{}}', {}), ('{"revision":false}', False),
    ('{"revision":3}', 3), ('{"revision":"r","games":"invalid"}', 'r'),
])
def test_state_reads_only_revision_without_validating_other_fields(text, revision):
    events = []
    assert state.read_revision(FilePort(events, text=text)) == revision
    assert events == [('read', (), {'encoding': 'utf-8'})]


@pytest.mark.parametrize('error', [OSError('read'), ValueError('read'), TypeError('read')])
def test_state_tolerates_existing_read_failure_types(error):
    assert state.read_revision(FilePort([], error_at='read', error=error)) is None


@pytest.mark.parametrize('error', [AttributeError('read'), RuntimeError('read'), LookupError('read')])
def test_state_other_read_failure_types_escape(error):
    with pytest.raises(type(error), match='read'):
        state.read_revision(FilePort([], error_at='read', error=error))


@pytest.mark.parametrize('text', ['[1]', '[]', 'null', 'true', '123', '"text"'])
def test_valid_nonobject_existing_json_attribute_error_escapes(text):
    with pytest.raises(AttributeError):
        state.read_revision(FilePort([], text=text))


@pytest.mark.parametrize('text', ['', '{', '{"revision":', 'not JSON', '\x00'])
def test_invalid_existing_json_is_tolerated(text):
    assert state.read_revision(FilePort([], text=text)) is None


@pytest.mark.parametrize('stage', ['loads', 'get'])
@pytest.mark.parametrize('error', [OSError('json port'), ValueError('json port'), TypeError('json port')])
def test_state_read_tolerance_includes_json_decoder_and_revision_getter(stage, error):
    def fail(*args):
        raise error
    parsed = SimpleNamespace(get=fail if stage == 'get' else lambda key: 'r')
    decoder = SimpleNamespace(loads=fail if stage == 'loads' else lambda text: parsed)
    assert state.read_revision(FilePort([]), json_module=decoder) is None


def test_state_write_exact_compact_unicode_json_newline_and_encoding():
    events = []
    state.write_payload(FilePort(events), {'name': '遊戲', 'name_en': None})
    assert events == [
        ('mkdir', (), {'parents': True, 'exist_ok': True}),
        ('write', ('{"name":"遊戲","name_en":null}\n',), {'encoding': 'utf-8'}),
    ]


def test_state_write_serializes_after_mkdir_before_write():
    events = []
    payload = {'version': 3}

    def dumps(value, **kwargs):
        events.append(('dumps', (value,), kwargs))
        return 'serialized'

    state.write_payload(FilePort(events), payload, json_module=SimpleNamespace(dumps=dumps))
    assert events == [
        ('mkdir', (), {'parents': True, 'exist_ok': True}),
        ('dumps', (payload,), {'ensure_ascii': False, 'separators': (',', ':')}),
        ('write', ('serialized\n',), {'encoding': 'utf-8'}),
    ]


@pytest.mark.parametrize('stage', ['mkdir', 'write'])
@pytest.mark.parametrize('error', [OSError('state failure'), ValueError('state failure'), TypeError('state failure')])
def test_state_write_errors_propagate(stage, error):
    events = []
    with pytest.raises(type(error), match='state failure'):
        state.write_payload(FilePort(events, error_at=stage, error=error), {})
    assert events[-1][0] == stage


def test_payload_serialization_failure_keeps_created_parent_but_does_not_write(tmp_path):
    path = tmp_path / 'new' / 'catalog.json'
    with pytest.raises(TypeError):
        state.write_payload(path, {'not_json': object()})
    assert path.parent.is_dir()
    assert not path.exists()


@pytest.mark.parametrize('error', [OSError('exists'), ValueError('exists'), TypeError('exists')])
def test_state_existence_errors_propagate(error):
    with pytest.raises(type(error), match='exists'):
        state.exists(FilePort([], error_at='exists', error=error))


def publish(directory, rows, generated_at):
    return write_catalog_projection(
        directory, rows, generated_at,
        exists=state.exists, read_revision=state.read_revision, write_payload=state.write_payload,
    )


def test_real_file_keeps_timestamp_when_complete_rows_are_unchanged(tmp_path):
    rows = [{'appid': 7, 'name': '中文', 'private': {'original': True}}]
    first = publish(tmp_path, rows, 'first')
    original_bytes = (tmp_path / 'catalog.json').read_bytes()
    second = publish(tmp_path, rows, 'second')
    assert first == second
    assert (tmp_path / 'catalog.json').read_bytes() == original_bytes
    parsed = json.loads(original_bytes)
    assert parsed['generated_at'] == 'first'
    assert parsed['games'] == [{'appid': 7, 'name': '中文'}]
    assert original_bytes.endswith(b'\n')


def test_real_file_updates_for_unknown_metadata_only_change(tmp_path):
    first = publish(tmp_path, [{'appid': 7, 'private': 'first'}], 'first')
    second = publish(tmp_path, [{'appid': 7, 'private': 'second'}], 'second')
    assert first['catalog_revision'] != second['catalog_revision']
    parsed = json.loads((tmp_path / 'catalog.json').read_text())
    assert parsed['generated_at'] == 'second'
    assert parsed['games'] == [{'appid': 7}]


def test_matching_revision_skips_invalid_existing_payload_without_repair(tmp_path):
    rows = [{'appid': 7}]
    old = json.dumps({'revision': catalog_revision(rows), 'version': 999, 'games': 'invalid'})
    (tmp_path / 'catalog.json').write_text(old, encoding='utf-8')
    publish(tmp_path, rows, 'now')
    assert (tmp_path / 'catalog.json').read_text(encoding='utf-8') == old


@pytest.mark.parametrize('text', ['{', '{}', '{"revision":"old"}'])
def test_missing_or_unreadable_existing_revision_republishes(tmp_path, text):
    (tmp_path / 'catalog.json').write_text(text, encoding='utf-8')
    result = publish(tmp_path, [], 'now')
    parsed = json.loads((tmp_path / 'catalog.json').read_text())
    assert parsed == {'version': 3, 'revision': result['catalog_revision'],
                      'generated_at': 'now', 'count': 0, 'games': []}
