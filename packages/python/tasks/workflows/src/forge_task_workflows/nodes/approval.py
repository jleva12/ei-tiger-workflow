"""Approval: a person in the organization decides, and the run waits for them.

The run pauses at an approval gate of the task framework and lets the worker
go; an admin or a member (as the step says) decides from the run's page, with
a comment, and the run carries on down Approved or Rejected. With a timeout,
nobody deciding in time is a rejection.
"""

from __future__ import annotations

from forge_task_workflows.engine import Outcome, StepRun


async def approval(step: StepRun) -> Outcome:
    s = step.settings
    message = step.render(s.message, what="Its message").strip() or f"Approve {step.node.name or 'this step'}?"
    decision = await step.control.approval(
        key=step.key,
        reason=message,
        details={
            "step": step.node.id,
            "step_name": step.node.name,
            "approvers": s.approvers,
            "workflow_id": step.run.workflow_id,
            "workflow_name": step.run.workflow_name,
        },
        timeout_seconds=int(s.timeout_hours * 3600) or None,
    )
    branch = "approved" if decision.approved else "rejected"
    await step.note(f"{branch} by {decision.actor_name or decision.actor_id or 'no one'}")
    return Outcome(
        branch,
        {
            "approved": decision.approved,
            "decided_by": decision.actor_name or decision.actor_id or "",
            "comment": decision.comment,
            "decided_at": step.services.clock().isoformat(),
        },
    )
