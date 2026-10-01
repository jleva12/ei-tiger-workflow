from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from etf import (
    Actor,
    AuditEventType,
    BatchStatus,
    EtfConfig,
    JobDefinition,
    JobLauncher,
    JobOperator,
    JobParameters,
    JobRegistry,
    LaunchRequest,
    LimitedSkipPolicy,
    LockAcquisitionError,
    Serializer,
    Step,
    StepContext,
    StepResult,
    StopExecution,
    ValidationAuthorizationError,
    ValidationDecision,
    ValidationSchemaError,
)
from etf.audit import AuditQuery, AuditSink, CompositeAuditSink, StoreBackedAuditSink
from etf.locking import InMemoryLockProvider, Lock, RunLockProvider
from etf.model import FailureRecord
from etf.policies import (
    BackoffRetryPolicy,
    JobInstanceResolver,
    RetryDecision,
    RetryPolicy,
)
from etf.stores.memory import InMemoryStateStore


class RecordingStep(Step):
    def __init__(self, name: str = "work") -> None:
        self.name = name
        self.calls = 0

    async def execute(self, ctx: StepContext) -> StepResult:
        self.calls += 1
        return StepResult.completed()


def make_config(step: Step, **overrides) -> tuple[EtfConfig, InMemoryStateStore]:
    store = InMemoryStateStore()
    registry = JobRegistry()
    definition_options = overrides.pop("definition_options", {})
    registry.register(JobDefinition("job", [step], **definition_options))
    lock_provider = overrides.pop("lock_provider", InMemoryLockProvider())
    cfg = EtfConfig(
        store=store,
        audit=StoreBackedAuditSink(store),
        lock_provider=lock_provider,
        registry=registry,
        **overrides,
    )
    return cfg, store


def request(*, key: str | None = None, identity: str = "1") -> LaunchRequest:
    return LaunchRequest(
        "job", JobParameters(identifying={"id": identity}), idempotency_key=key
    )


class NeverRetry(RetryPolicy):
    def should_retry(self, failure: FailureRecord, attempt: int) -> RetryDecision:
        return RetryDecision(False)


class AttemptStep(Step):
    name = "attempt"

    def __init__(self, succeed_on: int | None = None) -> None:
        self.seen: list[int] = []
        self.succeed_on = succeed_on

    async def execute(self, ctx: StepContext) -> StepResult:
        self.seen.append(ctx.attempt)
        if self.succeed_on == ctx.attempt:
            return StepResult.completed()
        raise RuntimeError("retry me")


async def test_job_policy_override_and_one_indexed_attempts():
    never = AttemptStep()
    cfg, _ = make_config(
        never,
        definition_options={"retry_policy": NeverRetry()},
        retry_policy=BackoffRetryPolicy(base_delay=timedelta(0)),
    )
    run = await JobLauncher(cfg).launch(request(identity="never"))
    assert run.status is BatchStatus.FAILED
    assert never.seen == [1]

    third = AttemptStep(succeed_on=3)
    cfg, _ = make_config(
        third,
        retry_policy=BackoffRetryPolicy(max_attempts=3, base_delay=timedelta(0)),
    )
    run = await JobLauncher(cfg).launch(request(identity="third"))
    assert run.status is BatchStatus.COMPLETED
    assert third.seen == [1, 2, 3]


class AlwaysFail(Step):
    name = "skip"

    async def execute(self, ctx: StepContext) -> StepResult:
        raise ValueError("skip")


async def test_skip_count_is_incremented_once():
    exception_type = f"{ValueError.__module__}.{ValueError.__qualname__}"
    cfg, store = make_config(
        AlwaysFail(),
        skip_policy=LimitedSkipPolicy(1, frozenset({exception_type})),
    )
    run = await JobLauncher(cfg).launch(request())
    step_run = (await store.find_step_runs(run.id))[0]
    assert run.status is BatchStatus.COMPLETED
    assert step_run.skip_count == 1


class StringSerializer(Serializer):
    def serialize(self, value: object) -> object:
        return json.dumps(value)

    def deserialize(self, raw: object) -> object:
        return json.loads(raw) if isinstance(raw, str) else raw


class CheckpointStep(Step):
    name = "checkpoint"

    async def execute(self, ctx: StepContext) -> StepResult:
        if not ctx.get("saved"):
            ctx.put("saved", True)
            await ctx.checkpoint()
        return StepResult.completed()


class LabelledCheckpointStep(Step):
    name = "labelled"

    async def execute(self, ctx: StepContext) -> StepResult:
        ctx.put("turn", 1)
        await ctx.checkpoint("Triage the defect (triage)")
        await ctx.checkpoint()
        return StepResult.completed()


async def test_a_checkpoint_message_is_on_its_audit_event():
    cfg, store = make_config(LabelledCheckpointStep())
    run = await JobLauncher(cfg).launch(request())
    events = await store.query_audit(
        AuditQuery(run_id=run.id, event_types=[AuditEventType.CHECKPOINT_SAVED])
    )
    messages = [event.message for event in events]
    assert run.status is BatchStatus.COMPLETED
    # The step's two, in order; the framework's own at the step's end says nothing.
    assert messages[:2] == ["Triage the defect (triage)", ""]
    assert set(messages[2:]) <= {""}
    assert {event.step_name for event in events} == {"labelled"}


async def test_custom_serializer_is_used_on_load_and_save():
    cfg, store = make_config(CheckpointStep(), serializer=StringSerializer())
    run = await JobLauncher(cfg).launch(request())
    persisted = (await store.find_step_runs(run.id))[0]
    assert run.status is BatchStatus.COMPLETED
    assert isinstance(persisted.execution_context, str)
    assert json.loads(persisted.execution_context)["saved"] is True


class CooperativeStep(Step):
    name = "cooperative"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0

    async def execute(self, ctx: StepContext) -> StepResult:
        self.calls += 1
        self.started.set()
        for _ in range(200):
            if ctx.should_stop():
                raise StopExecution("control signal")
            await asyncio.sleep(0.001)
        return StepResult.completed()


async def test_active_stop_is_cooperative_and_does_not_conflict():
    step = CooperativeStep()
    cfg, store = make_config(
        step, control_poll_interval=timedelta(milliseconds=2)
    )
    task = asyncio.create_task(JobLauncher(cfg).launch(request()))
    await step.started.wait()
    run = (await store.list_runs())[0]
    await JobOperator(cfg).stop(run.id)
    stopped = await task
    instance = await store.get_job_instance(stopped.instance_id)
    assert stopped.status is BatchStatus.STOPPED
    assert instance.status is BatchStatus.STOPPED


async def test_active_pause_can_resume_from_the_interrupted_step():
    step = CooperativeStep()
    cfg, store = make_config(
        step, control_poll_interval=timedelta(milliseconds=2)
    )
    task = asyncio.create_task(JobLauncher(cfg).launch(request()))
    await step.started.wait()
    run = (await store.list_runs())[0]
    await JobOperator(cfg).pause(run.id)
    paused = await task
    assert paused.status is BatchStatus.PAUSED
    resumed = await JobOperator(cfg).resume(paused.id)
    assert resumed.status is BatchStatus.COMPLETED
    assert step.calls == 2


class NonCooperativeStep(Step):
    name = "non_cooperative"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def execute(self, ctx: StepContext) -> StepResult:
        self.started.set()
        await asyncio.sleep(0.05)
        return StepResult.completed()


async def test_stop_wins_race_with_non_cooperative_step_completion():
    step = NonCooperativeStep()
    cfg, store = make_config(
        step, control_poll_interval=timedelta(milliseconds=2)
    )
    task = asyncio.create_task(JobLauncher(cfg).launch(request()))
    await step.started.wait()
    run = (await store.list_runs())[0]
    await JobOperator(cfg).stop(run.id)
    stopped = await task
    assert stopped.status is BatchStatus.STOPPED
    assert (await store.get_job_instance(stopped.instance_id)).status is BatchStatus.STOPPED


class ValidationStep(Step):
    name = "validation"

    async def execute(self, ctx: StepContext) -> StepResult:
        await ctx.request_validation(
            reason="restricted",
            required_role="approver",
            decision_schema={
                "type": "object",
                "properties": {"decision": {"enum": ["APPROVE"]}},
            },
        )
        return StepResult.completed()


async def test_validation_enforces_role_and_schema_without_consuming_gate():
    cfg, store = make_config(ValidationStep())
    run = await JobLauncher(cfg).launch(request())
    assert run.open_validation_id is not None
    gate = await store.get_validation_request(run.open_validation_id)

    with pytest.raises(ValidationAuthorizationError):
        await JobOperator(cfg).submit_validation_decision(
            gate.id, ValidationDecision.approve(Actor.human("unauthorized"))
        )
    gate = await store.get_validation_request(gate.id)
    assert gate.status.value == "PENDING"

    with pytest.raises(ValidationSchemaError):
        await JobOperator(cfg).submit_validation_decision(
            gate.id,
            ValidationDecision.reject(
                Actor.human("approver", roles={"approver"})
            ),
        )
    gate = await store.get_validation_request(gate.id)
    assert gate.status.value == "PENDING"

    completed = await JobOperator(cfg).submit_validation_decision(
        gate.id,
        ValidationDecision.approve(Actor.human("approver", roles={"approver"})),
    )
    assert completed.status is BatchStatus.COMPLETED


class BlockingStep(Step):
    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def execute(self, ctx: StepContext) -> StepResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return StepResult.completed()


async def test_concurrent_same_key_returns_single_run():
    step = BlockingStep()
    cfg, store = make_config(step)
    launcher = JobLauncher(cfg)
    first_task = asyncio.create_task(launcher.launch(request(key="same")))
    await step.started.wait()
    duplicate = await launcher.launch(request(key="same"))
    step.release.set()
    first = await first_task
    assert duplicate.id == first.id
    assert step.calls == 1
    assert len(await store.list_runs()) == 1


async def test_concurrent_different_keys_cannot_create_duplicate_attempts():
    step = BlockingStep()
    cfg, store = make_config(step)
    launcher = JobLauncher(cfg)
    first_task = asyncio.create_task(launcher.launch(request(key="first")))
    await step.started.wait()
    with pytest.raises(LockAcquisitionError):
        await launcher.launch(request(key="second"))
    step.release.set()
    await first_task
    assert step.calls == 1
    assert len(await store.list_runs()) == 1


class CountingLock(Lock):
    def __init__(self, inner: Lock) -> None:
        self.inner = inner
        self.refreshes = 0

    async def refresh(self, ttl: timedelta) -> bool:
        self.refreshes += 1
        return await self.inner.refresh(ttl)

    async def release(self) -> None:
        await self.inner.release()


class CountingLockProvider(RunLockProvider):
    def __init__(self) -> None:
        self.inner = InMemoryLockProvider()
        self.last_lock: CountingLock | None = None

    async def acquire(self, key: str, owner: str, ttl: timedelta) -> Lock | None:
        inner = await self.inner.acquire(key, owner, ttl)
        if inner is None:
            return None
        self.last_lock = CountingLock(inner)
        return self.last_lock


class SlowStep(Step):
    name = "slow"

    async def execute(self, ctx: StepContext) -> StepResult:
        await asyncio.sleep(0.12)
        return StepResult.completed()


async def test_long_run_refreshes_lock_lease():
    provider = CountingLockProvider()
    cfg, _ = make_config(
        SlowStep(), lock_provider=provider, lock_ttl=timedelta(milliseconds=60)
    )
    run = await JobLauncher(cfg).launch(request())
    assert run.status is BatchStatus.COMPLETED
    assert provider.last_lock is not None
    assert provider.last_lock.refreshes >= 2


class FailOnStepStarted(AuditSink):
    async def emit(self, event) -> None:
        if event.event_type is AuditEventType.STEP_STARTED:
            raise RuntimeError("audit unavailable")

    async def query(self, query: AuditQuery):
        return []


async def test_external_audit_failure_does_not_roll_back_committed_state():
    step = RecordingStep()
    cfg, store = make_config(step)
    cfg.audit = CompositeAuditSink(StoreBackedAuditSink(store), FailOnStepStarted())
    result = await JobLauncher(cfg).launch(request())
    run = (await store.list_runs())[0]
    step_run = (await store.find_step_runs(run.id))[0]
    trail = await store.query_audit(AuditQuery(run_id=run.id, limit=100))
    assert result.status is BatchStatus.COMPLETED
    assert step_run.status is BatchStatus.COMPLETED
    assert AuditEventType.STEP_STARTED in {event.event_type for event in trail}


class FailOnceResolver(JobInstanceResolver):
    def __init__(self) -> None:
        self.failed = False

    def resolve_identity(self, job_name: str, parameters: JobParameters) -> str:
        if not self.failed:
            self.failed = True
            raise RuntimeError("resolver failed")
        return "resolved"


async def test_failed_setup_releases_idempotency_reservation():
    cfg, _ = make_config(RecordingStep(), instance_resolver=FailOnceResolver())
    launcher = JobLauncher(cfg)
    with pytest.raises(RuntimeError, match="resolver failed"):
        await launcher.launch(request(key="retryable"))
    run = await launcher.launch(request(key="retryable"))
    assert run.status is BatchStatus.COMPLETED
