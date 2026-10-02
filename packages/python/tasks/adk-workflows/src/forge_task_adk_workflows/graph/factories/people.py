"""
People: steps that pause the run for a person (ADK's ``RequestInput``), and
carry on with their answer. Each asks under an ID of its own
(``base.interrupt_id``), with a payload saying what kind of pause it is.

- ``approval``: asks the approvers to decide (``{kind: "approval", approvers,
  expires_at}``) and takes its Approved or Rejected way, handing on
  ``{approved, decided_by, comment, decided_at}``. With a time limit, a
  decision after it, or the timer's (``pauses.EXPIRED``), is a rejection by
  Forge; a limit of 0 waits until someone decides.
- ``human_input``: asks its question and hands on the answer, held to its
  response schema (a Pydantic model ADK checks answers against).
"""

from datetime import datetime, timedelta
from typing import Any

from google.adk import Context, Event
from google.adk.events import RequestInput
from google.adk.workflow import FunctionNode
from pydantic import BaseModel, ValidationError

from forge_task_adk_workflows.graph.data import as_data
from forge_task_adk_workflows.graph.errors import RunFailed
from forge_task_adk_workflows.graph.factories.base import (
    BuildContext,
    asked,
    config_of,
    failed,
    interrupt_id,
    label_of,
    schema_of,
    settings_of,
    text,
)
from forge_task_adk_workflows.graph.names import adk_name
from forge_task_adk_workflows.graph.schemas import held_to, problems, to_model
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.step_settings import ApprovalConfig


class Decision(BaseModel):
    """A decision on an approval; ADK checks every answer against it."""

    approved: bool
    #: Who decided: the person, or Forge for the timer.
    decided_by: str | None = None
    comment: str | None = None
    #: Set by the timer when the time ran out, never by a person.
    expired: bool | None = None


def approval(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, ApprovalConfig)
    limit = timedelta(hours=s.timeout_hours) if s.timeout_hours else None
    services = ctx.services
    fallback = f"Approve {text(node.get('name')) or 'this step'}?"

    def run(adk: Context, node_input: Any) -> Any:
        key = interrupt_id(node, adk)
        if key not in adk.resume_inputs:
            try:
                message = services.evaluator.render(s.message, ctx.data(adk, node_input), what="Its message").strip()
            except StepFailed as error:
                raise failed(node, error) from None
            deadline = services.clock() + limit if limit else None
            return RequestInput(
                interrupt_id=key,
                message=message or fallback,
                payload={
                    "kind": "approval",
                    "step": node["id"],
                    "approvers": s.approvers,
                    "expires_at": deadline.isoformat() if deadline else None,
                },
                response_schema=Decision,
            )
        answer = _answer(adk.resume_inputs[key])
        if not isinstance(answer, dict):
            answer = {"approved": answer is True}
        now = services.clock()
        expires_at = asked(adk, key).get("expires_at")
        late = isinstance(expires_at, str) and now > datetime.fromisoformat(expires_at)
        hours = f"{s.timeout_hours:g} hour{'s' if s.timeout_hours != 1 else ''}"
        if answer.get("expired"):
            decision = {
                "approved": False,
                "decided_by": "Forge",
                "comment": f"No decision within {hours}",
            }
        elif late:
            who = text(answer.get("decided_by")) or "Someone"
            decision = {
                "approved": False,
                "decided_by": "Forge",
                "comment": f"{who} decided after the {hours} it had",
            }
        else:
            decision = {
                "approved": answer.get("approved") is True,
                "decided_by": text(answer.get("decided_by")),
                "comment": text(answer.get("comment")),
            }
        decision["decided_at"] = now.isoformat()
        return Event(output=decision, route="approved" if decision["approved"] else "rejected")  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    return FunctionNode(name=adk_name(text(node.get("name"))), func=run, rerun_on_resume=True)


def human_input(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    config = config_of(node)
    message = text(config.get("message"))
    schema = schema_of(config.get("response_schema"))
    model = to_model(schema) if schema else None
    services = ctx.services

    def run(adk: Context, node_input: Any) -> Any:
        key = interrupt_id(node, adk)
        if key not in adk.resume_inputs:
            try:
                question = services.evaluator.render(message, ctx.data(adk, node_input), what="Its message")
            except StepFailed as error:
                raise failed(node, error) from None
            return RequestInput(
                interrupt_id=key,
                message=question,
                payload={"kind": "human_input", "step": node["id"]},
                response_schema=model,
            )
        answer = _answer(adk.resume_inputs[key])
        if model is None:
            return answer
        try:
            return held_to(model, answer)
        except ValidationError as error:
            raise RunFailed(
                f"{label_of(node)} failed: the answer doesn't fit what it asks: {problems(error, 'answer')}",
                step=node["id"],
            ) from None

    return FunctionNode(name=adk_name(text(node.get("name"))), func=run, rerun_on_resume=True)


def _answer(value: Any) -> Any:
    # An answer as data. ADK fills what the answer left out of its schema
    # with None; left out, it's left out.
    data = as_data(value)
    if isinstance(data, dict):
        return {key: item for key, item in data.items() if item is not None}
    return data
