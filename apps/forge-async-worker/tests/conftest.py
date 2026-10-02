"""The worker on a SQLite run store, with a clock the tests move, a fake SAQ
queue and job, and the stand-in ADK workflows task of toy_tasks.py."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from forge_async_worker import saq_worker
from forge_async_worker.config import WorkerSettings
from forge_async_worker.queue import RunQueue
from forge_async_worker.saq_worker import WorkerState, start_state
from forge_task_adk_workflows.run_store import Actor, RunStore, metadata

from .toy_tasks import ScriptedTask, toy_registry

ALICE = Actor("user-alice", "Alice")
BOB = Actor("user-bob", "Bob")
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class FakeSaqQueue:
    """The parts of saq.Queue the worker uses; ``down`` makes Redis unreachable."""

    name = "adk_workflows"

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.down = False

    async def enqueue(self, function: str, **kwargs: Any) -> object:
        if self.down:
            raise ConnectionError("Redis is down")
        self.sent.append((function, kwargs))
        return object()

    async def disconnect(self) -> None:
        return None


class FakeJob:
    """The parts of saq.Job the worker reads and writes."""

    def __init__(self, function: str = "run_adk") -> None:
        self.id = "job-1"
        self.function = function
        self.timeout, self.heartbeat, self.retries = 10, 0, 1  # SAQ's defaults
        self.touched = 0

    async def update(self, **fields: object) -> None:
        for name, value in fields.items():
            setattr(self, name, value)
        self.touched += 1


def worker_settings(**values: Any) -> WorkerSettings:
    return WorkerSettings(_env_file=None, **{"enabled_tasks": ["adk_workflows"], **values})  # type: ignore[call-arg]


def at(when: datetime) -> int:
    """A time as SAQ schedules it."""
    return int(when.timestamp())


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def store(clock: Clock) -> AsyncIterator[RunStore]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield RunStore(engine, clock=clock)
    await engine.dispose()


def payload(*steps: dict[str, Any], **values: Any) -> dict[str, Any]:
    """A run's payload (RunPayload), its input the stand-in task's steps."""
    return {
        "tenant_id": "org-1",
        "agent_id": "ag-1",
        "revision": 3,
        "name": "Support desk",
        "document": {"name": "Support desk"},
        "input": {"steps": list(steps)},
        "session_id": "session-1",
        "run_as": "user-alice",
        **values,
    }


@dataclass
class Worker:
    state: WorkerState
    saq: FakeSaqQueue
    clock: Clock

    @property
    def store(self) -> RunStore:
        return self.state.store

    @property
    def task(self) -> ScriptedTask:
        return self.state.runtime.tasks["adk_workflows"]  # type: ignore[return-value]

    @property
    def sent(self) -> list[tuple[str, dict[str, Any]]]:
        return self.saq.sent

    async def start(self, *steps: dict[str, Any], **values: Any) -> dict[str, Any]:
        """A new run, queued, as the admin API creates it."""
        return await self.store.create(
            organization_id="org-1",
            agent_id="ag-1",
            agent_name="Support desk",
            revision=3,
            session_id="session-1",
            payload=values.pop("raw", None) or payload(*steps, **values),
            requested_by=ALICE,
        )

    async def run(self, run_id: str, job: FakeJob | None = None) -> str:
        """One run_adk job; what became of the run."""
        out = await self.state.jobs.run_adk(run_id, job=job)
        assert out["run_id"] == run_id
        return str(out["outcome"])

    async def get(self, run_id: str) -> dict[str, Any]:
        run = await self.store.get(run_id)
        assert run is not None
        return run

    async def kinds(self, run_id: str) -> list[str]:
        return [event["kind"] for event in await self.store.events(run_id)]


@pytest.fixture
async def worker(store: RunStore, clock: Clock) -> AsyncIterator[Worker]:
    saq = FakeSaqQueue()
    state = await start_state(
        worker_settings(), registry=toy_registry(), store=store, queue=RunQueue(saq), redis_url=None
    )
    yield Worker(state, saq, clock)
    await saq_worker.close_state(state)
