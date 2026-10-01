"""Lifecycle tests for the ETF orchestration against the in-memory reference store.

Covers: happy path, human-in-the-loop pause/approve/reject, failure + restart with a
config change, step-level restart idempotency, completed-instance rejection, and
ingress idempotency-key dedupe.
"""

from __future__ import annotations

import pytest

from etf import (
    Actor,
    BatchStatus,
    EtfConfig,
    JobDefinition,
    JobLauncher,
    JobOperator,
    JobParameters,
    JobRegistry,
    LaunchRequest,
    Step,
    StepContext,
    StepResult,
    ValidationDecision,
)
from etf.audit import StoreBackedAuditSink
from etf.exceptions import JobInstanceAlreadyCompleteError, RunNotRestartableError
from etf.locking import InMemoryLockProvider
from etf.status import AuditEventType
from etf.stores.memory import InMemoryStateStore


# --------------------------------------------------------------------------- #
# Test steps
# --------------------------------------------------------------------------- #
class RecordingStep(Step):
    """Counts how many times it actually executed (to prove restart-skip)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def execute(self, ctx: StepContext) -> StepResult:
        self.calls += 1
        return StepResult.completed()


class FlakyStep(Step):
    name = "flaky"

    async def execute(self, ctx: StepContext) -> StepResult:
        if not ctx.params.get("fixed"):
            raise RuntimeError("boom")
        return StepResult.completed()


class ApprovalStep(Step):
    name = "approval"

    async def execute(self, ctx: StepContext) -> StepResult:
        outcome = await ctx.request_validation(reason="needs sign-off", payload={"k": "v"})
        if not outcome.approved:
            raise RuntimeError("rejected")
        ctx.put("approved_by", outcome.actor_id)
        return StepResult.completed()


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def make(*steps: Step, job_name: str = "job"):
    store = InMemoryStateStore()
    registry = JobRegistry()
    registry.register(JobDefinition(name=job_name, steps=list(steps)))
    cfg = EtfConfig(
        store=store,
        audit=StoreBackedAuditSink(store),
        lock_provider=InMemoryLockProvider(),
        registry=registry,
    )
    return JobLauncher(cfg), JobOperator(cfg), store


def req(job_name: str = "job", *, key=None, **identifying) -> LaunchRequest:
    return LaunchRequest(
        job_name=job_name,
        parameters=JobParameters(identifying=identifying or {"id": "1"}),
        idempotency_key=key,
        requested_by=Actor.service("test"),
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_happy_path_completes_all_steps():
    a, b, c = RecordingStep("a"), RecordingStep("b"), RecordingStep("c")
    launcher, _, _ = make(a, b, c)
    run = await launcher.launch(req())
    assert run.status is BatchStatus.COMPLETED
    assert (a.calls, b.calls, c.calls) == (1, 1, 1)


async def test_completed_instance_cannot_rerun():
    launcher, _, _ = make(RecordingStep("a"))
    await launcher.launch(req(id="X"))
    with pytest.raises(JobInstanceAlreadyCompleteError):
        await launcher.launch(req(id="X"))


async def test_idempotency_key_dedupes_delivery():
    step = RecordingStep("a")
    launcher, _, _ = make(step)
    run1 = await launcher.launch(req(key="dup", id="Y"))
    run2 = await launcher.launch(req(key="dup", id="Y"))  # redelivery
    assert run1.id == run2.id
    assert step.calls == 1  # executed exactly once


async def test_pause_and_approve_resumes():
    approval, after = ApprovalStep(), RecordingStep("after")
    launcher, operator, _ = make(approval, after)
    run = await launcher.launch(req())
    assert run.status is BatchStatus.AWAITING_VALIDATION
    assert after.calls == 0

    status = await operator.get_status(run.id)
    assert status.open_validation is not None
    run = await operator.submit_validation_decision(
        status.open_validation.id, ValidationDecision.approve(Actor.human("u1"))
    )
    assert run.status is BatchStatus.COMPLETED
    assert after.calls == 1


async def test_pause_and_reject_stops():
    launcher, operator, _ = make(ApprovalStep(), RecordingStep("after"))
    run = await launcher.launch(req())
    status = await operator.get_status(run.id)
    assert status.open_validation is not None
    run = await operator.submit_validation_decision(
        status.open_validation.id, ValidationDecision.reject(Actor.human("u1"))
    )
    assert run.status is BatchStatus.STOPPED


async def test_failure_then_restart_with_override():
    pre, flaky, post = RecordingStep("pre"), FlakyStep(), RecordingStep("post")
    launcher, operator, _ = make(pre, flaky, post)

    run = await launcher.launch(req())
    assert run.status is BatchStatus.FAILED
    assert pre.calls == 1 and post.calls == 0

    # Restart supplying the missing config -> completes; 'pre' is skipped (already done).
    run = await operator.restart(run.instance_id, parameter_overrides={"fixed": True})
    assert run.status is BatchStatus.COMPLETED
    assert pre.calls == 1          # NOT re-executed (restart idempotency)
    assert post.calls == 1


async def test_restart_refused_when_no_prior_failure():
    launcher, operator, _ = make(RecordingStep("a"))
    run = await launcher.launch(req())
    assert run.status is BatchStatus.COMPLETED
    with pytest.raises(RunNotRestartableError):
        await operator.restart(run.instance_id)


async def test_audit_trail_records_failure_with_exception():
    launcher, operator, _ = make(FlakyStep())
    run = await launcher.launch(req())
    trail = await operator.get_audit_trail(run.id)
    failed = [e for e in trail if e.event_type is AuditEventType.STEP_FAILED]
    assert failed and failed[0].failure is not None
    assert "boom" in failed[0].failure.message
    assert failed[0].failure.exception_type.endswith("RuntimeError")


async def test_different_identifying_params_are_distinct_instances():
    step = RecordingStep("a")
    launcher, _, store = make(step)
    r1 = await launcher.launch(req(id="A"))
    r2 = await launcher.launch(req(id="B"))
    assert r1.instance_id != r2.instance_id
    assert step.calls == 2
