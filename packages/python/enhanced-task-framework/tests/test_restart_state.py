"""State that must survive failures and restarts: a failed step's own record (failures,
attempts, exit status, end time), the retry attempt across a pause, the run-level
context that completed steps hand to later ones, and human decisions, which a restart
may reuse only while the parameters they were given for are unchanged."""

from __future__ import annotations

from datetime import timedelta

from etf import Actor, BatchStatus, Step, StepContext, StepResult, ValidationDecision
from etf.policies import BackoffRetryPolicy
from etf.status import AuditEventType
from helpers import launch_request, make_harness

NO_DELAY_RETRIES = BackoffRetryPolicy(max_attempts=3, base_delay=timedelta(0))


class AlwaysFails(Step):
    name = "always_fails"

    async def execute(self, ctx: StepContext) -> StepResult:
        raise ConnectionError(f"downstream unavailable (attempt {ctx.attempt})")


async def test_failed_step_record_keeps_failures_attempts_and_end_time():
    launcher, operator, _, _ = make_harness(AlwaysFails(), retry_policy=NO_DELAY_RETRIES)

    run = await launcher.launch(launch_request())

    assert run.status is BatchStatus.FAILED
    [step_run] = (await operator.get_status(run.id)).steps
    assert step_run.status is BatchStatus.FAILED
    assert step_run.exit_status.code == "FAILED"
    assert step_run.attempt == 3
    assert [f.attempt for f in step_run.failures] == [1, 2, 3]
    assert step_run.failures[-1].message == "downstream unavailable (attempt 3)"
    assert step_run.end_time is not None


class FailsThenGates(Step):
    """Attempt 1 fails; attempt 2 pauses for approval, then completes."""

    name = "fails_then_gates"

    def __init__(self) -> None:
        self.attempts_seen: list[int] = []

    async def execute(self, ctx: StepContext) -> StepResult:
        self.attempts_seen.append(ctx.attempt)
        if ctx.attempt == 1:
            raise ConnectionError("transient")
        await ctx.request_validation(reason="approve")
        return StepResult.completed()


async def test_retry_attempt_survives_a_validation_pause():
    step = FailsThenGates()
    launcher, operator, _, store = make_harness(step, retry_policy=NO_DELAY_RETRIES)

    run = await launcher.launch(launch_request())
    assert run.status is BatchStatus.AWAITING_VALIDATION
    [paused] = await store.find_step_runs(run.id)
    assert paused.attempt == 2

    run = await operator.submit_validation_decision(
        run.open_validation_id, ValidationDecision.approve(Actor.human("lead"))
    )
    assert run.status is BatchStatus.COMPLETED
    assert step.attempts_seen == [1, 2, 2]  # re-executed on attempt 2, not reset to 1


class Produce(Step):
    name = "produce"

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, ctx: StepContext) -> StepResult:
        self.calls += 1
        ctx.run_context.put("customer", {"id": "C-42"})
        return StepResult.completed()


class Consume(Step):
    name = "consume"

    def __init__(self) -> None:
        self.fail = True
        self.seen: list[object] = []

    async def execute(self, ctx: StepContext) -> StepResult:
        self.seen.append(ctx.run_context.get("customer"))
        if self.fail:
            raise ConnectionError("downstream unavailable")
        return StepResult.completed()


async def test_restart_carries_run_context_from_completed_steps():
    produce, consume = Produce(), Consume()
    launcher, operator, _, _ = make_harness(
        produce, consume, retry_policy=BackoffRetryPolicy(max_attempts=1)
    )
    run = await launcher.launch(launch_request())
    assert run.status is BatchStatus.FAILED

    consume.fail = False
    restarted = await operator.restart(run.instance_id)

    assert restarted.status is BatchStatus.COMPLETED
    assert produce.calls == 1  # skipped on restart...
    assert consume.seen == [{"id": "C-42"}, {"id": "C-42"}]  # ...but its output survives


class ChargeWithTwoGates(Step):
    """Approve the amount, then confirm; the gates see the run's current amount."""

    name = "charge"

    def __init__(self) -> None:
        self.charged: list[object] = []

    async def execute(self, ctx: StepContext) -> StepResult:
        amount = ctx.params.get("amount")
        await ctx.request_validation(reason=f"approve amount={amount}")
        await ctx.request_validation(reason=f"confirm charge of {amount}")
        self.charged.append(amount)
        return StepResult.completed()


async def _approve_first_gate_then_reject_second(launcher, operator, store):
    request = launch_request()
    request.parameters = request.parameters.with_overrides({"amount": 100})
    run = await launcher.launch(request)
    run = await operator.submit_validation_decision(
        run.open_validation_id, ValidationDecision.approve(Actor.human("lead"))
    )
    gate = await store.get_validation_request(run.open_validation_id)
    assert gate.reason == "confirm charge of 100"
    run = await operator.submit_validation_decision(
        gate.id, ValidationDecision.reject(Actor.human("lead"))
    )
    assert run.status is BatchStatus.STOPPED
    return run


async def test_restart_with_changed_parameters_asks_every_gate_again():
    step = ChargeWithTwoGates()
    launcher, operator, _, store = make_harness(step)
    stopped = await _approve_first_gate_then_reject_second(launcher, operator, store)

    restarted = await operator.restart(
        stopped.instance_id, parameter_overrides={"amount": 1_000_000}
    )

    assert restarted.status is BatchStatus.AWAITING_VALIDATION
    gate = await store.get_validation_request(restarted.open_validation_id)
    assert gate.reason == "approve amount=1000000"  # not the approval given for 100
    assert step.charged == []
    trail = await operator.get_audit_trail(restarted.id)
    assert any(
        e.event_type is AuditEventType.ANNOTATION
        and e.attributes.get("prior_run_id") == stopped.id
        for e in trail
    )


async def test_restart_with_same_parameters_keeps_prior_approvals():
    step = ChargeWithTwoGates()
    launcher, operator, _, store = make_harness(step)
    stopped = await _approve_first_gate_then_reject_second(launcher, operator, store)

    restarted = await operator.restart(stopped.instance_id)

    assert restarted.status is BatchStatus.AWAITING_VALIDATION
    gate = await store.get_validation_request(restarted.open_validation_id)
    assert gate.reason == "confirm charge of 100"  # the amount approval still applies
