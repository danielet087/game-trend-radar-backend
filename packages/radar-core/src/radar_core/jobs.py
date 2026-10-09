"""Explicit outcomes for resumable collection and publication jobs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class JobStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    COOLING_DOWN = "cooling_down"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class JobResult:
    """A job outcome that does not infer success from the process exit code.

    COMPLETE represents verified collection coverage and durable job state.
    A publication-dependent job becomes successful only after publication is
    confirmed. SKIPPED reports this attempt; it never invents a prior success.
    """

    job: str
    status: JobStatus
    reason: str = ""
    collection_complete: bool = False
    state_persisted: bool = False
    published: bool = False
    requires_publication: bool = False
    target_slot: str | None = None
    input_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.job, str):
            raise TypeError("job must be a string")
        if not self.job.strip():
            raise ValueError("job must not be empty")
        if not isinstance(self.status, JobStatus):
            raise TypeError("status must be a JobStatus")
        if not isinstance(self.reason, str):
            raise TypeError("reason must be a string")
        for field in ("collection_complete", "state_persisted", "published",
                      "requires_publication"):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be a boolean")
        for field in ("target_slot", "input_revision"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field} must be a string or None")
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{field} must not be empty")
        if self.status is JobStatus.COMPLETE and not self.collection_complete:
            raise ValueError("complete requires verified collection coverage")
        if self.status is JobStatus.COMPLETE and not self.state_persisted:
            raise ValueError("complete requires persisted state")

    @property
    def successful(self) -> bool:
        """Whether this attempt qualifies as a durable, verified success."""
        return (
            self.status is JobStatus.COMPLETE
            and self.collection_complete
            and self.state_persisted
            and (not self.requires_publication or self.published)
        )

    def to_dict(self) -> dict[str, str | bool | int | None]:
        """Serialize the explicit, versioned result without dropping evidence."""
        return {
            "schema_version": 1,
            "job": self.job,
            "status": self.status.value,
            "reason": self.reason,
            "collection_complete": self.collection_complete,
            "state_persisted": self.state_persisted,
            "published": self.published,
            "requires_publication": self.requires_publication,
            "target_slot": self.target_slot,
            "input_revision": self.input_revision,
            "successful": self.successful,
        }

    @classmethod
    def from_dict(cls, value: object) -> JobResult:
        """Read a versioned receipt and validate its evidence, not its claim.

        All completion and publication flags must be explicitly present.
        ``successful`` is recomputed; a conflicting serialized claim is invalid.
        Consumers must treat an invalid typed receipt as a failed validation,
        rather than falling back to an older process-success heuristic.
        """
        if not isinstance(value, dict):
            raise TypeError("job result must be a dictionary")
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise ValueError("unsupported job result schema_version")
        required = {
            "schema_version", "job", "status", "reason", "collection_complete",
            "state_persisted", "published", "requires_publication",
        }
        missing = required - value.keys()
        if missing:
            raise ValueError(f"missing job result fields: {', '.join(sorted(missing))}")
        allowed = required | {"target_slot", "input_revision", "successful"}
        unknown = value.keys() - allowed
        if unknown:
            raise ValueError(f"unknown job result fields: {', '.join(sorted(unknown))}")
        if type(value["status"]) is not str:
            raise TypeError("serialized status must be a string")
        result = cls(
            job=value["job"], status=JobStatus(value["status"]), reason=value["reason"],
            collection_complete=value["collection_complete"],
            state_persisted=value["state_persisted"], published=value["published"],
            requires_publication=value["requires_publication"],
            target_slot=value.get("target_slot"), input_revision=value.get("input_revision"),
        )
        if "successful" in value:
            if type(value["successful"]) is not bool:
                raise TypeError("serialized successful must be a boolean")
            if value["successful"] != result.successful:
                raise ValueError("serialized successful disagrees with job evidence")
        return result

