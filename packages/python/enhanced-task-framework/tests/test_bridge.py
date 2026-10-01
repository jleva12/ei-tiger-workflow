"""AsyncBridge behavior (audit T3/T13/T26): timeout leaves the run recoverable,
close() drains in-flight work, and a closed bridge refuses further use."""

from __future__ import annotations

import asyncio
import threading

import pytest

from etf import BatchStatus, JobLauncher, Step, StepContext, StepResult
from etf.bridge import AsyncBridge
from helpers import launch_request, make_cfg


class HangingStep(Step):
    name = "hang"

    def __init__(self) -> None:
        self.started = threading.Event()  # readable from the test (worker) thread
        self.hang = True

    async def execute(self, ctx: StepContext) -> StepResult:
        if self.hang:
            self.started.set()
            await asyncio.Event().wait()
        return StepResult.completed()


def test_run_timeout_leaves_run_failed_not_running():
    """The T3 headline: a timed-out bridge call must not strand the run RUNNING."""
    step = HangingStep()
    cfg, store = make_cfg(step)
    bridge = AsyncBridge().start()
    try:
        bridge.run(store.initialize())
        with pytest.raises(TimeoutError):
            bridge.run(JobLauncher(cfg).launch(launch_request()), timeout=0.3)

        # run() cancelled and drained the coroutine before raising, so the
        # fail-fast transition has already been persisted and the lock released.
        run = bridge.run(store.list_runs())[0]
        assert run.status is BatchStatus.FAILED

        step.hang = False
        from etf import JobOperator

        restarted = bridge.run(JobOperator(cfg).restart(run.instance_id))
        assert restarted.status is BatchStatus.COMPLETED
    finally:
        bridge.close()


def test_close_drains_inflight_work():
    step = HangingStep()
    cfg, store = make_cfg(step)
    bridge = AsyncBridge().start()
    bridge.run(store.initialize())

    outcome: dict[str, object] = {}

    def worker() -> None:
        try:
            outcome["result"] = bridge.run(JobLauncher(cfg).launch(launch_request()))
        except BaseException as exc:  # noqa: BLE001 - recording for assertions
            outcome["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    assert step.started.wait(timeout=5)

    bridge.close()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), asyncio.CancelledError)

    # The drained run was fail-fasted to FAILED before the loop stopped.
    run = asyncio.run(store.list_runs())[0]
    assert run.status is BatchStatus.FAILED


def test_closed_bridge_refuses_start_and_run():
    bridge = AsyncBridge().start()
    bridge.close()
    bridge.close()  # idempotent
    with pytest.raises(RuntimeError, match="bridge closed"):
        bridge.start()
    coro = asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError, match="bridge closed"):
            bridge.run(coro)
    finally:
        coro.close()


def test_run_passes_results_and_exceptions_through():
    bridge = AsyncBridge().start()
    try:
        async def add(a: int, b: int) -> int:
            return a + b

        assert bridge.run(add(2, 3)) == 5

        async def boom() -> None:
            raise ValueError("business error")

        # A coroutine's own exception is not an interruption: no cancel/drain involved.
        with pytest.raises(ValueError, match="business error"):
            bridge.run(boom())
    finally:
        bridge.close()
