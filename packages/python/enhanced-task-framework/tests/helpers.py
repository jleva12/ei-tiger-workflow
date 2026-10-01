"""Shared test harness: config factory, fake clock, common steps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from etf import (
    Actor,
    EtfConfig,
    JobDefinition,
    JobLauncher,
    JobOperator,
    JobParameters,
    JobRegistry,
    LaunchRequest,
    Step,
    StepContext,
    StepResult,
)
from etf.audit import StoreBackedAuditSink
from etf.locking import InMemoryLockProvider
from etf.policies import Clock
from etf.stores.memory import InMemoryStateStore


class FakeClock(Clock):
    """A manually advanced clock so tests control staleness/SLA arithmetic."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class RecordingStep(Step):
    """Counts executions (proves restart-skip / re-execution behavior)."""

    def __init__(self, name: str = "work") -> None:
        self.name = name
        self.calls = 0

    async def execute(self, ctx: StepContext) -> StepResult:
        self.calls += 1
        return StepResult.completed()


def make_cfg(*steps: Step, job_name: str = "job", **overrides):
    """Build an in-memory EtfConfig around the given steps.

    Returns ``(cfg, store)``; pass EtfConfig field overrides as kwargs, plus
    ``definition_options`` for JobDefinition kwargs (e.g. transitions).
    """
    store = overrides.pop("store", None) or InMemoryStateStore()
    registry = JobRegistry()
    definition_options = overrides.pop("definition_options", {})
    registry.register(JobDefinition(name=job_name, steps=list(steps), **definition_options))
    cfg = EtfConfig(
        store=store,
        audit=StoreBackedAuditSink(store),
        lock_provider=overrides.pop("lock_provider", InMemoryLockProvider()),
        registry=registry,
        **overrides,
    )
    return cfg, store


def make_harness(*steps: Step, **overrides):
    """``(launcher, operator, cfg, store)`` for tests that drive the full lifecycle."""
    cfg, store = make_cfg(*steps, **overrides)
    return JobLauncher(cfg), JobOperator(cfg), cfg, store


def launch_request(job_name: str = "job", *, key: str | None = None, **identifying) -> LaunchRequest:
    return LaunchRequest(
        job_name=job_name,
        parameters=JobParameters(identifying=identifying or {"id": "1"}),
        idempotency_key=key,
        requested_by=Actor.service("test"),
    )
