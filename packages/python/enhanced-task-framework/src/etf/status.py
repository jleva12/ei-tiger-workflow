"""Status enums and the ``ExitStatus`` value object.

``BatchStatus`` answers *how a run ended*; ``ExitStatus`` answers *what to do next*
(it drives conditional flow between steps), mirroring Spring Batch. Statuses are stored
as their string values so the persisted representation is stable and human-readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar


class BatchStatus(str, Enum):
    """Lifecycle status for instances, runs, and steps.

    The companion :data:`_SEVERITY` map defines an ordering used to compute a run's
    aggregate status as the *most severe* of its step statuses (Spring Batch semantics).
    """

    PENDING = "PENDING"  # created, not yet started
    RUNNING = "RUNNING"  # actively executing
    PAUSING = "PAUSING"  # pause requested, winding down to a checkpoint
    PAUSED = "PAUSED"  # checkpointed, worker released (generic pause)
    AWAITING_VALIDATION = "AWAITING_VALIDATION"  # paused specifically for human-in-the-loop
    STOPPING = "STOPPING"  # cooperative stop requested
    STOPPED = "STOPPED"  # halted; restartable
    COMPLETED = "COMPLETED"  # finished successfully (terminal)
    FAILED = "FAILED"  # finished with error; restartable
    ABANDONED = "ABANDONED"  # retired; terminal and NOT restartable
    UNKNOWN = "UNKNOWN"  # indeterminate (severity ceiling)

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_restartable(self) -> bool:
        return self in _RESTARTABLE

    @property
    def is_running(self) -> bool:
        return self is BatchStatus.RUNNING

    @property
    def is_paused(self) -> bool:
        return self in {BatchStatus.PAUSED, BatchStatus.AWAITING_VALIDATION}

    @classmethod
    def most_severe(cls, statuses: "list[BatchStatus]") -> "BatchStatus":
        """Aggregate status = the most severe of the given statuses."""
        if not statuses:
            return cls.PENDING
        return max(statuses, key=lambda s: _SEVERITY[s])


# Higher value == more severe. Used purely for aggregation, not for flow control.
_SEVERITY: dict[BatchStatus, int] = {
    BatchStatus.COMPLETED: 0,
    BatchStatus.PENDING: 1,
    BatchStatus.RUNNING: 3,
    BatchStatus.PAUSING: 4,
    BatchStatus.PAUSED: 5,
    BatchStatus.AWAITING_VALIDATION: 6,
    BatchStatus.STOPPING: 7,
    BatchStatus.STOPPED: 8,
    BatchStatus.FAILED: 9,
    BatchStatus.ABANDONED: 10,
    BatchStatus.UNKNOWN: 11,
}

_TERMINAL = {BatchStatus.COMPLETED, BatchStatus.ABANDONED}
_RESTARTABLE = {BatchStatus.FAILED, BatchStatus.STOPPED}


@dataclass(frozen=True, slots=True)
class ExitStatus:
    """A ``(code, description)`` pair that drives conditional flow between steps.

    Distinct from :class:`BatchStatus`: a step may finish with ``BatchStatus.COMPLETED``
    but an ``ExitStatus`` code of ``"VALIDATION_REQUIRED"`` or ``"RECONCILE"`` that the
    job's flow uses to choose the next step. Codes are free-form strings; the constants
    below are the conventional ones.
    """

    code: str
    description: str = ""
    exit_attributes: dict[str, str] = field(default_factory=dict)

    UNKNOWN: ClassVar["ExitStatus"]
    EXECUTING: ClassVar["ExitStatus"]
    COMPLETED: ClassVar["ExitStatus"]
    FAILED: ClassVar["ExitStatus"]
    STOPPED: ClassVar["ExitStatus"]
    PAUSED: ClassVar["ExitStatus"]
    VALIDATION_REQUIRED: ClassVar["ExitStatus"]

    def with_description(self, description: str) -> "ExitStatus":
        return ExitStatus(self.code, description, dict(self.exit_attributes))

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


# Conventional exit codes.
ExitStatus.UNKNOWN = ExitStatus("UNKNOWN")  # type: ignore[attr-defined]
ExitStatus.EXECUTING = ExitStatus("EXECUTING")  # type: ignore[attr-defined]
ExitStatus.COMPLETED = ExitStatus("COMPLETED")  # type: ignore[attr-defined]
ExitStatus.FAILED = ExitStatus("FAILED")  # type: ignore[attr-defined]
ExitStatus.STOPPED = ExitStatus("STOPPED")  # type: ignore[attr-defined]
ExitStatus.PAUSED = ExitStatus("PAUSED")  # type: ignore[attr-defined]
ExitStatus.VALIDATION_REQUIRED = ExitStatus("VALIDATION_REQUIRED")  # type: ignore[attr-defined]


class AuditEventType(str, Enum):
    """The catalogue of auditable events. See ``docs/DESIGN.md`` §7."""

    RUN_CREATED = "RUN_CREATED"
    RUN_STARTED = "RUN_STARTED"
    STATUS_CHANGED = "STATUS_CHANGED"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    RETRY_ATTEMPT = "RETRY_ATTEMPT"
    ITEM_SKIPPED = "ITEM_SKIPPED"
    CHECKPOINT_SAVED = "CHECKPOINT_SAVED"
    VALIDATION_REQUESTED = "VALIDATION_REQUESTED"
    VALIDATION_DECISION = "VALIDATION_DECISION"
    PAUSED = "PAUSED"
    RESUMED = "RESUMED"
    RESTARTED = "RESTARTED"
    PARAMETERS_OVERRIDDEN = "PARAMETERS_OVERRIDDEN"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RECOVERED = "RECOVERED"  # orphaned run reclaimed by the crash-recovery sweep
    IDEMPOTENT_SKIP = "IDEMPOTENT_SKIP"
    ANNOTATION = "ANNOTATION"  # free-form note emitted by a step via ctx.emit_audit


class ActorKind(str, Enum):
    SYSTEM = "SYSTEM"  # the framework / automated transition
    HUMAN = "HUMAN"  # a person (HITL decisions)
    SERVICE = "SERVICE"  # another service / ingress


class ValidationStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ValidationDecisionType(str, Enum):
    APPROVE = "APPROVE"  # proceed as-is
    REJECT = "REJECT"  # stop/fail the run
    RETRY = "RETRY"  # discard the gate and re-execute the paused step; it pauses afresh
    OVERRIDE = "OVERRIDE"  # proceed, applying parameter_overrides
