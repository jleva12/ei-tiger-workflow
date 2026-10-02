"""Runs JobSpecs against the registered task types. Queue-agnostic: the
inline queue here runs follow-ups in-process (tests, the CLI); the worker runs
an ADK workflow run's job off its queue, on the run store, and queues no
follow-ups."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from forge_tasks.errors import TaskError
from forge_tasks.tasks import JobOutcome, JobResult, JobSpec, JobStatus

if TYPE_CHECKING:
    from forge_tasks.protocols import Job, JobQueue, Task

log = logging.getLogger(__name__)

OBSERVER_TIMEOUT = 10.0  # seconds a task's observer may take per job


def _elapsed_ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


class UnknownJobError(TaskError):
    permanent = True


class JobRunner:
    """Validates a JobSpec, runs the job, and hands follow-ups to the queue.

    Error contract (shared by every task type):
      TransientError      -> propagates; the queue retries with backoff
      permanent TaskError / bad payload -> JobResult(status=FAILED)

    A task with an ``observer`` (protocols.JobObserver) is told about every
    attempt of its jobs, result or exception, before the follow-ups go out.
    """

    def __init__(self, tasks: Mapping[str, Task], queue: JobQueue) -> None:
        self.tasks = dict(tasks)
        self.queue = queue

    def resolve(self, spec: JobSpec) -> tuple[Job, BaseModel]:
        task = self.tasks.get(spec.task_type)
        if task is None:
            raise UnknownJobError(f"task type {spec.task_type!r} is not enabled here")
        job = task.jobs.get(spec.kind)
        if job is None:
            raise UnknownJobError(f"{spec.task_type!r} has no job {spec.kind!r}; has {sorted(task.jobs)}")
        try:
            payload = job.payload_model.model_validate(spec.payload)
        except ValidationError as exc:
            raise UnknownJobError(f"invalid payload for {spec.label()}: {exc}") from exc
        return job, payload

    def lock_key(self, spec: JobSpec) -> str | None:
        try:
            job, payload = self.resolve(spec)
        except UnknownJobError:
            return None
        key = job.lock_key(payload)
        return f"{spec.task_type}:{key}" if key else None

    async def describe(self, spec: JobSpec) -> str | None:
        """The job's own words for what it works on (an ADK workflow's name),
        from its optional ``describe(payload)`` (plain or async); None when it
        has none. Never raises: a description is a nicety."""
        try:
            job, payload = self.resolve(spec)
        except UnknownJobError:
            return None
        describe = getattr(job, "describe", None)
        if describe is None:
            return None
        try:
            text = describe(payload)
            if inspect.isawaitable(text):
                text = await text
        except Exception:
            log.warning("describing %s failed", spec.label(), exc_info=True)
            return None
        return str(text)[:200] if text else None

    async def run(self, spec: JobSpec) -> JobResult:
        t0 = time.perf_counter()
        try:
            result = await self._run(spec)
        except Exception as exc:  # a TransientError (the queue retries it) or a bug
            await self._observe(JobOutcome(spec, None, exc, _elapsed_ms(t0)))
            raise
        elapsed = _elapsed_ms(t0)
        result.detail.setdefault("duration_ms", elapsed)
        await self._observe(JobOutcome(spec, result, duration_ms=elapsed))
        for follow in result.followups:
            if follow.tenant is None and spec.tenant is not None:
                follow = follow.model_copy(update={"tenant_id": spec.tenant})  # the same organization's work
            await self.queue.enqueue(follow)
        log.info("job %s -> %s", spec.label(), result.status.value, extra={"job": spec.label(), **result.detail})
        return result

    async def _run(self, spec: JobSpec) -> JobResult:
        try:
            job, payload = self.resolve(spec)
            return await job.run(payload)
        except TaskError as exc:
            if not exc.permanent:
                raise
            log.error("job %s failed permanently: %s", spec.label(), exc)
            return JobResult.failed(f"{type(exc).__name__}: {exc}")

    async def _observe(self, outcome: JobOutcome) -> None:
        """Tells the task's observer, if it has one. Never raises: observing
        must not change a job's outcome."""
        observer = getattr(self.tasks.get(outcome.spec.task_type), "observer", None)
        if observer is None:
            return
        try:
            await asyncio.wait_for(observer.job_finished(outcome), OBSERVER_TIMEOUT)
        except Exception:
            log.warning("observer of job %s failed", outcome.spec.label(), exc_info=True)


class InlineJobQueue:
    """Runs follow-ups in-process, breadth-first. For tests, the CLI and local dev."""

    def __init__(self, *, max_jobs: int = 100_000) -> None:
        self.runner: JobRunner | None = None
        self.history: list[tuple[JobSpec, JobResult]] = []
        self._pending: deque[JobSpec] = deque()
        self._draining = False
        self.max_jobs = max_jobs

    def bind(self, runner: JobRunner) -> None:
        self.runner = runner

    async def enqueue(self, spec: JobSpec, *, countdown: float | None = None) -> None:
        self._pending.append(spec)
        if not self._draining:
            await self.drain()

    async def submit(self, spec: JobSpec) -> JobResult:
        """Run ``spec`` and everything it fans out to; returns the root result."""
        assert self.runner is not None, "call bind(runner) first"
        self._draining = True
        try:
            result = await self.runner.run(spec)
            self.history.append((spec, result))
            await self._drain_pending()
        finally:
            self._draining = False
        return result

    async def drain(self) -> None:
        self._draining = True
        try:
            await self._drain_pending()
        finally:
            self._draining = False

    async def _drain_pending(self) -> None:
        assert self.runner is not None
        while self._pending:
            if len(self.history) >= self.max_jobs:
                raise RuntimeError("inline queue job limit reached (runaway fan-out?)")
            spec = self._pending.popleft()
            self.history.append((spec, await self.runner.run(spec)))


def ok(**detail: Any) -> JobResult:
    return JobResult(status=JobStatus.OK, detail=detail)


def skipped(reason: str, **detail: Any) -> JobResult:
    return JobResult(status=JobStatus.SKIPPED, detail={"reason": reason, **detail})
