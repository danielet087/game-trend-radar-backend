"""The data home flag: strict documents and a compare-and-swap command."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from radar_core.storage import data_home, documents, primary, primary_store
from radar_core.storage.sql import SqliteClient, StorageError

SOURCE = data_home.REPOSITORY + "@" + data_home.BRANCH


class ParseTests(unittest.TestCase):
    def test_missing_flag_keeps_git(self):
        self.assertEqual(data_home.parse(None), {"site": "git", "git_projection": True})

    def test_valid_documents_round_trip(self):
        for site, projection in (("git", True), ("database", True), ("database", False)):
            with self.subTest(site=site, projection=projection):
                raw = data_home.document(site=site, git_projection=projection)
                self.assertEqual(data_home.parse(raw), {"site": site, "git_projection": projection})

    def test_invalid_documents_are_rejected(self):
        valid = {"schema_version": 1, "site": "database", "git_projection": False}
        cases = [
            {**valid, "site": "git"},  # the site would be built from a Git copy nobody updates
            {**valid, "schema_version": 2}, {**valid, "schema_version": True},
            {**valid, "site": "r2"}, {**valid, "git_projection": "false"}, {**valid, "git_projection": 0},
            {key: value for key, value in valid.items() if key != "site"}, {**valid, "note": "extra"},
        ]
        for raw in [json.dumps(case).encode() for case in cases] + [b"[]", b"not json", b"\xff"]:
            with self.subTest(raw=raw):
                with self.assertRaises(StorageError) as raised:
                    data_home.parse(raw)
                self.assertEqual(raised.exception.code, "INVALID_DATA_HOME")
        with self.assertRaises(StorageError):
            data_home.document(site="git", git_projection=False)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.database = SqliteClient()
        self.addCleanup(self.database.close)
        documents.initialize(self.database)
        primary_store.bootstrap_primary(self.database, SOURCE, {"data/catalog.json": b'{"games": []}\n'}, "a" * 40)
        connection = patch.object(primary, "client", return_value=self.database)
        connection.start()
        self.addCleanup(connection.stop)

    def run_command(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = data_home.main(list(argv))
        return code, json.loads(output.getvalue())

    def pending(self):
        return [item["operation_id"] for item in primary_store.projection_pending(self.database, SOURCE)]

    def test_show_set_and_switch_back(self):
        code, report = self.run_command("show")
        self.assertEqual((code, report["flag"], report["site"], report["git_projection"]), (0, "absent", "git", True))
        self.assertEqual(report["source_id"], SOURCE)

        code, report = self.run_command("set", "--site", "database", "--git-projection", "true")
        self.assertEqual((code, report["status"], report["generation"]), (0, "written", 2))
        self.assertEqual(report["previous"], {"site": "git", "git_projection": True})
        self.assertTrue(report["projection_pending"])
        site_on = self.pending()
        self.assertEqual(len(site_on), 1)

        code, report = self.run_command("set", "--site", "database", "--git-projection", "true")
        self.assertEqual((code, report["status"], report["generation"]), (0, "unchanged", 2))

        # Switching the projection off is never pushed to Git.
        code, report = self.run_command("set", "--site", "database", "--git-projection", "false")
        self.assertEqual((code, report["status"], report["projection_pending"]), (0, "written", False))
        self.assertEqual(self.pending(), site_on)
        self.assertFalse(data_home.mode(self.database, SOURCE)["git_projection"])

        # Switching back is a new operation even though the document matches an earlier one.
        code, report = self.run_command("set", "--site", "database", "--git-projection", "true")
        self.assertEqual((code, report["status"], report["generation"]), (0, "written", 4))
        self.assertEqual(len(self.pending()), 2)
        head, files = primary_store.read_files(self.database, SOURCE)
        self.assertEqual(files["data/catalog.json"], b'{"games": []}\n')
        self.assertEqual(data_home.parse(files[data_home.FLAG]), {"site": "database", "git_projection": True})

    def test_invalid_combination_and_inactive_source_fail_without_writing(self):
        code, report = self.run_command("set", "--site", "git", "--git-projection", "false")
        self.assertEqual((code, report["status"], report["code"]), (1, "error", "INVALID_DATA_HOME"))
        self.assertEqual(primary_store.read_files(self.database, SOURCE)[0]["generation"], 1)
        code, report = self.run_command("--repository", "owner/inactive", "show")
        self.assertEqual((code, report["code"]), (1, "PRIMARY_NOT_ACTIVE"))

    def test_concurrent_writer_stops_the_switch(self):
        head, _ = primary_store.read_files(self.database, SOURCE)
        original = data_home.current
        def raced(*args, **kwargs):
            result = original(*args, **kwargs)
            primary_store.publish_patch(self.database, SOURCE, base_generation=head["generation"],
                changed={"data/catalog.json": b'{"games": [1]}\n'}, operation_id="other-writer")
            return result
        with patch.object(data_home, "current", side_effect=raced):
            code, report = self.run_command("set", "--site", "database", "--git-projection", "true")
        self.assertEqual((code, report["code"]), (1, "STALE_GENERATION"))
        self.assertEqual(data_home.mode(self.database, SOURCE), {"site": "git", "git_projection": True})


if __name__ == "__main__":
    unittest.main()
