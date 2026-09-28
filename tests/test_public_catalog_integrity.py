import json
from pathlib import Path
import tempfile
import unittest
from scripts.build_public_steam_shards import build, valid_record


class PublicationTests(unittest.TestCase):
    def test_bad_or_empty_authoritative_input_cannot_remove_public_records(self):
        for value in ['broken-json', '{}', '{"games":[]}']:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); frontend = root / 'frontend'
                game = frontend / 'data/games/123.json'; game.parent.mkdir(parents=True)
                game.write_text('{"appid":123,"release_start":"2027-01-01"}')
                source = root / 'input.json'; source.write_text(value)
                with self.assertRaises(ValueError):
                    build(source, frontend, authoritative_future=True)
                self.assertTrue(game.exists())

    def test_rejects_impossible_date_before_month_generation(self):
        self.assertFalse(valid_record({'appid':123, 'followers':5000, 'release_start':'2027-02-30'}))
        self.assertTrue(valid_record({'appid':123, 'followers':5000, 'release_start':'2027-02-28'}))

    def test_same_count_metadata_changes_update_snapshot_revision_and_all_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); frontend=root/'frontend'; source=root/'input.json'
            row={'appid':123,'followers':5000,'release_start':'2027-02-28',
                 'release_precision':'day','release_display_precision':'date_full','name':'Test'}
            source.write_text(json.dumps({'generated_at':'first','games':[row]}))
            build(source,frontend,authoritative_future=True)
            data=frontend/'data'; first=json.loads((data/'index.json').read_text())
            source.write_text(json.dumps({'generated_at':'second','games':[{**row,'followers':6000}]}))
            build(source,frontend,authoritative_future=True)
            second=json.loads((data/'index.json').read_text())
            self.assertEqual(first['game_count'],second['game_count'])
            self.assertNotEqual(first['catalog_revision'],second['catalog_revision'])
            self.assertEqual(second['generated_at'],'second')
            self.assertEqual(json.loads((data/'catalog.json').read_text())['games'][0]['followers'],6000)


if __name__=='__main__': unittest.main()
