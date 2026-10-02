"""
A run's pauses, for its timer (the worker, next phase) and tests: what it
waits on, and when the timer answers it.

A node pauses the run by asking for input (ADK's ``RequestInput``): an
Approval for a decision, Human input for an answer, a long Delay for the
timer. Each asks with its payload's ``kind``; an approval's ``expires_at``
and a delay's ``until`` say when the timer answers it (an approval then
rejects, a delay wakes). The run resumes when the question is answered: a
message with the function response (:meth:`Pause.answer`), sent to ADK's
runner for the same session.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from google.adk.sessions import Session
from google.genai import types

#: ADK's name for a node's question (``RequestInput``).
REQUEST_INPUT = "adk_request_input"

#: The timer's answer to an approval that expired.
EXPIRED = {"approved": False, "decided_by": "Forge", "expired": True}
#: The timer's answer to a delay whose time came.
WOKEN = {"woken": True}


@dataclass(frozen=True)
class Pause:
    """
    A question a run waits on.

    :ivar invocation_id: The run (ADK's invocation) that asked.
    :ivar interrupt_id: The question's ID (its function call's).
    :ivar kind: ``approval``, ``human_input`` or ``delay``.
    :ivar message: What the person is asked.
    :ivar payload: The node's payload: the approvers and deadline of an
        approval, the time a delay ends.
    :ivar due: When the timer answers it; None when only a person does.
    """

    invocation_id: str
    interrupt_id: str
    kind: str
    message: str
    payload: dict[str, Any]
    due: datetime | None

    def timer_answer(self) -> dict[str, Any]:
        """
        :return: What the timer answers: an approval expired, a delay woken.
        :raises ValueError: When only a person answers it.
        """
        if self.kind == "approval":
            return dict(EXPIRED)
        if self.kind == "delay":
            return dict(WOKEN)
        raise ValueError(f"The timer doesn't answer a {self.kind} pause.")

    def answer(self, response: dict[str, Any]) -> types.Content:
        """
        :param response: The answer: a decision, a person's answer, the timer's.
        :return: The message that resumes the run with it.
        """
        return types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=self.interrupt_id, name=REQUEST_INPUT, response=response
                    )
                )
            ],
        )


def pending_pauses(session: Session) -> list[Pause]:
    """
    :param session: A run's ADK session, as its session service reads it.
    :return: The questions its runs wait on, oldest first.
    """
    # A question is answered by the first answer with its ID after it: an ID
    # may be asked again, in a later run of the session.
    waiting: dict[str, list[Pause]] = {}
    order: list[Pause] = []
    for event in session.events:
        for response in event.get_function_responses():
            asked = waiting.get(response.id or "")
            if response.name == REQUEST_INPUT and asked:
                order.remove(asked.pop(0))
        for call in event.get_function_calls():
            if call.name != REQUEST_INPUT or not call.id:
                continue
            args = call.args or {}
            payload = args.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            pause = Pause(
                invocation_id=event.invocation_id,
                interrupt_id=call.id,
                kind=str(payload.get("kind") or ""),
                message=str(args.get("message") or ""),
                payload=payload,
                due=_due(payload),
            )
            waiting.setdefault(call.id, []).append(pause)
            order.append(pause)
    return order


def due_pauses(session: Session, now: datetime) -> list[Pause]:
    """
    :param session: A run's ADK session.
    :param now: The time now.
    :return: The questions the timer answers now: approvals past their
        deadline, delays whose time came.
    """
    return [pause for pause in pending_pauses(session) if pause.due is not None and pause.due <= now]


def _due(payload: dict[str, Any]) -> datetime | None:
    when = payload.get("expires_at") or payload.get("until")
    if not isinstance(when, str):
        return None
    try:
        return datetime.fromisoformat(when)
    except ValueError:
        return None
