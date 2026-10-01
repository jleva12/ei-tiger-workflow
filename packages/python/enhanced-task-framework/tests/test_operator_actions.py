"""Operator actions are attributed to whoever performed them, can be authorized by the
host (denials are audited), never approve a human gate implicitly, and a rejected
validation decision leaves a trace."""

from __future__ import annotations

from datetime import timedelta

import pytest

from etf import (
    Actor,
    BatchStatus,
    OperatorAction,
    Step,
    StepContext,
    StepResult,
    ValidationDecision,
)
from etf.exceptions import ValidationAuthorizationError, ValidationStateError
from etf.model import JobInstance, JobRun
from etf.policies import BackoffRetryPolicy
from etf.status import ActorKind, AuditEventType, ValidationStatus
from helpers import FakeClock, RecordingStep, launch_request, make_harness

LEAD = Actor.human("lead-1", roles={"ops"})
MALLORY = Actor.human("mallory")


class Gate(Step):
    name = "gate"

    def __init__(self, required_role: str | None = None) -> None:
        self.required_role = required_role

    async def execute(self, ctx: StepContext) -> StepResult:
        await ctx.request_validation(reason="approve", required_role=self.required_role)
        return StepResult.completed()


class Fails(Step):
    name = "fails"

    async def execute(self, ctx: StepContext) -> StepResult:
        raise ValueError("boom")


def actors_of(events, event_type: AuditEventType) -> list[str]:
    return [e.actor.id for e in events if e.event_type is event_type]


async def test_restart_and_abandon_are_credited_to_the_operator():
    launcher, operator, _, _ = make_harness(
        Fails(), retry_policy=BackoffRetryPolicy(max_attempts=1)
    )
    request = launch_request()
    request.requested_by = Actor.service("checkout")
    failed = await launcher.launch(request)

    restarted = await operator.restart(
        failed.instance_id, parameter_overrides={"batch": 5}, actor=LEAD
    )
    abandoned = await operator.abandon(restarted.id, actor=LEAD)

    assert abandoned.status is BatchStatus.ABANDONED
    events = await operator.get_audit_trail(restarted.id)
    assert actors_of(events, AuditEventType.RESTARTED) == ["lead-1"]
    assert actors_of(events, AuditEventType.PARAMETERS_OVERRIDDEN) == ["lead-1"]
    assert actors_of(events, AuditEventType.STATUS_CHANGED) == ["lead-1"]
    overridden = next(
        e for e in events if e.event_type is AuditEventType.PARAMETERS_OVERRIDDEN
    )
    assert overridden.attributes["before"] == {}
    assert overridden.attributes["after"] == {"batch": 5}
    # The run's own lifecycle events are still attributed to whom it serves.
    assert actors_of(events, AuditEventType.RUN_STARTED) == ["checkout"]


async def test_stop_is_credited_to_the_operator():
    clock = FakeClock()
    _, operator, cfg, store = make_harness(RecordingStep(), clock=clock)
    instance = await store.create_job_instance(
        JobInstance(id="inst", job_name="job", identity_hash="h")
    )
    run = await store.create_job_run(
        JobRun(id="run", instance_id=instance.id, job_name="job",
               status=BatchStatus.RUNNING, requested_by=Actor.service("checkout"))
    )

    await operator.stop(run.id, actor=LEAD)

    events = await operator.get_audit_trail(run.id)
    assert actors_of(events, AuditEventType.STATUS_CHANGED) == ["lead-1"]


async def test_sweeps_are_credited_to_the_system():
    clock = FakeClock()
    _, operator, cfg, store = make_harness(RecordingStep(), clock=clock)
    instance = await store.create_job_instance(
        JobInstance(id="inst", job_name="job", identity_hash="h")
    )
    await store.create_job_run(
        JobRun(id="run", instance_id=instance.id, job_name="job", status=BatchStatus.RUNNING,
               requested_by=Actor.service("checkout"), last_updated=clock.now())
    )
    clock.advance(timedelta(hours=1))

    await operator.recover_stale_runs(older_than=timedelta(minutes=30))

    events = await operator.get_audit_trail("run")
    recovered = [e for e in events if e.event_type is AuditEventType.RECOVERED]
    assert recovered[0].actor.kind is ActorKind.SYSTEM


async def test_resume_without_a_decision_never_approves_a_gate():
    launcher, operator, _, store = make_harness(Gate())
    paused = await launcher.launch(launch_request())

    with pytest.raises(ValidationStateError, match="submit_validation_decision"):
        await operator.resume(paused.id)
    with pytest.raises(ValidationStateError):
        await operator.resume(paused.id, parameter_overrides={"cap": 10})

    gate = await store.get_validation_request(paused.open_validation_id)
    assert gate.status is ValidationStatus.PENDING
    run = await operator.resume(paused.id, ValidationDecision.approve(LEAD))
    assert run.status is BatchStatus.COMPLETED


async def test_authorizer_decides_every_operator_action_and_denials_are_audited():
    seen: list[OperatorAction] = []

    async def authorizer(action: OperatorAction) -> None:
        seen.append(action)
        if "ops" not in action.actor.roles:
            raise PermissionError(f"{action.actor.id} may not {action.name}")

    launcher, operator, _, _ = make_harness(
        Fails(), retry_policy=BackoffRetryPolicy(max_attempts=1), authorizer=authorizer
    )
    failed = await launcher.launch(launch_request())

    with pytest.raises(PermissionError):
        await operator.restart(failed.instance_id, actor=MALLORY)
    restarted = await operator.restart(failed.instance_id, actor=LEAD)

    assert [(a.name, a.actor.id) for a in seen] == [
        ("restart", "mallory"),
        ("restart", "lead-1"),
    ]
    assert seen[0].run_id == failed.id and seen[0].job_name == "job"
    denied = [
        e for e in await operator.get_audit_trail(failed.id)
        if e.event_type is AuditEventType.ANNOTATION and e.attributes.get("denied")
    ]
    assert [(e.actor.id, e.attributes["action"]) for e in denied] == [("mallory", "restart")]
    assert restarted.attempt == 2


async def test_rejected_validation_decisions_are_audited():
    launcher, operator, _, store = make_harness(Gate(required_role="approver"))
    paused = await launcher.launch(launch_request())

    with pytest.raises(ValidationAuthorizationError):
        await operator.submit_validation_decision(
            paused.open_validation_id, ValidationDecision.approve(MALLORY)
        )

    rejected = [
        e for e in await operator.get_audit_trail(paused.id)
        if e.event_type is AuditEventType.ANNOTATION and e.attributes.get("rejected")
    ]
    assert [(e.actor.id, e.attributes["rejected"]) for e in rejected] == [
        ("mallory", "ValidationAuthorizationError")
    ]
    gate = await store.get_validation_request(paused.open_validation_id)
    assert gate.status is ValidationStatus.PENDING
