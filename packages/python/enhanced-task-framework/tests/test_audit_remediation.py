"""Regression coverage for the concurrency remediation tracked in task.md.

This file was added after the original 137-test suite was baselined. Existing tests
remain untouched so these cases add constraints without weakening prior behavior.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta

import pytest

from etf import (
    Actor,
    BackoffRetryPolicy,
    BatchStatus,
    BlockingStep,
    EtfConfig,
    JobDefinition,
    JobInstance,
    JobLauncher,
    JobOperator,
    JobParameters,
    JobRegistry,
    JobRun,
    Step,
    StepContext,
    StepResult,
    ValidationDecision,
    ValidationStateError,
)
from etf.audit import StoreBackedAuditSink
from etf.bridge import AsyncBridge
from etf.exceptions import RunNotRestartableError
from etf.locking import InMemoryLockProvider
from etf.status import ValidationStatus
from etf.stores.memory import InMemoryStateStore
from helpers import (
    FakeClock,
    RecordingStep,
    launch_request,
    make_cfg,
    make_harness,
)


# --------------------------------------------------------------------------- #
# R1 — retry context isolation
# --------------------------------------------------------------------------- #
class MutateThenRetryStep(Step):
    name = "mutate_then_retry"

    def __init__(self, *, checkpoint_first: bool = False) -> None:
        self.checkpoint_first = checkpoint_first
        self.observed: list[dict[str, object]] = []

    async def execute(self, ctx: StepContext) -> StepResult:
        self.observed.append(
            {
                "step_durable": ctx.get("step_durable"),
                "step_volatile": ctx.get("step_volatile"),
                "run_durable": ctx.run_context.get("run_durable"),
                "run_volatile": ctx.run_context.get("run_volatile"),
            }
        )
        if ctx.attempt == 1:
            if self.checkpoint_first:
                ctx.put("step_durable", "kept")
                ctx.run_context.put("run_durable", "kept")
                await ctx.checkpoint()
            ctx.put("step_volatile", "discard")
            ctx.run_context.put("run_volatile", "discard")
            raise RuntimeError("retry after mutating context")
        return StepResult.completed()


async def test_retry_discards_uncheckpointed_context_mutations():
    step = MutateThenRetryStep()
    cfg, store = make_cfg(
        step,
        retry_policy=BackoffRetryPolicy(
            max_attempts=2,
            base_delay=timedelta(0),
        ),
    )

    run = await JobLauncher(cfg).launch(launch_request())

    assert run.status is BatchStatus.COMPLETED
    assert step.observed[1] == {
        "step_durable": None,
        "step_volatile": None,
        "run_durable": None,
        "run_volatile": None,
    }
    persisted = (await store.find_step_runs(run.id))[0].execution_context
    assert "step_volatile" not in persisted


async def test_retry_preserves_mutations_that_were_checkpointed_before_failure():
    step = MutateThenRetryStep(checkpoint_first=True)
    cfg, _ = make_cfg(
        step,
        retry_policy=BackoffRetryPolicy(
            max_attempts=2,
            base_delay=timedelta(0),
        ),
    )

    run = await JobLauncher(cfg).launch(launch_request())

    assert run.status is BatchStatus.COMPLETED
    assert step.observed[1] == {
        "step_durable": "kept",
        "step_volatile": None,
        "run_durable": "kept",
        "run_volatile": None,
    }


class NestedCursorStep(Step):
    """Advances nested cursors in place, never checkpointing past the start."""

    name = "nested_cursor"

    def __init__(self) -> None:
        self.observed: list[tuple[int, int]] = []

    async def execute(self, ctx: StepContext) -> StepResult:
        if ctx.get("cursor") is None:
            ctx.put("cursor", {"offset": 0})
            ctx.run_context.put("totals", {"processed": 0})
            await ctx.checkpoint()
        cursor = ctx.get("cursor")
        totals = ctx.run_context.get("totals")
        self.observed.append((cursor["offset"], totals["processed"]))
        cursor["offset"] += 10
        totals["processed"] += 10
        raise RuntimeError("failed before the next checkpoint")


async def test_retry_discards_uncheckpointed_nested_mutations():
    step = NestedCursorStep()
    cfg, store = make_cfg(
        step,
        retry_policy=BackoffRetryPolicy(max_attempts=3, base_delay=timedelta(0)),
    )

    run = await JobLauncher(cfg).launch(launch_request())

    assert run.status is BatchStatus.FAILED
    assert step.observed == [(0, 0), (0, 0), (0, 0)]
    # What a restart resumes from is the checkpoint, not a failed attempt's progress.
    stored_run = await store.get_job_run(run.id)
    stored_step = (await store.find_step_runs(run.id))[0]
    assert stored_step.execution_context == {"cursor": {"offset": 0}}
    assert stored_run.execution_context == {"totals": {"processed": 0}}


# --------------------------------------------------------------------------- #
# R2/R3/R4 — validation state-machine invariants
# --------------------------------------------------------------------------- #
class SwallowPauseAndSucceedStep(Step):
    name = "swallow_pause"

    async def execute(self, ctx: StepContext) -> StepResult:
        try:
            await ctx.request_validation(
                reason="approval is mandatory",
                required_role="approver",
            )
        except Exception:  # noqa: BLE001 - deliberately reproduces the misuse
            pass
        return StepResult.completed()


async def test_swallowed_pause_followed_by_success_still_pauses():
    launcher, _, _, store = make_harness(SwallowPauseAndSucceedStep())

    run = await launcher.launch(launch_request())

    assert run.status is BatchStatus.AWAITING_VALIDATION
    pending = await store.list_pending_validations()
    assert len(pending) == 1
    step_run = (await store.find_step_runs(run.id))[0]
    assert step_run.status is BatchStatus.PAUSED


class RestrictedGateStep(Step):
    name = "restricted_gate"

    def __init__(self, *, sla_seconds: int | None = None) -> None:
        self.sla_seconds = sla_seconds

    async def execute(self, ctx: StepContext) -> StepResult:
        await ctx.request_validation(
            reason="restricted approval",
            required_role="approver",
            sla_seconds=self.sla_seconds,
        )
        return StepResult.completed()


async def test_plain_resume_cannot_override_rejected_validation():
    launcher, operator, _, store = make_harness(RestrictedGateStep())
    paused = await launcher.launch(launch_request())
    assert paused.open_validation_id is not None
    stopped = await operator.submit_validation_decision(
        paused.open_validation_id,
        ValidationDecision.reject(
            Actor.human("reviewer", roles={"approver"})
        ),
    )

    with pytest.raises(ValidationStateError, match="rejected|resolved"):
        await operator.resume(stopped.id)

    assert (await store.get_job_run(stopped.id)).status is BatchStatus.STOPPED


async def test_plain_resume_cannot_override_expired_validation():
    clock = FakeClock()
    launcher, operator, _, store = make_harness(
        RestrictedGateStep(sla_seconds=30),
        clock=clock,
    )
    paused = await launcher.launch(launch_request())
    clock.advance(timedelta(minutes=1))
    await operator.expire_validations()
    stopped = await store.get_job_run(paused.id)

    with pytest.raises(ValidationStateError, match="expired|resolved"):
        await operator.resume(stopped.id)

    assert (await store.get_job_run(stopped.id)).status is BatchStatus.STOPPED


class NonAtomicResumeFaultStore(InMemoryStateStore):
    """Inject a failure after a validation request commits but before its run resumes."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.failed = False

    @property
    def supports_transactions(self) -> bool:
        return False

    async def run_in_transaction(self, fn, *, attempts: int = 3):
        return await fn()

    def defer_after_commit(self, callback) -> bool:
        return False

    async def update_job_run(self, run: JobRun, expected_version: int) -> JobRun:
        if (
            self.armed
            and not self.failed
            and run.status is BatchStatus.RUNNING
            and run.open_validation_id is None
        ):
            self.failed = True
            raise RuntimeError("injected non-transactional resume failure")
        return await super().update_job_run(run, expected_version)


async def test_duplicate_decision_repairs_partial_non_transactional_resume():
    store = NonAtomicResumeFaultStore()
    cfg, _ = make_cfg(RestrictedGateStep(), store=store)
    launcher = JobLauncher(cfg)
    operator = JobOperator(cfg)
    paused = await launcher.launch(launch_request())
    request_id = paused.open_validation_id
    assert request_id is not None
    decision = ValidationDecision.approve(
        Actor.human("reviewer", roles={"approver"})
    )
    store.armed = True

    with pytest.raises(RuntimeError, match="injected"):
        await operator.submit_validation_decision(request_id, decision)

    request = await store.get_validation_request(request_id)
    wedged = await store.get_job_run(paused.id)
    assert request.status is ValidationStatus.APPROVED
    assert wedged.status is BatchStatus.AWAITING_VALIDATION

    repaired = await operator.submit_validation_decision(
        request_id,
        ValidationDecision.approve(
            Actor.human("reviewer", roles={"approver"})
        ),
    )
    assert repaired.status is BatchStatus.COMPLETED


# --------------------------------------------------------------------------- #
# R5/R6 — stale recovery and exhaustive launch states
# --------------------------------------------------------------------------- #
async def _seed_run(
    cfg: EtfConfig,
    store: InMemoryStateStore,
    *,
    suffix: str,
    status: BatchStatus,
    age: timedelta,
) -> JobRun:
    parameters = JobParameters(identifying={"id": suffix})
    identity = cfg.instance_resolver.resolve_identity("job", parameters)
    now = cfg.clock.now()
    instance = await store.create_job_instance(
        JobInstance(
            id=f"instance-{suffix}",
            job_name="job",
            identity_hash=identity,
            identifying_parameters=dict(parameters.identifying),
            status=status,
            created_at=now - age,
            updated_at=now - age,
        )
    )
    return await store.create_job_run(
        JobRun(
            id=f"run-{suffix}",
            instance_id=instance.id,
            job_name="job",
            parameters=parameters,
            status=status,
            create_time=now - age,
            last_updated=now - age,
        )
    )


async def test_recovery_limit_does_not_hide_older_stale_run():
    clock = FakeClock()
    cfg, store = make_cfg(RecordingStep(), clock=clock)
    stale = await _seed_run(
        cfg,
        store,
        suffix="old",
        status=BatchStatus.RUNNING,
        age=timedelta(hours=2),
    )
    await _seed_run(
        cfg,
        store,
        suffix="new",
        status=BatchStatus.RUNNING,
        age=timedelta(0),
    )

    recovered = await JobOperator(cfg).recover_stale_runs(
        older_than=timedelta(minutes=30),
        limit=1,
    )

    assert [run.id for run in recovered] == [stale.id]
    assert (await store.get_job_run(stale.id)).status is BatchStatus.FAILED


@pytest.mark.parametrize(
    "status",
    [BatchStatus.STOPPING, BatchStatus.PAUSING],
)
async def test_launch_does_not_create_duplicate_attempt_for_transitional_run(
    status: BatchStatus,
):
    cfg, store = make_cfg(RecordingStep())
    existing = await _seed_run(
        cfg,
        store,
        suffix=status.value.lower(),
        status=status,
        age=timedelta(hours=1),
    )
    request = launch_request(
        key=f"new-{status.value}",
        id=status.value.lower(),
    )

    # Nobody holds the instance lock, so the run is orphaned mid-halt: the launch
    # settles it instead of returning it as accepted, and — a halt was requested —
    # does not restart it.
    with pytest.raises(RunNotRestartableError):
        await JobLauncher(cfg).launch(request)

    settled = await store.get_job_run(existing.id)
    assert settled.status is BatchStatus.FAILED
    assert settled.failures[-1].attributes == {"orphaned_in": status.value}
    assert len(await store.find_runs_for_instance(existing.instance_id)) == 1


# --------------------------------------------------------------------------- #
# R7/R8 — bridge
# --------------------------------------------------------------------------- #
class ControlledSlowBlockingStep(BlockingStep):
    name = "controlled_slow_blocking"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def execute_sync(self, ctx: StepContext) -> StepResult:
        self.started.set()
        self.release.wait(2)
        self.finished.set()
        return StepResult.completed()


def test_bridge_timeout_waits_for_blocking_work_to_unwind():
    step = ControlledSlowBlockingStep()
    cfg, store = make_cfg(step)
    bridge = AsyncBridge(cancel_grace=0.02).start()
    bridge.run(store.initialize())
    timer = threading.Timer(0.15, step.release.set)
    timer.start()
    try:
        with pytest.raises(TimeoutError):
            bridge.run(
                JobLauncher(cfg).launch(launch_request()),
                timeout=0.02,
            )
        finished_when_timeout_returned = step.finished.is_set()
        run = bridge.run(store.list_runs())[0]
        assert finished_when_timeout_returned is True
        assert run.status is BatchStatus.FAILED
    finally:
        step.release.set()
        timer.join(timeout=1)
        bridge.close()


def test_bridge_finalizer_runs_after_inflight_cleanup():
    bridge = AsyncBridge().start()
    started = threading.Event()
    events: list[str] = []
    outcome: dict[str, BaseException] = {}

    async def inflight() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cleanup")

    async def finalizer() -> None:
        events.append("finalizer")

    def submit() -> None:
        try:
            bridge.run(inflight())
        except BaseException as exc:  # noqa: BLE001 - asserting shutdown propagation
            outcome["error"] = exc

    thread = threading.Thread(target=submit)
    thread.start()
    assert started.wait(1)
    try:
        bridge.close(finalizer=finalizer)
    finally:
        bridge.close()
    thread.join(timeout=2)

    assert events == ["cleanup", "finalizer"]
    assert isinstance(outcome.get("error"), asyncio.CancelledError)


# --------------------------------------------------------------------------- #
# R11 — production transaction requirement
# --------------------------------------------------------------------------- #
class NonTransactionalStore(InMemoryStateStore):
    @property
    def supports_transactions(self) -> bool:
        return False


def test_config_can_require_transactional_store():
    store = NonTransactionalStore()
    registry = JobRegistry()
    registry.register(JobDefinition("job", [RecordingStep()]))

    with pytest.raises(ValueError, match="transaction"):
        EtfConfig(
            store=store,
            audit=StoreBackedAuditSink(store),
            lock_provider=InMemoryLockProvider(),
            registry=registry,
            require_transactional_store=True,
        )
