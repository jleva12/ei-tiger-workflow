"""``StateStore`` — the primary pluggable persistence contract.

Everything ETF persists flows through this ABC: job instances (the idempotency anchor),
runs, step runs, durable checkpoints, validation gates, idempotency keys, and the audit
stream. Implement these ~two-dozen async methods against any backend and pass your
instance to :class:`etf.operator.JobLauncher` / :class:`etf.operator.JobOperator`;
nothing else in the framework changes.

**Concurrency contract.** ``update_*`` methods take an ``expected_version`` and must
raise :class:`~etf.exceptions.OptimisticLockError` on mismatch (the runner reloads and
retries). ``create_job_instance`` must atomically enforce uniqueness of
``(job_name, identity_hash)`` and raise
:class:`~etf.exceptions.JobInstanceAlreadyExistsError` on conflict. These two guarantees
are what make idempotency and single-writer safety possible.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .audit import AuditEvent, AuditQuery
from .model import (
    JobInstance,
    JobRun,
    StepRun,
    ValidationRequest,
)
from .status import BatchStatus


@dataclass(slots=True, frozen=True)
class InstanceQuery:
    """Which instances :meth:`StateStore.query_instances` returns: those matching every
    filter given, newest first.

    ``job_name_prefixes`` matches a job name starting with any of them (``"workflows."``
    for a family of jobs), and ``exclude_job_name_prefixes`` one starting with none of
    them; ``labels`` matches instances carrying every one of those label values.
    """

    job_names: Sequence[str] | None = None
    job_name_prefixes: Sequence[str] | None = None
    exclude_job_name_prefixes: Sequence[str] | None = None
    statuses: Sequence[BatchStatus] | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    limit: int = 50
    offset: int = 0

    def matches(self, instance: JobInstance) -> bool:
        if self.job_names is not None and instance.job_name not in self.job_names:
            return False
        if self.job_name_prefixes is not None and not any(
            instance.job_name.startswith(prefix) for prefix in self.job_name_prefixes
        ):
            return False
        if self.exclude_job_name_prefixes and any(
            instance.job_name.startswith(prefix) for prefix in self.exclude_job_name_prefixes
        ):
            return False
        if self.statuses is not None and instance.status not in self.statuses:
            return False
        return all(instance.labels.get(key) == value for key, value in self.labels.items())


@dataclass(slots=True)
class InstancePage:
    """One page of :meth:`StateStore.query_instances`, and how many match in all."""

    items: list[JobInstance]
    total: int


class StateStore(ABC):
    """Abstract persistence for all ETF state. See module docstring for the contract."""

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def initialize(self) -> None:
        """Prepare the backend: create collections/tables and required indexes.

        Required indexes for the default semantics:
          * unique ``(job_name, identity_hash)`` on job instances;
          * unique ``idempotency_key`` (with TTL) on the dedupe collection;
          * ``(instance_id, attempt)`` on runs; ``(run_id, step_name)`` on step runs;
          * ``(run_id, sequence)`` on audit events.
        """

    async def close(self) -> None:
        """Release connections/resources. Default: no-op."""
        return None

    async def healthcheck(self) -> bool:
        """Return True if the backend is reachable. Default: True."""
        return True

    @property
    def supports_transactions(self) -> bool:
        """Whether :meth:`transaction` provides real multi-document atomicity."""
        return False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Optional multi-document transaction scope. Default: a no-op context.

        Stores that support it (e.g. Mongo replica sets) should override to wrap the body
        in a real transaction so, e.g., a run+step transition commits atomically.
        """
        yield

    async def run_in_transaction(
        self,
        fn: Callable[[], Awaitable[object]],
        *,
        attempts: int = 3,
    ) -> object:
        """Run a callback in a transaction.

        Stores with retryable transaction errors should override this method and replay
        ``fn`` when the backend says the whole transaction is safe to retry.
        """
        async with self.transaction():
            return await fn()

    def defer_after_commit(self, callback: Callable[[], Awaitable[None]]) -> bool:
        """Queue ``callback`` for successful outer-transaction commit.

        Returns ``True`` when deferred. The default store has no transaction-local
        callback support, so callers should execute the callback immediately.
        """
        return False

    # ------------------------------------------------------------------ #
    # Job instances (idempotency anchor)
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def create_job_instance(self, instance: JobInstance) -> JobInstance:
        """Insert a new instance, atomically enforcing uniqueness of (job_name, identity_hash).

        Raises :class:`~etf.exceptions.JobInstanceAlreadyExistsError` on conflict.
        """

    @abstractmethod
    async def find_job_instance(self, job_name: str, identity_hash: str) -> JobInstance | None:
        ...

    @abstractmethod
    async def get_job_instance(self, instance_id: str) -> JobInstance:
        ...

    @abstractmethod
    async def update_job_instance(self, instance: JobInstance, expected_version: int) -> JobInstance:
        """Optimistically update an instance. Raises ``OptimisticLockError`` on mismatch."""

    @abstractmethod
    async def list_job_instances(
        self,
        *,
        job_name: str | None = None,
        status: BatchStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[JobInstance]:
        ...

    async def query_instances(self, query: InstanceQuery) -> InstancePage:
        """Instances matching ``query``, newest first, with the total that match.

        This default pages through :meth:`list_job_instances` and filters in memory, so
        every store supports it; stores override it with an indexed query.
        """
        matched: list[JobInstance] = []
        offset, page = 0, 500
        while True:
            batch = await self.list_job_instances(limit=page, offset=offset)
            matched.extend(i for i in batch if query.matches(i))
            if len(batch) < page:
                break
            offset += page
        return InstancePage(items=matched[query.offset : query.offset + query.limit], total=len(matched))

    # ------------------------------------------------------------------ #
    # Job runs (executions)
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def create_job_run(self, run: JobRun) -> JobRun:
        ...

    @abstractmethod
    async def get_job_run(self, run_id: str) -> JobRun:
        ...

    @abstractmethod
    async def update_job_run(self, run: JobRun, expected_version: int) -> JobRun:
        """Optimistically update a run. Raises ``OptimisticLockError`` on mismatch."""

    @abstractmethod
    async def find_runs_for_instance(self, instance_id: str) -> list[JobRun]:
        ...

    @abstractmethod
    async def find_latest_run(self, instance_id: str) -> JobRun | None:
        """Return the most recent run (highest attempt) for an instance, if any."""

    @abstractmethod
    async def list_runs(
        self,
        *,
        job_name: str | None = None,
        status: BatchStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[JobRun]:
        ...

    async def list_stale_runs(
        self,
        *,
        status: BatchStatus,
        updated_before: datetime,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JobRun]:
        """Return stale runs oldest-first, applying the cutoff before pagination.

        The generic implementation scans pages from :meth:`list_runs` so existing
        custom stores remain compatible. Production stores should override this with
        an indexed ``(status, last_updated)`` query.
        """
        if limit <= 0:
            return []
        matched: list[JobRun] = []
        scan_offset = 0
        page_size = max(100, limit + offset)
        needed = limit + offset
        while len(matched) < needed:
            page = await self.list_runs(
                status=status,
                limit=page_size,
                offset=scan_offset,
            )
            if not page:
                break
            matched.extend(
                run
                for run in page
                if run.last_updated is None
                or run.last_updated <= updated_before
            )
            scan_offset += len(page)
            if len(page) < page_size:
                break
        matched.sort(key=lambda run: run.last_updated or run.create_time)
        return matched[offset:needed]

    # ------------------------------------------------------------------ #
    # Step runs
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def create_step_run(self, step_run: StepRun) -> StepRun:
        ...

    @abstractmethod
    async def get_step_run(self, step_run_id: str) -> StepRun:
        ...

    @abstractmethod
    async def update_step_run(self, step_run: StepRun, expected_version: int) -> StepRun:
        """Optimistically update a step run. Raises ``OptimisticLockError`` on mismatch."""

    @abstractmethod
    async def find_step_runs(self, run_id: str) -> list[StepRun]:
        ...

    @abstractmethod
    async def find_step_run(self, run_id: str, step_name: str) -> StepRun | None:
        ...

    @abstractmethod
    async def find_last_step_run(
        self,
        instance_id: str,
        step_name: str,
        *,
        status: BatchStatus | None = None,
    ) -> StepRun | None:
        """Most recent step run for a step across all of an instance's runs.

        With ``status=COMPLETED`` this powers restart-skip: a step already completed in a
        prior run is not re-executed.
        """

    # ------------------------------------------------------------------ #
    # Human-in-the-loop validation
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def create_validation_request(self, request: ValidationRequest) -> ValidationRequest:
        ...

    @abstractmethod
    async def get_validation_request(self, request_id: str) -> ValidationRequest:
        ...

    @abstractmethod
    async def update_validation_request(
        self, request: ValidationRequest, expected_version: int
    ) -> ValidationRequest:
        """Optimistically resolve a validation request."""

    @abstractmethod
    async def find_open_validation(self, run_id: str) -> ValidationRequest | None:
        ...

    @abstractmethod
    async def list_pending_validations(
        self,
        *,
        required_role: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ValidationRequest]:
        ...

    async def list_overdue_validations(
        self,
        *,
        now: datetime,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ValidationRequest]:
        """PENDING requests whose ``sla_deadline`` is at or before ``now``, earliest
        deadline first. Requests without a deadline are never returned.

        The generic implementation scans :meth:`list_pending_validations` so existing
        custom stores remain compatible. Production stores should override this with
        an indexed ``(status, sla_deadline)`` query.
        """
        if limit <= 0:
            return []
        overdue: list[ValidationRequest] = []
        scan_offset = 0
        page_size = 100
        while True:
            page = await self.list_pending_validations(limit=page_size, offset=scan_offset)
            if not page:
                break
            overdue.extend(
                request
                for request in page
                if request.sla_deadline is not None and request.sla_deadline <= now
            )
            scan_offset += len(page)
            if len(page) < page_size:
                break
        overdue.sort(key=lambda request: request.sla_deadline or now)
        return overdue[offset : offset + limit]

    # ------------------------------------------------------------------ #
    # Idempotency keys (ingress dedupe)
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def register_idempotency_key(
        self, key: str, run_id: str, ttl: timedelta | None = None
    ) -> str | None:
        """Atomically claim ``key`` for ``run_id``.

        Returns ``None`` if the key was newly registered (caller should proceed), or the
        **existing** ``run_id`` if the key was already claimed (duplicate delivery — the
        caller returns that run instead of launching a new one).
        """

    @abstractmethod
    async def rebind_idempotency_key(
        self,
        key: str,
        expected_run_id: str,
        run_id: str,
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomically replace/refresh an owned idempotency reservation."""

    @abstractmethod
    async def release_idempotency_key(self, key: str, run_id: str) -> bool:
        """Release an uncommitted reservation if it is still owned by ``run_id``."""

    # ------------------------------------------------------------------ #
    # Audit (the StoreBackedAuditSink delegates here)
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def append_audit(self, event: AuditEvent) -> None:
        ...

    @abstractmethod
    async def query_audit(self, query: AuditQuery) -> list[AuditEvent]:
        ...

    @abstractmethod
    async def next_audit_sequence(self, run_id: str) -> int:
        """Return the next monotonically increasing audit sequence number for a run."""


__all__ = ["StateStore", "AuditEvent", "AuditQuery", "Sequence"]
