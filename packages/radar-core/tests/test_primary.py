"""DB-primary behavior with real local Git and deterministic storage failures."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from radar_core.publication import PublicationError, PublicationScopeError, SubprocessGitRepository, publish_with_retry, snapshot_revision
from radar_core.storage import data_home, primary
from radar_core.storage.sql import StorageError


SOURCE = "example/radar@main"


class Store:
    def __init__(self):
        self.files = {"data/live.json": b'{"db": 1}\n', "data/other.json": b'{"independent": true}\n'}
        self.generation = 1
        self.operations, self.acks = {}, []
        self.active, self.fail, self.race = True, None, False

    def head(self):
        manifest = {p: {"digest": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw)} for p, raw in self.files.items()}
        digest = snapshot_revision(manifest)
        return {"source_id": SOURCE, "generation": self.generation, "revision": digest,
                "manifest_digest": digest, "manifest": manifest}

    def primary_status(self, db, source):
        if self.fail:
            raise StorageError(self.fail)
        return {"active": self.active, "mode": "primary" if self.active else "legacy"}

    def read_files(self, db, source, paths=None, *, revision=None, known_files=None):
        if self.fail:
            raise StorageError(self.fail)
        return self.head(), {p: raw for p, raw in self.files.items() if paths is None or primary._selected(p, paths)}

    def publish_patch(self, db, source, *, base_generation, changed, deleted, operation_id, payload_revision=None,
                      projection_required=True):
        if self.fail:
            raise StorageError(self.fail)
        if operation_id in self.operations:
            return self.operations[operation_id]
        if self.race:
            self.race = False
            self.files["data/live.json"] = b'{"db": 1, "concurrent": 2}\n'
            self.generation += 1
        if base_generation != self.generation:
            raise StorageError("STALE_GENERATION")
        for name in deleted:
            self.files.pop(name, None)
        self.files.update(changed)
        if changed or deleted:
            self.generation += 1
        receipt = {**self.head(), "operation_id": operation_id, "projection_pending": projection_required,
                   "delivery_target": "git" if projection_required else "database_only",
                   "changed": bool(changed or deleted)}
        self.operations[operation_id] = receipt
        return receipt

    def projection_pending(self, db, source):
        return [value for key, value in self.operations.items()
                if value["delivery_target"] == "git" and key not in {ack[0] for ack in self.acks}]

    def acknowledge_projection(self, db, source, operation_id, git_revision, **evidence):
        self.acks.append((operation_id, git_revision, evidence))


class PrimaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote, self.seed, self.work = (self.root / name for name in ("remote.git", "seed", "work"))
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "init", "--initial-branch=main", str(self.seed))
        (self.seed / "data").mkdir()
        (self.seed / "data/live.json").write_text('{"git": 0}\n')
        (self.seed / "data/removed.json").write_text('{"stale": true}\n')
        (self.seed / "README.md").write_text("application code\n")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "initial")
        self.git(self.seed, "remote", "add", "origin", str(self.remote))
        self.git(self.seed, "push", "origin", "HEAD:main")
        self.git(self.root, "clone", str(self.remote), str(self.work))
        self.store = Store()
        self.addCleanup(patch.stopall)
        patch.object(primary, "_store", return_value=self.store).start()
        patch.object(primary, "client", return_value=object()).start()
        patch.dict(os.environ, {"RADAR_DATABASE_PRIMARY": "0"}).start()

    def git(self, cwd, *args):
        result = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, text=True)
        if result.returncode:
            self.fail("Fixture Git operation failed: " + args[0])
        return result.stdout.strip()

    def repo(self):
        return SubprocessGitRepository(self.work, disposable_checkout=True, primary=primary.GitPrimary(SOURCE))

    def publish(self, apply, repository=None, run=1, paths=("data/live.json",), **options):
        return publish_with_retry(repository or self.repo(), apply, paths=list(paths), message="test",
            input_revision="input", payload_revision=snapshot_revision({"run": run}), **options)

    def projection_off(self, git_projection=False):
        self.store.files[data_home.FLAG] = data_home.document(site="database", git_projection=git_projection)

    def update(self, root):
        path = root / "data/live.json"
        data = json.loads(path.read_bytes())
        data["observation"] = "遊戲 103582791429521430"
        path.write_text(json.dumps(data, ensure_ascii=False) + "\n")

    def test_db_authority_and_true_git_ack(self):
        receipt = self.publish(self.update)
        self.assertEqual(receipt.published_revision, self.git(self.remote, "rev-parse", "main"))
        public = json.loads(self.git(self.remote, "show", "main:data/live.json"))
        self.assertEqual(public["db"], 1)
        self.assertNotIn("git", public)
        self.assertEqual(self.store.acks[0][1], receipt.published_revision)
        self.assertEqual(self.store.acks[0][2]["manifest_digest"], self.store.head()["manifest_digest"])
        self.assertEqual(self.git(self.work, "status", "--porcelain"), "")
        self.assertFalse((self.work / "data/removed.json").exists())
        self.assertIn("data/other.json", self.git(self.remote, "ls-tree", "-r", "--name-only", "main"))

    def test_cas_race_replays_frozen_apply_against_new_db(self):
        self.store.race = True
        calls = []
        def apply(root):
            calls.append(1)
            self.update(root)
        receipt = self.publish(apply)
        self.assertEqual(receipt.attempts, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(self.store.files["data/live.json"])["concurrent"], 2)

    def test_git_failure_retains_db_and_pending_then_recovery_publishes_it(self):
        repository = self.repo()
        repository.push = lambda revision=None: False
        with self.assertRaises(PublicationError):
            self.publish(self.update, repository, max_attempts=2)
        self.assertIn("observation", json.loads(self.store.files["data/live.json"]))
        self.assertEqual(len(self.store.operations), 1)
        self.assertEqual(self.store.acks, [])
        receipt = self.publish(self.update)
        self.assertEqual(len(self.store.operations), 1)
        self.assertEqual(self.store.acks[0][1], receipt.published_revision)

    def test_uncommitted_replay_cannot_escape_duplicate_db_operation(self):
        self.publish(self.update)
        def changed_replay(root):
            (root / "data/live.json").write_text('{"unauthorized_candidate": 1}')
        self.publish(changed_replay)
        public = self.git(self.remote, "show", "main:data/live.json")
        self.assertNotIn("unauthorized_candidate", public)

    def test_projection_off_commits_database_without_push_or_ack(self):
        self.projection_off()
        before = self.git(self.remote, "rev-parse", "main")
        repository = self.repo()
        receipt = self.publish(self.update, repository)
        self.assertTrue(repository.database_only)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), before)
        self.assertEqual(receipt.published_revision, self.store.head()["manifest_digest"])
        self.assertEqual(self.store.acks, [])
        self.assertEqual([op["delivery_target"] for op in self.store.operations.values()], ["database_only"])
        self.assertIn("observation", json.loads(self.store.files["data/live.json"]))
        # The builder baseline still holds the committed snapshot; it is never pushed.
        self.assertIn("observation", json.loads((self.work / "data/live.json").read_bytes()))
        self.assertEqual(self.git(self.work, "status", "--porcelain"), "")

    def test_projection_off_concurrent_writer_retries_through_generation(self):
        self.projection_off()
        self.store.race = True
        receipt = self.publish(self.update)
        self.assertEqual(receipt.attempts, 2)
        live = json.loads(self.store.files["data/live.json"])
        self.assertEqual((live["concurrent"], live["observation"]), (2, "遊戲 103582791429521430"))
        self.assertEqual(receipt.published_revision, self.store.head()["manifest_digest"])

    def test_projection_back_on_pushes_the_database_snapshot(self):
        self.projection_off()
        self.publish(self.update)
        self.projection_off(git_projection=True)
        receipt = self.publish(lambda root: None, run=2)
        self.assertEqual(receipt.published_revision, self.git(self.remote, "rev-parse", "main"))
        public = json.loads(self.git(self.remote, "show", "main:data/live.json"))
        self.assertEqual(public["observation"], "遊戲 103582791429521430")
        flag = json.loads(self.git(self.remote, "show", "main:" + data_home.FLAG))
        self.assertEqual((flag["site"], flag["git_projection"]), ("database", True))
        self.assertEqual(self.store.acks[0][1], receipt.published_revision)

    def test_invalid_flag_stops_before_database_or_checkout_changes(self):
        for raw in (data_home.document(site="database", git_projection=False).replace(b"database", b"git"),
                    b"[]\n", b'{"schema_version": 1, "site": "database", "git_projection": "off"}\n'):
            with self.subTest(raw=raw):
                self.store.files[data_home.FLAG] = raw
                with self.assertRaises(StorageError) as raised:
                    self.publish(self.update)
                self.assertEqual(raised.exception.code, "INVALID_DATA_HOME")
                self.assertEqual(self.store.operations, {})
                self.assertFalse((self.work / data_home.FLAG).exists())

    def test_publications_cannot_change_or_remove_the_flag(self):
        self.projection_off(git_projection=True)
        switch = data_home.document(site="database", git_projection=False)
        for apply in (lambda root: (root / data_home.FLAG).write_bytes(switch),
                      lambda root: (root / data_home.FLAG).unlink()):
            with self.subTest(apply=apply):
                with self.assertRaises(StorageError) as raised:
                    self.publish(apply, paths=("data/publication/",))
                self.assertEqual(raised.exception.code, "DATA_HOME_READ_ONLY")
                self.assertEqual(self.store.operations, {})
        self.assertTrue(data_home.parse(self.store.files[data_home.FLAG])["git_projection"])

    def test_deletion_is_durable_and_unowned_db_document_is_preserved(self):
        self.publish(lambda root: (root / "data/live.json").unlink())
        self.assertNotIn("data/live.json", self.store.files)
        self.assertIn("data/other.json", self.store.files)
        self.assertNotIn("data/live.json", self.git(self.remote, "ls-tree", "-r", "--name-only", "main"))

    def test_outage_and_inactive_source_fail_without_git_fallback(self):
        for code in ("NETWORK_ERROR", "PRIMARY_NOT_ACTIVE"):
            with self.subTest(code=code):
                self.store.fail = code if code == "NETWORK_ERROR" else None
                self.store.active = code != "PRIMARY_NOT_ACTIVE"
                with self.assertRaises(StorageError) as raised:
                    self.publish(self.update)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.store.operations, {})

    def test_scope_violation_and_invalid_json_do_not_commit_db(self):
        def outside(root):
            (root / "README.md").write_text("changed")
        with self.assertRaises(PublicationScopeError):
            self.publish(outside)
        self.git(self.work, "reset", "--hard", "origin/main")
        with self.assertRaises(StorageError):
            self.publish(lambda root: (root / "data/live.json").write_text('{"n": NaN}'))
        self.assertEqual(self.store.operations, {})

    def test_hydrate_preserves_raw_bytes_and_rejects_symlink_before_write(self):
        raw = b'{ "id" : "103582791429521430", "name":"\xe9\x81\x8a\xe6\x88\xb2" }\r\n'
        self.store.files["data/live.json"] = raw
        primary.hydrate(self.work, SOURCE, clean_baseline=True)
        self.assertEqual((self.work / "data/live.json").read_bytes(), raw)
        self.assertEqual(self.git(self.work, "status", "--porcelain"), "")
        target = self.root / "outside.json"
        target.write_text('{"untouched": true}')
        (self.work / "data/live.json").unlink()
        (self.work / "data/live.json").symlink_to(target)
        with self.assertRaises(StorageError):
            primary.hydrate(self.work, SOURCE)
        self.assertEqual(target.read_text(), '{"untouched": true}')

    def test_disabled_cli_never_opens_database(self):
        with patch.object(primary, "client", side_effect=AssertionError("must not connect")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(primary.main(["hydrate", "--root", str(self.work), "--source", SOURCE]), 0)

    def test_read_helpers_missing_file_and_same_snapshot(self):
        head, files = primary.read_files("example/radar", ["data/live.json", "data/other.json"])
        self.assertEqual(head["generation"], 1)
        self.assertEqual(len(files), 2)
        self.assertEqual(primary.read_json(SOURCE, "data/live.json"), {"db": 1})
        with self.assertRaises(StorageError) as raised:
            primary.read_json(SOURCE, "data/missing.json")
        self.assertEqual(raised.exception.code, "FILE_NOT_FOUND")

    def test_enabled_constructor_requires_database_and_activated_source(self):
        with patch.dict(os.environ, {"RADAR_DATABASE_PRIMARY": "1"}), patch.object(primary, "infer_source", return_value=SOURCE):
            with patch.object(primary, "client", side_effect=StorageError("INVALID_CONFIG")):
                with self.assertRaises(StorageError):
                    primary.git_primary(self.work)
            self.store.active = False
            with self.assertRaises(StorageError) as raised:
                primary.git_primary(self.work)
            self.assertEqual(raised.exception.code, "PRIMARY_NOT_ACTIVE")

    def test_real_sqlite_cas_and_git_projection_ack(self):
        from radar_core.storage import documents, primary_store
        from radar_core.storage.sql import SqliteClient
        database = SqliteClient()
        self.addCleanup(database.close)
        documents.initialize(database)
        primary_store.bootstrap_primary(database, SOURCE, self.store.files, "a" * 40)
        with patch.object(primary, "_store", return_value=primary_store), patch.object(primary, "client", return_value=database):
            raced = []
            def apply(root):
                if not raced:
                    raced.append(True)
                    head, _ = primary_store.read_files(database, SOURCE)
                    primary_store.publish_patch(database, SOURCE, base_generation=head["generation"],
                        changed={"data/live.json": b'{"db": 1, "real_concurrent": 3}\n'},
                        operation_id="other-writer")
                self.update(root)
            receipt = self.publish(apply)
            self.assertEqual(receipt.attempts, 2)
            head, files = primary_store.read_files(database, SOURCE)
            self.assertEqual(json.loads(files["data/live.json"])["real_concurrent"], 3)
            self.assertEqual(primary_store.projection_pending(database, SOURCE), [])
            proof = primary.resolve_git_revision(SOURCE, receipt.published_revision)
            self.assertEqual(proof["manifest_digest"], head["manifest_digest"])
            historical = primary.read_json(SOURCE, "data/live.json", revision=receipt.published_revision)
            self.assertEqual(historical["observation"], "遊戲 103582791429521430")

    def test_explicit_database_only_write_does_not_fabricate_git_ack(self):
        from radar_core.storage import documents, primary_store
        from radar_core.storage.sql import SqliteClient
        database = SqliteClient()
        self.addCleanup(database.close)
        documents.initialize(database)
        primary_store.bootstrap_primary(database, SOURCE, self.store.files, "a" * 40)
        with patch.object(primary, "_store", return_value=primary_store), patch.object(primary, "client", return_value=database):
            head, _ = primary.read_files(SOURCE)
            receipt = primary.write_files(SOURCE, {"data/private_cache.json": b'{"id":"103582791429521430"}\n'},
                base_generation=head["generation"], operation_id="cache-op", projection_required=False)
            self.assertFalse(receipt["projection_pending"])
            self.assertEqual(receipt["delivery_target"], "database_only")
            self.assertEqual(primary_store.projection_pending(database, SOURCE), [])
            self.assertEqual(database.execute("SELECT * FROM radar_projections"), [])
            self.assertEqual(primary.read_json(SOURCE, "data/private_cache.json"), {"id": "103582791429521430"})

    def sqlite(self, files):
        from radar_core.storage import documents, primary_store
        from radar_core.storage.sql import SqliteClient
        database = SqliteClient()
        self.addCleanup(database.close)
        documents.initialize(database)
        primary_store.bootstrap_primary(database, SOURCE, files, "a" * 40)
        for target, value in ((primary, "_store"), (primary, "client")):
            patch.object(target, value, return_value=primary_store if value == "_store" else database).start()
        return database, primary_store

    def test_real_sqlite_projection_off_receipt_resolves_later(self):
        database, store = self.sqlite({**self.store.files,
            data_home.FLAG: data_home.document(site="database", git_projection=False)})
        before = self.git(self.remote, "rev-parse", "main")
        first = self.publish(self.update)
        later = self.publish(lambda root: (root / "data/live.json").write_text('{"db": 2}\n'), run=2)
        head, _ = store.read_files(database, SOURCE)
        self.assertEqual(later.published_revision, head["manifest_digest"])
        self.assertNotEqual(first.published_revision, later.published_revision)
        self.assertEqual(store.projection_pending(database, SOURCE), [])
        self.assertEqual(database.execute("SELECT * FROM radar_projections"), [])
        historical = primary.read_json(SOURCE, "data/live.json", revision=first.published_revision)
        self.assertEqual(historical["observation"], "遊戲 103582791429521430")
        self.assertEqual(primary.read_json(SOURCE, "data/live.json", revision=later.published_revision), {"db": 2})
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), before)

    def test_real_sqlite_switch_between_attempts_reuses_the_committed_operation(self):
        database, store = self.sqlite(self.store.files)
        repository = self.repo()
        def push(revision=None):
            repository.push = lambda revision=None: SubprocessGitRepository.push(repository, revision)
            # The owner switches the projection off while this first push fails.
            head, _ = store.read_files(database, SOURCE)
            store.publish_patch(database, SOURCE, base_generation=head["generation"],
                changed={data_home.FLAG: data_home.document(site="database", git_projection=False)},
                operation_id="switch-off", projection_required=False)
            return False
        repository.push = push
        before = self.git(self.remote, "rev-parse", "main")
        receipt = self.publish(self.update, repository)
        self.assertEqual(receipt.attempts, 2)
        head, files = store.read_files(database, SOURCE)
        self.assertEqual(receipt.published_revision, head["manifest_digest"])
        self.assertEqual(json.loads(files["data/live.json"])["observation"], "遊戲 103582791429521430")
        # The Git-bound commit of the first attempt waits for the projection to return.
        self.assertEqual(len(store.projection_pending(database, SOURCE)), 1)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), before)


if __name__ == "__main__":
    unittest.main()
