"""A failed run always records why: earlier attempts survive a crash, an audit outage
cannot leave the run RUNNING, a concurrent stop does not hide the failure, step code
calling ``sys.exit`` fails the step instead of the event loop, cancellations say why,
a transient lock-refresh error does not abandon the run — and every stored failure
text is scrubbed and bounded."""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

import pytest

from etf import (
    AsyncBridge,
    BatchStatus,
    BlockingStep,
    JobOperator,
    Step,
    StepContext,
    StepResult,
)
from etf.locking import InMemoryLockProvider
from etf.policies import BackoffRetryPolicy
from etf.status import AuditEventType
from etf.stores.memory import InMemoryStateStore
from helpers import launch_request, make_harness

NO_RETRY = BackoffRetryPolicy(max_attempts=1)


def fast_retries(attempts: int) -> BackoffRetryPolicy:
    return BackoffRetryPolicy(max_attempts=attempts, base_delay=timedelta(0))


class FailsThenHangs(Step):
    name = "fails_then_hangs"

    def __init__(self) -> None:
        self.hanging = asyncio.Event()

    async def execute(self, ctx: StepContext) -> StepResult:
        if ctx.attempt == 1:
            raise ValueError("payment API returned 502")
        self.hanging.set()
        await asyncio.Event().wait()
        return StepResult.completed()


async def test_crash_keeps_failures_recorded_by_earlier_attempts():
    step = FailsThenHangs()
    launcher, _, _, store = make_harness(step, retry_policy=fast_retries(3))
    launch = asyncio.create_task(launcher.launch(launch_request()))
    await step.hanging.wait()

    launch.cancel("host shutting down")
    with pytest.raises(asyncio.CancelledError):
        await launch

    [run] = await store.list_runs()
    assert run.status is BatchStatus.FAILED
    assert [(f.exception_type, f.message) for f in run.failures] == [
        ("builtins.ValueError", "payment API returned 502"),
        ("asyncio.exceptions.CancelledError", "host shutting down"),
    ]
    [step_run] = await store.find_step_runs(run.id)
    assert [f.exception_type for f in step_run.failures] == [
        "builtins.ValueError",
        "asyncio.exceptions.CancelledError",
    ]


class FailedEventsRefused(InMemoryStateStore):
    """An audit store that cannot take the run-level FAILED event."""

    async def append_audit(self, event):
        if event.event_type is AuditEventType.FAILED:
            raise ConnectionError("audit collection write failed")
        return await super().append_audit(event)


class Fails(Step):
    name = "fails"

    async def execute(self, ctx: StepContext) -> StepResult:
        raise ValueError("the real business reason")


async def test_audit_outage_while_failing_still_marks_the_run_failed(caplog):
    launcher, _, _, store = make_harness(
        Fails(), retry_policy=NO_RETRY, store=FailedEventsRefused()
    )

    with pytest.raises(ConnectionError):
        await launcher.launch(launch_request())

    [run] = await store.list_runs()
    assert run.status is BatchStatus.FAILED  # not left RUNNING for the sweep
    assert run.failures[0].message == "the real business reason"
    assert "marked FAILED without its audit event" in caplog.text


class StopsItselfThenFails(Step):
    name = "stops_then_fails"

    def __init__(self) -> None:
        self.operator: JobOperator | None = None

    async def execute(self, ctx: StepContext) -> StepResult:
        assert self.operator is not None
        await self.operator.stop(ctx.run_id)  # a stop request lands mid-failure
        raise ValueError("ledger mismatch")


async def test_failure_is_kept_when_a_stop_request_lands_at_the_same_time():
    step = StopsItselfThenFails()
    launcher, operator, _, store = make_harness(step, retry_policy=NO_RETRY)
    step.operator = operator

    run = await launcher.launch(launch_request())

    assert run.status is BatchStatus.FAILED
    assert [r.id for r in await store.list_runs(status=BatchStatus.FAILED)] == [run.id]
    failed = [
        e for e in await operator.get_audit_trail(run.id)
        if e.event_type is AuditEventType.FAILED
    ]
    assert failed[-1].message == "builtins.ValueError: ledger mismatch"
    assert failed[-1].step_name == "stops_then_fails"
    assert failed[-1].attributes == {"halt_requested": "STOPPING"}


class ExitsAsync(Step):
    name = "exits"

    async def execute(self, ctx: StepContext) -> StepResult:
        sys.exit(2)


class ExitsInThread(BlockingStep):
    name = "exits_in_thread"

    def execute_sync(self, ctx: StepContext) -> StepResult:
        sys.exit("usage: tool [-h]")


@pytest.mark.parametrize(
    ("step", "message"),
    [
        (ExitsAsync(), "step code raised SystemExit(2)"),
        (ExitsInThread(), "step code raised SystemExit('usage: tool [-h]')"),
    ],
)
async def test_step_calling_sys_exit_fails_the_step_not_the_event_loop(step, message):
    launcher, _, _, _ = make_harness(step, retry_policy=NO_RETRY)

    run = await launcher.launch(launch_request())

    assert run.status is BatchStatus.FAILED
    assert (run.failures[-1].exception_type, run.failures[-1].message) == (
        "etf.exceptions.StepExitError",
        message,
    )


class Hangs(Step):
    name = "hangs"

    async def execute(self, ctx: StepContext) -> StepResult:
        await asyncio.Event().wait()
        return StepResult.completed()


def test_bridge_timeout_is_recorded_as_the_reason():
    launcher, _, _, store = make_harness(Hangs())
    bridge = AsyncBridge(cancel_grace=0.5).start()
    try:
        with pytest.raises(TimeoutError):
            bridge.run(launcher.launch(launch_request()), timeout=0.1)
        [run] = bridge.run(store.list_runs())
    finally:
        bridge.close()
    assert run.status is BatchStatus.FAILED
    assert run.failures[-1].message == "AsyncBridge.run timed out after 0.1s"


class FlakyRefreshLocks(InMemoryLockProvider):
    """The first lease refresh errors (a store blip); later ones succeed."""

    def __init__(self) -> None:
        super().__init__()
        self.refresh_errors = 1

    async def _refresh(self, key, owner, lease_id, ttl):
        if self.refresh_errors:
            self.refresh_errors -= 1
            raise ConnectionError("primary stepped down")
        return await super()._refresh(key, owner, lease_id, ttl)


class Slow(Step):
    name = "slow"

    async def execute(self, ctx: StepContext) -> StepResult:
        await asyncio.sleep(0.5)
        return StepResult.completed()


async def test_transient_lock_refresh_error_does_not_abandon_the_run():
    locks = FlakyRefreshLocks()
    launcher, _, _, _ = make_harness(
        Slow(), lock_provider=locks, lock_ttl=timedelta(seconds=0.3)
    )

    run = await launcher.launch(launch_request())

    assert locks.refresh_errors == 0  # the error happened, and was retried
    assert run.status is BatchStatus.COMPLETED


class Unprintable(Exception):
    def __str__(self) -> str:
        raise RuntimeError("broken __str__")


class RaisesUnprintable(Step):
    name = "raises_unprintable"

    async def execute(self, ctx: StepContext) -> StepResult:
        raise Unprintable()


async def test_exception_with_a_broken_str_is_still_recorded():
    launcher, _, _, _ = make_harness(RaisesUnprintable(), retry_policy=NO_RETRY)

    run = await launcher.launch(launch_request())

    assert run.status is BatchStatus.FAILED
    assert run.failures[-1].message == "<unprintable Unprintable>"


class LeaksASecret(Step):
    name = "leaks"

    async def execute(self, ctx: StepContext) -> StepResult:
        try:
            raise RuntimeError("inner call used token=SECRET")
        except RuntimeError as exc:
            raise ValueError("upstream rejected token=SECRET: " + "x" * 10_000) from exc


async def test_every_failure_text_is_scrubbed_and_bounded():
    launcher, operator, _, store = make_harness(
        LeaksASecret(),
        retry_policy=NO_RETRY,
        stack_trace_scrubber=lambda text: text.replace("SECRET", "[redacted]"),
        max_failure_message_length=200,
    )

    run = await launcher.launch(launch_request())

    failure = (await store.get_job_run(run.id)).failures[-1]
    texts = [failure.message, failure.stack_trace, *failure.cause_chain]
    assert all("SECRET" not in text for text in texts)
    assert failure.cause_chain == ["RuntimeError: inner call used token=[redacted]"]
    assert failure.message.startswith("upstream rejected token=[redacted]")
    assert failure.message.endswith("x")
    assert len(failure.message) < 200 + 60
    events = await operator.get_audit_trail(run.id)
    assert all("SECRET" not in e.message for e in events)
    assert all(
        "SECRET" not in e.failure.message for e in events if e.failure is not None
    )


class AlwaysFails(Step):
    name = "always_fails"

    async def execute(self, ctx: StepContext) -> StepResult:
        raise ConnectionError(f"attempt {ctx.attempt}")


async def test_failures_per_record_keep_the_first_and_most_recent():
    launcher, operator, _, store = make_harness(
        AlwaysFails(), retry_policy=fast_retries(30), max_failures_per_record=5
    )

    run = await launcher.launch(launch_request())

    run = await store.get_job_run(run.id)
    [step_run] = await store.find_step_runs(run.id)
    for failures in (run.failures, step_run.failures):
        assert [f.attempt for f in failures] == [1, 27, 28, 29, 30]
    step_failed = [
        e for e in await operator.get_audit_trail(run.id)
        if e.event_type is AuditEventType.STEP_FAILED
    ]
    assert len(step_failed) == 30  # the audit trail keeps every one
