"""The lifecycle, end to end, against every store: the in-memory reference store and
``BeanieStateStore`` (over mongomock-motor, without transactions). Divergence between
the two would mean the tests pass on a store production does not use."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from etf import (
    Actor,
    BatchStatus,
    JobInstance,
    JobParameters,
    JobRun,
    Step,
    StepContext,
    StepResult,
    ValidationDecision,
)
from etf.policies import BackoffRetryPolicy
from etf.status import ValidationStatus
from etf.stores.memory import InMemoryStateStore
from helpers import FakeClock, launch_request, make_harness


async def memory_store():
    return InMemoryStateStore()


async def mongo_store():
    pytest.importorskip("beanie")
    mongomock_motor = pytest.importorskip("mongomock_motor")
    from etf.stores.beanie_store import BeanieStateStore

    store = BeanieStateStore(
        "mongodb://mock",
        f"etf_lifecycle_{uuid.uuid4().hex}",
        client=mongomock_motor.AsyncMongoMockClient(),
        use_transactions=False,  # mongomock has no replica-set transactions
    )
    await store.initialize()
    return store


@pytest.fixture(params=[memory_store, mongo_store], ids=["memory", "mongo"])
def new_store(request):
    return request.param


class Produce(Step):
    name = "produce"

    async def execute(self, ctx: StepContext) -> StepResult:
        ctx.put("cursor", {"offset": 10})
        await ctx.checkpoint()
        ctx.run_context.put("customer", {"id": "C-42"})
        return StepResult.completed()


class Consume(Step):
    name = "consume"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[object] = []

    async def execute(self, ctx: StepContext) -> StepResult:
        self.seen.append(ctx.run_context.get("customer"))
        if self.fail:
            raise ConnectionError("downstream unavailable")
        return StepResult.completed()


class Gate(Step):
    name = "gate"

    def __init__(self, sla_seconds: int | None = None) -> None:
        self.sla_seconds = sla_seconds

    async def execute(self, ctx: StepContext) -> StepResult:
        outcome = await ctx.request_validation(reason="approve", sla_seconds=self.sla_seconds)
        ctx.put("approved_by", outcome.actor_id)
        return StepResult.completed()


async def test_run_completes_with_checkpoints_and_step_outputs(new_store):
    consume = Consume()
    launcher, operator, _, store = make_harness(Produce(), consume, store=await new_store())

    run = await launcher.launch(launch_request(key="k1"))

    assert run.status is BatchStatus.COMPLETED
    assert consume.seen == [{"id": "C-42"}]
    steps = {s.step_name: s for s in (await operator.get_status(run.id)).steps}
    assert steps["produce"].execution_context == {"cursor": {"offset": 10}}
    assert (await store.get_job_instance(run.instance_id)).status is BatchStatus.COMPLETED
    # A duplicate delivery of completed work returns it rather than re-running it.
    assert (await launcher.launch(launch_request(key="k1"))).id == run.id


async def test_gate_pauses_then_completes_on_approval(new_store):
    launcher, operator, _, store = make_harness(Gate(), store=await new_store())

    paused = await launcher.launch(launch_request())
    assert paused.status is BatchStatus.AWAITING_VALIDATION
    run = await operator.submit_validation_decision(
        paused.open_validation_id, ValidationDecision.approve(Actor.human("lead"))
    )

    assert run.status is BatchStatus.COMPLETED
    [step] = await store.find_step_runs(run.id)
    assert step.execution_context["approved_by"] == "lead"


async def test_failure_is_recorded_and_restart_resumes_with_context(new_store):
    consume = Consume(fail=True)
    launcher, operator, _, store = make_harness(
        Produce(),
        consume,
        store=await new_store(),
        retry_policy=BackoffRetryPolicy(max_attempts=2, base_delay=timedelta(0)),
    )

    failed = await launcher.launch(launch_request())
    assert failed.status is BatchStatus.FAILED
    step = next(s for s in await store.find_step_runs(failed.id) if s.step_name == "consume")
    assert (step.status, step.attempt, len(step.failures)) == (BatchStatus.FAILED, 2, 2)

    consume.fail = False
    restarted = await operator.restart(failed.instance_id, actor=Actor.human("lead"))

    assert restarted.status is BatchStatus.COMPLETED
    assert consume.seen[-1] == {"id": "C-42"}  # carried over from the skipped step


async def test_orphaned_run_is_recovered_then_redriven(new_store):
    clock = FakeClock()
    launcher, operator, cfg, store = make_harness(Consume(), store=await new_store(), clock=clock)
    parameters = JobParameters(identifying={"id": "1"})
    instance = await store.create_job_instance(
        JobInstance(
            id="inst", job_name="job",
            identity_hash=cfg.instance_resolver.resolve_identity("job", parameters),
            identifying_parameters={"id": "1"}, status=BatchStatus.RUNNING,
        )
    )
    await store.create_job_run(
        JobRun(id="orphan", instance_id=instance.id, job_name="job", parameters=parameters,
               status=BatchStatus.RUNNING, last_updated=clock.now())
    )
    clock.advance(timedelta(hours=1))

    [recovered] = await operator.recover_stale_runs(older_than=timedelta(minutes=30))
    assert recovered.status is BatchStatus.FAILED
    assert recovered.failures[-1].exception_type == "etf.WorkerLost"
    redriven = await operator.redrive(instance.id)

    assert redriven.status is BatchStatus.COMPLETED and redriven.attempt == 2


async def test_overdue_approval_expires(new_store):
    clock = FakeClock()
    launcher, operator, _, store = make_harness(
        Gate(sla_seconds=60), store=await new_store(), clock=clock
    )
    paused = await launcher.launch(launch_request())
    clock.advance(timedelta(minutes=5))

    [expired] = await operator.expire_validations()

    assert expired.id == paused.open_validation_id
    assert expired.status is ValidationStatus.EXPIRED
    assert (await store.get_job_run(paused.id)).status is BatchStatus.STOPPED
