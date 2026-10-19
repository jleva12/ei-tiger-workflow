"""Tasks an agent hands off, as chats see them.

An agent hands a task-mode agent (an intake agent, a sub-agent given a task)
its task by calling it (ADK's task delegation): the call stays open while the
person talks with the task, over as many turns as it takes, and its response
is what the task hands back. Chats built on ``@assistant-ui/react-google-adk``
take a call still open when a turn ends as one waiting on them: they offer to
approve it, and with the person's next message they cancel it
(``{"cancelled": true}``), which ADK refuses beside text. Only calls marked
long-running (``longRunningToolIds``) are left open. So:

- :class:`TaskCallsPlugin` tags each event that hands off a task with its
  calls' IDs (``custom_metadata`` :data:`TASK_CALLS_KEY`), kept with it;
- the wire format (``wire``) marks those calls long-running for chats, and
  only for chats: ADK's own resuming reads ``long_running_tool_ids``;
- :func:`without_cancelled_tasks` drops a cancellation a chat sent for an
  open hand-off anyway.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

if TYPE_CHECKING:
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events.event import Event

#: An event's custom metadata that lists the calls in it handing off a task.
TASK_CALLS_KEY = "forge:task_calls"

#: What a chat answers a call it cancels with.
CANCELLED = {"cancelled": True}


class TaskCallsPlugin(BasePlugin):
    """Tags the events that hand off a task with the calls' IDs (:data:`TASK_CALLS_KEY`)."""

    def __init__(self) -> None:
        super().__init__(name="forge_task_calls")

    async def on_event_callback(self, *, invocation_context: InvocationContext, event: Event) -> Event | None:
        calls = event.get_function_calls()
        if not calls:
            return None
        agent = invocation_context.agent
        if not isinstance(agent, BaseAgent):
            return None
        root = agent.root_agent
        ids = [call.id for call in calls if call.id and _is_task(root.find_agent(call.name or ""))]
        if not ids:
            return None
        event.custom_metadata = {**(event.custom_metadata or {}), TASK_CALLS_KEY: ids}
        return event


def _is_task(agent: Any) -> bool:
    return isinstance(agent, LlmAgent) and agent.mode == "task"


def open_task_calls(events: Iterable[Event]) -> set[str]:
    """:return: The IDs of the calls handing off a task that nothing has answered yet."""
    handed: set[str] = set()
    answered: set[str] = set()
    for event in events:
        tagged = (event.custom_metadata or {}).get(TASK_CALLS_KEY)
        if isinstance(tagged, list):
            handed.update(str(call) for call in tagged)
        answered.update(response.id for response in event.get_function_responses() if response.id)
    return handed - answered


def without_cancelled_tasks(message: types.Content | None, events: Iterable[Event]) -> types.Content | None:
    """
    :return: ``message`` without the cancellations a chat sent for hand-offs
        still open (the task goes on); as it is when it has none, or when
        nothing else would be left of it.
    """
    if message is None or not message.parts:
        return message
    cancelling = {
        part.function_response.id
        for part in message.parts
        if part.function_response is not None and part.function_response.response == CANCELLED
    }
    if not cancelling:
        return message
    dropped = cancelling & open_task_calls(events)
    kept = [
        part
        for part in message.parts
        if part.function_response is None or part.function_response.id not in dropped
    ]
    if not dropped or not kept:
        return message
    return message.model_copy(update={"parts": kept})
