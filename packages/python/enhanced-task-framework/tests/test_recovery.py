"""Crash recovery (audit T1/T27), validation cleanup (T11), stack-trace hygiene (T20).

Covers both halves of crash recovery: the runner's fail-fast transition to FAILED on
unexpected exceptions, and the ``recover_stale_runs`` sweep that reclaims runs whose
worker died without any chance to clean up.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from etf import BatchStatus, LockAcquisitionError, Step, StepContext, StepResult
from etf.locking import InMemoryLockProvider
from etf.model import JobInstance, JobRun
from etf.status import AuditEventType, ValidationStatus
from etf.stores.memory import InMemoryStateStore
from helpers import FakeClock, RecordingStep, launch_request, make_harness


class HangingStep(Step):
    """Blocks forever (cancellable) while ``hang`` is set — a wedged worker."""

    name = "hang"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.hang = True

    async def execute(self, ctx: StepContext) -> StepResult:
        if self.hang:
            self.started.set()
            await asyncio.Event().wait()
        return StepResult.completed()


# --------------------------------------------------------------------------- #
# Fail-fast (T1, half 1)
# --------------------------------------------------------------------------- #
async def test_kill_mid_run_leaves_run_failed_and_restartable():
    """Cancelling the runner (worker killed) must not strand the run in RUNNING."""
    step = HangingStep()
    launcher, operator, cfg, store = make_harness(step, RecordingStep("after"))
    task = asyncio.create_task(launcher.launch(launch_request()))
    await step.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    run = (await store.list_runs())[0]
    assert run.status is BatchStatus.FAILED
    assert run.failures and run.failures[-1].exception_type.endswith("CancelledError")
    assert (await store.get_job_instance(run.instance_id)).status is BatchStatus.FAILED

    # The lock was released and the run is restartable — recovery is complete.
    step.hang = False
    restarted = await operator.restart(run.instance_id)
    assert restarted.status is BatchStatus.COMPLETED


class FailingUpdateStore(InMemoryStateStore):
    """Raises once from update_step_run after the trigger is armed (a store outage)."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_step_update = False

    async def update_step_run(self, step_run, expected_version):
        if self.fail_next_step_update and step_run.status is BatchStatus.COMPLETED:
            self.fail_next_step_update = False
            raise RuntimeError("store outage")
        return await super().update_step_run(step_run, expected_version)


async def test_store_error_mid_run_fail_fasts_to_failed():
    store = FailingUpdateStore()
    launcher, _, cfg, _ = make_harness(RecordingStep(), store=store)
    store.fail_next_step_update = True
    with pytest.raises(RuntimeError, match="store outage"):
        await launcher.launch(launch_request())
    run = (await store.list_runs())[0]
    assert run.status is BatchStatus.FAILED
    assert run.failures[-1].exception_type.endswith("RuntimeError")


class BrokenStore(InMemoryStateStore):
    """Once armed, terminal-state writes fail — so the step-completion write (the
    original error) AND the fail-fast FAILED repair both hit the outage."""

    def __init__(self) -> None:
        super().__init__()
        self.broken = False

    async def update_step_run(self, step_run, expected_version):
        if self.broken and step_run.status is BatchStatus.COMPLETED:
            raise RuntimeError("primary down")
        return await super().update_step_run(step_run, expected_version)

    async def update_job_run(self, run, expected_version):
        if self.broken and run.status in (BatchStatus.COMPLETED, BatchStatus.FAILED):
            raise RuntimeError("primary down")
        return await super().update_job_run(run, expected_version)


async def test_fail_fast_swallows_secondary_errors_and_propagates_original():
    """When even the FAILED write fails, the original exception must still surface."""
    store = BrokenStore()
    launcher, _, _, _ = make_harness(RecordingStep(), store=store)
    store.broken = True
    with pytest.raises(RuntimeError, match="primary down"):
        await launcher.launch(launch_request())
    # Not repaired, still RUNNING — exactly the case the recovery sweep exists for.
    assert (await store.list_runs())[0].status is BatchStatus.RUNNING


# --------------------------------------------------------------------------- #
# The sweep (T1, half 2 / T27)
# --------------------------------------------------------------------------- #
async def _orphan_run(cfg, store, status: BatchStatus, **run_kwargs) -> JobRun:
    """Persist an instance + run in the given non-terminal status, as left by a
    worker that died without cleanup (no lock is held)."""
    instance = await store.create_job_instance(
        JobInstance(id=f"inst-{status.value}", job_name="job", identity_hash=status.value)
    )
    return await store.create_job_run(
        JobRun(
            id=f"run-{status.value}",
            instance_id=instance.id,
            job_name="job",
            status=status,
            last_updated=cfg.clock.now(),
            **run_kwargs,
        )
    )


@pytest.mark.parametrize(
    "status", [BatchStatus.RUNNING, BatchStatus.STOPPING, BatchStatus.PAUSING]
)
async def test_sweep_recovers_orphaned_run(status):
    clock = FakeClock()
    launcher, operator, cfg, store = make_harness(RecordingStep(), clock=clock)
    run = await _orphan_run(cfg, store, status)
    clock.advance(timedelta(hours=1))

    recovered = await operator.recover_stale_runs(older_than=timedelta(minutes=30))

    assert [r.id for r in recovered] == [run.id]
    fresh = await store.get_job_run(run.id)
    assert fresh.status is BatchStatus.FAILED
    assert fresh.failures[-1].exception_type == "etf.WorkerLost"
    assert (await store.get_job_instance(run.instance_id)).status is BatchStatus.FAILED
    trail = await operator.get_audit_trail(run.id)
    assert any(e.event_type is AuditEventType.RECOVERED for e in trail)

    # T27: the recovered run restarts cleanly.
    restarted = await operator.restart(run.instance_id)
    assert restarted.status is BatchStatus.COMPLETED


async def test_sweep_skips_run_whose_lock_is_held():
    clock = FakeClock()
    _, operator, cfg, store = make_harness(RecordingStep(), clock=clock)
    run = await _orphan_run(cfg, store, BatchStatus.RUNNING)
    clock.advance(timedelta(hours=1))

    lock = await cfg.lock_provider.acquire(run.instance_id, "live-worker", cfg.lock_ttl)
    assert lock is not None
    try:
        assert await operator.recover_stale_runs(older_than=timedelta(minutes=30)) == []
        assert (await store.get_job_run(run.id)).status is BatchStatus.RUNNING
    finally:
        await lock.release()


class StallOnceStep(Step):
    """The first execution stalls until released (a worker whose lease silently
    expires); later executions complete at once."""

    name = "work"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def execute(self, ctx: StepContext) -> StepResult:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
        return StepResult.completed()


def _expiring_lease_harness(*steps, **overrides):
    """A harness whose lock leases expire when the test advances ``mono[0]``."""
    mono = [1000.0]
    clock = FakeClock()
    launcher, operator, cfg, store = make_harness(
        *steps,
        clock=clock,
        lock_provider=InMemoryLockProvider(time_source=lambda: mono[0]),
        lock_ttl=timedelta(seconds=60),
        **overrides,
    )
    return launcher, operator, store, clock, mono


async def test_stale_owner_cannot_overwrite_a_recovered_run():
    work, after = StallOnceStep(), RecordingStep("after")
    # A slow control monitor isolates the write guard from the monitor's takeover check.
    launcher, operator, store, clock, mono = _expiring_lease_harness(
        work, after, control_poll_interval=timedelta(hours=1)
    )
    stale = asyncio.create_task(launcher.launch(launch_request()))
    await work.started.wait()
    run = (await store.list_runs())[0]

    mono[0] += 61  # the stalled worker's lease expires without it noticing
    clock.advance(timedelta(hours=1))
    recovered = await operator.recover_stale_runs(older_than=timedelta(minutes=30))
    assert [r.id for r in recovered] == [run.id]
    restarted = await operator.restart(run.instance_id)
    assert restarted.status is BatchStatus.COMPLETED

    work.release.set()  # the stale worker resumes and tries to finish its step
    with pytest.raises(LockAcquisitionError, match="settled as FAILED"):
        await stale

    assert (await store.get_job_run(run.id)).status is BatchStatus.FAILED
    assert after.calls == 1  # only the new owner ran the next step
    assert (await store.get_job_instance(run.instance_id)).status is BatchStatus.COMPLETED
    trail = [e.event_type for e in await operator.get_audit_trail(run.id)]
    assert AuditEventType.COMPLETED not in trail


async def test_control_monitor_cancels_a_step_once_its_run_is_taken_over():
    hang = HangingStep()
    launcher, operator, store, clock, mono = _expiring_lease_harness(
        hang, control_poll_interval=timedelta(milliseconds=10)
    )
    stale = asyncio.create_task(launcher.launch(launch_request()))
    await hang.started.wait()
    run = (await store.list_runs())[0]

    mono[0] += 61
    clock.advance(timedelta(hours=1))
    assert [r.id for r in await operator.recover_stale_runs(older_than=timedelta(minutes=30))] == [run.id]

    with pytest.raises(LockAcquisitionError):
        await asyncio.wait_for(stale, timeout=5)
    assert (await store.get_job_run(run.id)).status is BatchStatus.FAILED


async def test_sweep_leaves_the_instance_status_of_a_newer_run():
    clock = FakeClock()
    _, operator, cfg, store = make_harness(RecordingStep(), clock=clock)
    old = await _orphan_run(cfg, store, BatchStatus.RUNNING)
    clock.advance(timedelta(hours=1))
    await operator.recover_stale_runs(older_than=timedelta(minutes=30))
    newer = await operator.restart(old.instance_id)
    assert newer.status is BatchStatus.COMPLETED

    # The old run reappears as an orphan (e.g. written by a pre-fix stale worker).
    stale = await store.get_job_run(old.id)
    stale.status = BatchStatus.RUNNING
    await store.update_job_run(stale, stale.version)
    clock.advance(timedelta(hours=1))
    assert [r.id for r in await operator.recover_stale_runs(older_than=timedelta(minutes=30))] == [old.id]

    assert (await store.get_job_instance(old.instance_id)).status is BatchStatus.COMPLETED


async def test_sweep_ignores_recently_updated_runs():
    clock = FakeClock()
    _, operator, cfg, store = make_harness(RecordingStep(), clock=clock)
    run = await _orphan_run(cfg, store, BatchStatus.RUNNING)
    clock.advance(timedelta(minutes=5))  # inside the staleness window
    assert await operator.recover_stale_runs(older_than=timedelta(minutes=30)) == []
    assert (await store.get_job_run(run.id)).status is BatchStatus.RUNNING


async def test_sweep_expires_dangling_validation_request():
    from etf.model import ValidationRequest

    clock = FakeClock()
    _, operator, cfg, store = make_harness(RecordingStep(), clock=clock)
    vr = await store.create_validation_request(
        ValidationRequest(
            id="vr-1", run_id="run-RUNNING", instance_id="inst-RUNNING",
            step_name="work", reason="orphaned gate",
        )
    )
    await _orphan_run(cfg, store, BatchStatus.RUNNING, open_validation_id=vr.id)
    clock.advance(timedelta(hours=1))

    recovered = await operator.recover_stale_runs(older_than=timedelta(minutes=30))

    assert len(recovered) == 1
    assert recovered[0].open_validation_id is None
    fresh_vr = await store.get_validation_request(vr.id)
    assert fresh_vr.status is ValidationStatus.EXPIRED
    assert fresh_vr.decided_at is not None


# --------------------------------------------------------------------------- #
# Validation cleanup (T11)
# --------------------------------------------------------------------------- #
class GateStep(Step):
    name = "gate"

    def __init__(self, sla_seconds: int | None = None) -> None:
        self.sla_seconds = sla_seconds

    async def execute(self, ctx: StepContext) -> StepResult:
        await ctx.request_validation(reason="sign off", sla_seconds=self.sla_seconds)
        return StepResult.completed()


async def test_abandon_resolves_open_validation_request():
    launcher, operator, _, store = make_harness(GateStep())
    run = await launcher.launch(launch_request())
    assert run.status is BatchStatus.AWAITING_VALIDATION

    gate_id = run.open_validation_id
    assert gate_id is not None
    abandoned = await operator.abandon(run.id)

    assert abandoned.status is BatchStatus.ABANDONED
    assert abandoned.open_validation_id is None
    assert await store.list_pending_validations() == []  # nothing left PENDING
    gate = await store.get_validation_request(gate_id)
    assert gate.status is ValidationStatus.EXPIRED


async def test_expire_validations_expires_past_sla_and_stops_run():
    clock = FakeClock()
    launcher, operator, _, store = make_harness(GateStep(sla_seconds=60), clock=clock)
    run = await launcher.launch(launch_request())
    assert run.status is BatchStatus.AWAITING_VALIDATION

    clock.advance(timedelta(seconds=120))
    expired = await operator.expire_validations()

    assert [v.id for v in expired] == [run.open_validation_id]
    assert expired[0].status is ValidationStatus.EXPIRED
    fresh = await store.get_job_run(run.id)
    assert fresh.status is BatchStatus.STOPPED  # restartable, not wedged
    assert fresh.open_validation_id is None


async def test_expire_validations_leaves_future_sla_pending():
    clock = FakeClock()
    launcher, operator, _, store = make_harness(GateStep(sla_seconds=3600), clock=clock)
    run = await launcher.launch(launch_request())
    clock.advance(timedelta(seconds=60))
    assert await operator.expire_validations() == []
    assert (await store.get_job_run(run.id)).status is BatchStatus.AWAITING_VALIDATION


class ParamGateStep(Step):
    """A gate whose SLA comes from the run's parameters (None = no deadline)."""

    name = "gate"

    async def execute(self, ctx: StepContext) -> StepResult:
        await ctx.request_validation(reason="sign off", sla_seconds=ctx.params.get("sla"))
        return StepResult.completed()


async def test_expire_validations_is_not_starved_by_gates_without_a_deadline():
    clock = FakeClock()
    launcher, operator, _, store = make_harness(ParamGateStep(), clock=clock)
    for i in range(3):  # older gates that may legitimately wait forever
        await launcher.launch(launch_request(id=f"open-{i}"))
        clock.advance(timedelta(seconds=1))
    request = launch_request(id="with-sla")
    request.parameters = request.parameters.with_overrides({"sla": 60})
    overdue = await launcher.launch(request)
    clock.advance(timedelta(hours=1))

    expired = await operator.expire_validations(limit=3)

    assert [v.id for v in expired] == [overdue.open_validation_id]
    assert (await store.get_job_run(overdue.id)).status is BatchStatus.STOPPED
    trail = await operator.get_audit_trail(overdue.id)
    assert any(
        e.event_type is AuditEventType.VALIDATION_DECISION
        and e.attributes.get("resolution") == ValidationStatus.EXPIRED.value
        for e in trail
    )
    assert len(await store.list_pending_validations()) == 3  # no-deadline gates untouched


async def _stale_run(cfg, store, name: str) -> JobRun:
    instance = await store.create_job_instance(
        JobInstance(id=f"inst-{name}", job_name="job", identity_hash=name)
    )
    return await store.create_job_run(
        JobRun(
            id=f"run-{name}",
            instance_id=instance.id,
            job_name="job",
            status=BatchStatus.RUNNING,
            last_updated=cfg.clock.now(),
        )
    )


async def test_recovery_pages_past_runs_held_by_live_workers():
    clock = FakeClock()
    _, operator, cfg, store = make_harness(RecordingStep(), clock=clock)
    # Healthy long-running steps: old last_updated (the heartbeat only refreshes the
    # lock), but a live worker holds each lease.
    live = [await _stale_run(cfg, store, f"live-{i}") for i in range(3)]
    clock.advance(timedelta(minutes=1))
    await _stale_run(cfg, store, "orphan")
    clock.advance(timedelta(hours=1))
    leases = [
        await cfg.lock_provider.acquire(run.instance_id, "live-worker", cfg.lock_ttl)
        for run in live
    ]
    try:
        recovered = await operator.recover_stale_runs(
            older_than=timedelta(minutes=30), limit=2
        )
        assert [r.id for r in recovered] == ["run-orphan"]
        for run in live:
            assert (await store.get_job_run(run.id)).status is BatchStatus.RUNNING
    finally:
        for lease in leases:
            assert lease is not None
            await lease.release()


class RefusesToFailOneRun(InMemoryStateStore):
    """A store that cannot write one run's FAILED state (e.g. DocumentTooLarge)."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    async def update_job_run(self, run, expected_version):
        if run.id == self.run_id and run.status is BatchStatus.FAILED:
            raise RuntimeError("document too large")
        return await super().update_job_run(run, expected_version)


async def test_a_run_the_sweep_cannot_update_does_not_stop_it():
    clock = FakeClock()
    _, operator, cfg, store = make_harness(
        RecordingStep(), clock=clock, store=RefusesToFailOneRun("run-poison")
    )
    await _stale_run(cfg, store, "poison")  # oldest, so it comes first
    clock.advance(timedelta(minutes=1))
    await _stale_run(cfg, store, "a")
    await _stale_run(cfg, store, "b")
    clock.advance(timedelta(hours=1))

    recovered = await operator.recover_stale_runs(older_than=timedelta(minutes=30))
    assert sorted(r.id for r in recovered) == ["run-a", "run-b"]
    # A later sweep skips it again without failing.
    assert await operator.recover_stale_runs(older_than=timedelta(minutes=30)) == []

    assert (await store.get_job_run("run-poison")).status is BatchStatus.RUNNING
    assert (await store.get_job_run("run-a")).status is BatchStatus.FAILED
    assert (await store.get_job_run("run-b")).status is BatchStatus.FAILED


# --------------------------------------------------------------------------- #
# Stack-trace hygiene (T20)
# --------------------------------------------------------------------------- #
class ExplodingStep(Step):
    name = "explode"

    async def execute(self, ctx: StepContext) -> StepResult:
        raise RuntimeError("boom with hunter2 inside")


async def test_stack_trace_is_scrubbed_then_truncated():
    launcher, _, _, store = make_harness(
        ExplodingStep(),
        max_stack_trace_length=80,
        stack_trace_scrubber=lambda trace: trace.replace("hunter2", "[redacted]"),
    )
    run = await launcher.launch(launch_request())
    assert run.status is BatchStatus.FAILED
    trace = (await store.get_job_run(run.id)).failures[0].stack_trace
    assert "hunter2" not in trace
    assert trace.startswith("Traceback")
    # The end, where the raised exception is, survives truncation.
    assert trace.endswith("Error: boom with [redacted] inside\n")
    assert "characters truncated" in trace
    assert len(trace) <= 80 + len("\n... [99999 characters truncated] ...\n")
