"""Offline release ownership, completion, interruption and HTTP safety checks."""
from copy import deepcopy
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs

from radar_backend.storage.backup_release import (
    ASSET_NAMES, MARKER, BackupError, BackupReleaseClient, NoRedirect,
    daily_backup_exists, publish_daily_backup,
)

REPOSITORY = "danielet087/game-trend-radar-backend"
DAY = "2026-10-15"
TOKEN = "secret-never-expose-value"
TAG = "radar-storage-v1-" + DAY


def http_error(status):
    return HTTPError("https://private-host/" + TOKEN, status, TOKEN, {}, io.BytesIO(TOKEN.encode()))


class Response(io.BytesIO):
    def __init__(self, value, status=200):
        super().__init__(json.dumps(value).encode() if value is not None else b"")
        self.status = status


class GitHub:
    """Stateful API: data survives a response lost after mutation commits."""
    def __init__(self):
        self.release = None
        self.requests = []
        self.assets = {}
        self.next_asset_id = 10
        self.fail_upload = None
        self.fail_create_once = False
        self.fail_upload_once = None
        self.create_count = 0

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        parsed = urlsplit(request.full_url)
        method, path = request.get_method(), parsed.path
        if method == "GET":
            if self.release is None:
                raise http_error(404)
            return Response(deepcopy(self.release))
        if method == "POST" and path.endswith("/releases"):
            if self.release is not None:
                raise http_error(422)
            payload = json.loads(request.data)
            self.create_count += 1
            self.release = {"id": 1, "tag_name": payload["tag_name"], "body": payload["body"],
                            "draft": payload["draft"], "prerelease": payload["prerelease"], "assets": []}
            if self.fail_create_once:
                self.fail_create_once = False
                raise URLError(TOKEN)
            return Response(self.release, 201)
        if method == "POST" and path.endswith("/assets"):
            name = parse_qs(parsed.query)["name"][0]
            if name == self.fail_upload:
                raise http_error(503)
            if any(a["name"] == name for a in self.release["assets"]):
                raise http_error(422)
            asset = {"id": self.next_asset_id, "name": name, "state": "uploaded", "size": len(request.data),
                     "digest": "sha256:" + hashlib.sha256(request.data).hexdigest()}
            self.next_asset_id += 1
            self.release["assets"].append(asset)
            self.assets[asset["id"]] = request.data
            if name == self.fail_upload_once:
                self.fail_upload_once = None
                raise URLError(TOKEN)
            return Response(asset, 201)
        if method == "DELETE":
            asset_id = int(path.rsplit("/", 1)[1])
            self.release["assets"] = [a for a in self.release["assets"] if a["id"] != asset_id]
            self.assets.pop(asset_id, None)
            return Response(None, 204)
        if method == "PATCH":
            self.release.update(json.loads(request.data))
            return Response(self.release)
        raise AssertionError("unexpected request")


class Faults:
    def __init__(self, faults):
        self.faults = iter(faults)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        value = next(self.faults)
        if isinstance(value, Exception):
            raise value
        return value


class BackupReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.files = self.make_files()
        self.api = GitHub()

    def make_files(self, content="SELECT '遊戲雷達';"):
        (self.folder / "radar.sql.gz").write_bytes(gzip.compress(content.encode(), mtime=0))
        (self.folder / "manifest.json").write_text('{"schema_version":1,"day":"' + DAY + '"}\n')
        sums = "".join(hashlib.sha256((self.folder / name).read_bytes()).hexdigest() + "  " + name + "\n"
                       for name in ASSET_NAMES[:2])
        (self.folder / "SHA256SUMS").write_text(sums)
        return [self.folder / name for name in ASSET_NAMES]

    def client(self, api=None, **kwargs):
        return BackupReleaseClient(REPOSITORY, DAY, TOKEN, opener=api or self.api, sleep=lambda _: None, **kwargs)

    def methods(self):
        return [request.get_method() for request, _ in self.api.requests]

    def assert_safe(self, error):
        text = str(error)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("private-host", text)

    def test_publish_then_skip_checks_complete_digests_and_keeps_version(self):
        result = self.client().publish(self.files)
        self.assertEqual(result["status"], "published")
        uploaded = [parse_qs(urlsplit(r.full_url).query)["name"][0] for r, _ in self.api.requests
                    if urlsplit(r.full_url).hostname == "uploads.github.com"]
        self.assertEqual(uploaded, list(ASSET_NAMES))
        release_before = deepcopy(self.api.release)
        self.api.requests.clear()
        self.assertTrue(self.client().exists())
        # A completed daily release wins even if this run has newer data.
        self.make_files("SELECT 'newer';")
        self.assertEqual(self.client().publish(self.files)["status"], "skipped")
        self.assertTrue(all(m == "GET" for m in self.methods()))
        self.assertEqual(self.api.release, release_before)
        for request, timeout in self.api.requests:
            self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
            self.assertLessEqual(timeout, 30)

    def test_public_functions_support_injected_opener(self):
        self.assertFalse(daily_backup_exists(REPOSITORY, DAY, TOKEN, opener=self.api))
        result = publish_daily_backup(REPOSITORY, DAY, TOKEN, self.files, opener=self.api)
        self.assertEqual(result["tag"], TAG)
        self.assertTrue(daily_backup_exists(REPOSITORY, DAY, TOKEN, opener=self.api))

    def test_partial_upload_is_not_complete_and_next_run_resumes(self):
        self.api.fail_upload = "manifest.json"
        with self.assertRaises(BackupError):
            self.client().publish(self.files)
        self.assertFalse(self.client().exists())
        sql_asset_id = self.api.release["assets"][0]["id"]
        self.api.fail_upload = None
        self.api.requests.clear()
        self.assertEqual(self.client().publish(self.files)["status"], "published")
        self.assertEqual(self.api.release["assets"][0]["id"], sql_asset_id)
        self.assertNotIn("DELETE", self.methods())
        self.assertEqual(self.api.create_count, 1)

    def test_owned_partial_with_different_or_starter_assets_replaces_only_partial_assets(self):
        self.api.fail_upload = "manifest.json"
        with self.assertRaises(BackupError):
            self.client().publish(self.files)
        old = self.api.release["assets"][0]["id"]
        self.make_files("SELECT 'changed';")
        self.api.fail_upload = None
        self.api.requests.clear()
        self.client().publish(self.files)
        deletions = [r.full_url for r, _ in self.api.requests if r.get_method() == "DELETE"]
        self.assertEqual(deletions, [f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{old}"])
        self.assertTrue(self.client().exists())

    def test_lost_create_and_upload_response_reconciles_without_duplicate_backup(self):
        self.api.fail_create_once = True
        self.api.fail_upload_once = "radar.sql.gz"
        self.assertEqual(self.client().publish(self.files)["status"], "published")
        self.assertEqual(self.api.create_count, 1)
        self.assertEqual(len(self.api.release["assets"]), 3)
        self.assertNotIn("DELETE", self.methods())

    def test_three_asset_names_without_completion_receipt_never_skip(self):
        self.client().publish(self.files)
        self.api.release["body"] = MARKER
        self.assertFalse(self.client().exists())
        self.api.requests.clear()
        self.assertEqual(self.client().publish(self.files)["status"], "published")
        self.assertEqual(self.methods().count("PATCH"), 1)
        self.assertNotIn("POST", self.methods())

    def test_same_tag_without_exact_ownership_marker_is_never_modified(self):
        self.client().publish(self.files)
        self.api.release["body"] = "Someone else's backup " + MARKER
        self.api.requests.clear()
        for operation in (lambda: self.client().exists(), lambda: self.client().publish(self.files)):
            with self.assertRaises(BackupError) as failure:
                operation()
            self.assertEqual(failure.exception.code, "BACKUP_RELEASE_NOT_OWNED")
        self.assertTrue(all(m == "GET" for m in self.methods()))

    def test_corrupt_completed_digest_checksums_or_missing_asset_never_overwritten(self):
        self.client().publish(self.files)
        valid = deepcopy(self.api.release)
        for damage in ("digest", "checksums", "missing", "missing_digest"):
            self.api.release = deepcopy(valid)
            if damage == "digest": self.api.release["assets"][0]["digest"] = "sha256:" + "0" * 64
            elif damage == "missing": self.api.release["assets"].pop()
            elif damage == "missing_digest": self.api.release["assets"][0].pop("digest")
            else:
                receipt = json.loads(self.api.release["body"].split("\n", 1)[1])
                receipt["checksums"] = receipt["checksums"].replace("radar.sql.gz", "other.sql.gz")
                self.api.release["body"] = MARKER + "\n" + json.dumps(receipt)
            self.api.requests.clear()
            with self.subTest(damage=damage), self.assertRaises(BackupError):
                self.client().publish(self.files)
            self.assertTrue(all(m == "GET" for m in self.methods()))

    def test_unexpected_asset_prevents_any_deletion(self):
        self.client().publish(self.files)
        self.api.release["body"] = MARKER
        self.api.release["assets"].append({"id": 999, "name": "unrelated-file.txt"})
        self.api.requests.clear()
        with self.assertRaises(BackupError): self.client().publish(self.files)
        self.assertEqual(self.methods(), ["GET"])

    def test_local_checksums_must_match_before_creating_release(self):
        (self.folder / "radar.sql.gz").write_bytes(b"changed after checksum")
        with self.assertRaises(BackupError) as failure:
            self.client().publish(self.files)
        self.assertEqual(failure.exception.code, "BACKUP_CHECKSUM_INVALID")
        self.assertIsNone(self.api.release)
        self.assertEqual(self.methods(), ["GET"])

    def test_credentials_and_repository_are_validated_before_network(self):
        for repository, day, token in [("a/b?evil=1", DAY, TOKEN), ("https://evil.test", DAY, TOKEN),
                                      (REPOSITORY, "2026-02-30", TOKEN), (REPOSITORY, DAY, TOKEN + "\n"),
                                      (REPOSITORY, DAY, ""), (REPOSITORY, DAY, "x y")]:
            with self.subTest(repository=repository, day=day), self.assertRaises(BackupError):
                BackupReleaseClient(repository, day, token, opener=self.api)
        self.assertEqual(self.api.requests, [])

    def test_redirect_and_auth_failures_never_follow_or_echo_response(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, TOKEN, {}, "https://evil.test"))
        for status in (301, 302, 307, 308, 401, 403):
            api = Faults([http_error(status)])
            with self.subTest(status=status), self.assertRaises(BackupError) as failure:
                self.client(api).exists()
            self.assert_safe(failure.exception)
            self.assertEqual(len(api.requests), 1)
            self.assertEqual(urlsplit(api.requests[0][0].full_url).hostname, "api.github.com")

    def test_network_and_transient_http_failures_are_bounded_and_masked(self):
        for errors in ([URLError(TOKEN)] * 3, [http_error(503) for _ in range(3)]):
            api = Faults(errors)
            with self.assertRaises(BackupError) as failure: self.client(api).exists()
            self.assert_safe(failure.exception)
            self.assertEqual(len(api.requests), 3)
            self.assertTrue(all(0 < timeout <= 30 for _, timeout in api.requests))

    def test_all_requests_use_only_fixed_hosts_and_make_latest_false(self):
        self.client().publish(self.files)
        hosts = {urlsplit(r.full_url).hostname for r, _ in self.api.requests}
        self.assertEqual(hosts, {"api.github.com", "uploads.github.com"})
        for request, _ in self.api.requests:
            if request.get_method() in {"POST", "PATCH"} and urlsplit(request.full_url).hostname == "api.github.com":
                self.assertEqual(json.loads(request.data)["make_latest"], "false")

    def test_deadline_exhaustion_stops_before_request(self):
        ticks = iter([0, 301])
        with self.assertRaises(BackupError) as failure:
            self.client(clock=lambda: next(ticks)).exists()
        self.assertEqual(failure.exception.code, "BACKUP_TIME_BUDGET_EXCEEDED")
        self.assertEqual(self.api.requests, [])


if __name__ == "__main__":
    unittest.main()
