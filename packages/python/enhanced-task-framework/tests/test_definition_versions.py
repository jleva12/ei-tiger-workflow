"""Changing a job definition safely: versions register side by side, a flow edited
without a version bump is refused rather than resumed against the wrong steps, and a
failed instance can be moved to a newer version explicitly on restart."""

from __future__ import annotations

import pytest

from etf import (
    Actor,
    BatchStatus,
    EtfConfig,
    JobDefinition,
    JobOperator,
    JobRegistry,
    Step,
    StepContext,
    StepResult,
    ValidationDecision,
)
from etf.audit import StoreBackedAuditSink
from etf.exceptions import DefinitionVersionMismatchError
from etf.policies import BackoffRetryPolicy
from etf.status import AuditEventType
from helpers import RecordingStep, launch_request, make_harness


class Gate(Step):
    name = "gate"

    async def execute(self, ctx: StepContext) -> StepResult:
        await ctx.request_validation(reason="approve")
        return StepResult.completed()


def test_registry_keeps_versions_side_by_side():
    registry = JobRegistry()
    v1 = JobDefinition("job", [RecordingStep("a")])
    v2 = JobDefinition("job", [RecordingStep("a"), RecordingStep("b")], version=2)
    registry.register(v1)
    registry.register(v2)
    assert registry.get("job") is v2
    assert registry.get("job", 1) is v1

    same_flow = JobDefinition("job", [RecordingStep("a")])  # e.g. new step code
    registry.register(same_flow)
    assert registry.get("job", 1) is same_flow
    assert registry.versions("job") == [1, 2]
    with pytest.raises(ValueError, match="new version"):
        registry.register(JobDefinition("job", [RecordingStep("renamed")]))
    with pytest.raises(DefinitionVersionMismatchError, match="version 3 is not registered"):
        registry.get("job", 3)


async def test_flow_edited_without_a_version_bump_is_refused():
    launcher, _, cfg, store = make_harness(RecordingStep("a"), Gate())
    paused = await launcher.launch(launch_request())
    assert paused.status is BatchStatus.AWAITING_VALIDATION

    # A deploy inserts a step before the gate but keeps version 1.
    edited = JobRegistry()
    edited.register(JobDefinition("job", [RecordingStep("a"), RecordingStep("new"), Gate()]))
    operator = JobOperator(
        EtfConfig(
            store=store,
            audit=StoreBackedAuditSink(store),
            lock_provider=cfg.lock_provider,
            registry=edited,
        )
    )
    with pytest.raises(DefinitionVersionMismatchError, match="changed"):
        await operator.submit_validation_decision(
            paused.open_validation_id, ValidationDecision.approve(Actor.human("lead"))
        )
    assert (await store.get_job_run(paused.id)).status is BatchStatus.AWAITING_VALIDATION


class Flaky(Step):
    name = "b"

    def __init__(self, fails: bool) -> None:
        self.fails = fails
        self.calls = 0

    async def execute(self, ctx: StepContext) -> StepResult:
        self.calls += 1
        if self.fails:
            raise ValueError("bug in version 1")
        return StepResult.completed()


async def test_restart_can_move_a_failed_instance_to_a_newer_version():
    a, broken = RecordingStep("a"), Flaky(fails=True)
    launcher, operator, cfg, store = make_harness(
        a, broken, retry_policy=BackoffRetryPolicy(max_attempts=1)
    )
    failed = await launcher.launch(launch_request())
    assert failed.status is BatchStatus.FAILED

    fixed, c = Flaky(fails=False), RecordingStep("c")
    cfg.registry.register(JobDefinition("job", [RecordingStep("a"), fixed, c], version=2))

    # A plain restart stays on the version the run started with.
    again = await operator.restart(failed.instance_id)
    assert again.status is BatchStatus.FAILED and again.definition_version == 1
    # Moving to version 2 skips the completed step and runs the rest there.
    moved = await operator.restart(failed.instance_id, definition_version=2)

    assert moved.status is BatchStatus.COMPLETED and moved.definition_version == 2
    assert (a.calls, fixed.calls, c.calls) == (1, 1, 1)
    assert (await store.get_job_instance(failed.instance_id)).definition_version == 2
    restarted = [
        e for e in await operator.get_audit_trail(moved.id)
        if e.event_type is AuditEventType.RESTARTED
    ]
    assert restarted[0].attributes["definition_version"] == {"from": 1, "to": 2}
