import json
from pathlib import Path
import tempfile
import unittest
from scripts.build_public_steam_shards import build, valid_record


class PublicationTests(unittest.TestCase):
    def test_player_categories_survive_followers_publication_and_new_empty_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); frontend = root / 'frontend'; source = root / 'input.json'
            row = {'appid': 123, 'followers': 5000, 'release_start': '2027-02-28',
                   'release_precision': 'day', 'release_display_precision': 'date_full',
                   'categories': [{'id': 1, 'description': 'Multi-player'}],
                   'categories_source': 'Steam Store appdetails cc=TW categories',
                   'categories_checked_at': '2026-10-05T01:00:00Z'}
            source.write_text(json.dumps({'games': [row]}))
            build(source, frontend, authoritative_future=True)
            follower_only = {key: value for key, value in row.items() if not key.startswith('categories')}
            source.write_text(json.dumps({'games': [{**follower_only, 'followers': 6000}]}))
            build(source, frontend, authoritative_future=True)
            for path in ('catalog.json', 'calendar/2027-02.json', 'steam_upcoming.json'):
                public = json.loads((frontend / 'data' / path).read_text())['games'][0]
                self.assertEqual(public['categories'], row['categories'])
                self.assertEqual(public['categories_checked_at'], row['categories_checked_at'])
                self.assertEqual(public['followers'], 6000)
            source.write_text(json.dumps({'games': [{**row, 'categories': [],
                               'categories_checked_at': '2026-10-05T02:00:00Z'}]}))
            build(source, frontend, authoritative_future=True)
            for path in ('catalog.json', 'calendar/2027-02.json', 'steam_upcoming.json'):
                public = json.loads((frontend / 'data' / path).read_text())['games'][0]
                self.assertEqual(public['categories'], [])
                self.assertEqual(public['categories_checked_at'], '2026-10-05T02:00:00Z')
            self.assertEqual(json.loads((frontend / 'data/games/123.json').read_text())['categories'], [])

    def test_untrusted_or_older_player_modes_cannot_erase_latest_official_categories(self):
        from scripts.build_public_steam_shards import merge_game
        current = {'appid': 123, 'categories': [{'id': 1, 'description': 'Multi-player'}],
                   'categories_source': 'Steam Store appdetails cc=TW categories',
                   'categories_checked_at': '2026-10-05T01:00:00Z'}
        for override in (
            {'categories_checked_at': '2026-10-04T01:00:00Z'},
            {'categories_checked_at': '2026-10-05T02:00:00'},
            {'categories_checked_at': '9999-12-31T01:00:00Z'},
            {'categories_source': 'community tag guess'},
            {'categories': [{'id': True, 'description': 'Multi-player'}]},
        ):
            with self.subTest(override=override):
                incoming = {**current, 'categories': [], 'followers': 7000,
                            'categories_checked_at': '2026-10-05T02:00:00Z', **override}
                merged = merge_game(current, incoming)
                for key in ('categories', 'categories_source', 'categories_checked_at'):
                    self.assertEqual(merged[key], current[key])
                self.assertEqual(merged['followers'], 7000)

    def test_localized_taxonomy_survives_catalog_projection(self):
        from scripts.public_catalog import write_catalog_projection
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            row = {'appid':123, 'tags':['Capitalism'], 'genres':['RPG'],
                   'tag_ids':{'Capitalism':4845}, 'tag_labels_zh_tw':{'Capitalism':'資本主義'},
                   'genre_labels_zh_tw':{'RPG':'角色扮演'}}
            write_catalog_projection(data, [row], 'now')
            self.assertEqual(json.loads((data/'catalog.json').read_text())['games'][0], row)

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

    def test_newer_public_date_audit_survives_older_master_snapshot(self):
        from scripts.build_public_steam_shards import merge_game
        current = {'appid': 123, 'followers': 6000, 'release_start': '2027-02-28',
                   'release_display_precision': 'date_full', 'release_date_verified_at': '2026-09-28T00:00:00Z'}
        incoming = {**current, 'followers': 7000, 'release_start': '2027-02-27',
                    'release_date_verified_at': '2026-09-27T00:00:00Z'}
        merged = merge_game(current, incoming)
        self.assertEqual(merged['release_start'], current['release_start'])
        self.assertEqual(merged['release_date_verified_at'], current['release_date_verified_at'])
        self.assertEqual(merged['followers'], 7000)

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
