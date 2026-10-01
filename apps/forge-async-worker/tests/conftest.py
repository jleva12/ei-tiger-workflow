"""Doubles for the SAQ side of the worker (job, queues, lock client) and a worker
state on in-memory backends: the toy tasks of toy_tasks.py and the task
framework in memory."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from forge_async_worker import saq_worker
from forge_async_worker.config import WorkerSettings
from forge_async_worker.queue import SaqJobQueue
from forge_async_worker.saq_worker import JOB_OPTIONS, MAX_RETRIES, WorkerState, start_state

from .toy_tasks import toy_registry


class FakeJob:
    """The parts of saq.Job the worker reads and writes. A delivery is its
    queue, key and enqueue time; SAQ's retries of it keep all three."""

    def __init__(self, attempts: int = 1, *, key: str | None = None, queued: int = 1_760_000_000) -> None:
        self.id = "job-1"
        self.key = key or f"k-{uuid.uuid4().hex[:8]}"
        self.queued = queued
        self.attempts = attempts
        self.retries = MAX_RETRIES + 1
        self.retry_delay = 0.0
        self.retry_backoff: bool | float = True
        self.touched = 0

    @property
    def retryable(self) -> bool:
        return self.retries > self.attempts

    def redelivered(self) -> FakeJob:
        """SAQ's next attempt of this delivery."""
        again = FakeJob(self.attempts + 1, key=self.key, queued=self.queued)
        again.retries = self.retries
        return again

    async def update(self, **fields: object) -> None:
        for name, value in fields.items():
            setattr(self, name, value)
        self.touched += 1


class FakeLock:
    def __init__(self, free: bool) -> None:
        self.free, self.released = free, False

    async def acquire(self) -> bool:
        return self.free

    async def release(self) -> None:
        self.released = True


class FakeLocks:
    def __init__(self, free: bool = True) -> None:
        self.free = free
        self.taken: list[str] = []
        self.locks: list[FakeLock] = []

    def lock(self, name: str, *, timeout: int, blocking_timeout: int) -> FakeLock:
        assert (timeout, blocking_timeout) == (saq_worker.LOCK_TTL, saq_worker.LOCK_WAIT)
        self.taken.append(name)
        self.locks.append(FakeLock(self.free))
        return self.locks[-1]


class FakeQueue:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[tuple[str, dict]] = []

    async def enqueue(self, function: str, **kwargs: object) -> None:
        self.sent.append((function, kwargs))

    async def disconnect(self) -> None:
        return None


@dataclass
class FakeWorker:
    queue: FakeQueue


def worker_settings(**values: Any) -> WorkerSettings:
    return WorkerSettings(  # type: ignore[call-arg]
        _env_file=None,
        enabled_tasks=["notes", "alerts"],
        etf={"store": "memory"},
        **values,
    )


@dataclass
class Worker:
    state: WorkerState
    queues: dict[str, FakeQueue]
    locks: FakeLocks

    @property
    def runtime(self) -> Any:
        return self.state.runtime

    def context(self, job: FakeJob | None = None, *, queue: str = "notes", locks: Any = None) -> dict:
        """What SAQ hands a job function, with the worker's own entries."""
        return {
            "worker": FakeWorker(self.queues[queue]),
            "job": job or FakeJob(),
            saq_worker.CONTEXT_KEY: self.state,
            "redis": locks or self.locks,
        }

    async def runs(self) -> list[Any]:
        return await self.state.runs.store.list_runs()


@pytest.fixture
async def worker():
    queues = {"notes": FakeQueue("notes"), "alerts": FakeQueue("alerts")}
    state = await start_state(
        worker_settings(),
        registry=toy_registry(),
        queue=SaqJobQueue(queues, job_options=JOB_OPTIONS),
        redis_url=None,
    )
    yield Worker(state, queues, FakeLocks())
    await saq_worker.close_state(state)
