"""No task is lost when its worker goes away: a delivery that finds a run nobody owns
settles it instead of reporting success, and a run whose worker died or was cancelled
is restarted — by its redelivery, or by ``JobOperator.redrive`` for hosts without
redelivery — up to ``max_auto_redrives`` times in a row. A run that failed on its own
merits still needs an explicit restart."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from etf import (
    BatchStatus,
    JobInstance,
    JobParameters,
    JobRun,
    RunNotRestartableError,
    Step,
    StepContext,
    StepResult,
)
from etf.policies import BackoffRetryPolicy
from etf.status import ActorKind, AuditEventType
from helpers import FakeClock, RecordingStep, launch_request, make_harness


class HangsUntilReleased(Step):
    """Hangs on every execution until ``release`` is set; counts executions."""

    name = "work"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def execute(self, ctx: StepContext) -> StepResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return StepResult.completed()


async def _launch_then_cancel(launcher, step: HangsUntilReleased, request) -> None:
    """A worker is shut down mid-step (its task is cancelled)."""
    step.started.clear()
    task = asyncio.create_task(launcher.launch(request))
    await step.started.wait()
    task.cancel("worker shutting down")
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_redelivery_restarts_a_run_whose_worker_was_cancelled():
    step = HangsUntilReleased()
    launcher, operator, _, store = make_harness(step)
    request = launch_request(key="delivery-1")
    await _launch_then_cancel(launcher, step, request)
    [first] = await store.list_runs()
    assert first.status is BatchStatus.FAILED

    step.release.set()
    redelivered = await launcher.launch(request)  # the queue redelivers the message

    assert redelivered.status is BatchStatus.COMPLETED
    assert redelivered.id != first.id and redelivered.attempt == 2
    restarted = [
        e for e in await operator.get_audit_trail(redelivered.id)
        if e.event_type is AuditEventType.RESTARTED
    ]
    assert restarted[0].attributes["redrive"] == "asyncio.exceptions.CancelledError"
    assert restarted[0].actor.kind is ActorKind.SYSTEM
    # A further duplicate is served by the new attempt, not re-run.
    again = await launcher.launch(request)
    assert again.id == redelivered.id and step.calls == 2


async def _seed_orphan(cfg, store, status: BatchStatus = BatchStatus.RUNNING) -> JobRun:
    """A run left by a worker that died: active status, nobody holds the lock."""
    parameters = JobParameters(identifying={"id": "1"})
    identity = cfg.instance_resolver.resolve_identity("job", parameters)
    instance = await store.create_job_instance(
        JobInstance(
            id="inst-orphan",
            job_name="job",
            identity_hash=identity,
            identifying_parameters={"id": "1"},
            status=status,
        )
    )
    return await store.create_job_run(
        JobRun(
            id="run-orphan",
            instance_id=instance.id,
            job_name="job",
            parameters=parameters,
            status=status,
            current_step="work",
            last_updated=cfg.clock.now(),
        )
    )


async def test_redelivery_settles_and_restarts_a_run_whose_worker_died():
    step = RecordingStep("work")
    launcher, _, cfg, store = make_harness(step)
    orphan = await _seed_orphan(cfg, store)

    result = await launcher.launch(launch_request())  # redelivery, lock is free

    assert result.status is BatchStatus.COMPLETED and result.attempt == 2
    settled = await store.get_job_run(orphan.id)
    assert settled.status is BatchStatus.FAILED
    assert settled.failures[-1].exception_type == "etf.WorkerLost"
    assert step.calls == 1


async def test_operator_redrive_restarts_what_the_sweep_recovered():
    clock = FakeClock()
    step = RecordingStep("work")
    _, operator, cfg, store = make_harness(step, clock=clock)
    orphan = await _seed_orphan(cfg, store)
    clock.advance(timedelta(hours=1))

    [recovered] = await operator.recover_stale_runs(older_than=timedelta(minutes=30))
    assert recovered.id == orphan.id
    redriven = await operator.redrive(recovered.instance_id)

    assert redriven.status is BatchStatus.COMPLETED and step.calls == 1
    with pytest.raises(RunNotRestartableError):  # nothing left to re-drive
        await operator.redrive(recovered.instance_id)


class FailsOnItsMerits(Step):
    name = "work"

    async def execute(self, ctx: StepContext) -> StepResult:
        raise ValueError("invalid order")


async def test_a_business_failure_is_not_restarted_by_redelivery():
    launcher, operator, _, store = make_harness(
        FailsOnItsMerits(), retry_policy=BackoffRetryPolicy(max_attempts=1)
    )
    request = launch_request(key="delivery-1")
    failed = await launcher.launch(request)
    assert failed.status is BatchStatus.FAILED

    with pytest.raises(RunNotRestartableError, match="use JobOperator.restart"):
        await launcher.launch(request)
    with pytest.raises(RunNotRestartableError):
        await operator.redrive(failed.instance_id)
    assert len(await store.list_runs()) == 1


async def test_automatic_redrives_stop_after_max_auto_redrives_in_a_row():
    step = HangsUntilReleased()
    launcher, operator, _, store = make_harness(step, max_auto_redrives=1)
    request = launch_request(key="delivery-1")
    await _launch_then_cancel(launcher, step, request)  # interrupted once
    await _launch_then_cancel(launcher, step, request)  # re-driven, interrupted again

    with pytest.raises(RunNotRestartableError, match="max_auto_redrives=1"):
        await launcher.launch(request)
    assert len(await store.list_runs()) == 2

    # An explicit restart is still possible.
    step.release.set()
    [latest] = [r for r in await store.list_runs() if r.attempt == 2]
    assert (await operator.restart(latest.instance_id)).status is BatchStatus.COMPLETED


class FlakyUpstream(Exception):
    """A failure the host classifies as retryable (a rate limit, say)."""


class FailsUntilFixed(Step):
    name = "work"

    def __init__(self) -> None:
        self.error: Exception | None = FlakyUpstream("429 from the provider")

    async def execute(self, ctx: StepContext) -> StepResult:
        if self.error is not None:
            raise self.error
        return StepResult.completed()


def mark_flaky_retryable(exc: BaseException, record) -> None:
    record.retryable = isinstance(exc, FlakyUpstream)
    record.attributes["classified"] = True


async def test_a_retryable_failure_is_restarted_by_its_redelivery():
    step = FailsUntilFixed()
    launcher, _, _, store = make_harness(
        step,
        retry_policy=BackoffRetryPolicy(max_attempts=1),
        failure_classifier=mark_flaky_retryable,
    )
    request = launch_request(key="delivery-1")
    failed = await launcher.launch(request)
    assert failed.status is BatchStatus.FAILED
    assert failed.failures[-1].retryable is True
    assert failed.failures[-1].attributes == {"classified": True}

    step.error = None
    redelivered = await launcher.launch(request)
    assert redelivered.status is BatchStatus.COMPLETED and redelivered.attempt == 2

    # A failure the classifier leaves non-retryable still needs an explicit restart.
    step.error = ValueError("bad input")
    other = launch_request(key="delivery-2", id="2")
    assert (await launcher.launch(other)).status is BatchStatus.FAILED
    with pytest.raises(RunNotRestartableError):
        await launcher.launch(other)
