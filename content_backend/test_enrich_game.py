import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


class _DummyOpenCC:
    def __init__(self, *_):
        pass

    def convert(self, value):
        return value


sys.modules.setdefault("opencc", types.SimpleNamespace(OpenCC=_DummyOpenCC))

from enrich_game import upsert_document, valid_date


class EnrichmentTests(unittest.TestCase):
    def test_dates(self):
        self.assertTrue(valid_date("2026-10-29"))
        self.assertFalse(valid_date("2026-Q4"))
        self.assertFalse(valid_date("2026-02-30"))

    def test_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steam_upcoming.json"
            path.write_text(
                json.dumps({"count": 0, "games": []}),
                encoding="utf-8",
            )
            record = {
                "appid": 123,
                "followers": 6000,
                "release_start": "2026-10-29",
                "header_image": (
                    "https://shared.akamai.steamstatic.com/"
                    "store_item_assets/steam/apps/123/header.jpg"
                ),
                "language_support": {
                    "tchinese": True,
                    "schinese": True,
                    "english": True,
                },
                "tags": ["Action"],
            }
            self.assertTrue(
                upsert_document(path, record, "2026-10-29")
            )
            self.assertFalse(
                upsert_document(path, record, "2026-10-29")
            )
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["count"], 1)
            self.assertEqual(doc["games"][0]["appid"], 123)


if __name__ == "__main__":
    unittest.main()
