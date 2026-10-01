"""Smaller audit fixes: idempotency poll budget (T9), BlockingStep (T14),
error scoping in _resolve_instance (T19), and step-run ordering ties (T22)."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest

from etf import (
    BatchStatus,
    BlockingStep,
    JobLauncher,
    JobOperator,
    StepContext,
    StepResult,
)
from etf.exceptions import JobAlreadyRunningError
from etf.model import JobRun, StepRun
from etf.model import JobParameters
from etf.stores.memory import InMemoryStateStore
from helpers import RecordingStep, launch_request, make_cfg, make_harness


# --------------------------------------------------------------------------- #
# T9 — idempotency reservation poll budget
# --------------------------------------------------------------------------- #
async def test_duplicate_gives_up_only_after_the_poll_budget():
    """A reservation whose run never appears fails with JobAlreadyRunningError —
    but only after the configured budget, not a hard-coded 200 ms."""
    from datetime import timedelta

    cfg, store = make_cfg(RecordingStep(), idempotency_poll_budget=timedelta(seconds=0.4))
    await store.register_idempotency_key("dup-key", "ghost-run-id")

    started = time.monotonic()
    with pytest.raises(JobAlreadyRunningError):
        await JobLauncher(cfg).launch(launch_request(key="dup-key"))
    assert time.monotonic() - started >= 0.35


async def test_duplicate_waits_for_a_slow_winner_to_commit():
    """The winner's run appears mid-poll: the duplicate returns it instead of erroring."""
    cfg, store = make_cfg(RecordingStep())
    await store.register_idempotency_key("slow-key", "winner-run")
    # The winning worker holds the instance lease while it runs.
    lease = await cfg.lock_provider.acquire("inst-1", "winner", cfg.lock_ttl)
    assert lease is not None

    async def commit_late():
        await asyncio.sleep(0.3)  # longer than the old fixed 200 ms budget
        await store.create_job_run(
            JobRun(
                id="winner-run",
                instance_id="inst-1",
                job_name="job",
                parameters=JobParameters(identifying={"id": "1"}),
                status=BatchStatus.RUNNING,
            )
        )

    committer = asyncio.create_task(commit_late())
    run = await JobLauncher(cfg).launch(launch_request(key="slow-key"))
    await committer
    assert run.id == "winner-run"


# --------------------------------------------------------------------------- #
# T14 — BlockingStep runs its sync body off the event loop
# --------------------------------------------------------------------------- #
class SyncStep(BlockingStep):
    name = "sync_work"

    def __init__(self) -> None:
        self.thread_id: int | None = None

    def execute_sync(self, ctx: StepContext) -> StepResult:
        self.thread_id = threading.get_ident()
        ctx.put("crunched", True)
        return StepResult.completed(write_count=1)


async def test_blocking_step_executes_in_a_worker_thread():
    step = SyncStep()
    launcher, _, _, store = make_harness(step)
    run = await launcher.launch(launch_request())
    assert run.status is BatchStatus.COMPLETED
    assert step.thread_id is not None and step.thread_id != threading.get_ident()
    persisted = (await store.find_step_runs(run.id))[0]
    assert persisted.execution_context.get("crunched") is True


# --------------------------------------------------------------------------- #
# T19 — _resolve_instance must only treat KeyError as "not an instance id"
# --------------------------------------------------------------------------- #
class OutageStore(InMemoryStateStore):
    async def get_job_instance(self, instance_id: str):
        raise RuntimeError("store outage")


async def test_restart_propagates_store_outage_instead_of_masking_it():
    cfg, _ = make_cfg(RecordingStep(), store=OutageStore())
    with pytest.raises(RuntimeError, match="store outage"):
        await JobOperator(cfg).restart("some-id")


# --------------------------------------------------------------------------- #
# T22 — find_last_step_run tie-break under a fixed clock
# --------------------------------------------------------------------------- #
async def test_find_last_step_run_breaks_updated_at_ties_by_creation_order():
    store = InMemoryStateStore()
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for n, run_id in (("1", "run-a"), ("2", "run-b")):
        await store.create_step_run(
            StepRun(id=f"sr-{n}", run_id=run_id, instance_id="inst", step_name="s",
                    updated_at=fixed)
        )
    latest = await store.find_last_step_run("inst", "s")
    assert latest is not None and latest.id == "sr-2"  # later-created wins the tie
