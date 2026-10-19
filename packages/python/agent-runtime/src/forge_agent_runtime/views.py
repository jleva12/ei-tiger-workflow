"""What a caller sees of a conversation: its events and state, as a view.

ADK's session holds everything an agent did: the model's thoughts, every
tool's arguments and results, the state its plugins keep (intake records,
requests, summaries), and the errors behind a failed reply as they were
raised. The people an agent talks with, and the programs that call it, see
only what's meant for them:

- **Full** (:data:`FULL_VIEW`): everything, as ADK keeps it. The builder's
  test chat draws a turn on the canvas from it; the runtime gives it to
  whoever manages the organization's agents.
- **Public** (:func:`public_view`), for everyone else (chat pages, API
  keys, A2A callers):

  - thought parts are left out;
  - a tool call's arguments and its result are shown only for the tools a
    chat draws (rich answers, workflow runs, knowledge base citations,
    asking the person, approvals, sign-ins, handing back a task) and those
    their node says to show (``show_to_person``). Any other call keeps its
    name and ID, so calls and responses still pair up, with ``{}`` for its
    arguments and result. What the person answers is theirs, and stays;
  - only the state a chat reads is kept: its title, follow-up suggestions,
    the context meter's, and what the agent declares;
  - custom metadata keeps the turn's outcome and the markers a chat reads;
  - an error says what failed, not why: its code, and a message saying so,
    unless it's one meant for the person (a refusal saying what they need).

Which calls are shown is decided as each call is made, by
:class:`ShownCallsPlugin`, from the tool itself (:data:`SHOWN_ATTR`), and
kept with the event (``custom_metadata`` :data:`SHOWN_CALLS_KEY`), so a
conversation replayed later is shown the same way as it streamed.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from google.adk.plugins.base_plugin import BasePlugin

from forge_agent_runtime.tasks import TASK_CALLS_KEY
from forge_common.adk.ask_person import ASK_PERSON
from forge_common.adk.context_window import STATE_PREFIX as CONTEXT_PREFIX
from forge_common.adk.follow_ups import QUIET_KEY as NO_FOLLOW_UPS_KEY
from forge_common.adk.follow_ups import STATE_KEY as FOLLOW_UPS_KEY
from forge_common.adk.titles import TITLE_KEY
from forge_common.adk.toolset import SHOWN_ATTR
from forge_common.adk.turns import TURN_KEY

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events.event import Event
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.sessions import Session

    from forge_agent_runtime.document import ChatAgentDocument

#: An event's custom metadata that lists the calls in it a person is shown in full.
SHOWN_CALLS_KEY = "forge:shown_calls"

#: Calls a chat draws whatever made them: ADK's own requests of the person
#: (asking them, an approval, a sign-in) and a task handing back what it did.
SHOWN_NAMES = frozenset({ASK_PERSON, "adk_request_confirmation", "adk_request_credential", "finish_task"})

#: The state a chat reads, besides what the agent declares.
SHOWN_STATE = frozenset({TITLE_KEY, FOLLOW_UPS_KEY})

#: The custom metadata a chat reads.
SHOWN_METADATA = frozenset({TURN_KEY, TASK_CALLS_KEY, NO_FOLLOW_UPS_KEY})

#: Errors whose message is meant for the person: a refusal says what they need.
SHOWN_ERRORS = frozenset({"ACCESS_DENIED"})

#: What a failure is called when why isn't the person's to see.
FAILED = "The agent ran into a problem. Try again."
MODEL_FAILED = "The agent's model couldn't answer just now. Try again."
MODEL_SIGN_IN_FAILED = "The agent couldn't reach its model."

#: The statuses Google's API fails with, which ``error_event`` uses as codes.
_MODEL_STATUSES = frozenset(
    {
        "INVALID_ARGUMENT",
        "FAILED_PRECONDITION",
        "OUT_OF_RANGE",
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "NOT_FOUND",
        "RESOURCE_EXHAUSTED",
        "CANCELLED",
        "INTERNAL",
        "UNAVAILABLE",
        "DEADLINE_EXCEEDED",
        "UNKNOWN",
    }
)


@dataclass(frozen=True)
class EventView:
    """
    What a caller sees of a conversation.

    :ivar full: Everything, as ADK keeps it.
    :ivar state: The state keys a public view keeps besides :data:`SHOWN_STATE`:
        what the agent declares and what a chat may send.
    """

    full: bool = False
    state: frozenset[str] = frozenset()


#: Everything, as ADK keeps it.
FULL_VIEW = EventView(full=True)


def public_view(document: ChatAgentDocument | None = None) -> EventView:
    """:return: What anyone but the agent's managers sees of its conversations."""
    from forge_agent_runtime.executor import FIXED_STATE

    declared: Iterable[str] = ()
    if document is not None:
        properties = document.state_schema.get("properties")
        if isinstance(properties, dict):
            declared = properties
    return EventView(full=False, state=frozenset({*FIXED_STATE, *declared}))


def public_error(code: str | None, message: str | None) -> str | None:
    """
    :return: What a public view says of a failure: its message when it's
        meant for the person, else that the agent (or its model) failed.
    """
    if not message:
        return message
    if code in SHOWN_ERRORS:
        return message
    if code == "MODEL_AUTH_FAILED":
        return MODEL_SIGN_IN_FAILED
    if code is not None and (code.startswith(("MODEL_", "PROVIDER_")) or code in _MODEL_STATUSES):
        return MODEL_FAILED
    return FAILED


class Projector:
    """
    Shows a conversation's events as a view sees them. A call's result is
    shown when the call was, so the projector remembers the shown calls of
    the events it has seen: give it the conversation's earlier events
    (``events``) before a turn's new ones.
    """

    def __init__(self, view: EventView, events: Iterable[Event] = ()) -> None:
        self.view = view
        self.shown: set[str] = set()
        self.see(events)

    def see(self, events: Iterable[Event]) -> None:
        """Remember which calls these events show."""
        for event in events:
            self._note(event)

    def event(self, event: Event) -> Event:
        """:return: The event as the view sees it (a copy, when it differs)."""
        if self.view.full:
            return event
        self._note(event)
        return self._public(event)

    def session(self, session: Session) -> Session:
        """:return: The conversation as the view sees it: its state and its events."""
        if self.view.full:
            return session
        self.see(session.events)
        return session.model_copy(
            update={
                "state": self.state(session.state),
                "events": [self._public(event) for event in session.events],
            }
        )

    def state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """:return: The state the view keeps."""
        if self.view.full:
            return dict(state)
        return {key: value for key, value in state.items() if self._shown_state(key)}

    def _shown_state(self, key: str) -> bool:
        return key in SHOWN_STATE or key in self.view.state or key.startswith(CONTEXT_PREFIX)

    def _note(self, event: Event) -> None:
        metadata = event.custom_metadata or {}
        for key in (SHOWN_CALLS_KEY, TASK_CALLS_KEY):
            tagged = metadata.get(key)
            if isinstance(tagged, list):
                self.shown.update(str(call) for call in tagged)
        for call in event.get_function_calls():
            if call.id and call.name in SHOWN_NAMES:
                self.shown.add(call.id)

    def _shows(self, call_id: str | None, name: str | None) -> bool:
        return (call_id is not None and call_id in self.shown) or name in SHOWN_NAMES

    def _public(self, event: Event) -> Event:
        update: dict[str, Any] = {}
        content = event.content
        if content is not None and content.parts:
            person = event.author == "user"
            parts = []
            changed = False
            for part in content.parts:
                if part.thought and part.function_call is None and part.function_response is None:
                    changed = True
                    continue
                call, response = part.function_call, part.function_response
                if call is not None and not person and not self._shows(call.id, call.name) and call.args:
                    part = part.model_copy(update={"function_call": call.model_copy(update={"args": {}})})
                    changed = True
                elif (
                    response is not None
                    and not person
                    and not self._shows(response.id, response.name)
                    and response.response
                ):
                    part = part.model_copy(
                        update={"function_response": response.model_copy(update={"response": {}})}
                    )
                    changed = True
                parts.append(part)
            if changed:
                update["content"] = content.model_copy(update={"parts": parts}) if parts else None
        delta = event.actions.state_delta if event.actions is not None else None
        if delta:
            kept = {key: value for key, value in delta.items() if self._shown_state(key)}
            if len(kept) != len(delta):
                update["actions"] = event.actions.model_copy(update={"state_delta": kept})
        metadata = event.custom_metadata
        if metadata:
            shown = {key: value for key, value in metadata.items() if key in SHOWN_METADATA}
            turn = shown.get(TURN_KEY)
            if isinstance(turn, dict) and turn.get("errorMessage"):
                shown[TURN_KEY] = {
                    **turn,
                    "errorMessage": public_error(turn.get("errorCode"), turn.get("errorMessage")),
                }
            if shown != metadata:
                update["custom_metadata"] = shown or None
        if event.error_message:
            message = public_error(event.error_code, event.error_message)
            if message != event.error_message:
                update["error_message"] = message
        return event.model_copy(update=update) if update else event


class ShownCallsPlugin(BasePlugin):
    """
    Tags each event's calls of tools a person is shown in full
    (:data:`SHOWN_CALLS_KEY`): the tools the model was offered are noted as
    it's asked (``before_model_callback``), and its calls looked up among them.
    """

    #: How many model requests' tools it keeps at most, should a run never end.
    MOST = 1024

    def __init__(self) -> None:
        super().__init__(name="forge_shown_calls")
        self._tools: OrderedDict[tuple[str, str], frozenset[str]] = OrderedDict()

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        shown = frozenset(
            name for name, tool in llm_request.tools_dict.items() if getattr(tool, SHOWN_ATTR, False)
        )
        key = (callback_context.invocation_id, callback_context.agent_name)
        self._tools[key] = shown
        self._tools.move_to_end(key)
        while len(self._tools) > self.MOST:
            self._tools.popitem(last=False)
        return None

    async def on_event_callback(self, *, invocation_context: InvocationContext, event: Event) -> Event | None:
        calls = event.get_function_calls()
        if not calls:
            return None
        offered = self._tools.get((invocation_context.invocation_id, event.author), frozenset())
        ids = [call.id for call in calls if call.id and (call.name in SHOWN_NAMES or call.name in offered)]
        if ids:
            event.custom_metadata = {**(event.custom_metadata or {}), SHOWN_CALLS_KEY: ids}
        # The event is tagged where it is: ADK asks no plugin after one that
        # returns an event, and the others after this one tag it too.
        return None

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        for key in [key for key in self._tools if key[0] == invocation_context.invocation_id]:
            del self._tools[key]


__all__ = [
    "FULL_VIEW",
    "SHOWN_CALLS_KEY",
    "SHOWN_NAMES",
    "EventView",
    "Projector",
    "ShownCallsPlugin",
    "public_error",
    "public_view",
]
