"""Offline restore of exact snapshot bytes with isolated, untrusted SQL input."""

import contextlib
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from radar_backend.storage import documents
from radar_backend.storage.sql import SqliteClient, StorageError
from scripts.storage_restore import main, namespace_for_source, restore


CAPTURED_AT = "2026-10-15T15:30:32+08:00"
RAW_FILES = {
    "master.json": b'\xef\xbb\xbf{\r\n  "group_id64": 103582791475572379, "unknown": [1, 2]\r\n}\r\n',
    "nested/繁體中文.jsonl": '{"name":"遊戲雷達","id":"103582791475572379"}  \r\n{"empty":null}\n'.encode(),
    "nested/empty.json": b"",
    "notes.txt": "保留原始快照的所有檔案\n".encode(),
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class StorageRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def database(self, sources=None, name="source.sqlite"):
        path = self.root / name
        client = SqliteClient(path)
        self.addCleanup(client.close)
        documents.initialize(client)
        heads = {}
        for source_id, files in (sources if sources is not None else {"owner/backend": RAW_FILES}).items():
            heads[source_id] = documents.publish_snapshot(
                client, source_id, "revision-0001", files, CAPTURED_AT)
        return path, client, heads

    def sql_gzip(self, client, *, suffix="", name="snapshot.sql.gz"):
        path = self.root / name
        sql = "\n".join(client.connection.iterdump()) + "\n" + suffix
        path.write_bytes(gzip.compress(sql.encode("utf-8"), mtime=0))
        return path

    def assert_restored(self, output, report, sources, heads):
        self.assertEqual(report["format"], "radar-raw-restore-v1")
        self.assertEqual(report["total_files"], sum(len(files) for files in sources.values()))
        self.assertEqual(report["total_bytes"], sum(len(raw) for files in sources.values() for raw in files.values()))
        self.assertEqual(json.loads((output / "RESTORE_MANIFEST.json").read_text(encoding="utf-8")), report)
        self.assertEqual({source["source_id"] for source in report["sources"]}, set(sources))
        expected_paths = {"RESTORE_MANIFEST.json"}
        for source in report["sources"]:
            source_id = source["source_id"]
            namespace = namespace_for_source(source_id)
            self.assertEqual(source["namespace"], namespace)
            for field in ("revision", "generation", "manifest_digest", "captured_at"):
                self.assertEqual(source[field], heads[source_id][field])
            files = {entry["path"]: entry for entry in source["files"]}
            self.assertEqual(set(files), set(sources[source_id]))
            for relative, raw in sources[source_id].items():
                file_path = output / namespace / relative
                self.assertEqual(file_path.read_bytes(), raw)
                self.assertEqual(files[relative]["sha256"], sha(raw))
                self.assertEqual(files[relative]["raw_bytes"], len(raw))
                expected_paths.add(file_path.relative_to(output).as_posix())
        self.assertEqual({path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}, expected_paths)

    def test_sqlite_restore_preserves_every_raw_byte_and_original_database(self):
        sources = {"owner/backend": RAW_FILES, "owner/igdb": {"master.json": b'{"separate":true}\n'}}
        path, _, heads = self.database(sources)
        before = path.read_bytes()
        output = self.root / "restored"
        report = restore(sqlite_path=path, output=output)
        self.assert_restored(output, report, sources, heads)
        self.assertEqual(sha(path.read_bytes()), sha(before))

    def test_gzip_sql_restore_accepts_own_tables_indexes_and_triggers(self):
        sources = {"owner/backend": RAW_FILES}
        _, client, heads = self.database(sources)
        compressed = self.sql_gzip(client)
        before = compressed.read_bytes()
        output = self.root / "from-gzip"
        report = restore(sql_gzip=compressed, output=output)
        self.assert_restored(output, report, sources, heads)
        self.assertEqual(compressed.read_bytes(), before)

    def test_sqlite_read_snapshot_includes_committed_wal_bytes_without_mutating_input(self):
        path, client, _ = self.database()
        self.assertEqual(client.execute("PRAGMA journal_mode=WAL")[0]["journal_mode"], "wal")
        client.execute("PRAGMA wal_autocheckpoint=0")
        files = {"master.json": b'{"revision":"only-in-wal"}\n'}
        head = documents.publish_snapshot(client, "owner/backend", "revision-0002", files,
                                          "2026-10-15T16:00:00+08:00")
        wal = Path(str(path) + "-wal")
        self.assertTrue(wal.is_file())
        before = {path: path.read_bytes(), wal: wal.read_bytes()}
        output = self.root / "from-wal"
        report = restore(sqlite_path=path, output=output)
        self.assert_restored(output, report, {"owner/backend": files}, {"owner/backend": head})
        for original, raw in before.items():
            self.assertEqual(original.read_bytes(), raw)

    def test_source_filter_restores_exactly_one_namespace_from_both_input_types(self):
        sources = {"owner/backend": RAW_FILES, "owner/igdb": {"data.json": b"[1,2,3]\n"}}
        path, client, heads = self.database(sources)
        compressed = self.sql_gzip(client)
        for mode, source in (("sqlite_path", path), ("sql_gzip", compressed)):
            with self.subTest(mode=mode):
                output = self.root / mode
                report = restore(**{mode: source}, output=output, source_id="owner/igdb")
                self.assert_restored(output, report, {"owner/igdb": sources["owner/igdb"]}, heads)

    def test_unknown_source_fails_without_publishing_output(self):
        path, _, _ = self.database()
        output = self.root / "missing-source"
        with self.assertRaises(StorageError):
            restore(sqlite_path=path, output=output, source_id="does-not-exist")
        self.assertFalse(output.exists())

    def test_namespace_is_safe_deterministic_and_contains_full_source_hash(self):
        sources = ["a/b", "a-b", "a_b", "A/B", "../../outside", "/tmp/absolute", "\\..\\outside",
                   "繁體中文", ".", "..", "x" * 512]
        namespaces = []
        for source in sources:
            with self.subTest(source=source):
                namespace = namespace_for_source(source)
                self.assertIsInstance(namespace, str)
                self.assertEqual(namespace, namespace_for_source(source))
                self.assertIn(sha(source.encode("utf-8")), namespace)
                self.assertEqual(Path(namespace).name, namespace)
                self.assertNotIn("/", namespace)
                self.assertNotIn("\\", namespace)
                self.assertNotIn(namespace, (".", "..", ""))
                self.assertLessEqual(len(namespace.encode("utf-8")), 255)
                namespaces.append(namespace)
        self.assertEqual(len(set(namespaces)), len(sources))

    def test_traversal_source_id_stays_in_its_own_namespace(self):
        sources = {"../outside": {"master.json": b"{}"}, "a/b": {"master.json": b"[1]"},
                   "a-b": {"master.json": b"[2]"}}
        path, _, heads = self.database(sources)
        output = self.root / "safe-sources"
        report = restore(sqlite_path=path, output=output)
        self.assert_restored(output, report, sources, heads)
        self.assertFalse((self.root / "outside").exists())

    def test_corrupt_chunk_fails_without_any_partial_output_for_both_input_types(self):
        path, client, _ = self.database({"a-good": {"one.json": b"{}"}, "z-bad": RAW_FILES})
        object_digest = sha(RAW_FILES["master.json"])
        row = client.execute("SELECT chunk_digests_json FROM radar_objects WHERE digest=?", [object_digest])[0]
        chunk_digest = json.loads(row["chunk_digests_json"])[0]
        client.execute("UPDATE radar_chunks SET payload=? WHERE digest=?", ["Y29ycnVwdA==", chunk_digest])
        compressed = self.sql_gzip(client)
        for mode, source in (("sqlite_path", path), ("sql_gzip", compressed)):
            with self.subTest(mode=mode):
                output = self.root / ("corrupt-" + mode)
                before = set(self.root.iterdir())
                with self.assertRaises(StorageError):
                    restore(**{mode: source}, output=output)
                self.assertFalse(output.exists())
                self.assertEqual(set(self.root.iterdir()), before)

    def test_existing_directory_or_regular_file_is_never_overwritten(self):
        path, _, _ = self.database()
        for kind in ("directory", "file"):
            with self.subTest(kind=kind):
                output = self.root / kind
                if kind == "directory":
                    output.mkdir()
                    marker = output / "keep.txt"
                else:
                    marker = output
                marker.write_bytes(b"do not replace")
                with self.assertRaises(StorageError):
                    restore(sqlite_path=path, output=output)
                self.assertEqual(marker.read_bytes(), b"do not replace")
                if kind == "directory":
                    self.assertEqual(list(output.iterdir()), [marker])

    def test_existing_and_dangling_symlink_outputs_are_rejected(self):
        path, _, _ = self.database()
        real = self.root / "real-target"
        real.mkdir()
        marker = real / "keep.txt"
        marker.write_bytes(b"untouched")
        for name, target in (("linked", real), ("dangling", self.root / "never-created")):
            with self.subTest(name=name):
                output = self.root / name
                output.symlink_to(target, target_is_directory=True)
                with self.assertRaises(StorageError):
                    restore(sqlite_path=path, output=output)
                self.assertTrue(output.is_symlink())
                self.assertEqual(output.readlink(), target)
        self.assertEqual(marker.read_bytes(), b"untouched")
        self.assertFalse((self.root / "never-created").exists())

    def test_malicious_sql_cannot_attach_or_write_external_database(self):
        _, client, _ = self.database()
        external = self.root / "external.sqlite"
        with sqlite3.connect(external) as conn:
            conn.execute("CREATE TABLE keeper (value TEXT)")
            conn.execute("INSERT INTO keeper VALUES ('unchanged')")
        before = external.read_bytes()
        escaped = str(external).replace("'", "''")
        compressed = self.sql_gzip(client, suffix=(
            f"ATTACH DATABASE '{escaped}' AS escaped;\n"
            "UPDATE escaped.keeper SET value='changed';\nDETACH DATABASE escaped;\n"))
        output = self.root / "malicious-attach"
        with self.assertRaises(StorageError):
            restore(sql_gzip=compressed, output=output)
        self.assertFalse(output.exists())
        self.assertEqual(external.read_bytes(), before)

    def test_forbidden_sql_statements_cannot_publish_output(self):
        _, client, _ = self.database()
        attacks = {
            "detach": "DETACH DATABASE main;",
            "pragma": "PRAGMA user_version=123;",
            "writable_schema": "PRAGMA writable_schema=ON;",
            "extension": "SELECT load_extension('untrusted-extension');",
            "virtual_table": "CREATE VIRTUAL TABLE radar_chunks_fts USING fts5(payload);",
            "foreign_table": "CREATE TABLE unrelated_private_data (value TEXT);",
            "foreign_index": "CREATE INDEX unrelated_index ON radar_sources(revision);",
            "foreign_trigger": "CREATE TRIGGER unrelated_trigger AFTER INSERT ON radar_chunks BEGIN DELETE FROM radar_objects; END;",
        }
        for name, sql in attacks.items():
            with self.subTest(name=name):
                compressed = self.sql_gzip(client, suffix=sql, name=name + ".sql.gz")
                output = self.root / ("attack-" + name)
                with self.assertRaises(StorageError):
                    restore(sql_gzip=compressed, output=output)
                self.assertFalse(output.exists())

    def test_own_name_with_tampered_trigger_definition_is_rejected(self):
        _, client, _ = self.database()
        original = client.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                                  ["radar_source_event_insert"])[0]["sql"]
        replacement = ("CREATE TRIGGER radar_source_event_insert AFTER INSERT ON radar_sources "
                       "BEGIN DELETE FROM radar_chunks; END")
        compressed = self.sql_gzip(client)
        sql = gzip.decompress(compressed.read_bytes()).decode("utf-8")
        self.assertIn(original, sql)
        compressed.write_bytes(gzip.compress(sql.replace(original, replacement).encode(), mtime=0))
        output = self.root / "bad-schema"
        with self.assertRaises(StorageError) as caught:
            restore(sql_gzip=compressed, output=output)
        self.assertEqual(caught.exception.code, "INVALID_STORAGE_SCHEMA")
        self.assertFalse(output.exists())

    def test_sql_dump_without_final_commit_cannot_publish_uncommitted_data(self):
        _, client, _ = self.database()
        compressed = self.sql_gzip(client)
        sql = gzip.decompress(compressed.read_bytes()).decode("utf-8")
        self.assertTrue(sql.rstrip().endswith("COMMIT;"))
        sql = sql.rstrip()[:-len("COMMIT;")]
        compressed.write_bytes(gzip.compress(sql.encode(), mtime=0))
        output = self.root / "uncommitted"
        with self.assertRaises(StorageError):
            restore(sql_gzip=compressed, output=output)
        self.assertFalse(output.exists())

    def forge_manifest_path(self, client, source_id, bad_path):
        digest = sha(b"{}")
        raw = json.dumps({"format": documents.MANIFEST_FORMAT, "files": [
            {"path": bad_path, "digest": digest, "raw_bytes": 2},
        ]}, ensure_ascii=False).encode()
        manifest_digest = sha(raw)
        documents._ensure_raw_objects(client, {manifest_digest: raw})
        client.execute("""UPDATE radar_sources SET manifest_digest=?, revision=?, generation=generation+1
            WHERE source_id=?""", [manifest_digest, "forged-" + str(len(bad_path)), source_id])

    def test_forged_manifest_paths_cannot_escape_output(self):
        paths = ["../escaped.json", "nested/../../escaped.json", str(self.root / "absolute-escaped.json"), "nested\\..\\escaped.json",
                 "nested//file.json", "./file.json", ".", "C:/escaped.json",
                 "nested/stream:alternate", "CON/file.json", "nested/file.json."]
        for index, bad_path in enumerate(paths):
            with self.subTest(path=bad_path):
                path, client, _ = self.database({"owner/backend": {"safe.json": b"{}"}}, name=f"path-{index}.sqlite")
                self.forge_manifest_path(client, "owner/backend", bad_path)
                output = self.root / f"path-output-{index}"
                before = set(self.root.iterdir())
                with self.assertRaises(StorageError):
                    restore(sqlite_path=path, output=output)
                self.assertFalse(output.exists())
                self.assertEqual(set(self.root.iterdir()), before)

    def test_path_file_directory_collision_leaves_no_partial_output(self):
        path, _, _ = self.database({"owner/backend": {"nested": b"{}", "nested/file.json": b"[]"}})
        output = self.root / "path-collision"
        before = set(self.root.iterdir())
        with self.assertRaises(StorageError):
            restore(sqlite_path=path, output=output)
        self.assertFalse(output.exists())
        self.assertEqual(set(self.root.iterdir()), before)

    def test_invalid_gzip_and_invalid_sqlite_leave_no_output(self):
        invalid = self.root / "not-a-backup"
        invalid.write_bytes(b"this is not a database or gzip stream")
        for mode in ("sqlite_path", "sql_gzip"):
            with self.subTest(mode=mode):
                output = self.root / ("invalid-" + mode)
                with self.assertRaises(StorageError):
                    restore(**{mode: invalid}, output=output)
                self.assertFalse(output.exists())

    def test_restore_requires_exactly_one_existing_input(self):
        path, client, _ = self.database()
        compressed = self.sql_gzip(client)
        for kwargs in ({}, {"sqlite_path": path, "sql_gzip": compressed},
                       {"sqlite_path": self.root / "missing.sqlite"},
                       {"sql_gzip": self.root / "missing.sql.gz"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(StorageError):
                restore(output=self.root / "invalid-input", **kwargs)
        self.assertFalse((self.root / "invalid-input").exists())
        self.assertFalse((self.root / "missing.sqlite").exists())

    def test_cli_restores_sqlite_and_gzip_with_source_filter(self):
        path, client, heads = self.database()
        compressed = self.sql_gzip(client)
        for option, source in (("--sqlite", path), ("--sql-gzip", compressed)):
            with self.subTest(option=option):
                output = self.root / ("cli-" + option[2:])
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main([option, str(source), "--output", str(output), "--source-id", "owner/backend"])
                self.assertEqual(code, 0)
                report = json.loads((output / "RESTORE_MANIFEST.json").read_text(encoding="utf-8"))
                self.assert_restored(output, report, {"owner/backend": RAW_FILES}, heads)

    def test_cli_argument_exclusivity_and_required_output(self):
        for argv in ([], ["--sqlite", "one", "--sql-gzip", "two", "--output", "out"],
                     ["--sqlite", "one"], ["--output", "out"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(argv)
            self.assertEqual(caught.exception.code, 2)

    def test_cli_operational_error_returns_nonzero_and_leaves_existing_output(self):
        path, _, _ = self.database()
        output = self.root / "existing-cli"
        output.mkdir()
        marker = output / "keep"
        marker.write_bytes(b"untouched")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--sqlite", str(path), "--output", str(output)])
        self.assertNotEqual(code, 0)
        self.assertEqual(marker.read_bytes(), b"untouched")


if __name__ == "__main__":
    unittest.main()
