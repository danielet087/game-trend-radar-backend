from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.domain import catalog_metadata as rules


NOW = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
SOURCE = 'Steam Store appdetails cc=TW categories'


def category_row(*, appid=7, categories=None, checked='2026-10-09T09:00:00Z', source=SOURCE):
    return {
        'appid': appid,
        'categories': [{'id': 1, 'description': 'Multi-player'}] if categories is None else categories,
        'categories_source': source,
        'categories_checked_at': checked,
    }


def snapshot(row):
    return rules.player_category_snapshot(row, now=lambda: NOW)


def release_row(checked, **fields):
    return {
        'appid': 7,
        'release_display_precision': 'date_full',
        'release_date_verified_at': checked,
        **fields,
    }


@pytest.mark.parametrize('source', sorted(rules.PLAYER_CATEGORY_SOURCES))
@pytest.mark.parametrize('categories', [[], [{'id': 1, 'description': ''}], [
    {'id': 1, 'description': 'Multi-player', 'extra': 'accepted'},
    {'id': 1, 'description': 'Duplicate category accepted'},
]])
def test_snapshot_accepts_explicit_official_categories_and_preserves_row_values(source, categories):
    row = category_row(source=source, categories=categories)
    observed, payload = snapshot(row)
    assert observed == NOW
    assert payload == {key: row[key] for key in rules.PLAYER_CATEGORY_FIELDS}
    assert payload['categories'] is categories
    assert payload is not row


@pytest.mark.parametrize('source', [None, '', 'Steam', 'third party', 1, True])
def test_snapshot_rejects_unverified_sources_before_reading_clock(source):
    clock = Mock(side_effect=AssertionError('clock should be lazy'))
    assert rules.player_category_snapshot(category_row(source=source), now=clock) is None
    clock.assert_not_called()


@pytest.mark.parametrize('categories', [None, {}, (), '1', 1, True])
def test_snapshot_rejects_nonlist_categories_before_reading_clock(categories):
    row = category_row()
    row['categories'] = categories
    clock = Mock(side_effect=AssertionError('clock should be lazy'))
    assert rules.player_category_snapshot(row, now=clock) is None
    clock.assert_not_called()


@pytest.mark.parametrize('item', [
    None, 1, 'item', [], {}, {'id': 1}, {'description': 'missing id'},
    {'id': True, 'description': 'boolean'}, {'id': False, 'description': 'boolean'},
    {'id': 0, 'description': 'zero'}, {'id': -1, 'description': 'negative'},
    {'id': 1.0, 'description': 'float'}, {'id': '1', 'description': 'string'},
    {'id': 1, 'description': None}, {'id': 1, 'description': 2},
])
def test_snapshot_rejects_malformed_category_items_without_clock(item):
    clock = Mock(side_effect=AssertionError('clock should be lazy'))
    assert rules.player_category_snapshot(category_row(categories=[item]), now=clock) is None
    clock.assert_not_called()


@pytest.mark.parametrize('checked', [
    '2026-10-09T09:00:00Z', '2026-10-09T09:00:00+00:00',
    '2026-10-09T09:00:00-00:00', '2026-10-09T09:00:00+00:00:00',
    datetime(2026, 10, 9, 9, tzinfo=timezone.utc),
])
def test_snapshot_accepts_all_zero_offset_time_representations(checked):
    observed, _ = snapshot(category_row(checked=checked))
    assert observed == NOW


@pytest.mark.parametrize('checked', [
    None, '', 'invalid', 0, True, '2026-10-09', '2026-10-09T09:00:00',
    '2026-10-09T17:00:00+08:00', '2026-10-09T08:00:00-01:00',
])
def test_snapshot_rejects_invalid_naive_and_nonzero_offset_times_before_clock(checked):
    clock = Mock(side_effect=AssertionError('clock should be lazy'))
    assert rules.player_category_snapshot(category_row(checked=checked), now=clock) is None
    clock.assert_not_called()


@pytest.mark.parametrize('offset,accepted', [
    (timedelta(days=-1), True), (timedelta(0), True),
    (timedelta(minutes=5), True), (timedelta(minutes=5, microseconds=1), False),
])
def test_snapshot_future_tolerance_is_inclusive_and_clock_is_read_once(offset, accepted):
    clock = Mock(return_value=NOW)
    result = rules.player_category_snapshot(category_row(checked=NOW + offset), now=clock)
    assert (result is not None) is accepted
    clock.assert_called_once_with()


def test_snapshot_reads_injected_clock_only_after_parsing_valid_metadata():
    parser = Mock(return_value=NOW)
    clock = Mock(return_value=NOW)
    result = rules.player_category_snapshot(
        category_row(), now=clock, datetime_type=SimpleNamespace(fromisoformat=parser),
    )
    assert result[0] is NOW
    parser.assert_called_once_with('2026-10-09T09:00:00+00:00')
    clock.assert_called_once_with()


def test_snapshot_fields_and_sources_remain_injectable_without_extra_admission_gate():
    row = category_row(source='custom')
    marker = []
    row['custom_field'] = marker
    result = rules.player_category_snapshot(row, now=lambda: NOW, fields=('custom_field',), sources={'custom'})
    assert result[1] == {'custom_field': marker}
    assert result[1]['custom_field'] is marker


@pytest.mark.parametrize('error', [ValueError('bad clock'), TypeError('bad clock')])
def test_snapshot_keeps_original_clock_error_rejection(error):
    assert rules.player_category_snapshot(category_row(), now=Mock(side_effect=error)) is None


@pytest.mark.parametrize('error', [AttributeError('shape'), RuntimeError('clock')])
def test_snapshot_does_not_hide_unexpected_clock_errors(error):
    with pytest.raises(type(error), match=str(error)):
        rules.player_category_snapshot(category_row(), now=Mock(side_effect=error))


def test_snapshot_unhashable_source_still_raises_before_validation():
    with pytest.raises(TypeError):
        snapshot(category_row(source=[]))


def test_snapshot_valid_payload_missing_explicit_field_still_raises():
    row = category_row()
    row.pop('categories_source')
    with pytest.raises(KeyError, match='categories_source'):
        rules.player_category_snapshot(row, now=lambda: NOW, sources={None})


@pytest.mark.parametrize('existing_checked,incoming_checked,winner', [
    ('2026-10-09T08:00:00Z', '2026-10-09T09:00:00Z', 'incoming'),
    ('2026-10-09T09:00:00Z', '2026-10-09T08:00:00Z', 'existing'),
    ('2026-10-09T09:00:00Z', '2026-10-09T09:00:00Z', 'incoming'),
    ('invalid', '2026-10-09T09:00:00Z', 'incoming'),
    ('2026-10-09T09:00:00Z', 'invalid', 'existing'),
])
def test_preserve_selects_newest_valid_snapshot_and_incoming_wins_ties(existing_checked, incoming_checked, winner):
    existing = category_row(categories=[{'id': 1, 'description': 'old'}], checked=existing_checked)
    incoming = category_row(categories=[{'id': 2, 'description': 'new'}], checked=incoming_checked)
    marker = []
    incoming.update(followers=9000, unknown=marker)
    expected = incoming if winner == 'incoming' else existing
    result = rules.preserve_player_categories(existing, incoming, snapshot=snapshot)
    assert result['categories'] is expected['categories']
    assert result['categories_checked_at'] == expected['categories_checked_at']
    assert result['followers'] == 9000
    assert result['unknown'] is marker
    assert incoming['categories'][0]['description'] == 'new'
    assert result is not incoming


def test_preserve_official_empty_categories_are_new_metadata_not_missing_metadata():
    existing = category_row(checked='2026-10-09T08:00:00Z')
    incoming = category_row(categories=[])
    result = rules.preserve_player_categories(existing, incoming, snapshot=snapshot)
    assert result['categories'] is incoming['categories']
    assert result['categories'] == []


@pytest.mark.parametrize('appids', [(7, 8), ('7', 7), (None, 7)])
def test_preserve_different_appids_cannot_copy_existing_categories(appids):
    existing = category_row(appid=appids[0])
    incoming = {'appid': appids[1], 'followers': 9000}
    port = Mock(side_effect=snapshot)
    assert rules.preserve_player_categories(existing, incoming, snapshot=port) == incoming
    port.assert_called_once_with(incoming)


def test_preserve_missing_appids_keep_original_none_equality_behavior():
    existing = category_row()
    existing.pop('appid')
    incoming = {'followers': 5000}
    result = rules.preserve_player_categories(existing, incoming, snapshot=snapshot)
    assert result['categories'] is existing['categories']
    assert result['followers'] == 5000


def test_preserve_invalid_both_snapshots_removes_all_category_fields_without_mutating_rows():
    existing = category_row(source='unverified')
    incoming = category_row(source='unverified')
    original = dict(incoming)
    assert rules.preserve_player_categories(existing, incoming, snapshot=snapshot) == {'appid': 7}
    assert incoming == original


def test_preserve_snapshot_callback_sees_original_rows_in_incoming_then_existing_order():
    existing = category_row()
    incoming = category_row()
    port = Mock(side_effect=[(1, {'result': 'incoming'}), (2, {'result': 'existing'})])
    result = rules.preserve_player_categories(existing, incoming, snapshot=port)
    assert [call.args[0] for call in port.call_args_list] == [incoming, existing]
    assert port.call_args_list[0].args[0] is incoming
    assert port.call_args_list[1].args[0] is existing
    assert result == {'appid': 7, 'result': 'existing'}


def test_preserve_snapshot_callback_errors_propagate_without_mutating_rows():
    incoming = category_row()
    original = dict(incoming)
    with pytest.raises(RuntimeError, match='snapshot failed'):
        rules.preserve_player_categories({}, incoming, snapshot=Mock(side_effect=RuntimeError('snapshot failed')))
    assert incoming == original


def test_preserve_custom_field_removal_does_not_remove_unspecified_metadata():
    marker = []
    incoming = {'categories': marker, 'custom': 'unsafe'}
    result = rules.preserve_player_categories({'appid': 7}, incoming, fields=('custom',), snapshot=lambda row: None)
    assert result == {'categories': marker}
    assert result['categories'] is marker


@pytest.mark.parametrize('prior,incoming,winner', [
    ('2026-10-09T09:00:00Z', '2026-10-09T08:00:00Z', 'existing'),
    ('2026-10-09T08:00:00Z', '2026-10-09T09:00:00Z', 'incoming'),
    ('2026-10-09T09:00:00Z', '2026-10-09T09:00:00Z', 'incoming'),
    ('2026-10-09T09:00:00', '2026-10-09T08:00:00Z', 'existing'),
    ('2026-10-09T17:00:00+08:00', '2026-10-09T08:00:00Z', 'existing'),
    ('2026-10-09T17:00:00+08:00', '2026-10-09T09:00:00Z', 'incoming'),
    ('2026-10-09T09:00:00Z', 'invalid', 'existing'),
    ('invalid', '2026-10-09T09:00:00Z', 'incoming'),
    ('invalid', 'invalid', 'incoming'),
    (None, None, 'incoming'),
])
def test_release_freshness_preserves_strict_newer_and_original_datetime_normalization(prior, incoming, winner):
    existing = release_row(prior, release_raw='old')
    incoming_row = release_row(incoming, release_raw='new')
    result = rules.keep_newer_release(existing, incoming_row)
    assert result['release_raw'] == ('old' if winner == 'existing' else 'new')
    assert result is not incoming_row
    assert incoming_row['release_raw'] == 'new'


@pytest.mark.parametrize('precision', [None, '', 'day', 'month', 'date', 'DATE_FULL'])
def test_release_other_precisions_cannot_overwrite_even_with_newer_audit(precision):
    existing = release_row('2026-10-09T09:00:00Z', release_display_precision=precision, release_raw='old')
    incoming = release_row('2026-10-09T08:00:00Z', release_raw='new')
    parser = Mock(side_effect=AssertionError('precision should short circuit parsing'))
    result = rules.keep_newer_release(existing, incoming, datetime_type=SimpleNamespace(fromisoformat=parser))
    assert result == incoming
    parser.assert_not_called()


def test_release_newer_audit_copies_all_provenance_fields_and_deletes_absent_old_values():
    existing = release_row('2026-10-09T09:00:00Z')
    incoming = release_row('2026-10-09T08:00:00Z')
    marker = []
    for index, field in enumerate(rules.RELEASE_FIELDS):
        if field not in ('release_display_precision', 'release_date_verified_at'):
            incoming[field] = f'incoming-{field}'
            if index % 2 == 0:
                existing[field] = marker if field == 'release_raw' else f'existing-{field}'
    incoming.update(appid=123, followers=9000, unknown=marker, name='new name')
    result = rules.keep_newer_release(existing, incoming)
    for field in rules.RELEASE_FIELDS:
        if field in existing:
            assert result[field] == existing[field]
        else:
            assert field not in result
    assert result['release_raw'] is marker
    assert result['unknown'] is marker
    assert result['appid'] == 123
    assert result['followers'] == 9000
    assert result['name'] == 'new name'
    assert 'release_start' in incoming


def test_release_freshness_does_not_require_matching_appid():
    existing = release_row('2026-10-09T09:00:00Z', appid=1, release_start='2027-01-01')
    incoming = release_row('2026-10-09T08:00:00Z', appid=2, release_start='2026-01-01')
    result = rules.keep_newer_release(existing, incoming)
    assert result['appid'] == 2
    assert result['release_start'] == '2027-01-01'


def test_release_custom_fields_are_copied_and_deleted_without_touching_other_release_fields():
    existing = release_row('2026-10-09T09:00:00Z', custom='old')
    incoming = release_row('2026-10-09T08:00:00Z', custom='new', absent='drop', release_raw='retained')
    result = rules.keep_newer_release(existing, incoming, fields=('custom', 'absent'))
    assert result['custom'] == 'old'
    assert 'absent' not in result
    assert result['release_raw'] == 'retained'
    assert result['release_date_verified_at'] == incoming['release_date_verified_at']


@pytest.mark.parametrize('error', [ValueError('invalid'), TypeError('invalid')])
def test_release_parse_rejections_use_original_minimum_utc_sentinel(error):
    parser = Mock(side_effect=[NOW, error])
    port = SimpleNamespace(fromisoformat=parser, min=datetime.min)
    existing = release_row('existing', release_raw='old')
    incoming = release_row('incoming', release_raw='new')
    assert rules.keep_newer_release(existing, incoming, datetime_type=port)['release_raw'] == 'old'
    assert parser.call_count == 2


@pytest.mark.parametrize('error', [AttributeError('shape'), RuntimeError('parse')])
def test_release_does_not_hide_unexpected_datetime_errors(error):
    parser = Mock(side_effect=error)
    with pytest.raises(type(error), match=str(error)):
        rules.keep_newer_release(release_row('old'), release_row('new'), datetime_type=SimpleNamespace(fromisoformat=parser))


def test_release_naive_values_use_injected_timezone():
    custom_zone = timezone(timedelta(hours=8))
    existing = release_row('2026-10-09T10:00:00', release_raw='old')
    incoming = release_row('2026-10-09T01:00:00Z', release_raw='new')
    result = rules.keep_newer_release(existing, incoming, timezone_type=SimpleNamespace(utc=custom_zone))
    assert result['release_raw'] == 'old'
