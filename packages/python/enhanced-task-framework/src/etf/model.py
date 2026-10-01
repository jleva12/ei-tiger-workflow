"""Storage-agnostic domain model.

These are plain dataclasses with no persistence dependency. ``StateStore``
implementations map them to/from their own representations (the default Beanie store
maps them to MongoDB documents). Keeping the core model free of any ODM is what lets the
persistence layer be swapped wholesale.
"""

from __future__ import annotations

import hashlib
import json
import copy
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from .status import (
    ActorKind,
    BatchStatus,
    ExitStatus,
    ValidationDecisionType,
    ValidationStatus,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _exception_text(exc: BaseException) -> str:
    """``str(exc)``, but never raising: a failure must be recordable even when the
    exception's own ``__str__`` is broken."""
    try:
        return str(exc)
    except Exception:
        return f"<unprintable {type(exc).__name__}>"


class FrozenDict(dict[str, Any]):
    """A deepcopy-friendly immutable mapping used by persisted value objects.

    ``MappingProxyType`` is not deepcopy/pickle friendly, while a frozen dataclass alone
    does not protect the dictionaries stored inside it.  This small dict subclass keeps
    the public mapping behavior callers expect and rejects every mutation path.
    """

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        dict.__init__(
            self,
            {
                key: self._freeze(value)
                for key, value in dict(values or {}).items()
            },
        )

    @classmethod
    def _freeze(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return cls(value)
        if isinstance(value, (list, tuple)):
            return tuple(cls._freeze(item) for item in value)
        return copy.deepcopy(value)

    @staticmethod
    def _immutable(*args: object, **kwargs: object) -> None:
        raise TypeError("mapping is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable  # type: ignore[assignment]
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable  # type: ignore[assignment]

    def __copy__(self) -> "FrozenDict":
        return FrozenDict(self)

    def __deepcopy__(self, memo: dict[int, object]) -> "FrozenDict":
        return FrozenDict(copy.deepcopy(dict(self), memo))


# --------------------------------------------------------------------------- #
# Actors & parameters
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Actor:
    """Who performed an action — threaded through audit events and decisions."""

    kind: ActorKind
    id: str
    display_name: str = ""
    roles: frozenset[str] = field(default_factory=frozenset)

    @staticmethod
    def system(id: str = "etf", roles: frozenset[str] | set[str] = frozenset()) -> "Actor":
        return Actor(ActorKind.SYSTEM, id, "system", frozenset(roles))

    @staticmethod
    def human(
        id: str, display_name: str = "", roles: frozenset[str] | set[str] = frozenset()
    ) -> "Actor":
        return Actor(ActorKind.HUMAN, id, display_name or id, frozenset(roles))

    @staticmethod
    def service(
        id: str, display_name: str = "", roles: frozenset[str] | set[str] = frozenset()
    ) -> "Actor":
        return Actor(ActorKind.SERVICE, id, display_name or id, frozenset(roles))


@dataclass(frozen=True, slots=True)
class JobParameters:
    """Run parameters split into identity-bearing and operational partitions.

    ``identifying`` participate in the :class:`JobInstance` identity hash (and are
    therefore **immutable across restarts**). ``non_identifying`` are operational knobs
    that a user *may* change when restarting after a failure (e.g. ``batch_size``).
    """

    identifying: dict[str, object] = field(default_factory=dict)
    non_identifying: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        overlap = set(self.identifying) & set(self.non_identifying)
        if overlap:
            raise ValueError(
                f"parameters cannot be both identifying and non-identifying: {sorted(overlap)!r}"
            )
        object.__setattr__(self, "identifying", FrozenDict(self.identifying))
        object.__setattr__(self, "non_identifying", FrozenDict(self.non_identifying))

    def get(self, key: str, default: object = None) -> object:
        if key in self.identifying:
            return self.identifying[key]
        return self.non_identifying.get(key, default)

    def merged(self) -> dict[str, object]:
        return {**self.non_identifying, **self.identifying}

    def with_overrides(self, non_identifying: dict[str, object]) -> "JobParameters":
        """Return a copy with non-identifying parameters overridden (identity preserved)."""
        return replace(self, non_identifying={**self.non_identifying, **non_identifying})

    def identity_payload(self) -> str:
        """Canonical JSON of identifying params, used to compute the identity hash."""
        return json.dumps(
            self.identifying,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


# --------------------------------------------------------------------------- #
# Failures & execution context snapshots
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FailureRecord:
    """Structured capture of an exception for audit and forensics."""

    exception_type: str
    message: str
    stack_trace: str = ""
    cause_chain: list[str] = field(default_factory=list)
    step_name: str | None = None
    attempt: int = 0
    retryable: bool = False
    fatal: bool = False
    occurred_at: datetime = field(default_factory=_utcnow)
    attributes: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        step_name: str | None = None,
        attempt: int = 0,
        retryable: bool = False,
        fatal: bool = False,
    ) -> "FailureRecord":
        import asyncio
        import traceback

        chain: list[str] = []
        cause: BaseException | None = exc.__cause__ or exc.__context__
        seen: set[int] = {id(exc)}
        while cause is not None and id(cause) not in seen:
            chain.append(f"{type(cause).__name__}: {_exception_text(cause)}")
            seen.add(id(cause))
            cause = cause.__cause__ or cause.__context__
        message = _exception_text(exc)
        if not message and isinstance(exc, asyncio.CancelledError):
            message = "cancelled with no reason given (host timeout, shutdown or task cancellation)"
        return cls(
            exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            message=message,
            stack_trace="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            cause_chain=chain,
            step_name=step_name,
            attempt=attempt,
            retryable=retryable,
            fatal=fatal,
        )


def check_labels(labels: dict[str, str] | None) -> dict[str, str]:
    """A copy of ``labels``, checked: string keys and values, keys without ``=``."""
    checked: dict[str, str] = {}
    for key, value in (labels or {}).items():
        if not isinstance(key, str) or not isinstance(value, str) or not key or "=" in key:
            raise ValueError(f"labels are non-empty string keys without '=' and string values, got {key!r}: {value!r}")
        checked[key] = value
    return checked


# --------------------------------------------------------------------------- #
# Core entities
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class JobInstance:
    """The idempotent unit: one per (job_name, identity_hash).

    ``labels`` are set once, from the launch that created the instance, and never
    change: short string pairs a host filters instances by (a tenant, a task type), with
    :meth:`~etf.store.StateStore.query_instances`. They are not part of its identity.
    """

    id: str
    job_name: str
    identity_hash: str
    definition_version: int = 1
    identifying_parameters: dict[str, object] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    status: BatchStatus = BatchStatus.PENDING
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    version: int = 0


@dataclass(slots=True)
class StepRun:
    """One execution of a step within a :class:`JobRun`."""

    id: str
    run_id: str
    instance_id: str
    step_name: str
    status: BatchStatus = BatchStatus.PENDING
    exit_status: ExitStatus = field(default_factory=lambda: ExitStatus.EXECUTING)  # type: ignore[attr-defined]
    attempt: int = 1
    # Per-step durable checkpoint state (resume point for this step).
    execution_context: object = field(default_factory=dict)
    read_count: int = 0
    write_count: int = 0
    skip_count: int = 0
    commit_count: int = 0
    attributes: dict[str, object] = field(default_factory=dict)
    failures: list[FailureRecord] = field(default_factory=list)
    start_time: datetime | None = None
    end_time: datetime | None = None
    updated_at: datetime = field(default_factory=_utcnow)
    version: int = 0


@dataclass(slots=True)
class JobRun:
    """One attempt to execute a :class:`JobInstance` (Spring Batch 'JobExecution')."""

    id: str
    instance_id: str
    job_name: str
    definition_version: int = 1
    parameters: JobParameters = field(default_factory=JobParameters)
    status: BatchStatus = BatchStatus.PENDING
    exit_status: ExitStatus = field(default_factory=lambda: ExitStatus.EXECUTING)  # type: ignore[attr-defined]
    attempt: int = 1
    #: The flow shape (``JobDefinition.fingerprint``) the run was created with.
    definition_fingerprint: str | None = None
    restart_of: str | None = None  # parent run id, if this is a restart
    current_step: str | None = None
    # Run-level durable checkpoint state (shared across steps).
    execution_context: object = field(default_factory=dict)
    failures: list[FailureRecord] = field(default_factory=list)
    open_validation_id: str | None = None  # set while AWAITING_VALIDATION
    requested_by: Actor | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    idempotency_key: str | None = None
    create_time: datetime = field(default_factory=_utcnow)
    start_time: datetime | None = None
    end_time: datetime | None = None
    last_updated: datetime = field(default_factory=_utcnow)
    version: int = 0  # optimistic concurrency token


# --------------------------------------------------------------------------- #
# Human-in-the-loop validation
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ValidationRequest:
    """An open human-in-the-loop gate. Created when a step pauses for validation."""

    id: str
    run_id: str
    instance_id: str
    step_name: str
    reason: str
    payload: dict[str, object] = field(default_factory=dict)  # data for the human to review
    decision_schema: dict[str, object] = field(default_factory=dict)  # expected decision shape
    required_role: str | None = None
    status: ValidationStatus = ValidationStatus.PENDING
    created_at: datetime = field(default_factory=_utcnow)
    sla_deadline: datetime | None = None
    decided_at: datetime | None = None
    decision_type: ValidationDecisionType | None = None
    decision_actor_id: str | None = None
    decision_comment: str = ""
    decision_overrides: dict[str, object] = field(default_factory=dict)
    version: int = 0


@dataclass(slots=True)
class ValidationDecision:
    """A human's resolution of a :class:`ValidationRequest`."""

    decision: ValidationDecisionType
    actor: Actor
    comment: str = ""
    parameter_overrides: dict[str, object] = field(default_factory=dict)
    request_id: str | None = None
    decided_at: datetime = field(default_factory=_utcnow)

    @staticmethod
    def approve(actor: Actor, comment: str = "") -> "ValidationDecision":
        return ValidationDecision(ValidationDecisionType.APPROVE, actor, comment)

    @staticmethod
    def reject(actor: Actor, comment: str = "") -> "ValidationDecision":
        return ValidationDecision(ValidationDecisionType.REJECT, actor, comment)

    @staticmethod
    def override(actor: Actor, parameter_overrides: dict[str, object], comment: str = "") -> "ValidationDecision":
        return ValidationDecision(
            ValidationDecisionType.OVERRIDE, actor, comment, dict(parameter_overrides)
        )


@dataclass(slots=True)
class ValidationOutcome:
    """What a resumed step receives back from ``ctx.request_validation(...)``.

    On the *first* call the step pauses (raises ``PauseForValidation``). After a human
    decision arrives and the run resumes, the step re-executes and the same
    ``request_validation`` call returns this object instead of pausing again — letting
    the step branch on the decision without bespoke re-entry bookkeeping.

    A step may contain several gates. Each gate is assigned a deterministic ordinal
    (0, 1, …) in call order per execution, and decided outcomes are persisted in the
    step context keyed by that ordinal — so on re-execution every gate receives *its
    own* prior decision and only the first undecided gate pauses.
    """

    decision: ValidationDecisionType
    approved: bool
    comment: str = ""
    overrides: dict[str, object] = field(default_factory=dict)
    actor_id: str | None = None
    request_id: str | None = None

    # Reserved step-context keys (owned by the runner/operator, not by steps):
    # decided outcomes keyed by gate ordinal, and the gate currently awaiting a decision.
    OUTCOMES_KEY = "__etf_validation_outcomes__"
    PENDING_GATE_KEY = "__etf_pending_validation__"

    @staticmethod
    def to_context_blob(decision: "ValidationDecision") -> dict[str, object]:
        """Serialize a decision into the step-context blob consumed on re-execution."""
        return {
            "decision": decision.decision.value,
            "comment": decision.comment,
            "overrides": decision.parameter_overrides,
            "actor_id": decision.actor.id,
            "request_id": decision.request_id,
        }

    @staticmethod
    def from_context_blob(blob: dict[str, object]) -> "ValidationOutcome":
        decision = ValidationDecisionType(str(blob["decision"]))
        return ValidationOutcome(
            decision=decision,
            approved=decision is not ValidationDecisionType.REJECT,
            comment=str(blob.get("comment", "")),
            overrides=dict(blob.get("overrides", {}) or {}),  # type: ignore[arg-type]
            actor_id=blob.get("actor_id"),  # type: ignore[arg-type]
            request_id=blob.get("request_id"),  # type: ignore[arg-type]
        )


def compute_identity_hash(job_name: str, parameters: JobParameters) -> str:
    """Default identity hash: stable SHA-256 over job name + canonical identifying params.

    Overridable via :class:`etf.policies.JobInstanceResolver`.
    """
    payload = f"{job_name}\x1f{parameters.identity_payload()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
