from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from scripts.collect_public_growth import eligible, parse_count, collect
from scripts.steam_official_followers import GROUP_BASE
from types import SimpleNamespace
from unittest.mock import Mock


class GrowthTests(unittest.TestCase):
    def test_exact_30_day_boundary(self):
        row = {"appid": 10, "followers": 6000, "release_start": "2026-09-01"}
        self.assertTrue(eligible(row, date(2026, 10, 1)))
        self.assertFalse(eligible(row, date(2026, 10, 2)))
        self.assertFalse(eligible({**row, "release_precision": "month"}, date(2026, 9, 28)))
        self.assertFalse(eligible({**row, "followers": 3000}, date(2026, 9, 28)))
        self.assertEqual(parse_count('<memberList><memberCount>6,521</memberCount></memberList>'), 6521)

    def test_stop_on_429_without_retrying_or_inventing_zero(self):
        class Session:
            headers = {}
            calls = 0
            def get(self, *args, **kwargs):
                self.calls += 1
                return type('Response', (), {'status_code': 429})()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'catalog.json').write_text(json.dumps({'count': 2, 'games': [
                {'appid': aid, 'followers': 6000, 'release_start': '2026-09-28'} for aid in [1, 2]]}))
            client = Session()
            checkpoint = root / 'checkpoint.json'
            checkpoint.write_text(json.dumps({'pending_candidates': {
                str(aid): {'appid': aid, 'group_id64': str(103582791429521408 + aid)}
                for aid in [1, 2]}}))
            result = collect(root, root / 'out.json', now=datetime(2026, 9, 28, tzinfo=timezone.utc),
                             session=client, checkpoint_path=checkpoint)
            self.assertEqual(client.calls, 1)
            self.assertEqual(result['measurements'], [])
            self.assertEqual(result['reason'], 'rate_limited')
            self.assertTrue(json.loads(checkpoint.read_text())['community_cooldown'])

    def test_same_taipei_day_uses_actual_cached_timestamp_without_network(self):
        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                raise AssertionError('Should reuse the actual observation')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'catalog.json').write_text(json.dumps({'count': 1, 'games': [
                {'appid': 1, 'followers': 6000, 'release_start': '2026-09-28', 'follower_checked_at': '2026-09-27T17:00:00Z'}]}))
            result = collect(root, root / 'out.json', now=datetime(2026, 9, 28, tzinfo=timezone.utc), session=Session())
            self.assertEqual(result['requests'], 0)
            self.assertEqual(result['measurements'][0]['at'], '2026-09-27T17:00:00Z')

    def test_shared_cooldown_allows_cached_reuse_and_leaves_unknown_groups_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            now = datetime(2026, 9, 28, tzinfo=timezone.utc)
            (root / 'catalog.json').write_text(json.dumps({'count': 3, 'games': [
                {'appid': aid, 'followers': 6000, 'release_start': '2026-09-28'} for aid in [1, 2, 3]]}))
            checkpoint = root / 'checkpoint.json'
            original = {'pending_candidates': {'2': {'appid': 2, 'group_id64': str(GROUP_BASE + 2)}},
                        'official_results': {'1': {'official_followers': 0, 'official_checked_at_taipei': now.isoformat()}},
                        'next_request_after_taipei': '2026-09-28T02:00:00Z',
                        'content_dispatches': {'2': {'status': 'dispatched'}}}
            checkpoint.write_text(json.dumps(original))
            session = SimpleNamespace(headers={}, get=Mock(side_effect=AssertionError('Cooldown blocks request')))
            result = collect(root, root / 'out.json', now=now, session=session, checkpoint_path=checkpoint)
            self.assertEqual(result['measurements'], [{'appid': 1, 'followers': 0, 'at': now.isoformat(), 'source': 'steam_community'}])
            self.assertEqual(result['requests'], 0)
            self.assertEqual(result['reused'], 1)
            self.assertEqual({p['appid']: p['reason'] for p in result['pending']}, {2: 'community_cooldown', 3: 'awaiting_group_resolution'})
            self.assertEqual(json.loads(checkpoint.read_text())['pending_candidates'], original['pending_candidates'])
            self.assertEqual(json.loads(checkpoint.read_text())['content_dispatches'], original['content_dispatches'])
            self.assertFalse(result['job_result']['successful'])
            session.get.assert_not_called()

    def test_success_persists_true_timestamp_and_reuses_it_next_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            now = datetime(2026, 9, 28, tzinfo=timezone.utc)
            (root / 'catalog.json').write_text(json.dumps({'count': 1, 'games': [
                {'appid': 1, 'followers': 6000, 'release_start': '2026-09-28'}]}))
            checkpoint = root / 'checkpoint.json'
            original = {'pending_candidates': {'1': {'appid': 1, 'group_id64': str(GROUP_BASE + 1)}}}
            checkpoint.write_text(json.dumps(original))
            body = f'<memberList><groupID64>{GROUP_BASE + 1}</groupID64><memberCount>6521</memberCount></memberList>'
            session = SimpleNamespace(headers={}, get=Mock(return_value=SimpleNamespace(status_code=200, headers={}, content=body.encode())))
            result = collect(root, root / 'out.json', now=now, session=session, checkpoint_path=checkpoint)
            self.assertEqual(result['requests'], 1)
            self.assertEqual(result['measurements'][0]['followers'], 6521)
            self.assertIn(f'/gid/{GROUP_BASE + 1}/', session.get.call_args.args[0])
            self.assertTrue(result['collection_complete'])
            self.assertFalse(result['job_result']['successful'])
            self.assertFalse(result['job_result']['published'])
            session.get.reset_mock()
            session.get.side_effect = AssertionError('Same-day cache must be reused')
            again = collect(root, root / 'out.json', now=now, session=session, checkpoint_path=checkpoint)
            self.assertEqual(again['requests'], 0)
            self.assertEqual(again['reused'], 1)
            self.assertEqual(again['measurements'], result['measurements'])
            self.assertEqual(json.loads(checkpoint.read_text())['pending_candidates'], original['pending_candidates'])

    def test_mismatched_group_has_no_count_and_partial_progress_survives(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            now = datetime(2026, 9, 28, tzinfo=timezone.utc)
            (root / 'catalog.json').write_text(json.dumps({'count': 2, 'games': [
                {'appid': aid, 'followers': 6000, 'release_start': '2026-09-28'} for aid in [1, 2]]}))
            checkpoint = root / 'checkpoint.json'
            checkpoint.write_text(json.dumps({'pending_candidates': {
                str(aid): {'appid': aid, 'group_id64': str(GROUP_BASE + aid)} for aid in [1, 2]}}))
            def xml(gid):
                return SimpleNamespace(status_code=200, headers={}, content=f'<memberList><groupID64>{gid}</groupID64><memberCount>6521</memberCount></memberList>'.encode())
            session = SimpleNamespace(headers={}, get=Mock(side_effect=[xml(GROUP_BASE + 1), xml(GROUP_BASE + 99)]))
            result = collect(root, root / 'out.json', now=now, session=session, sleep=lambda _: None, checkpoint_path=checkpoint)
            self.assertEqual(result['requests'], 2)
            self.assertEqual([item['appid'] for item in result['measurements']], [1])
            self.assertEqual(result['errors'], [2])
            self.assertEqual(result['reason'], 'source_unavailable')
            self.assertEqual(set(json.loads(checkpoint.read_text())['official_growth_observations']), {'1'})
            self.assertFalse(result['collection_complete'])

    def test_invalid_checkpoint_or_cooldown_fails_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'catalog.json').write_text(json.dumps({'count': 1, 'games': [
                {'appid': 1, 'followers': 6000, 'release_start': '2026-09-28'}]}))
            checkpoint = root / 'checkpoint.json'
            session = SimpleNamespace(headers={}, get=Mock())
            for invalid in ['{broken', '[]', json.dumps({'next_request_after_taipei': 'invalid'})]:
                checkpoint.write_text(invalid)
                with self.assertRaises(ValueError):
                    collect(root, root / 'out.json', now=datetime(2026, 9, 28, tzinfo=timezone.utc),
                            session=session, checkpoint_path=checkpoint)
                self.assertEqual(checkpoint.read_text(), invalid)
            session.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
