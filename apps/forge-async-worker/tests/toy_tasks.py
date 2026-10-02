"""A stand-in for the ADK workflows task, so the worker's tests exercise the
worker, not ADK: an ``adk_workflows`` task whose ``run`` job takes a run's
payload (RunPayload) and goes through the steps its input lists, carrying on
from what it kept, with the control calls an ADK workflow run makes.

Steps: ``step`` (nothing), ``approve`` and ``ask`` (an approval or a question,
gate ``gate-<index>``), ``wait`` (until ``until``), ``fail`` (a failed result)
and ``hook`` (the test's own ``task.hook``). Each finished step is kept,
checkpointed and noted.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from forge_task_adk_workflows.runs import RunPayload
from forge_tasks.control import JobControl, current_control
from forge_tasks.runner import ok
from forge_tasks.tasks import JobResult, Schedule, TaskContext, TaskRegistry

Hook = Callable[[JobControl, int], Awaitable[None]]


async def nothing(control: JobControl, index: int) -> None:
    return None


class ScriptedRun:
    name = "run"
    payload_model = RunPayload

    def __init__(self, task: ScriptedTask) -> None:
        self.task = task

    def lock_key(self, payload: RunPayload) -> None:
        return None

    async def run(self, payload: RunPayload) -> JobResult:
        control = current_control()
        assert control is not None
        self.task.attempts.append(control.run_id)
        steps: list[dict[str, Any]] = list((payload.input or {}).get("steps", []))
        said: list[Any] = control.load("said", [])
        for index in range(control.load("done", 0), len(steps)):
            step = steps[index]
            match step["do"]:
                case "approve":
                    decision = await control.approval(
                        key=f"gate-{index}",
                        reason=step.get("reason", "Ship it?"),
                        details={"kind": "approval", "approvers": "org:admin", "step": f"step-{index}"},
                        timeout_seconds=step.get("timeout"),
                    )
                    said.append({"approved": decision.approved, "by": decision.actor_name, "comment": decision.comment})
                case "ask":
                    decision = await control.approval(
                        key=f"gate-{index}",
                        reason="How many?",
                        details={"kind": "human_input", "response_schema": {"type": "object"}},
                    )
                    said.append(json.loads(decision.comment))
                case "wait":
                    if not control.load(f"waited-{index}"):
                        control.keep(f"waited-{index}", True)
                        await control.wait_until(datetime.fromisoformat(step["until"]), reason="Delay waits")
                case "fail":
                    return JobResult.failed(
                        "Refund failed", outcome="failed", result={"why": "no"}, step=f"step-{index}"
                    )
                case "hook":
                    await self.task.hook(control, index)
            control.keep("done", index + 1)
            control.keep("said", said)
            await control.checkpoint(f"step {index}")
            await control.note(f"step {index} finished", step=index)
        return ok(outcome="succeeded", result={"said": said}, steps=len(steps), session_id=payload.session_id)


class ScriptedTask:
    name = queue = "adk_workflows"

    def __init__(self, ctx: TaskContext) -> None:
        self.settings = ctx.options  # the ADK workflows task's (an AdkWorkflowsSettings), when given
        self.attempts: list[str] = []
        self.hook: Hook = nothing
        self.jobs: dict[str, Any] = {"run": ScriptedRun(self)}

    async def ensure_schema(self) -> None:
        return None

    async def close(self) -> None:
        return None


class ScriptedFactory:
    name = queue = "adk_workflows"
    schedules: list[Schedule] = []

    def build(self, ctx: TaskContext) -> ScriptedTask:
        return ScriptedTask(ctx)


def toy_registry() -> TaskRegistry:
    return TaskRegistry([ScriptedFactory()])
