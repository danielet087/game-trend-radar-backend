"""Cutover ordering, complete discovery, encrypted secrets and safe failures."""
from contextlib import redirect_stdout
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from nacl.public import PrivateKey, SealedBox

from radar_core.storage import documents, primary_store
from radar_core.storage.sql import StorageError
from scripts import database_primary_control as control
from scripts import storage_sync
from scripts import storage_legacy_evidence


REVISION = "a" * 40


class GitHubFixture:
    def __init__(self):
        self.variables, self.calls, self.writes, self.files = {}, [], [], {}
        self.fail_repository = None
        self.runs = {"workflow_runs": [], "total_count": 0}
        self.key = PrivateKey.generate()

    def call(self, method, repository, path, data=None, **kwargs):
        self.calls.append((method, repository, path, data))
        if repository == self.fail_repository:
            raise control.ControlError("GITHUB_PERMISSION_OR_REQUEST_FAILED", repository=repository, status=403)
        if method == "GET" and path == "/actions/secrets/public-key":
            return {"key_id": "fixture-key", "key": base64.b64encode(bytes(self.key.public_key)).decode()}
        if method == "GET" and path.startswith("/actions/runs?"):
            return self.runs
        if method == "GET":
            return {}
        self.writes.append((method, repository, path, data))
        return {}

    def variable(self, repository, name):
        return self.variables.get((repository, name))

    def set_variable(self, repository, name, value):
        self.writes.append(("variable", repository, name, value))
        self.variables[(repository, name)] = value

    def file(self, repository, path):
        return self.files[(repository, path)]

    def deployment(self):
        reviewed = {}
        for repository, workflows in control.DEPLOYMENTS.items():
            self.files[(repository, "requirements-core.txt")] = (
                "radar-core @ git+https://github.com/danielet087/game-trend-radar-backend.git@"
                + REVISION + "#subdirectory=packages/radar-core\n")
            if repository not in control.RUNTIMES:
                continue
            reviewed[repository] = {}
            for workflow in workflows:
                body = "env: " + control.PRIMARY_FLAG + "\nif: vars." + control.PAUSE_FLAG + " != '1'\n"
                self.files[(repository, ".github/workflows/" + workflow)] = body
                reviewed[repository][workflow] = hashlib.sha256(body.encode()).hexdigest()
        return reviewed


class DatabasePrimaryControlTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHubFixture()
        self.chosen = {repository: self.github for repository in control.RUNTIMES}

    def test_prepare_preserves_active_and_paused_flags_and_encrypts_exact_secrets(self):
        for repository in control.RUNTIMES:
            self.github.variables[(repository, control.PRIMARY_FLAG)] = "1"
            self.github.variables[(repository, control.PAUSE_FLAG)] = "1"
        keys = {repo: self.github.call("GET", repo, "/actions/secrets/public-key") for repo in control.RUNTIMES}
        secrets = {"TURSO_DATABASE_URL": "libsql://fixture.turso.io", "TURSO_AUTH_TOKEN": "private-test-token"}
        with patch.dict(os.environ, secrets):
            report = control.prepare(self.chosen, keys)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(value == "1" for value in self.github.variables.values()))
        self.assertEqual(len(self.github.writes), 8)
        decrypted = {}
        for method, repo, path, body in self.github.writes:
            self.assertEqual(method, "PUT")
            self.assertNotEqual(repo, "game-trend-radar-backend")
            self.assertEqual(set(body), {"key_id", "encrypted_value"})
            name = path.rsplit("/", 1)[1]
            raw = SealedBox(self.github.key).decrypt(base64.b64decode(body["encrypted_value"]))
            self.assertEqual(raw.decode(), secrets[name])
            decrypted.setdefault(repo, set()).add(name)
        self.assertEqual(set(decrypted), set(control.RUNTIMES) - {"game-trend-radar-backend"})
        for value in secrets.values():
            self.assertNotIn(value, json.dumps(report))
            self.assertNotIn(value, json.dumps(self.github.writes))

    def test_prepare_initializes_only_absent_flags_without_disabling_existing_primary(self):
        self.github.variables[("game-trend-radar", control.PRIMARY_FLAG)] = "1"
        keys = {repo: self.github.call("GET", repo, "/actions/secrets/public-key") for repo in control.RUNTIMES}
        with patch.dict(os.environ, {"TURSO_DATABASE_URL": "libsql://fixture.turso.io", "TURSO_AUTH_TOKEN": "token"}):
            control.prepare(self.chosen, keys)
        self.assertEqual(self.github.variable("game-trend-radar", control.PRIMARY_FLAG), "1")
        self.assertEqual(self.github.variable("game-trend-radar", control.PAUSE_FLAG), "0")
        self.assertEqual(len(self.github.variables), len(control.RUNTIMES) * 2)

    def test_all_repository_capabilities_are_discovered_before_any_secret_write(self):
        self.github.fail_repository = control.RUNTIMES[-1]
        with patch.dict(os.environ, {"FRONTEND_REPO_TOKEN": "first", "CONTENT_BACKEND_TOKEN": "second"}), \
                patch.object(control, "GitHub", return_value=self.github):
            with self.assertRaises(control.ControlError) as caught:
                control.clients()
        self.assertEqual(caught.exception.repository, control.RUNTIMES[-1])
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.github.writes, [])
        self.assertTrue(all(method == "GET" for method, *_ in self.github.calls))
        self.assertEqual({repo for _, repo, _, _ in self.github.calls}, set(control.RUNTIMES))

    def test_dedicated_rollout_credential_precedes_existing_publication_tokens(self):
        denied = GitHubFixture()
        denied.fail_repository = control.RUNTIMES[0]
        with patch.dict(os.environ, {"DATABASE_ROLLOUT_TOKEN": "rollout", "FRONTEND_REPO_TOKEN": "publication", "CONTENT_BACKEND_TOKEN": "content"}), \
                patch.object(control, "GitHub", side_effect=lambda token: self.github if token == "rollout" else denied):
            chosen, keys = control.clients()
        self.assertTrue(all(client is self.github for client in chosen.values()))
        self.assertEqual(set(keys), set(control.RUNTIMES))
        self.assertEqual(denied.calls, [])

    def test_drain_rejects_truncated_or_malformed_inventory_before_ignoring_tests(self):
        cases = [
            {"total_count": 101, "workflow_runs": [{"id": i, "path": "test.yml"} for i in range(100)]},
            {"workflow_runs": []}, {"total_count": 0},
            {"total_count": True, "workflow_runs": []},
            {"total_count": -1, "workflow_runs": []},
        ]
        for response in cases:
            with self.subTest(response=response):
                self.github.runs = response
                with patch.object(control.time, "sleep") as sleep:
                    with self.assertRaises(control.ControlError) as caught:
                        control.drain({control.RUNTIMES[0]: self.github})
                    self.assertEqual(caught.exception.code, "INCOMPLETE_WRITER_INVENTORY")
                    sleep.assert_not_called()

    def test_drain_ignores_own_controller_and_readonly_tests_but_waits_for_writer(self):
        self.github.runs = {"total_count": 2, "workflow_runs": [
            {"id": 77, "path": ".github/workflows/database-primary-control.yml"},
            {"id": 78, "path": ".github/workflows/test-radar-core.yml"}]}
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "77"}), patch.object(control.time, "sleep") as sleep:
            control.drain({control.RUNTIMES[0]: self.github})
            sleep.assert_not_called()
        self.github.runs = {"total_count": 1, "workflow_runs": [{"id": 79, "path": "steam-public-growth.yml"}]}
        with patch.object(control.time, "monotonic", side_effect=[0, 1]), redirect_stdout(io.StringIO()):
            with self.assertRaises(control.ControlError) as caught:
                control.drain({control.RUNTIMES[0]: self.github}, deadline_seconds=0)
        self.assertEqual(caught.exception.code, "WRITER_DRAIN_TIMEOUT")

    def test_deployment_hash_failure_precedes_pause_and_database_access(self):
        hashes = self.github.deployment()
        hashes["game-trend-radar"]["radar-insights.yml"] = "f" * 64
        with patch.object(control.TursoClient, "from_env") as database:
            with self.assertRaises(control.ControlError) as caught:
                control.cutover(self.chosen, REVISION, hashes)
        self.assertEqual(caught.exception.code, "WORKFLOW_REVIEW_MISMATCH")
        self.assertEqual(self.github.writes, [])
        database.assert_not_called()

    def test_core_pin_and_legacy_fence_are_both_required(self):
        hashes = self.github.deployment()
        self.github.files[("game-trend-radar-igdb-backend", "requirements-core.txt")] = "wrong pin"
        with self.assertRaises(control.ControlError) as caught:
            control.validate_deployments(self.chosen, REVISION, hashes)
        self.assertEqual(caught.exception.code, "CORE_PIN_MISMATCH")
        hashes = self.github.deployment()
        body = "if: vars." + control.PAUSE_FLAG + " != '1'\n"
        self.github.files[("game-trend-radar", ".github/workflows/legacy-once.yml")] = body
        hashes["game-trend-radar"]["legacy-once.yml"] = hashlib.sha256(body.encode()).hexdigest()
        with self.assertRaises(control.ControlError) as caught:
            control.validate_deployments(self.chosen, REVISION, hashes)
        self.assertEqual(caught.exception.code, "LEGACY_WRITER_NOT_FENCED")

    def test_partial_activation_keeps_every_writer_paused_and_does_not_enable_flags(self):
        order = []
        def activate(database, source):
            order.append(source)
            if len(order) == 2:
                raise StorageError("ACTIVATION_FAILED", "sensitive-provider-detail")
            return {}
        with patch.object(control, "validate_deployments"), patch.object(control, "drain"), \
                patch.object(control.TursoClient, "from_env", return_value=object()), \
                patch.object(documents, "initialize"), patch.object(storage_sync, "ingest", return_value=([], [])), \
                patch.object(storage_legacy_evidence, "migrate_legacy_evidence"), \
                patch.object(primary_store, "activate_primary", side_effect=activate):
            with self.assertRaises(StorageError):
                control.cutover(self.chosen, REVISION, {})
        self.assertEqual(len(order), 2)
        self.assertEqual(self.github.writes, [("variable", repo, control.PAUSE_FLAG, "1") for repo in control.RUNTIMES])
        self.assertTrue(all(self.github.variable(repo, control.PAUSE_FLAG) == "1" for repo in control.RUNTIMES))

    def test_final_import_failure_keeps_pause_and_prevents_activation(self):
        with patch.object(control, "validate_deployments"), patch.object(control, "drain"), \
                patch.object(control.TursoClient, "from_env", return_value=object()), \
                patch.object(documents, "initialize"), patch.object(storage_sync, "ingest", return_value=([], [{"code": "FAILED"}])), \
                patch.object(primary_store, "activate_primary") as activate:
            with self.assertRaises(control.ControlError) as caught:
                control.cutover(self.chosen, REVISION, {})
        self.assertEqual(caught.exception.code, "FINAL_IMPORT_FAILED")
        activate.assert_not_called()
        self.assertTrue(all(self.github.variable(repo, control.PAUSE_FLAG) == "1" for repo in control.RUNTIMES))

    def test_complete_activation_sets_every_primary_flag_before_first_resume(self):
        hashes = self.github.deployment()
        with patch.object(control, "drain"), patch.object(control.TursoClient, "from_env", return_value=object()), \
                patch.object(documents, "initialize"), patch.object(storage_sync, "ingest", return_value=([], [])), \
                patch.object(storage_legacy_evidence, "migrate_legacy_evidence"), \
                patch.object(primary_store, "activate_primary"):
            report = control.cutover(self.chosen, REVISION, hashes)
        self.assertEqual(report["status"], "passed")
        mutations = self.github.writes
        first_resume = next(i for i, (_, _, name, value) in enumerate(mutations) if name == control.PAUSE_FLAG and value == "0")
        activated = {repo for _, repo, name, value in mutations[:first_resume] if name == control.PRIMARY_FLAG and value == "1"}
        self.assertEqual(activated, set(control.RUNTIMES))

    def test_legacy_evidence_runs_after_final_import_and_failure_prevents_activation(self):
        order = []
        def ingest(database):
            order.append("ingest")
            return [], []
        def evidence(database, github):
            self.assertIs(github, self.github)
            order.append("evidence")
            raise StorageError("LEGACY_ADMISSION_EVIDENCE_MISMATCH", "safe")
        with patch.object(control, "validate_deployments"), patch.object(control, "drain"), \
                patch.object(control.TursoClient, "from_env", return_value=object()), \
                patch.object(documents, "initialize"), patch.object(storage_sync, "ingest", side_effect=ingest), \
                patch.object(storage_legacy_evidence, "migrate_legacy_evidence", side_effect=evidence), \
                patch.object(primary_store, "activate_primary") as activate:
            with self.assertRaises(StorageError):
                control.cutover(self.chosen, REVISION, {})
        self.assertEqual(order, ["ingest", "evidence"])
        activate.assert_not_called()
        self.assertTrue(all(self.github.variable(repo, control.PAUSE_FLAG) == "1" for repo in control.RUNTIMES))
        self.assertTrue(all(self.github.variable(repo, control.PRIMARY_FLAG) is None for repo in control.RUNTIMES))

    def test_resume_refuses_partial_authority_without_changing_runtime_flags(self):
        states = iter([True, False, False, False, False, False, False])
        with patch.object(control.TursoClient, "from_env", return_value=object()), patch.object(documents, "initialize"), \
                patch.object(primary_store, "primary_status", side_effect=lambda *_: {"active": next(states)}):
            with self.assertRaises(control.ControlError) as caught:
                control.resume(self.chosen)
        self.assertEqual(caught.exception.code, "PARTIAL_AUTHORITY_REQUIRES_CUTOVER_RETRY")
        self.assertEqual(self.github.writes, [])

    def test_http_error_discards_provider_body_url_token_and_reason(self):
        marker = "sensitive-test-token"
        error = HTTPError("https://provider.invalid/" + marker, 403, marker, {}, io.BytesIO(marker.encode()))
        with patch.object(control, "build_opener") as build:
            build.return_value.open.side_effect = error
            client = control.GitHub(marker)
            with self.assertRaises(control.ControlError) as caught:
                client.call("GET", control.RUNTIMES[0], "/actions/secrets/public-key")
        self.assertEqual(caught.exception.status, 403)
        self.assertNotIn(marker, str(caught.exception))
        self.assertNotIn("provider.invalid", str(caught.exception))

    def test_main_reports_safe_error_codes_without_exception_text_or_credentials(self):
        marker = "never-output-private-token-or-url"
        errors = [StorageError("NETWORK_ERROR", marker), RuntimeError(marker),
                  control.ControlError("GITHUB_PERMISSION_OR_REQUEST_FAILED", repository=control.RUNTIMES[-1], status=403)]
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            summary = Path(temporary) / "summary.md"
            for error in errors:
                output = io.StringIO()
                with patch.object(control, "clients", side_effect=error), \
                        patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary), "TURSO_AUTH_TOKEN": marker}), \
                        redirect_stdout(output):
                    result = control.main(["--mode", "prepare", "--report", str(report)])
                self.assertEqual(result, 1)
                self.assertEqual(json.loads(report.read_text())["status"], "failed")
                self.assertNotIn(marker, output.getvalue() + report.read_text() + summary.read_text())


if __name__ == "__main__":
    unittest.main()
