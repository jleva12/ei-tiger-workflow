"""``InMemoryStateStore`` — a complete, dependency-free reference ``StateStore``.

Useful for tests and single-process deployments. It enforces the same contract as a
production store (unique instance identity, optimistic versioning, idempotency-key
dedupe, audit sequencing) so behavior matches the Mongo store. State is held in dicts and
returned as deep copies, so callers can mutate freely without aliasing the store.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from ..audit import AuditEvent, AuditQuery
from ..exceptions import JobInstanceAlreadyExistsError, OptimisticLockError
from ..model import JobInstance, JobRun, StepRun, ValidationRequest
from ..status import BatchStatus, ValidationStatus
from ..store import InstancePage, InstanceQuery, StateStore

logger = logging.getLogger(__name__)


class InMemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._instances: dict[str, JobInstance] = {}
        self._instance_index: dict[tuple[str, str], str] = {}   # (job_name, identity_hash) -> id
        self._runs: dict[str, JobRun] = {}
        self._steps: dict[str, StepRun] = {}
        self._step_index: dict[tuple[str, str], str] = {}
        self._step_seq: dict[str, int] = {}  # step_run_id -> creation order (sort tiebreak)
        self._next_step_seq = 0
        self._validations: dict[str, ValidationRequest] = {}
        self._idempotency: dict[str, tuple[str, float | None]] = {}
        self._audit: list[AuditEvent] = []
        self._audit_seq: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._tx_owner: asyncio.Task[object] | None = None
        self._tx_depth = 0
        self._after_commit: list | None = None

    @asynccontextmanager
    async def _guard(self):
        if self._tx_owner is asyncio.current_task():
            yield
            return
        async with self._lock:
            yield

    @asynccontextmanager
    async def transaction(self):
        task = asyncio.current_task()
        if self._tx_owner is task:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return
        await self._lock.acquire()
        self._tx_owner = task
        self._tx_depth = 1
        self._after_commit = []
        snapshot = copy.deepcopy(
            (
                self._instances,
                self._instance_index,
                self._runs,
                self._steps,
                self._step_index,
                self._step_seq,
                self._next_step_seq,
                self._validations,
                self._idempotency,
                self._audit,
                self._audit_seq,
            )
        )
        callbacks = []
        try:
            yield
        except BaseException:
            (
                self._instances,
                self._instance_index,
                self._runs,
                self._steps,
                self._step_index,
                self._step_seq,
                self._next_step_seq,
                self._validations,
                self._idempotency,
                self._audit,
                self._audit_seq,
            ) = snapshot
            raise
        else:
            callbacks = list(self._after_commit)
        finally:
            self._tx_depth = 0
            self._tx_owner = None
            self._after_commit = None
            self._lock.release()
        for callback in callbacks:
            try:
                await callback()
            except Exception:
                logger.exception("after-commit callback failed")

    @property
    def supports_transactions(self) -> bool:
        return True

    async def run_in_transaction(self, fn, *, attempts: int = 3):
        async with self.transaction():
            return await fn()

    def defer_after_commit(self, callback) -> bool:
        if self._tx_owner is asyncio.current_task() and self._after_commit is not None:
            self._after_commit.append(callback)
            return True
        return False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        return None

    # ------------------------------------------------------------------ #
    # Job instances
    # ------------------------------------------------------------------ #
    async def create_job_instance(self, instance: JobInstance) -> JobInstance:
        async with self._guard():
            key = (instance.job_name, instance.identity_hash)
            if key in self._instance_index:
                raise JobInstanceAlreadyExistsError(f"instance exists for {key}")
            instance.version = 1
            self._instances[instance.id] = copy.deepcopy(instance)
            self._instance_index[key] = instance.id
            return copy.deepcopy(instance)

    async def find_job_instance(self, job_name: str, identity_hash: str) -> JobInstance | None:
        async with self._guard():
            iid = self._instance_index.get((job_name, identity_hash))
            return copy.deepcopy(self._instances[iid]) if iid else None

    async def get_job_instance(self, instance_id: str) -> JobInstance:
        async with self._guard():
            return copy.deepcopy(self._instances[instance_id])

    async def update_job_instance(self, instance: JobInstance, expected_version: int) -> JobInstance:
        async with self._guard():
            current = self._instances[instance.id]
            if current.version != expected_version:
                raise OptimisticLockError(f"instance {instance.id} version mismatch")
            instance.version = expected_version + 1
            self._instances[instance.id] = copy.deepcopy(instance)
            return copy.deepcopy(instance)

    async def list_job_instances(
        self, *, job_name=None, status=None, limit=50, offset=0
    ) -> list[JobInstance]:
        async with self._guard():
            items = [
                copy.deepcopy(i)
                for i in self._instances.values()
                if (job_name is None or i.job_name == job_name)
                and (status is None or i.status == status)
            ]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items[offset : offset + limit]

    async def query_instances(self, query: InstanceQuery) -> InstancePage:
        async with self._guard():
            items = [copy.deepcopy(i) for i in self._instances.values() if query.matches(i)]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return InstancePage(items=items[query.offset : query.offset + query.limit], total=len(items))

    # ------------------------------------------------------------------ #
    # Job runs
    # ------------------------------------------------------------------ #
    async def create_job_run(self, run: JobRun) -> JobRun:
        async with self._guard():
            if any(
                r.instance_id == run.instance_id and r.attempt == run.attempt
                for r in self._runs.values()
            ):
                raise JobInstanceAlreadyExistsError(
                    f"run attempt {run.attempt} already exists for instance {run.instance_id}"
                )
            run.version = 1
            self._runs[run.id] = copy.deepcopy(run)
            return copy.deepcopy(run)

    async def get_job_run(self, run_id: str) -> JobRun:
        async with self._guard():
            return copy.deepcopy(self._runs[run_id])

    async def update_job_run(self, run: JobRun, expected_version: int) -> JobRun:
        async with self._guard():
            current = self._runs[run.id]
            if current.version != expected_version:
                raise OptimisticLockError(f"run {run.id} version mismatch")
            run.version = expected_version + 1
            self._runs[run.id] = copy.deepcopy(run)
            return copy.deepcopy(run)

    async def find_runs_for_instance(self, instance_id: str) -> list[JobRun]:
        async with self._guard():
            runs = [copy.deepcopy(r) for r in self._runs.values() if r.instance_id == instance_id]
        runs.sort(key=lambda r: r.attempt)
        return runs

    async def find_latest_run(self, instance_id: str) -> JobRun | None:
        runs = await self.find_runs_for_instance(instance_id)
        return runs[-1] if runs else None

    async def list_runs(self, *, job_name=None, status=None, limit=50, offset=0) -> list[JobRun]:
        async with self._guard():
            runs = [
                copy.deepcopy(r)
                for r in self._runs.values()
                if (job_name is None or r.job_name == job_name)
                and (status is None or r.status == status)
            ]
        runs.sort(key=lambda r: r.create_time, reverse=True)
        return runs[offset : offset + limit]

    async def list_stale_runs(
        self,
        *,
        status: BatchStatus,
        updated_before: datetime,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JobRun]:
        async with self._guard():
            runs = [
                copy.deepcopy(run)
                for run in self._runs.values()
                if run.status is status
                and (
                    run.last_updated is None
                    or run.last_updated <= updated_before
                )
            ]
        runs.sort(key=lambda run: run.last_updated or run.create_time)
        return runs[offset : offset + limit]

    # ------------------------------------------------------------------ #
    # Step runs
    # ------------------------------------------------------------------ #
    async def create_step_run(self, step_run: StepRun) -> StepRun:
        async with self._guard():
            key = (step_run.run_id, step_run.step_name)
            if key in self._step_index:
                raise JobInstanceAlreadyExistsError(
                    f"step run already exists for run={step_run.run_id} step={step_run.step_name}"
                )
            step_run.version = 1
            self._steps[step_run.id] = copy.deepcopy(step_run)
            self._step_index[key] = step_run.id
            self._step_seq[step_run.id] = self._next_step_seq
            self._next_step_seq += 1
            return copy.deepcopy(step_run)

    async def get_step_run(self, step_run_id: str) -> StepRun:
        async with self._guard():
            return copy.deepcopy(self._steps[step_run_id])

    async def update_step_run(self, step_run: StepRun, expected_version: int) -> StepRun:
        async with self._guard():
            current = self._steps[step_run.id]
            if current.version != expected_version:
                raise OptimisticLockError(f"step run {step_run.id} version mismatch")
            step_run.version = expected_version + 1
            self._steps[step_run.id] = copy.deepcopy(step_run)
            return copy.deepcopy(step_run)

    async def find_step_runs(self, run_id: str) -> list[StepRun]:
        async with self._guard():
            steps = [copy.deepcopy(s) for s in self._steps.values() if s.run_id == run_id]
        steps.sort(key=lambda s: s.start_time or s.updated_at)
        return steps

    async def find_step_run(self, run_id: str, step_name: str) -> StepRun | None:
        async with self._guard():
            for s in self._steps.values():
                if s.run_id == run_id and s.step_name == step_name:
                    return copy.deepcopy(s)
            return None

    async def find_last_step_run(
        self, instance_id: str, step_name: str, *, status: BatchStatus | None = None
    ) -> StepRun | None:
        async with self._guard():
            matches = [
                s
                for s in self._steps.values()
                if s.instance_id == instance_id
                and s.step_name == step_name
                and (status is None or s.status == status)
            ]
        if not matches:
            return None
        # Creation order breaks updated_at ties (deterministic with an injected
        # fixed clock, where every timestamp is identical).
        matches.sort(key=lambda s: (s.updated_at, self._step_seq.get(s.id, 0)))
        return copy.deepcopy(matches[-1])

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    async def create_validation_request(self, request: ValidationRequest) -> ValidationRequest:
        async with self._guard():
            request.version = 1
            self._validations[request.id] = copy.deepcopy(request)
            return copy.deepcopy(request)

    async def get_validation_request(self, request_id: str) -> ValidationRequest:
        async with self._guard():
            return copy.deepcopy(self._validations[request_id])

    async def update_validation_request(
        self, request: ValidationRequest, expected_version: int
    ) -> ValidationRequest:
        async with self._guard():
            current = self._validations[request.id]
            if current.version != expected_version:
                raise OptimisticLockError(f"validation {request.id} version mismatch")
            request.version = expected_version + 1
            self._validations[request.id] = copy.deepcopy(request)
            return copy.deepcopy(request)

    async def find_open_validation(self, run_id: str) -> ValidationRequest | None:
        async with self._guard():
            for v in self._validations.values():
                if v.run_id == run_id and v.status == ValidationStatus.PENDING:
                    return copy.deepcopy(v)
            return None

    async def list_pending_validations(
        self, *, required_role: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[ValidationRequest]:
        async with self._guard():
            items = [
                copy.deepcopy(v)
                for v in self._validations.values()
                if v.status == ValidationStatus.PENDING
                and (required_role is None or v.required_role == required_role)
            ]
        items.sort(key=lambda v: v.created_at)
        return items[offset : offset + limit]

    async def list_overdue_validations(
        self, *, now: datetime, limit: int = 100, offset: int = 0
    ) -> list[ValidationRequest]:
        async with self._guard():
            items = [
                copy.deepcopy(v)
                for v in self._validations.values()
                if v.status == ValidationStatus.PENDING
                and v.sla_deadline is not None
                and v.sla_deadline <= now
            ]
        items.sort(key=lambda v: v.sla_deadline or now)
        return items[offset : offset + limit]

    # ------------------------------------------------------------------ #
    # Idempotency
    # ------------------------------------------------------------------ #
    async def register_idempotency_key(
        self, key: str, run_id: str, ttl: timedelta | None = None
    ) -> str | None:
        async with self._guard():
            now = asyncio.get_running_loop().time()
            existing = self._idempotency.get(key)
            if existing is not None and (existing[1] is None or existing[1] > now):
                return existing[0]
            expires = now + ttl.total_seconds() if ttl is not None else None
            self._idempotency[key] = (run_id, expires)
            return None

    async def rebind_idempotency_key(
        self, key: str, expected_run_id: str, run_id: str, ttl: timedelta | None = None
    ) -> bool:
        async with self._guard():
            current = self._idempotency.get(key)
            if current is None or current[0] != expected_run_id:
                return False
            now = asyncio.get_running_loop().time()
            expires = now + ttl.total_seconds() if ttl is not None else None
            self._idempotency[key] = (run_id, expires)
            return True

    async def release_idempotency_key(self, key: str, run_id: str) -> bool:
        async with self._guard():
            current = self._idempotency.get(key)
            if current is None or current[0] != run_id:
                return False
            del self._idempotency[key]
            return True

    # ------------------------------------------------------------------ #
    # Audit
    # ------------------------------------------------------------------ #
    async def append_audit(self, event: AuditEvent) -> None:
        async with self._guard():
            self._audit.append(copy.deepcopy(event))

    async def query_audit(self, query: AuditQuery) -> list[AuditEvent]:
        async with self._guard():
            events = [
                copy.deepcopy(e)
                for e in self._audit
                if (query.run_id is None or e.run_id == query.run_id)
                and (query.instance_id is None or e.instance_id == query.instance_id)
                and (query.event_types is None or e.event_type in query.event_types)
                and (query.since is None or e.at >= query.since)
                and (query.until is None or e.at <= query.until)
            ]
        events.sort(key=lambda e: (e.run_id, e.sequence))
        return events[query.offset : query.offset + query.limit]

    async def next_audit_sequence(self, run_id: str) -> int:
        async with self._guard():
            nxt = self._audit_seq.get(run_id, 0) + 1
            self._audit_seq[run_id] = nxt
            return nxt
