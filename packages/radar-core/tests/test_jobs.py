"""Offline job-result evidence tests; process success is insufficient."""

from dataclasses import FrozenInstanceError
import json
import unittest

from radar_core.jobs import JobResult, JobStatus


class JobResultTests(unittest.TestCase):
    def test_complete_requires_verified_coverage_and_durable_state(self):
        for complete, persisted in ((False, False), (False, True), (True, False)):
            with self.subTest(complete=complete, persisted=persisted):
                with self.assertRaises(ValueError):
                    JobResult("candidate", JobStatus.COMPLETE,
                              collection_complete=complete, state_persisted=persisted)

    def test_collector_without_publication_can_complete(self):
        result = JobResult("candidate", JobStatus.COMPLETE,
                           collection_complete=True, state_persisted=True)
        self.assertTrue(result.successful)
        self.assertFalse(result.published)

    def test_publication_failure_does_not_discard_collection_evidence(self):
        result = JobResult("growth", JobStatus.COMPLETE, reason="publication_failed",
                           collection_complete=True, state_persisted=True,
                           requires_publication=True)
        self.assertFalse(result.successful)
        self.assertTrue(result.collection_complete)
        self.assertTrue(result.state_persisted)
        published = JobResult("growth", JobStatus.COMPLETE,
                              collection_complete=True, state_persisted=True,
                              requires_publication=True, published=True)
        self.assertTrue(published.successful)

    def test_partial_cooling_skipped_and_failed_cannot_be_daily_success(self):
        for status in (JobStatus.PARTIAL, JobStatus.COOLING_DOWN,
                       JobStatus.SKIPPED, JobStatus.FAILED):
            with self.subTest(status=status):
                result = JobResult("candidate", status, reason="attempt_outcome",
                                   collection_complete=True, state_persisted=True,
                                   published=True, requires_publication=True)
                self.assertFalse(result.successful)
                self.assertFalse(result.to_dict()["successful"])

    def test_serialization_is_json_safe_and_keeps_resume_identifiers(self):
        result = JobResult("candidate", JobStatus.COOLING_DOWN,
                           reason="http_429", state_persisted=True,
                           target_slot="2026-10-08", input_revision="a" * 40)
        serialized = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(serialized, {
            "schema_version": 1, "job": "candidate", "status": "cooling_down",
            "reason": "http_429", "collection_complete": False,
            "state_persisted": True, "published": False,
            "requires_publication": False, "target_slot": "2026-10-08",
            "input_revision": "a" * 40, "successful": False,
        })

    def test_persisted_evidence_is_immutable(self):
        result = JobResult("candidate", JobStatus.PARTIAL)
        with self.assertRaises(FrozenInstanceError):
            result.collection_complete = True

    def test_boolean_flags_do_not_coerce_integers_or_strings(self):
        for name in ("collection_complete", "state_persisted", "published",
                     "requires_publication"):
            for value in (0, 1, "false", None):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(TypeError):
                        JobResult("candidate", JobStatus.PARTIAL, **{name: value})

    def test_status_must_be_an_explicit_enum(self):
        for status in ("complete", "partial", None, 1, True):
            with self.subTest(status=status):
                with self.assertRaises(TypeError):
                    JobResult("candidate", status)

    def test_serialization_round_trip_preserves_the_evidence(self):
        result = JobResult("growth", JobStatus.COMPLETE,
                           collection_complete=True, state_persisted=True,
                           requires_publication=True, published=True,
                           target_slot="2026-10-08", input_revision="a" * 40)
        self.assertEqual(JobResult.from_dict(json.loads(json.dumps(result.to_dict()))), result)

    def test_read_receipt_recomputes_success_and_rejects_forged_claim(self):
        result = JobResult("growth", JobStatus.COMPLETE,
                           collection_complete=True, state_persisted=True,
                           requires_publication=True, published=False)
        receipt = result.to_dict()
        receipt.pop("successful")
        self.assertFalse(JobResult.from_dict(receipt).successful)
        receipt["successful"] = True
        with self.assertRaises(ValueError):
            JobResult.from_dict(receipt)
        receipt["successful"] = 0
        with self.assertRaises(TypeError):
            JobResult.from_dict(receipt)

    def test_read_receipt_requires_all_flags_and_known_schema(self):
        original = JobResult("candidate", JobStatus.PARTIAL).to_dict()
        for field in ("job", "status", "reason", "collection_complete",
                      "state_persisted", "published", "requires_publication"):
            receipt = dict(original); receipt.pop(field)
            with self.subTest(missing=field):
                with self.assertRaises(ValueError):
                    JobResult.from_dict(receipt)
        for version in (True, "1", 0, 2, None):
            receipt = {**original, "schema_version": version}
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    JobResult.from_dict(receipt)
        for field in ("collection_complete", "state_persisted", "published",
                      "requires_publication"):
            receipt = {**original, field: 1}
            with self.subTest(invalid_flag=field):
                with self.assertRaises(TypeError):
                    JobResult.from_dict(receipt)

    def test_read_receipt_rejects_unknown_status_fields_and_incomplete_complete(self):
        receipt = JobResult("candidate", JobStatus.PARTIAL).to_dict()
        for mutation in ({"status": "unknown"}, {"status": "complete"},
                         {"unexpected_field": True}):
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    JobResult.from_dict({**receipt, **mutation})
        for value in (None, [], "complete"):
            with self.assertRaises(TypeError):
                JobResult.from_dict(value)

    def test_identifiers_and_reason_are_not_implicitly_coerced(self):
        for arguments, error in (({"job": " "}, ValueError),
                                 ({"job": 3}, TypeError),
                                 ({"reason": None}, TypeError),
                                 ({"target_slot": True}, TypeError),
                                 ({"target_slot": ""}, ValueError),
                                 ({"input_revision": 123}, TypeError),
                                 ({"input_revision": " "}, ValueError)):
            with self.subTest(arguments=arguments):
                values = {"job": "candidate", "status": JobStatus.PARTIAL, **arguments}
                with self.assertRaises(error):
                    JobResult(**values)


if __name__ == "__main__":
    unittest.main()
