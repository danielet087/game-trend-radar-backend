from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from scripts.collect_public_growth import eligible, parse_count, collect


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
            result = collect(root, root / 'out.json', now=datetime(2026, 9, 28, tzinfo=timezone.utc), session=client)
            self.assertEqual(client.calls, 1)
            self.assertEqual(result['measurements'], [])
            self.assertEqual(result['reason'], 'rate_limited')

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


if __name__ == '__main__':
    unittest.main()
