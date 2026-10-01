"""Pluggable strategy contracts and their default implementations.

Each is a small ABC the integrator can replace: how instance identity is computed, when
to retry/skip a failure, whether a failed run may restart, plus injectable ``Clock`` and
``IdGenerator`` (the latter two keep the core deterministic and testable).
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .model import FailureRecord, JobInstance, JobParameters, JobRun, compute_identity_hash


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
class JobInstanceResolver(ABC):
    """Computes the identity hash that makes a :class:`~etf.model.JobInstance` unique."""

    @abstractmethod
    def resolve_identity(self, job_name: str, parameters: JobParameters) -> str:
        ...


class HashingInstanceResolver(JobInstanceResolver):
    """Default: stable SHA-256 over the job name + canonical identifying parameters."""

    def resolve_identity(self, job_name: str, parameters: JobParameters) -> str:
        return compute_identity_hash(job_name, parameters)


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry: bool
    delay: timedelta = timedelta(0)
    reason: str = ""


class RetryPolicy(ABC):
    """Decides whether a failed step attempt should be retried, and after what delay."""

    @abstractmethod
    def should_retry(self, failure: FailureRecord, attempt: int) -> RetryDecision:
        ...


class BackoffRetryPolicy(RetryPolicy):
    """Exponential backoff up to ``max_attempts``, gated on retryable exception types.

    ``retryable_types`` (by fully-qualified name) and/or the failure's ``retryable``
    flag determine eligibility. ``non_retryable_types`` always short-circuit to no-retry.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: timedelta = timedelta(seconds=1),
        max_delay: timedelta = timedelta(minutes=5),
        multiplier: float = 2.0,
        retryable_types: frozenset[str] = frozenset(),
        non_retryable_types: frozenset[str] = frozenset(),
    ) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.multiplier = multiplier
        self.retryable_types = retryable_types
        self.non_retryable_types = non_retryable_types

    def should_retry(self, failure: FailureRecord, attempt: int) -> RetryDecision:
        if failure.exception_type in self.non_retryable_types:
            return RetryDecision(False, reason="non-retryable exception type")
        if attempt >= self.max_attempts:
            return RetryDecision(False, reason="max attempts reached")
        eligible = (
            failure.retryable
            or not self.retryable_types
            or failure.exception_type in self.retryable_types
        )
        if not eligible:
            return RetryDecision(False, reason="exception not eligible for retry")
        secs = min(
            self.base_delay.total_seconds() * (self.multiplier ** (attempt - 1)),
            self.max_delay.total_seconds(),
        )
        return RetryDecision(True, timedelta(seconds=secs), reason="backoff retry")


# --------------------------------------------------------------------------- #
# Skip (Spring Batch fault tolerance)
# --------------------------------------------------------------------------- #
class SkipPolicy(ABC):
    """Decides whether a failure should be skipped rather than failing the step."""

    @abstractmethod
    def should_skip(self, failure: FailureRecord, skip_count: int) -> bool:
        ...


class NeverSkipPolicy(SkipPolicy):
    """Default: never skip."""

    def should_skip(self, failure: FailureRecord, skip_count: int) -> bool:
        return False


class LimitedSkipPolicy(SkipPolicy):
    """Skip up to ``skip_limit`` failures of the given (skippable) exception types."""

    def __init__(self, skip_limit: int, skippable_types: frozenset[str]) -> None:
        self.skip_limit = skip_limit
        self.skippable_types = skippable_types

    def should_skip(self, failure: FailureRecord, skip_count: int) -> bool:
        return skip_count < self.skip_limit and failure.exception_type in self.skippable_types


# --------------------------------------------------------------------------- #
# Restart
# --------------------------------------------------------------------------- #
class RestartPolicy(ABC):
    """Governs whether a failed/stopped instance may be restarted."""

    @abstractmethod
    def can_restart(self, instance: JobInstance, last_run: JobRun) -> bool:
        ...


class DefaultRestartPolicy(RestartPolicy):
    """Allow restart of FAILED/STOPPED runs up to an optional attempt ceiling."""

    def __init__(self, max_attempts: int | None = None) -> None:
        self.max_attempts = max_attempts

    def can_restart(self, instance: JobInstance, last_run: JobRun) -> bool:
        if not last_run.status.is_restartable:
            return False
        if self.max_attempts is not None and last_run.attempt >= self.max_attempts:
            return False
        return True


# --------------------------------------------------------------------------- #
# Clock & ids (injected for determinism/testability)
# --------------------------------------------------------------------------- #
class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        ...


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class IdGenerator(ABC):
    @abstractmethod
    def new_id(self) -> str:
        ...


class UuidGenerator(IdGenerator):
    """Default id generator (UUIDv4). Swap for ULID/snowflake for sortable ids."""

    def new_id(self) -> str:
        return uuid.uuid4().hex
