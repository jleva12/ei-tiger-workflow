"""Follow-up questions after a reply, for a chat to show above its composer.
Needs ``forge-common[adk]``.

A chat asks for them with a run: :data:`REQUEST_KEY` in the run's state delta,
``True`` or ``{"count": 3}``, as forge-ui's ``useAdkAssistant`` sends it with
``suggestFollowUps``. :class:`FollowUpsPlugin` goes on the ``App``::

    app = App(name="ca_x", root_agent=root, plugins=[FollowUpsPlugin()])

When the agent that took the turn ends it with a reply, the plugin asks the
model that wrote the reply (without thinking) for a few short messages the
person might send next, from the last few messages of the conversation, and
keeps them in the state under :data:`STATE_KEY`
(``{"suggestions": [...]}``; :func:`follow_ups_of` reads it). The event that
sets them is the turn's last, so the chat has them as soon as the turn ends.

Each turn starts by clearing the last turn's, so they always follow the last
reply. A turn that ends waiting on the person (a question, an approval, a
sign-in) or without a reply gets none, and so does a reply whose
``custom_metadata`` sets :data:`QUIET_KEY` (an intake's questions, which the
person answers rather than follows up), and so does a run that didn't ask: an
A2A call, another client, or an agent another runs as a tool (``AgentTool``),
which is given the caller's state but not its request.

Suggesting never fails a turn: a failure, or a model slower than ``timeout``,
is logged and the turn ends without them. The call goes straight to the
model, so other plugins (usage recording, the context window) don't see it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from google.adk.models.llm_request import LlmRequest
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from forge_common.adk.usage import MODEL_KEY

if TYPE_CHECKING:
    from google.adk.agents.base_agent import BaseAgent
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events.event import Event

__all__ = [
    "DEFAULT_INSTRUCTION",
    "MOST",
    "QUIET_KEY",
    "REQUEST_KEY",
    "STATE_KEY",
    "FollowUpsPlugin",
    "follow_ups_of",
    "parse_follow_ups",
    "requested_count",
]

log = logging.getLogger(__name__)

#: What a run sends in its state delta to get follow-ups: true, or {"count": n}.
REQUEST_KEY = "forge:suggest_follow_ups"

#: Where the last reply's follow-ups are kept: {"suggestions": [...]}, or None.
STATE_KEY = "forge:follow_ups"

#: The most follow-ups a run may ask for.
MOST = 5

#: A reply whose ``custom_metadata`` sets this to true gets none: it's a
#: question the person answers (an intake's), not a reply to follow up.
QUIET_KEY = "forge:no_follow_ups"

DEFAULT_INSTRUCTION = (
    "You suggest what a person might say next in a chat with an AI assistant. "
    "Given the end of the conversation, write {count} short follow-up messages "
    "the PERSON could send next, in their voice: specific to what was just "
    "discussed, each different, each under 60 characters, no numbering. Write "
    "in the conversation's language. Reply with only a JSON array of {count} "
    "strings."
)


def requested_count(value: object, default: int) -> int | None:
    """
    :param value: What a run sent under :data:`REQUEST_KEY`.
    :param default: How many when it doesn't say.
    :return: How many follow-ups it asked for (1 to :data:`MOST`); None when it didn't ask.
    """
    if value is True:
        return max(1, min(default, MOST))
    if isinstance(value, Mapping):
        count = value.get("count", default)
        if isinstance(count, int) and not isinstance(count, bool):
            return max(1, min(count, MOST))
        return max(1, min(default, MOST))
    return None


def follow_ups_of(state: Mapping[str, Any]) -> list[str]:
    """:return: The last reply's follow-ups in ``state``; none when it has none."""
    kept = state.get(STATE_KEY)
    suggestions = kept.get("suggestions") if isinstance(kept, Mapping) else None
    if not isinstance(suggestions, list):
        return []
    return [each for each in suggestions if isinstance(each, str) and each]


class FollowUpsPlugin(BasePlugin):
    """
    Suggests follow-up questions after a reply, for the runs that ask.

    :param count: How many a run gets when it doesn't say.
    :param recent_messages: How many of the conversation's last messages they're drawn from.
    :param message_chars: How much of each message is read.
    :param model: The model that writes them (``provider/model`` on
        ``ProviderModels``); the one that wrote the reply when None.
    :param thinking_level: How long that model thinks; "off" keeps them quick
        and cheap. Only ``ProviderModels`` (``select``) takes one.
    :param instruction: The model's instruction; ``{count}`` is filled in.
    :param timeout: Seconds to wait for them before ending the turn without.
    """

    def __init__(
        self,
        *,
        count: int = 3,
        recent_messages: int = 6,
        message_chars: int = 2_000,
        model: str | None = None,
        thinking_level: str = "off",
        instruction: str = DEFAULT_INSTRUCTION,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(name="forge_follow_ups")
        self.count = count
        self.recent_messages = recent_messages
        self.message_chars = message_chars
        self.model = model
        self.thinking_level = thinking_level
        self.instruction = instruction
        self.timeout = timeout
        # The agent that took each running invocation's turn, by invocation:
        # the first to start. Agents it hands to, or that hand back to it,
        # end before it does.
        self._takers: dict[str, str] = {}

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        invocation = callback_context.invocation_id
        if invocation in self._takers:
            return None
        self._takers[invocation] = agent.name
        # The last reply's go when the next turn starts.
        if callback_context.state.get(STATE_KEY) is not None:
            callback_context.state[STATE_KEY] = None
        return None

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        invocation = callback_context.invocation_id
        if self._takers.get(invocation) != agent.name:
            return None
        # Once a turn: the agent may run again within it (handed back to).
        self._takers[invocation] = ""
        try:
            suggestions = await asyncio.wait_for(
                self._suggest(agent, callback_context._invocation_context), self.timeout
            )
        except Exception:
            log.warning("Couldn't suggest follow-ups in %s", invocation, exc_info=True)
            return None
        if suggestions:
            callback_context.state[STATE_KEY] = {"suggestions": suggestions}
        return None

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        self._takers.pop(invocation_context.invocation_id, None)

    async def _suggest(self, agent: BaseAgent, ic: InvocationContext) -> list[str]:
        turn = _this_turn(ic)
        count = _requested(turn, self.count)
        reply = _reply(turn)
        llm = getattr(agent, "canonical_model", None)
        if count is None or reply is None or llm is None:
            return []
        transcript = self.transcript(ic.session.events)
        if not transcript:
            return []
        request = LlmRequest(
            contents=[types.Content(role="user", parts=[types.Part(text=transcript)])],
            config=types.GenerateContentConfig(
                system_instruction=self.instruction.format(count=count)
            ),
        )
        wrote = (reply.custom_metadata or {}).get(MODEL_KEY)
        select = getattr(llm, "select", None)
        if callable(select):
            # ProviderModels: the reply's model, or the one asked for, not thinking.
            select(request, self.model or wrote, self.thinking_level)
        else:
            request.model = self.model or getattr(llm, "model", None)
        text = ""
        async for response in llm.generate_content_async(request):
            if response.partial or not response.content:
                continue
            text += "".join(
                part.text for part in response.content.parts or () if part.text and not part.thought
            )
        suggestions = parse_follow_ups(text, count)
        if not suggestions:
            log.warning("Couldn't read follow-ups from %r", text[:200])
        return suggestions

    def transcript(self, events: Iterable[Event]) -> str:
        """The conversation's last messages, as "Person:" and "Assistant:" lines."""
        lines: list[str] = []
        for event in events:
            if event.partial or not event.content or not event.content.parts:
                continue
            text = " ".join(
                part.text for part in event.content.parts if part.text and not part.thought
            ).strip()
            if text:
                speaker = "Person" if event.author == "user" else "Assistant"
                lines.append(f"{speaker}: {text[: self.message_chars]}")
        recent = lines[-self.recent_messages :]
        return "\n\n".join(recent) if any(line.startswith("Assistant:") for line in recent) else ""


def parse_follow_ups(text: str, count: int) -> list[str]:
    """:return: The follow-ups in a model's answer: a JSON array of strings it may wrap."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    found: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip() and item.strip() not in found:
            found.append(item.strip())
    return found[:count]


def _this_turn(ic: InvocationContext) -> list[Event]:
    """The invocation's events so far, the person's message first."""
    events: list[Event] = []
    for event in reversed(ic.session.events):
        if event.invocation_id != ic.invocation_id:
            break
        events.append(event)
    events.reverse()
    return events


def _requested(turn: Iterable[Event], default: int) -> int | None:
    """How many follow-ups the run asked for: in the state its message set."""
    for event in turn:
        if event.author == "user" and REQUEST_KEY in (event.actions.state_delta or {}):
            return requested_count(event.actions.state_delta[REQUEST_KEY], default)
    return None


def _reply(turn: Iterable[Event]) -> Event | None:
    """The turn's last message, when it's a reply: text, not waiting on anything."""
    last = None
    for event in turn:
        if event.author != "user" and not event.partial and event.content and event.content.parts:
            last = event
    if last is None or last.error_code or last.long_running_tool_ids or last.get_function_calls():
        return None
    if (last.custom_metadata or {}).get(QUIET_KEY):
        return None
    parts = last.content.parts if last.content else None
    return last if any(part.text and not part.thought for part in parts or ()) else None
