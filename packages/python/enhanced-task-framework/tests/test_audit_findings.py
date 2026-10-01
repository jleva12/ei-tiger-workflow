"""Regression tests for the concurrency and processing defects found in the audit.

These tests intentionally exercise boundaries that the original suite did not cover:
pre-start bridge cancellation, cancellation of threaded work, crash windows around
PENDING runs, delivery idempotency, definition upgrades, transaction callback usage,
lease loss, identity integrity, control-monitor resilience, and audit commit ordering.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from etf import (
    Actor,
    AuditEventType,
    BackoffRetryPolicy,
    BatchStatus,
    BlockingStep,
    EtfConfig,
    EtfError,
    JobDefinition,
    JobLauncher,
    JobOperator,
    JobParameters,
    JobRegistry,
    LaunchRequest,
    LockAcquisitionError,
    RunNotRestartableError,
    Step,
    StepContext,
    StepResult,
    ValidationDecision,
)
from etf.audit import AuditQuery, AuditSink, CompositeAuditSink, StoreBackedAuditSink
from etf.exceptions import DefinitionVersionMismatchError, JobInstanceAlreadyCompleteError
from etf.bridge import AsyncBridge
from etf.locking import InMemoryLockProvider, Lock, RunLockProvider
from etf.model import JobInstance, JobRun, compute_identity_hash
from etf.serialization import JsonSerializer
from etf.status import ValidationStatus
from etf.stores.memory import InMemoryStateStore
from helpers import FakeClock, RecordingStep, launch_request, make_cfg, make_harness


def test_bridge_timeout_cancels_a_coroutine_that_has_not_started():
    """A timed-out submission must never start later when the loop becomes available."""
    bridge = AsyncBridge().start()
    loop_blocked = threading.Event()
    release_loop = threading.Event()
    executed = threading.Event()

    async def block_event_loop() -> None:
        loop_blocked.set()
        release_loop.wait(2)

    async def late_command() -> None:
        executed.set()

    asyncio.run_coroutine_threadsafe(block_event_loop(), bridge._loop)
    assert loop_blocked.wait(1)
    try:
        with pytest.raises(TimeoutError):
            bridge.run(late_command(), timeout=0.02)
        release_loop.set()
        time.sleep(0.1)
        assert not executed.is_set()
    finally:
        release_loop.set()
        bridge.close()


class ControlledBlockingStep(BlockingStep):
    name = "controlled_blocking"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def execute_sync(self, ctx: StepContext) -> StepResult:
        self.started.set()
        self.release.wait(2)
        self.finished.set()
        return StepResult.completed()


async def test_cancelling_blocking_step_keeps_lock_until_sync_body_exits():
    """Cancellation must not release the instance lock around still-running work."""
    step = ControlledBlockingStep()
    launcher, _, cfg, store = make_harness(step)
    task = asyncio.create_task(launcher.launch(launch_request()))
    assert await asyncio.to_thread(step.started.wait, 1)
    run = (await store.list_runs())[0]

    task.cancel()
    await asyncio.sleep(0.03)
    replacement = await cfg.lock_provider.acquire(
        run.instance_id, "replacement-worker", cfg.lock_ttl
    )
    try:
        assert replacement is None
        assert not task.done()
    finally:
        if replacement is not None:
            await replacement.release()
        step.release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert step.finished.is_set()
    fresh_run = await store.get_job_run(run.id)
    step_run = (await store.find_step_runs(run.id))[0]
    assert fresh_run.status is BatchStatus.FAILED
    assert step_run.status is BatchStatus.FAILED


async def _seed_pending_run(cfg: EtfConfig, store: InMemoryStateStore, *, key: str) -> JobRun:
    parameters = JobParameters(identifying={"id": "pending"})
    identity = cfg.instance_resolver.resolve_identity("job", parameters)
    instance = await store.create_job_instance(
        JobInstance(
            id=f"instance-{key}",
            job_name="job",
            identity_hash=identity,
            identifying_parameters=dict(parameters.identifying),
            status=BatchStatus.RUNNING,
        )
    )
    run = await store.create_job_run(
        JobRun(
            id=f"run-{key}",
            instance_id=instance.id,
            job_name="job",
            parameters=parameters,
            status=BatchStatus.PENDING,
            idempotency_key=key,
            last_updated=cfg.clock.now(),
        )
    )
    await store.register_idempotency_key(key, run.id, ttl=timedelta(days=1))
    return run


async def test_idempotent_redelivery_advances_an_existing_pending_run():
    step = RecordingStep()
    cfg, store = make_cfg(step)
    seeded = await _seed_pending_run(cfg, store, key="pending-key")

    result = await JobLauncher(cfg).launch(
        LaunchRequest(
            "job",
            JobParameters(identifying={"id": "pending"}),
            idempotency_key="pending-key",
        )
    )

    assert result.id == seeded.id
    assert result.status is BatchStatus.COMPLETED
    assert step.calls == 1


async def test_recovery_sweep_reclaims_stale_pending_runs():
    clock = FakeClock()
    cfg, store = make_cfg(RecordingStep(), clock=clock)
    run = await _seed_pending_run(cfg, store, key="stale-pending")
    clock.advance(timedelta(hours=1))

    recovered = await JobOperator(cfg).recover_stale_runs(
        older_than=timedelta(minutes=30)
    )

    assert [item.id for item in recovered] == [run.id]
    assert (await store.get_job_run(run.id)).status is BatchStatus.FAILED


async def test_idempotent_redelivery_of_failed_run_is_blocked_not_reported_as_success():
    cfg, store = make_cfg(RecordingStep())
    run = await _seed_pending_run(cfg, store, key="failed-key")
    run.status = BatchStatus.FAILED
    await store.update_job_run(run, run.version)

    with pytest.raises(RunNotRestartableError):
        await JobLauncher(cfg).launch(
            LaunchRequest(
                "job",
                JobParameters(identifying={"id": "pending"}),
                idempotency_key="failed-key",
            )
        )


async def test_reusing_an_idempotency_key_for_a_different_payload_is_rejected():
    cfg, _ = make_cfg(RecordingStep())
    launcher = JobLauncher(cfg)
    first = await launcher.launch(
        LaunchRequest(
            "job",
            JobParameters(identifying={"id": "one"}),
            idempotency_key="same-key",
        )
    )
    assert first.status is BatchStatus.COMPLETED

    with pytest.raises(EtfError, match="different launch request"):
        await launcher.launch(
            LaunchRequest(
                "job",
                JobParameters(identifying={"id": "two"}),
                idempotency_key="same-key",
            )
        )


class GateStep(Step):
    name = "gate"

    async def execute(self, ctx: StepContext) -> StepResult:
        await ctx.request_validation(reason="approve")
        return StepResult.completed()


async def test_duplicate_validation_delivery_is_idempotent():
    launcher, operator, _, store = make_harness(GateStep())
    paused = await launcher.launch(launch_request())
    request_id = paused.open_validation_id
    assert request_id is not None

    decision = ValidationDecision.approve(Actor.human("approver"), comment="yes")
    first = await operator.submit_validation_decision(request_id, decision)
    duplicate = await operator.submit_validation_decision(
        request_id,
        ValidationDecision.approve(Actor.human("approver"), comment="yes"),
    )

    assert first.status is BatchStatus.COMPLETED
    assert duplicate.id == first.id
    assert duplicate.status is BatchStatus.COMPLETED
    request = await store.get_validation_request(request_id)
    assert request.status is ValidationStatus.APPROVED


async def test_conflicting_duplicate_validation_delivery_is_rejected():
    launcher, operator, _, _ = make_harness(GateStep())
    paused = await launcher.launch(launch_request())
    request_id = paused.open_validation_id
    assert request_id is not None

    await operator.submit_validation_decision(
        request_id,
        ValidationDecision.approve(Actor.human("approver"), comment="yes"),
    )
    with pytest.raises(EtfError):
        await operator.submit_validation_decision(
            request_id,
            ValidationDecision.approve(Actor.human("another"), comment="yes"),
        )


class RetryThenSucceed(Step):
    name = "retry_then_succeed"

    def __init__(self) -> None:
        self.calls = 0
        self.failed_once = asyncio.Event()

    async def execute(self, ctx: StepContext) -> StepResult:
        self.calls += 1
        if self.calls == 1:
            self.failed_once.set()
            raise RuntimeError("retry")
        return StepResult.completed()


async def test_stop_during_retry_backoff_prevents_another_attempt():
    step = RetryThenSucceed()
    launcher, operator, _, store = make_harness(
        step,
        retry_policy=BackoffRetryPolicy(
            max_attempts=3, base_delay=timedelta(seconds=0.25)
        ),
        control_poll_interval=timedelta(milliseconds=5),
    )
    task = asyncio.create_task(launcher.launch(launch_request()))
    await step.failed_once.wait()
    run = (await store.list_runs())[0]
    await operator.stop(run.id)

    result = await task
    assert result.status is BatchStatus.STOPPED
    assert step.calls == 1


class RequiredPrerequisite(Step):
    name = "required_prerequisite"

    async def execute(self, ctx: StepContext) -> StepResult:
        raise RuntimeError("prerequisite failed")


async def test_restart_from_step_cannot_bypass_failed_prerequisite():
    pre = RecordingStep("pre")
    post = RecordingStep("post")
    launcher, operator, _, _ = make_harness(pre, RequiredPrerequisite(), post)
    failed = await launcher.launch(launch_request())
    assert failed.status is BatchStatus.FAILED

    with pytest.raises(RunNotRestartableError):
        await operator.restart(failed.instance_id, from_step="post")
    assert post.calls == 0


class TransactionCallbackStore(InMemoryStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.callback_transactions = 0

    async def run_in_transaction(self, fn, *, attempts: int = 3):
        self.callback_transactions += 1
        async with self.transaction():
            return await fn()


async def test_orchestration_uses_retryable_transaction_callback_api():
    store = TransactionCallbackStore()
    cfg, _ = make_cfg(RecordingStep(), store=store)
    result = await JobLauncher(cfg).launch(launch_request())
    assert result.status is BatchStatus.COMPLETED
    assert store.callback_transactions > 0


class ReplayFirstTransactionStore(InMemoryStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = True
        self.replay_count = 0

    async def run_in_transaction(self, fn, *, attempts: int = 3):
        for attempt in range(attempts):
            try:
                async with self.transaction():
                    result = await fn()
                    if self.fail_next:
                        self.fail_next = False
                        self.replay_count += 1
                        raise RuntimeError("simulated transaction replay")
                    return result
            except RuntimeError as exc:
                if str(exc) != "simulated transaction replay" or attempt == attempts - 1:
                    raise


async def test_launch_callback_is_safe_when_a_transaction_body_is_replayed():
    store = ReplayFirstTransactionStore()
    cfg, _ = make_cfg(RecordingStep(), store=store)

    result = await JobLauncher(cfg).launch(launch_request(key="replay"))

    assert result.status is BatchStatus.COMPLETED
    assert store.replay_count == 1
    assert len(await store.list_runs()) == 1


async def test_validation_callback_is_safe_when_transaction_body_is_replayed():
    store = ReplayFirstTransactionStore()
    cfg, _ = make_cfg(GateStep(), store=store)
    launcher = JobLauncher(cfg)
    operator = JobOperator(cfg)
    paused = await launcher.launch(launch_request(key="validation-replay"))
    request_id = paused.open_validation_id
    assert request_id is not None

    store.fail_next = True
    result = await operator.submit_validation_decision(
        request_id,
        ValidationDecision.approve(Actor.human("approver")),
    )

    assert result.status is BatchStatus.COMPLETED
    assert store.replay_count == 2
    assert (
        await store.get_validation_request(request_id)
    ).status is ValidationStatus.APPROVED


class ReplayPauseTransactionStore(InMemoryStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.request_replay = False
        self.replayed = False

    async def create_validation_request(self, request):
        created = await super().create_validation_request(request)
        if not self.replayed:
            self.request_replay = True
        return created

    async def run_in_transaction(self, fn, *, attempts: int = 3):
        if self._tx_owner is asyncio.current_task():
            return await fn()
        for attempt in range(attempts):
            try:
                async with self.transaction():
                    result = await fn()
                    if self.request_replay:
                        self.request_replay = False
                        self.replayed = True
                        raise RuntimeError("replay pause transaction")
                    return result
            except RuntimeError as exc:
                if str(exc) != "replay pause transaction" or attempt == attempts - 1:
                    raise


async def test_pause_callback_is_safe_when_transaction_body_is_replayed():
    store = ReplayPauseTransactionStore()
    cfg, _ = make_cfg(GateStep(), store=store)

    paused = await JobLauncher(cfg).launch(launch_request())

    assert paused.status is BatchStatus.AWAITING_VALIDATION
    assert store.replayed is True
    assert len(await store.list_pending_validations()) == 1
    step_run = (await store.find_step_runs(paused.id))[0]
    assert step_run.status is BatchStatus.PAUSED


async def test_new_definition_version_does_not_repeat_completed_work():
    first_step = RecordingStep("work")
    cfg, _ = make_cfg(first_step)
    launcher = JobLauncher(cfg)
    first = await launcher.launch(launch_request(key="v1"))
    assert first.status is BatchStatus.COMPLETED

    second_step = RecordingStep("work")
    cfg.registry.register(JobDefinition("job", [second_step], version=2))
    # The same unit of work, delivered again after the upgrade, is not re-run.
    with pytest.raises(JobInstanceAlreadyCompleteError):
        await launcher.launch(launch_request(key="v2"))
    # New work runs on the latest version.
    second = await launcher.launch(launch_request(key="v2-new", id="2"))

    assert second.status is BatchStatus.COMPLETED
    assert second.instance_id != first.instance_id
    assert first.definition_version == 1
    assert second.definition_version == 2
    assert (await cfg.store.get_job_instance(second.instance_id)).definition_version == 2
    assert (first_step.calls, second_step.calls) == (1, 1)


class VersionedGateStep(Step):
    name = "gate"

    def __init__(self, version: int) -> None:
        self.version = version
        self.completed = 0

    async def execute(self, ctx: StepContext) -> StepResult:
        await ctx.request_validation(reason="approve")
        self.completed += 1
        return StepResult.completed()


async def test_paused_run_finishes_on_its_own_version_after_an_upgrade():
    v1, v2 = VersionedGateStep(1), VersionedGateStep(2)
    launcher, operator, cfg, store = make_harness(v1)
    paused = await launcher.launch(launch_request())
    request_id = paused.open_validation_id
    assert request_id is not None

    cfg.registry.register(JobDefinition("job", [v2], version=2))
    run = await operator.submit_validation_decision(
        request_id, ValidationDecision.approve(Actor.human("approver"))
    )

    assert run.status is BatchStatus.COMPLETED
    assert run.definition_version == 1
    assert (v1.completed, v2.completed) == (1, 0)


async def test_paused_run_refuses_a_decision_once_its_version_is_unregistered():
    launcher, _, cfg, store = make_harness(VersionedGateStep(1))
    paused = await launcher.launch(launch_request())
    request_id = paused.open_validation_id
    assert request_id is not None

    upgraded = JobRegistry()  # a deploy that registers only version 2
    upgraded.register(JobDefinition("job", [VersionedGateStep(2)], version=2))
    operator = JobOperator(
        EtfConfig(
            store=store,
            audit=StoreBackedAuditSink(store),
            lock_provider=cfg.lock_provider,
            registry=upgraded,
        )
    )
    with pytest.raises(DefinitionVersionMismatchError, match="version 1 is not registered"):
        await operator.submit_validation_decision(
            request_id, ValidationDecision.approve(Actor.human("approver"))
        )
    assert (
        await store.get_validation_request(request_id)
    ).status is ValidationStatus.PENDING


class LeaseLostLock(Lock):
    async def refresh(self, ttl: timedelta) -> bool:
        return False

    async def release(self) -> None:
        return None


class LeaseLostProvider(RunLockProvider):
    async def acquire(
        self, key: str, owner: str, ttl: timedelta
    ) -> Lock | None:
        return LeaseLostLock()


class CancellableHangingStep(Step):
    name = "cancellable_hang"

    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def execute(self, ctx: StepContext) -> StepResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


async def test_lease_loss_cancels_the_active_async_step_promptly():
    step = CancellableHangingStep()
    cfg, store = make_cfg(
        step,
        lock_provider=LeaseLostProvider(),
        lock_ttl=timedelta(milliseconds=30),
    )

    with pytest.raises(LockAcquisitionError):
        await asyncio.wait_for(
            JobLauncher(cfg).launch(launch_request()), timeout=0.5
        )
    assert step.cancelled.is_set()
    run = (await store.list_runs())[0]
    assert run.status is BatchStatus.RUNNING

    recovered = await JobOperator(cfg).recover_stale_runs(older_than=timedelta(0))
    assert [item.id for item in recovered] == [run.id]
    assert (await store.get_job_run(run.id)).status is BatchStatus.FAILED


def test_identifying_parameters_are_immutable_and_json_strict():
    parameters = JobParameters(
        identifying={"tenant": "A", "scope": {"regions": ["east"]}}
    )
    before = compute_identity_hash("job", parameters)
    with pytest.raises(TypeError):
        parameters.identifying["tenant"] = "B"
    with pytest.raises(TypeError):
        parameters.identifying["scope"]["regions"] = ["west"]
    with pytest.raises(AttributeError):
        parameters.identifying["scope"]["regions"].append("west")
    assert compute_identity_hash("job", parameters) == before

    with pytest.raises(TypeError):
        JobParameters(identifying={"at": datetime.now(timezone.utc)}).identity_payload()
    with pytest.raises(TypeError):
        JsonSerializer().serialize({"at": datetime.now(timezone.utc)})


class OneMonitorReadFailureStore(InMemoryStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.failed = False
        self.failure_observed = asyncio.Event()

    async def get_job_run(self, run_id: str):
        if self.armed and not self.failed:
            self.failed = True
            self.failure_observed.set()
            raise RuntimeError("transient monitor read failure")
        return await super().get_job_run(run_id)


class CooperativeWaitStep(Step):
    name = "cooperative_wait"

    def __init__(self, store: OneMonitorReadFailureStore) -> None:
        self.store = store
        self.observed_stop = False

    async def execute(self, ctx: StepContext) -> StepResult:
        self.store.armed = True
        for _ in range(250):
            if ctx.should_stop():
                from etf import StopExecution

                self.observed_stop = True
                raise StopExecution("stop observed")
            await asyncio.sleep(0.004)
        return StepResult.completed()


async def test_control_monitor_recovers_after_a_transient_store_read_error():
    store = OneMonitorReadFailureStore()
    step = CooperativeWaitStep(store)
    launcher, operator, _, _ = make_harness(
        step,
        store=store,
        control_poll_interval=timedelta(milliseconds=3),
    )
    task = asyncio.create_task(launcher.launch(launch_request()))
    await asyncio.wait_for(store.failure_observed.wait(), timeout=1)
    run = (await store.list_runs())[0]
    await operator.stop(run.id)

    result = await task
    assert result.status is BatchStatus.STOPPED
    assert step.observed_stop is True


async def test_recovery_settles_the_active_step_run_too():
    from etf.model import StepRun

    clock = FakeClock()
    cfg, store = make_cfg(RecordingStep(), clock=clock)
    run = await _seed_pending_run(cfg, store, key="active-step")
    run.status = BatchStatus.RUNNING
    run = await store.update_job_run(run, run.version)
    await store.create_step_run(
        StepRun(
            id="active-step-run",
            run_id=run.id,
            instance_id=run.instance_id,
            step_name="work",
            status=BatchStatus.RUNNING,
        )
    )
    clock.advance(timedelta(hours=1))

    await JobOperator(cfg).recover_stale_runs(older_than=timedelta(minutes=30))

    step_run = await store.get_step_run("active-step-run")
    assert step_run.status is BatchStatus.FAILED
    assert step_run.end_time is not None
    assert step_run.failures[-1].exception_type == "etf.WorkerLost"


class CommitFailureStore(InMemoryStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_current_commit = False

    async def append_audit(self, event) -> None:
        await super().append_audit(event)
        if event.event_type is AuditEventType.STEP_STARTED:
            self.fail_current_commit = True

    async def run_in_transaction(self, fn, *, attempts: int = 3):
        async with self.transaction():
            result = await fn()
            if self.fail_current_commit:
                self.fail_current_commit = False
                raise RuntimeError("simulated commit failure")
            return result


class ExternalAuditRecorder(AuditSink):
    def __init__(self, store: CommitFailureStore) -> None:
        self.store = store
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)

    async def query(self, query: AuditQuery):
        return list(self.events)


async def test_external_audit_events_are_not_published_before_state_commit():
    store = CommitFailureStore()
    recorder = ExternalAuditRecorder(store)
    registry = JobRegistry()
    registry.register(JobDefinition("job", [RecordingStep()]))
    cfg = EtfConfig(
        store=store,
        audit=CompositeAuditSink(StoreBackedAuditSink(store), recorder),
        lock_provider=InMemoryLockProvider(),
        registry=registry,
    )

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await JobLauncher(cfg).launch(launch_request())

    persisted = await store.query_audit(AuditQuery(limit=10_000))
    persisted_ids = {event.id for event in persisted}
    assert {event.id for event in recorder.events} <= persisted_ids
    assert AuditEventType.STEP_STARTED not in {
        event.event_type for event in recorder.events
    }


async def test_in_memory_store_matches_transaction_and_step_uniqueness_contracts():
    from etf.model import StepRun

    store = InMemoryStateStore()
    assert store.supports_transactions is True
    await store.create_step_run(
        StepRun(id="one", run_id="run", instance_id="instance", step_name="step")
    )
    with pytest.raises(Exception):
        await store.create_step_run(
            StepRun(
                id="two",
                run_id="run",
                instance_id="instance",
                step_name="step",
            )
        )


class AttributeStep(Step):
    name = "attributes"

    async def execute(self, ctx: StepContext) -> StepResult:
        return StepResult(
            write_count=1,
            attributes={"provider": "example", "cost": 3},
        )


async def test_step_result_attributes_are_persisted_and_audited():
    launcher, operator, _, store = make_harness(AttributeStep())
    run = await launcher.launch(launch_request())
    step_run = (await store.find_step_runs(run.id))[0]
    assert step_run.attributes == {"provider": "example", "cost": 3}

    completed = [
        event
        for event in await operator.get_audit_trail(run.id)
        if event.event_type is AuditEventType.STEP_COMPLETED
    ]
    assert completed[0].attributes == step_run.attributes

