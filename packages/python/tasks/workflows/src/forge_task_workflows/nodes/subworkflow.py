"""Run workflow: starts another of the organization's workflows with this step's input
(what the step before handed on), as the same member. Told to wait, the run
waits for the other to finish and hands on its result."""

from __future__ import annotations

from datetime import timedelta

from forge_task_workflows.engine import Outcome, StepRun
from forge_task_workflows.errors import StepFailed

PARENT_LABEL = "parent"


async def subworkflow(step: StepRun) -> Outcome:
    s = step.settings
    run = step.run
    if not s.workflow:
        raise StepFailed("It has no workflow picked")
    if s.workflow == run.workflow_id:
        raise StepFailed("A workflow can't run itself")
    depth = run.depth + 1
    if depth > step.services.settings.max_depth:
        raise StepFailed(f"Workflows run workflows {step.services.settings.max_depth} deep here, at most")
    parent = f"{step.control.instance_id}/{step.key}"
    progress = step.progress
    started = progress.get("started")
    if not started:
        started = await step.services.require_admin().start_workflow(
            organization_id=run.organization_id,
            user_id=run.run_as,
            workflow_id=s.workflow,
            input=step.token.previous,
            parent=parent,
            depth=depth,
        )
        await step.keep(started=started)
        await step.note(f"started {started.get('workflow_name') or s.workflow}")
    if not s.wait:
        return Outcome("next", {"run_id": started.get("key") or ""})
    other = await step.control.find_run({PARENT_LABEL: parent})
    if other is None or other.status in ("running", "waiting"):
        await step.wait_until(
            step.services.clock() + timedelta(seconds=step.services.settings.poll_seconds),
            reason=f"{started.get('workflow_name') or 'the other workflow'} is still running",
        )
    assert other is not None
    if other.status != "succeeded":
        raise StepFailed(
            f"{started.get('workflow_name') or 'The other workflow'} {other.status}: {other.error or ''}".strip()
        )
    result = (other.result or {}).get("detail", {})
    return Outcome("next", result.get("result") if isinstance(result, dict) else None)
