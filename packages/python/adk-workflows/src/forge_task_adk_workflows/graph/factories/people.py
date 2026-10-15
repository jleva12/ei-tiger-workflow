"""
People: steps that pause the run for a person (ADK's ``RequestInput``), and
carry on with their answer. Each asks under an ID of its own
(``base.interrupt_id``), with a payload saying what kind of pause it is.

- ``approval``: asks the approvers to decide (``{kind: "approval", approvers,
  expires_at}``) and takes its Approved or Rejected way, handing on
  ``{approved, decided_by, comment, decided_at}``. With a time limit, a
  decision made after it (when it was made, as the run hands it on, not when
  the run reads it), or the timer's (``pauses.EXPIRED``), is a rejection by
  Forge; a limit of 0 waits until someone decides.
- ``human_input``: asks its question and hands on the answer, held to its
  response schema (a Pydantic model ADK checks answers against).
- ``decision_board``: lays out its choices (those whose "ask when" holds),
  each an approval of its own that people decide on their own, in any order
  and at any time: a choice decided takes its Accepted or Rejected way
  (``<choice>:accepted`` / ``<choice>:rejected``) at once, while the others
  still wait, and the last one decided takes Done too. It's built as hidden
  nodes (:func:`board_parts`): the board's own node, which lays the choices
  out and keeps them in the session's state, and one per choice, which asks
  it (an approval's payload, with ``board`` saying which choice) and takes
  its way. Each hands on the board as it stands (``{accepted, rejected,
  open, decisions, decided}``: the choices' IDs, each decision by choice, and
  the one just made). Its time limit is each choice's, as an approval's.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.adk import Context, Event
from google.adk.events import RequestInput
from google.adk.workflow import FunctionNode
from pydantic import BaseModel, ValidationError

from forge_task_adk_workflows.graph.data import PRIVATE_STATE, as_data
from forge_task_adk_workflows.graph.errors import AgentBuildError, RunFailed
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
    where_of,
)
from forge_task_adk_workflows.graph.names import adk_name, choice_name
from forge_task_adk_workflows.graph.schemas import held_to, problems, to_model
from forge_task_adk_workflows.graph.services import RunServices
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.step_settings import ApprovalConfig, DecisionBoardConfig

#: A Decision board's way out once every choice it laid out is decided.
DONE = "done"
#: A choice's ways out, after its ID: ``<choice>:accepted``.
ACCEPTED = "accepted"
REJECTED = "rejected"


class Decision(BaseModel):
    """A decision on an approval; ADK checks every answer against it."""

    approved: bool
    #: Who decided: the person, or Forge for the timer.
    decided_by: str | None = None
    comment: str | None = None
    #: When it was made (ISO 8601), as the run hands it on: the time limit
    #: holds to it, not to when the run reads it.
    decided_at: str | None = None
    #: Set by the timer when the time ran out, never by a person.
    expired: bool | None = None


def approval(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, ApprovalConfig)
    limit = timedelta(hours=s.timeout_hours) if s.timeout_hours else None
    services = ctx.services
    fallback = f"Approve {text(node.get('name')) or 'this step'}?"

    async def run(adk: Context, node_input: Any) -> Any:
        key = interrupt_id(node, adk)
        if key not in adk.resume_inputs:
            try:
                message = (
                    await services.evaluator.render(s.message, ctx.data(adk, node_input), what="Its message")
                ).strip()
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
        decision = _decision(adk, key, s.timeout_hours, services)
        return Event(output=decision, route="approved" if decision["approved"] else "rejected")  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    return FunctionNode(name=adk_name(text(node.get("name"))), func=run, rerun_on_resume=True)


def human_input(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    config = config_of(node)
    message = text(config.get("message"))
    schema = schema_of(config.get("response_schema"))
    model = to_model(schema) if schema else None
    services = ctx.services

    async def run(adk: Context, node_input: Any) -> Any:
        key = interrupt_id(node, adk)
        if key not in adk.resume_inputs:
            try:
                question = await services.evaluator.render(message, ctx.data(adk, node_input), what="Its message")
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


def _decision(adk: Context, key: str, timeout_hours: float, services: RunServices) -> dict[str, Any]:
    """
    The decision an approval (or a board's choice) asked under ``key`` was
    answered with: ``{approved, decided_by, comment, decided_at}``. With a
    time limit, a decision made after it, or the timer's, is a rejection by
    Forge.
    """
    answer = _answer(adk.resume_inputs[key])
    if not isinstance(answer, dict):
        answer = {"approved": answer is True}
    decided_at = _when(answer.get("decided_at")) or services.clock()
    expires_at = asked(adk, key).get("expires_at")
    late = isinstance(expires_at, str) and decided_at > _utc(datetime.fromisoformat(expires_at))
    hours = f"{timeout_hours:g} hour{'s' if timeout_hours != 1 else ''}"
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
    decision["decided_at"] = decided_at.isoformat()
    return decision


@dataclass(frozen=True)
class BoardParts:
    """
    A Decision board, as the graph is built with it: its own node (what
    leads into the board leads there), which lays out its choices and leads
    to their nodes by their IDs as routes; and each choice's node, by choice
    ID, which asks it and takes its way.
    """

    opener: FunctionNode
    choices: dict[str, FunctionNode]


def decision_board(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    """The board's own node; the graph builds its choices' too (:func:`board_parts`)."""
    return board_parts(node, ctx).opener


def board_parts(node: dict[str, Any], ctx: BuildContext) -> BoardParts:
    s = settings_of(node, DecisionBoardConfig)
    ids = [choice.id for choice in s.choices]
    for choice in s.choices:
        if not choice.id or ":" in choice.id or choice.id == DONE:
            raise AgentBuildError(f"{where_of(node)}: a choice's ID can't be {choice.id!r}.")
        if ids.count(choice.id) > 1:
            raise AgentBuildError(f"{where_of(node)}: two of its choices have the ID {choice.id!r}.")
    labels = {choice.id: choice.label.strip() or f"Choice {index + 1}" for index, choice in enumerate(s.choices)}
    limit = timedelta(hours=s.timeout_hours) if s.timeout_hours else None
    services = ctx.services
    name = adk_name(text(node.get("name")))
    title = text(node.get("name")) or "the board"

    async def lay_out(adk: Context, node_input: Any) -> Event:
        data = ctx.data(adk, node_input)
        evaluator = services.evaluator
        laid: list[str] = []
        questions: dict[str, str] = {}
        try:
            message = (await evaluator.render(s.message, data, what="Its message")).strip()
            for choice in s.choices:
                label = labels[choice.id]
                if choice.ask_when.strip() and not await evaluator.truthy(
                    choice.ask_when, data, what=f"When {label} is asked"
                ):
                    continue
                question = ""
                if choice.question.strip():
                    question = (await evaluator.render(choice.question, data, what=f"{label}'s question")).strip()
                laid.append(choice.id)
                questions[choice.id] = question or f"{label}?"
        except StepFailed as error:
            raise failed(node, error) from None
        board = {"laid_out": laid, "questions": questions, "message": message, "decisions": {}}
        adk.state[_board_key(adk, node)] = board
        # Nothing laid out: it's done at once.
        return Event(output=_standing(board, labels), route=laid or DONE)  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    def asking(choice: str) -> FunctionNode:
        label = labels[choice]

        async def ask(adk: Context, node_input: Any) -> Any:
            key = interrupt_id(node, adk)
            board = dict(adk.state.get(_board_key(adk, node)) or {})
            question = text((board.get("questions") or {}).get(choice)) or f"{label}?"
            if key not in adk.resume_inputs:
                deadline = services.clock() + limit if limit else None
                return RequestInput(
                    interrupt_id=key,
                    message=question,
                    payload={
                        "kind": "approval",
                        "step": node["id"],
                        "approvers": s.approvers,
                        "expires_at": deadline.isoformat() if deadline else None,
                        "board": {
                            "name": title,
                            "message": text(board.get("message")),
                            "choice": choice,
                            "label": label,
                            "choices": len(board.get("laid_out") or []),
                        },
                    },
                    response_schema=Decision,
                )
            decision = {
                "choice": choice,
                "label": label,
                "question": question,
                **_decision(adk, key, s.timeout_hours, services),
            }
            board["decisions"] = {**dict(board.get("decisions") or {}), choice: decision}
            adk.state[_board_key(adk, node)] = board
            standing = _standing(board, labels, decided=decision)
            way = f"{choice}:{ACCEPTED if decision['approved'] else REJECTED}"
            # The last one decided takes Done too.
            route: str | list[str] = way if standing["open"] else [way, DONE]
            return Event(output=standing, route=route)  # type: ignore[call-arg]  # ADK's shorthand for actions.route

        return FunctionNode(name=choice_name(name, choice), func=ask, rerun_on_resume=True)

    return BoardParts(
        opener=FunctionNode(name=name, func=lay_out),
        choices={choice: asking(choice) for choice in ids},
    )


def _board_key(adk: Context, node: dict[str, Any]) -> str:
    # Where a board keeps its choices in the session's state: once per run of
    # the graph it's in (a loop's item has its own), shared by its nodes.
    return f"{PRIVATE_STATE}board:{adk.invocation_id}:{adk.node_path.rpartition('/')[0]}:{node['id']}"


def _standing(
    board: dict[str, Any], labels: dict[str, str], *, decided: dict[str, Any] | None = None
) -> dict[str, Any]:
    """What a board hands on: where its choices stand, by ID, in its order."""
    laid = [choice for choice in board.get("laid_out") or [] if choice in labels]
    decisions = board.get("decisions") or {}
    return {
        "accepted": [choice for choice in laid if (decisions.get(choice) or {}).get("approved") is True],
        "rejected": [
            choice for choice in laid if choice in decisions and decisions[choice].get("approved") is not True
        ],
        "open": [choice for choice in laid if choice not in decisions],
        "decisions": {choice: decisions[choice] for choice in laid if choice in decisions},
        "decided": decided,
    }


def _when(value: Any) -> datetime | None:
    """:return: When a decision was made, as an answer says (ISO 8601); None when it doesn't."""
    try:
        return _utc(datetime.fromisoformat(value)) if isinstance(value, str) and value else None
    except ValueError:
        return None


def _utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _answer(value: Any) -> Any:
    # An answer as data. ADK fills what the answer left out of its schema
    # with None; left out, it's left out.
    data = as_data(value)
    if isinstance(data, dict):
        return {key: item for key, item in data.items() if item is not None}
    return data
