"""Multi-gate validation attribution (audit T5/T28), the RETRY decision (T10),
the swallowed-signal guard (T15), and single-start audit trails (T21)."""

from __future__ import annotations

from etf import Actor, BatchStatus, Step, StepContext, StepResult, ValidationDecision
from etf.exceptions import StopExecution
from etf.model import ValidationOutcome
from etf.status import AuditEventType, ValidationDecisionType, ValidationStatus
from helpers import RecordingStep, launch_request, make_harness


class TwoGateStep(Step):
    """Two sequential HITL gates; records the outcome each gate received."""

    name = "two_gates"

    def __init__(self) -> None:
        self.executions = 0
        self.outcomes: dict[str, ValidationOutcome] = {}

    async def execute(self, ctx: StepContext) -> StepResult:
        self.executions += 1
        self.outcomes["gate1"] = await ctx.request_validation(reason="gate1")
        self.outcomes["gate2"] = await ctx.request_validation(reason="gate2")
        return StepResult.completed()


async def test_each_gate_receives_its_own_decision():
    """Approving gate 2 must never hand gate 1 the wrong outcome (T5)."""
    step = TwoGateStep()
    launcher, operator, _, store = make_harness(step)

    run = await launcher.launch(launch_request())
    assert run.status is BatchStatus.AWAITING_VALIDATION
    first_request = await store.get_validation_request(run.open_validation_id)
    assert first_request.reason == "gate1"

    run = await operator.submit_validation_decision(
        first_request.id, ValidationDecision.approve(Actor.human("u1"), comment="first")
    )
    assert run.status is BatchStatus.AWAITING_VALIDATION  # paused again, at gate 2
    second_request = await store.get_validation_request(run.open_validation_id)
    assert second_request.reason == "gate2"
    assert second_request.id != first_request.id

    run = await operator.submit_validation_decision(
        second_request.id, ValidationDecision.approve(Actor.human("u2"), comment="second")
    )
    assert run.status is BatchStatus.COMPLETED

    # Each gate saw its own decision — comments and request ids line up.
    assert step.outcomes["gate1"].comment == "first"
    assert step.outcomes["gate2"].comment == "second"
    assert step.outcomes["gate1"].request_id == first_request.id
    assert step.outcomes["gate2"].request_id == second_request.id
    assert step.executions == 3  # initial + after each decision


class PreWorkGateStep(Step):
    """Counts pre-gate work so RETRY can prove the step re-executed from scratch."""

    name = "prework_gate"

    def __init__(self) -> None:
        self.pre_gate_runs = 0
        self.outcome: ValidationOutcome | None = None

    async def execute(self, ctx: StepContext) -> StepResult:
        self.pre_gate_runs += 1
        self.outcome = await ctx.request_validation(reason="check my prework")
        return StepResult.completed()


async def test_retry_decision_reexecutes_step_and_pauses_afresh():
    """RETRY discards the gate: fresh execution, NEW validation request (T10)."""
    step = PreWorkGateStep()
    launcher, operator, _, store = make_harness(step)

    run = await launcher.launch(launch_request())
    first_id = run.open_validation_id

    retry = ValidationDecision(ValidationDecisionType.RETRY, Actor.human("u1"))
    run = await operator.submit_validation_decision(first_id, retry)

    assert run.status is BatchStatus.AWAITING_VALIDATION
    assert run.open_validation_id != first_id       # a brand-new gate
    assert step.pre_gate_runs == 2                  # pre-gate work ran again
    assert step.outcome is None                     # gate never returned an outcome
    old = await store.get_validation_request(first_id)
    assert old.status is ValidationStatus.APPROVED  # resolved, recorded as RETRY in audit

    run = await operator.submit_validation_decision(
        run.open_validation_id, ValidationDecision.approve(Actor.human("u1"))
    )
    assert run.status is BatchStatus.COMPLETED
    assert step.outcome is not None and step.outcome.approved


class StopBeforeGateStep(Step):
    """First execution stops cooperatively *before* its gate is reached."""

    name = "stop_then_gate"

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, ctx: StepContext) -> StepResult:
        self.executions += 1
        if self.executions == 1:
            raise StopExecution("wind down before the gate")
        await ctx.request_validation(reason="gate")
        return StepResult.completed()


async def test_plain_resume_does_not_auto_approve_an_unreached_gate():
    """Resuming a run stopped before its gate must pause at the gate, not skip it."""
    step = StopBeforeGateStep()
    launcher, operator, _, _ = make_harness(step)
    run = await launcher.launch(launch_request())
    assert run.status is BatchStatus.STOPPED

    resumed = await operator.resume(run.id)
    assert resumed.status is BatchStatus.AWAITING_VALIDATION  # gate asked properly
    assert step.executions == 2


class SwallowingStep(Step):
    """Anti-pattern under test: a broad handler that eats the pause signal."""

    name = "swallower"

    async def execute(self, ctx: StepContext) -> StepResult:
        try:
            await ctx.request_validation(reason="will be swallowed")
        except Exception:  # noqa: BLE001 - deliberately broad (the documented hazard)
            raise RuntimeError("converted the pause into a failure")
        return StepResult.completed()


async def test_swallowed_pause_signal_is_flagged_in_audit(caplog):
    launcher, operator, _, _ = make_harness(SwallowingStep())
    run = await launcher.launch(launch_request())
    assert run.status is BatchStatus.FAILED  # treated as business failure...
    trail = await operator.get_audit_trail(run.id)
    warnings = [
        e for e in trail
        if e.event_type is AuditEventType.ANNOTATION and "swallowed" in e.message
    ]
    assert warnings, "runner should flag the probably-swallowed pause signal"
    assert any("swallowed" in r.message for r in caplog.records)


async def test_audit_trail_has_one_run_started_and_resumed_on_reentry():
    """Re-entering a run must not log a second RUN_STARTED (T21)."""
    gate, after = TwoGateStep(), RecordingStep("after")
    launcher, operator, _, store = make_harness(gate, after)

    run = await launcher.launch(launch_request())
    run = await operator.submit_validation_decision(
        run.open_validation_id, ValidationDecision.approve(Actor.human("u1"))
    )
    run = await operator.submit_validation_decision(
        run.open_validation_id, ValidationDecision.approve(Actor.human("u1"))
    )
    assert run.status is BatchStatus.COMPLETED

    trail = await operator.get_audit_trail(run.id)
    starts = [e for e in trail if e.event_type is AuditEventType.RUN_STARTED]
    resumes = [e for e in trail if e.event_type is AuditEventType.RESUMED]
    assert len(starts) == 1
    assert len(resumes) == 2  # one per decision


async def test_reject_still_resolves_gate_and_stops_run():
    launcher, operator, _, store = make_harness(TwoGateStep())
    run = await launcher.launch(launch_request())
    gate_id = run.open_validation_id
    run = await operator.submit_validation_decision(
        gate_id, ValidationDecision.reject(Actor.human("u1"), comment="nope")
    )
    assert run.status is BatchStatus.STOPPED
    assert run.open_validation_id is None
    assert (await store.get_validation_request(gate_id)).status is ValidationStatus.REJECTED
