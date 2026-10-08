"""Offline publication contracts and real, temporary bare Git integration."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from radar_core.publication import (
    GitOperationError, PublicationError, PublicationReceipt,
    PublicationScopeError, SubprocessGitRepository, publish_with_retry,
    snapshot_revision,
)


class SnapshotTests(unittest.TestCase):
    def test_canonical_json_preserves_list_order_and_unicode(self):
        self.assertEqual(snapshot_revision({"b": 2, "a": "遊戲"}),
                         snapshot_revision({"a": "遊戲", "b": 2}))
        self.assertNotEqual(snapshot_revision([1, 2]), snapshot_revision([2, 1]))
        self.assertEqual(len(snapshot_revision(None)), 64)

    def test_non_json_and_non_finite_inputs_are_rejected(self):
        for value in [float("nan"), float("inf"), {1: "value"}, (1, 2), {"a": object()}]:
            with self.subTest(value=repr(value)):
                with self.assertRaises((TypeError, ValueError)):
                    snapshot_revision(value)

    def test_receipt_round_trip_and_strict_evidence_fields(self):
        receipt = PublicationReceipt("source-a", "a" * 64, "b" * 40,
                                     "refs/heads/main", True, 2, "c" * 64)
        self.assertEqual(PublicationReceipt.from_dict(receipt.to_dict()), receipt)
        for field, value in [
            ("changed", 1), ("attempts", True), ("attempts", 0),
            ("published_revision", "main"), ("payload_revision", "x" * 64),
            ("target_snapshot_revision", ""), ("input_revision", " "),
            ("target_ref", "refs/tags/v1"), ("schema_version", True),
        ]:
            raw = receipt.to_dict()
            raw[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises((TypeError, ValueError)):
                    PublicationReceipt.from_dict(raw)
        raw = receipt.to_dict()
        raw["published"] = True
        with self.assertRaises(ValueError):
            PublicationReceipt.from_dict(raw)
        del raw["published"]
        del raw["published_revision"]
        with self.assertRaises(ValueError):
            PublicationReceipt.from_dict(raw)


class GitPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.work = self.root / "work"
        self.other = self.root / "other"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "init", "--initial-branch=main", str(self.seed))
        self.git(self.seed, "config", "user.name", "Fixture")
        self.git(self.seed, "config", "user.email", "fixture@example.invalid")
        (self.seed / "data").mkdir()
        (self.seed / "data/live.json").write_text('{"initial": 1}\n', encoding="utf-8")
        (self.seed / "README.md").write_text("original\n", encoding="utf-8")
        (self.seed / ".gitignore").write_text("output/\n", encoding="utf-8")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Initial fixture")
        self.git(self.seed, "remote", "add", "origin", str(self.remote))
        self.git(self.seed, "push", "origin", "HEAD:main")
        self.git(self.root, "clone", str(self.remote), str(self.work))
        self.git(self.root, "clone", str(self.remote), str(self.other))
        self.git(self.other, "config", "user.name", "Other fixture")
        self.git(self.other, "config", "user.email", "other@example.invalid")

    @staticmethod
    def git(cwd, *args):
        result = subprocess.run(["git", *args], cwd=cwd, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise AssertionError(f"fixture Git command failed: {args[0]}")
        return result.stdout.strip()

    def repository(self, **kwargs):
        return SubprocessGitRepository(self.work, disposable_checkout=True, **kwargs)

    def publish(self, apply, *, repository=None, **kwargs):
        values = dict(paths=["data/live.json"], message="Replay frozen batch",
                      input_revision="frozen-source-2026-10-08", payload_revision=snapshot_revision({"batch": 2}))
        values.update(kwargs)
        return publish_with_retry(repository or self.repository(), apply, **values)

    def merge_batch(self, root):
        file = root / "data/live.json"
        payload = json.loads(file.read_text(encoding="utf-8"))
        payload["batch"] = 2
        file.write_text(json.dumps(payload), encoding="utf-8")

    def advance_remote(self):
        file = self.other / "data/live.json"
        file.write_text('{"initial":1,"newer":3}', encoding="utf-8")
        self.git(self.other, "add", "data/live.json")
        self.git(self.other, "commit", "-m", "Newer independent observation")
        self.git(self.other, "push", "origin", "HEAD:main")

    def test_real_push_returns_the_actual_remote_commit_and_snapshot(self):
        receipt = self.publish(self.merge_batch)
        remote_head = self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main")
        self.assertEqual(receipt.published_revision, remote_head)
        self.assertEqual(receipt.attempts, 1)
        self.assertTrue(receipt.changed)
        self.assertEqual(receipt.target_snapshot_revision, snapshot_revision({
            "data/live.json": {"present": True, "payload": {"initial": 1, "batch": 2}},
        }))

    def test_non_fast_forward_replays_frozen_batch_over_newer_remote_json(self):
        repository = self.repository()
        push = repository.push
        calls = []

        def race_push(revision=None):
            calls.append(True)
            if len(calls) == 1:
                self.advance_remote()
            return push(revision)

        repository.push = race_push
        receipt = self.publish(self.merge_batch, repository=repository)
        self.assertEqual(receipt.attempts, 2)
        self.assertEqual(json.loads((self.work / "data/live.json").read_text()),
                         {"initial": 1, "newer": 3, "batch": 2})
        self.assertEqual(receipt.published_revision,
                         self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main"))

    def test_noop_still_performs_push_and_retries_remote_race(self):
        repository = self.repository()
        push = repository.push
        calls = []

        def race_push(revision=None):
            calls.append(True)
            if len(calls) == 1:
                self.advance_remote()
            return push(revision)

        repository.push = race_push
        receipt = self.publish(lambda _: None, repository=repository)
        self.assertEqual(len(calls), 2)
        self.assertEqual(receipt.attempts, 2)
        self.assertFalse(receipt.changed)
        self.assertEqual(json.loads((self.work / "data/live.json").read_text())["newer"], 3)

    def test_noop_has_no_receipt_when_authenticated_push_cannot_acknowledge(self):
        repository = self.repository(push_command_prefix=("false",))
        with self.assertRaisesRegex(PublicationError, "after 2 attempts"):
            self.publish(lambda _: None, repository=repository, max_attempts=2)

    def test_rejected_push_is_bounded_and_never_leaks_remote_stderr(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\necho token-secret-123 >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        calls = []

        def apply(root):
            calls.append(True)
            self.merge_batch(root)

        with self.assertRaises(PublicationError) as caught:
            self.publish(apply, max_attempts=3)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("token-secret", str(caught.exception))
        self.assertEqual(json.loads(self.git(self.root, "--git-dir", str(self.remote),
                                             "show", "main:data/live.json")), {"initial": 1})

    def test_prefix_replaces_git_and_uses_injected_environment(self):
        wrapper = self.root / "push-wrapper.sh"
        wrapper.write_text(
            '#!/bin/sh\n[ "$PUBLICATION_TEST_AUTH" = "provided" ] || exit 8\nexec git "$@"\n',
            encoding="utf-8",
        )
        receipt = self.publish(self.merge_batch, repository=self.repository(
            push_command_prefix=("sh", str(wrapper)),
            environment={"PUBLICATION_TEST_AUTH": "provided"},
        ))
        self.assertTrue(receipt.changed)

    def test_owned_initial_dirty_collection_is_reset_before_apply(self):
        (self.work / "data/live.json").write_text('{"stale":true}', encoding="utf-8")
        self.git(self.work, "add", "data/live.json")
        self.publish(self.merge_batch)
        self.assertEqual(json.loads((self.work / "data/live.json").read_text()),
                         {"initial": 1, "batch": 2})

    def test_owned_initial_untracked_json_is_removed_before_replay(self):
        (self.work / "data/stale.json").write_text('{"old":true}', encoding="utf-8")
        self.publish(self.merge_batch, paths=["data/live.json", "data/stale.json"])
        self.assertFalse((self.work / "data/stale.json").exists())

    def test_unrelated_initial_tracked_and_hidden_staged_changes_are_rejected(self):
        file = self.work / "README.md"
        file.write_text("staged edit", encoding="utf-8")
        self.git(self.work, "add", "README.md")
        file.write_text("original\n", encoding="utf-8")
        with self.assertRaises(PublicationScopeError):
            self.publish(self.merge_batch)
        self.assertEqual(self.git(self.work, "show", ":README.md"), "staged edit")

    def test_unrelated_callback_change_fails_before_push(self):
        def apply(root):
            self.merge_batch(root)
            (root / "README.md").write_text("bad edit", encoding="utf-8")

        with self.assertRaises(PublicationScopeError):
            self.publish(apply)

    def test_commit_hook_cannot_add_unowned_files_to_the_published_commit(self):
        hook = self.work / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook edit' > README.md\ngit add README.md\n", encoding="utf-8")
        hook.chmod(0o755)
        original = self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main")
        with self.assertRaises(PublicationScopeError):
            self.publish(self.merge_batch)
        self.assertEqual(original, self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main"))

    def test_push_wrapper_cannot_publish_a_different_mutated_head(self):
        wrapper = self.root / "mutating-push.sh"
        wrapper.write_text(
            '#!/bin/sh\nprintf "wrapper edit" > README.md\ngit add README.md\n'
            'git -c user.name=Fixture -c user.email=fixture@example.invalid commit -m "Unexpected local change" >/dev/null\n'
            'exec git "$@"\n', encoding="utf-8",
        )
        with self.assertRaisesRegex(PublicationError, "HEAD changed"):
            self.publish(self.merge_batch, repository=self.repository(push_command_prefix=("sh", str(wrapper))))
        self.assertEqual(self.git(self.root, "--git-dir", str(self.remote), "show", "main:README.md"), "original")

    def test_existing_ignored_outputs_are_preserved_but_callback_cannot_change_them(self):
        (self.work / "output").mkdir()
        file = self.work / "output/frozen.json"
        file.write_text('{"frozen":true}', encoding="utf-8")
        self.publish(self.merge_batch)
        self.assertEqual(file.read_text(), '{"frozen":true}')

        def apply(root):
            (root / "output/frozen.json").write_text('{"frozen":false}', encoding="utf-8")

        with self.assertRaises(PublicationScopeError):
            self.publish(apply)

    def test_new_untracked_outside_owned_paths_is_rejected_even_if_ignored(self):
        def apply(root):
            (root / "output").mkdir()
            (root / "output/new.json").write_text("{}", encoding="utf-8")

        with self.assertRaises(PublicationScopeError):
            self.publish(apply)

    def test_remote_cannot_overwrite_preserved_ignored_input(self):
        (self.work / "output").mkdir()
        file = self.work / "output/frozen.json"
        file.write_text('{"frozen":true}', encoding="utf-8")
        (self.other / "output").mkdir()
        (self.other / "output/frozen.json").write_text('{"remote":true}', encoding="utf-8")
        self.git(self.other, "add", "--force", "output/frozen.json")
        self.git(self.other, "commit", "-m", "Conflicting input path")
        self.git(self.other, "push", "origin", "HEAD:main")
        with self.assertRaises(PublicationScopeError):
            self.publish(self.merge_batch)
        self.assertEqual(file.read_text(), '{"frozen":true}')

    def test_remote_ancestor_file_cannot_destroy_ignored_input_directory(self):
        (self.work / "output/input").mkdir(parents=True)
        file = self.work / "output/input/frozen.json"
        file.write_text('{"frozen":true}', encoding="utf-8")
        (self.other / "output").mkdir()
        (self.other / "output/input").write_text("replacement file", encoding="utf-8")
        self.git(self.other, "add", "--force", "output/input")
        self.git(self.other, "commit", "-m", "Conflicting ancestor path")
        self.git(self.other, "push", "origin", "HEAD:main")
        with self.assertRaises(PublicationScopeError):
            self.publish(self.merge_batch)
        self.assertEqual(file.read_text(), '{"frozen":true}')

    def test_corrupt_or_ambiguous_json_is_not_published(self):
        for raw in ["broken", '{"a":NaN}', '{"a":1,"a":2}']:
            with self.subTest(raw=raw):
                repository = self.repository()
                # Remove the previous failed apply without accepting its commit.
                self.git(self.work, "reset", "--hard", "origin/main")
                original = self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main")
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    self.publish(lambda root: (root / "data/live.json").write_text(raw), repository=repository)
                self.assertEqual(original, self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main"))

    def test_apply_exception_is_not_retried(self):
        calls = []

        def apply(_):
            calls.append(True)
            raise ValueError("invalid source document")

        with self.assertRaisesRegex(ValueError, "invalid source"):
            self.publish(apply)
        self.assertEqual(len(calls), 1)

    def test_directory_scope_permits_json_add_and_delete_but_not_non_json(self):
        (self.work / "data/history").mkdir()

        def apply(root):
            (root / "data/history").mkdir(exist_ok=True)
            (root / "data/history/new.json").write_text('{"new":true}', encoding="utf-8")
            (root / "data/live.json").unlink()

        receipt = self.publish(apply, paths=["data/history/", "data/live.json"])
        self.assertTrue(receipt.changed)
        self.assertEqual(receipt.target_snapshot_revision, snapshot_revision({
            "data/history/new.json": {"present": True, "payload": {"new": True}},
            "data/live.json": {"present": False},
        }))

        def invalid(root):
            (root / "data/history/new.txt").write_text("unowned", encoding="utf-8")

        with self.assertRaises(PublicationScopeError):
            self.publish(invalid, paths=["data/history/"])

    def test_safe_explicit_checkpoint_outside_data_is_supported(self):
        def apply(root):
            file = root / "experiments/daily/checkpoint.json"
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('{"pending":[1]}', encoding="utf-8")

        receipt = self.publish(apply, paths=["experiments/daily/checkpoint.json"])
        self.assertTrue(receipt.changed)

    def test_unsafe_scopes_symlinks_and_non_disposable_checkout_are_rejected(self):
        with self.assertRaises(ValueError):
            SubprocessGitRepository(self.work)
        for path in ["data", "data/../README.json", "/data/live.json", ".git/config",
                     "data/live.txt", "experiments/", "experiments/daily/", "data//live.json"]:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.publish(lambda _: None, paths=[path])
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")

        def symlink(root):
            (root / "data/live.json").unlink()
            (root / "data/live.json").symlink_to(outside)

        with self.assertRaises(PublicationScopeError):
            self.publish(symlink)

    def test_git_errors_hide_output_and_invalid_attempts_stop_before_apply(self):
        repository = self.repository()
        with self.assertRaises(GitOperationError) as caught:
            repository._run("show", "invalid-token-secret-reference")
        self.assertNotIn("token-secret", str(caught.exception))
        for attempts in [0, 21, True, "5"]:
            with self.subTest(attempts=attempts):
                with self.assertRaises(ValueError):
                    self.publish(lambda _: self.fail("must not apply"), max_attempts=attempts)


if __name__ == "__main__":
    unittest.main()
