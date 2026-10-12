"""How full each ADK agent's context window is, kept in the session's state,
and compaction while the agent is still in its turn. Needs ``forge-common[adk]``.

ADK compacts a session between invocations (an ``App``'s
``EventsCompactionConfig``: every N turns, or once the prompt the model last
reported passed a token threshold). Inside one invocation it only checks that
last reported prompt, which doesn't count what tools returned since, so a turn
that reads many files or pages outgrows the model's window before anything
compacts it.

:class:`ContextWindowPlugin` goes on an ``App`` beside the others::

    plugins = [usage, ContextWindowPlugin(window=windows_from(models))]
    app = App(name="ca_x", root_agent=root, plugins=plugins)

Before each model call it measures the request about to go out (instructions,
tool declarations and messages) and keeps the count in the state, one key per
agent (:func:`state_key`; :func:`context_of` reads it). When the request
reaches ``threshold`` of what the model may read (its window less what's kept
for the reply), or something asked for it (:func:`request_compaction`), it
summarizes the older part of what the agent sees, and rebuilds the request
with the summary in its place. What stays as it was:

- what started the agent's current turn (the user's message, or its input in a
  workflow), so the task stays word for word;
- its last ``keep_steps`` model steps (a reply and the tool results it got), so
  it reads what it just asked for; fewer when they alone fill half its window.

What came before the turn's start becomes one summary and the turn's earlier
steps another, each only when it's worth one; a later compaction folds the
earlier summaries into its own. Summaries reach the model as user messages
that say what they are.

Summaries are ADK compaction events (``EventCompaction``) that only their
agent reads: they're kept in the state (:func:`summaries_key`: by agent,
branch and isolation scope) and added to the agent's view for each of its
requests, where ADK's own contents step applies them. They're never appended
to the session, because ADK hides a compaction's whole time range from every
agent that sees the event, and an agent on no branch sees every branch: a
branch's summary would hide its siblings' events from it. So branches side by
side, steps in a workflow, and agents later in the run see the events as they
were, and compact their own view of them when they need to.

Agents that work apart from the conversation compact the same way: a task
(``mode="task"``, in its own isolation scope) and an agent another calls as a
tool (``AgentTool``, which runs it in a session of its own with the caller's
plugins and state). Each of their calls starts from what it was handed, so
their summaries go once the call is over (a task's once it finished), and a
summary that covers none of the session's events (one copied in with the
caller's state) is dropped.

Counts are estimates: characters over characters per token, which starts at
3.5 and then follows what the provider reported for each request the agent
sent. Once the model answers, the count is the provider's (prompt and reply).

Neither measuring nor compacting ever fails a turn: a failure is logged and
the request goes out as it was. A summary's model call goes straight to the
summarizer's model, so other plugins (usage recording) don't see it.

Built on ADK 2.10's contents step (``google.adk.flows.llm_flows.contents``):
which events an agent sees, and how its request is built from them.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, Literal

from google.adk.agents.llm.task._finish_task_tool import (
    FINISH_TASK_ERROR_RESULT,
    FINISH_TASK_SUCCESS_RESULT,
    FINISH_TASK_TOOL_NAME,
)
from google.adk.apps.base_events_summarizer import BaseEventsSummarizer
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.events._rewind_events import _apply_rewinds
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions, EventCompaction
from google.adk.flows.llm_flows import contents as adk_contents
from google.adk.models.google_llm import Gemini
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from forge_common.adk.usage import MODEL_KEY, tokens_of

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.sessions.state import State

__all__ = [
    "DEFAULT_PROMPT",
    "DEFAULT_WINDOW",
    "STATE_PREFIX",
    "SUMMARIES_PREFIX",
    "ContextWindowPlugin",
    "Size",
    "Span",
    "Window",
    "context_of",
    "measure",
    "plan_compaction",
    "request_compaction",
    "state_key",
    "summaries_key",
    "summaries_of",
    "windows_from",
]

log = logging.getLogger(__name__)

#: Each agent's context window is kept in the state under this and its name.
STATE_PREFIX = "forge:context:"

#: Each agent's summaries are kept in the state under this, its name, branch and scope.
SUMMARIES_PREFIX = "forge:summaries:"

#: Characters per token until a model call says otherwise. ADK guesses 4;
#: code and JSON, which tool results mostly are, run nearer 3.
DEFAULT_CHARS_PER_TOKEN = 3.5
_CHARS_PER_TOKEN = (1.5, 8.0)

#: Tokens guessed for each image, audio clip or file a request carries.
MEDIA_TOKENS = 1_500

DEFAULT_PROMPT = (
    "You are compacting the working context of an AI agent so it can carry on "
    "a long task within its model's context window. Below is the part of its "
    "history the summary replaces: messages, other agents' work it was shown, "
    "tool calls and (abridged) tool results, possibly starting from earlier "
    "summaries.\n\n"
    "Write a summary the agent can continue from without the original. Keep:\n"
    "- what it was asked to do, quoted exactly where the wording matters;\n"
    "- every concrete finding it will need later: names, file paths, URLs, "
    "identifiers, numbers, decisions, and short verbatim excerpts that matter;\n"
    "- which tools were called with which inputs, and what each established, so "
    "work isn't repeated;\n"
    "- what is done, what is still open, and the next step it planned.\n"
    "Write in the conversation's language. Add nothing that isn't in the history."
    "\n\n{conversation_history}"
)

Part = Literal["history", "turn"]

_FRAMES: dict[str, str] = {
    "history": "Summary of the conversation before the current request",
    "turn": "Summary of the work on the current request so far",
}
_FRAME_END = (
    ", compacted to fit the model's context window. Tool results in it are "
    "abridged: call a tool again when you need one in full.\n\n"
)


# ------------------------------------------------------------------- the state


def state_key(agent: str) -> str:
    """:return: The state key ``agent``'s context window is kept under."""
    return STATE_PREFIX + agent


def summaries_key(agent: str, branch: str | None = None, scope: str | None = None) -> str:
    """
    :return: The state key ``agent``'s summaries are kept under, on its
        branch and in its isolation scope (an agent on two branches, or in two
        scopes, sees two different conversations).
    """
    return (
        SUMMARIES_PREFIX + agent + (f"@{branch}" if branch else "") + (f"#{scope}" if scope else "")
    )


def summaries_of(
    state: State | Mapping[str, Any],
    agent: str,
    branch: str | None = None,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """
    :return: ``agent``'s summaries on its branch and in its scope, oldest
        first: each its ``part`` (history, turn), the ``start`` and ``end`` of
        what it replaces (event timestamps), its ``text`` and how many
        ``events`` it took in.
    """
    found = state.get(summaries_key(agent, branch, scope))
    if not isinstance(found, list):
        return []
    return [
        dict(entry)
        for entry in found
        if isinstance(entry, Mapping)
        and isinstance(entry.get("start"), int | float)
        and isinstance(entry.get("end"), int | float)
        and isinstance(entry.get("text"), str)
    ]


def context_of(state: State | Mapping[str, Any], agent: str) -> dict[str, Any] | None:
    """
    An agent's context window, as the plugin last kept it:

    - ``tokens``: what its conversation holds: the request it last sent
      (estimated, after any compaction), or once the model answered, that
      request's prompt and the reply as the provider counted them;
    - ``limit``: what a request may hold: the model's ``window`` less the
      ``reply`` kept for its answer; ``threshold``: where it compacts, the
      ``share`` of the limit; ``auto``: whether it compacts there by itself;
    - ``counted``: ``estimate`` (the request about to go out) or ``provider``
      (what the model reported); ``updated``: when, in seconds since the epoch;
    - ``model``: the model that last answered; ``reported``: the prompt the
      provider counted then;
    - ``compactions``: how many times it was compacted; ``last_compaction``:
      ``at``, ``tokens_before``, ``tokens_after`` and how many ``summaries``
      it has (:func:`summaries_of` reads them);
    - ``requested``: a compaction was asked for (:func:`request_compaction`);
    - ``apart``: it works apart from the conversation, on a branch or in an
      isolation scope (a task, an agent called as a tool, a parallel branch),
      so each of its calls starts from what it was handed, not the conversation.

    :param state: The session's state (any context's ``state``).
    :param agent: The agent's name.
    """
    found = state.get(state_key(agent))
    return dict(found) if isinstance(found, Mapping) else None


def request_compaction(context: CallbackContext, agent: str | None = None) -> None:
    """
    Asks for ``agent``'s context to be compacted before its next model call,
    however full it is: from a tool (its ``tool_context``) or a callback.

    :param context: A context of the session.
    :param agent: The agent's name; the context's own by default.
    """
    name = agent or context.agent_name
    context.state[state_key(name)] = {**(context_of(context.state, name) or {}), "requested": True}


# ------------------------------------------------------------- what's measured


@dataclass(frozen=True)
class Window:
    """
    What a model reads.

    :ivar tokens: Its context window.
    :ivar reply: Of it, what's kept for the reply (its most output tokens).
    """

    tokens: int
    reply: int = 0

    @property
    def prompt(self) -> int:
        """The most a request may hold."""
        return max(1, self.tokens - self.reply)


WindowOf = Callable[[str], "Window | None"]

#: A model the plugin can't look up: model_provider.yaml's defaults.
DEFAULT_WINDOW = Window(128_000, 16_384)


def windows_from(models: Any) -> WindowOf | None:
    """
    :param models: The ``ProviderModels`` agents run on (anything else: none).
    :return: A model's window as its configuration sets it (``contextWindow``,
        ``maxTokens``), by ``provider/model`` or its id.
    """
    config = getattr(models, "config", None)
    find = getattr(config, "find", None)
    if find is None:
        return None

    def window(model: str) -> Window | None:
        found = find(model)
        return (
            Window(found.model.context_window, found.model.max_tokens)
            if found is not None
            else None
        )

    return window


@dataclass(frozen=True)
class Size:
    """
    How much a model reads.

    :ivar chars: Its text (instructions, messages, tool calls, results and
        declarations), in characters.
    :ivar media: Tokens guessed for its images, audio and files.
    """

    chars: int = 0
    media: int = 0

    def __add__(self, other: Size) -> Size:
        return Size(self.chars + other.chars, self.media + other.media)

    def tokens(self, chars_per_token: float) -> int:
        return math.ceil(self.chars / chars_per_token) + self.media


def measure(request: LlmRequest) -> Size:
    """:return: How much the model reads of ``request``."""
    size = _sum(content_size(content) for content in request.contents)
    config = request.config
    if config is None:
        return size
    size += _instruction_size(config.system_instruction)
    for tool in config.tools or ():
        size += Size(len(_json(tool)))
    schema = config.response_json_schema or config.response_schema
    if schema is not None:
        size += Size(len(_json(schema)))
    return size


def content_size(content: types.Content | None) -> Size:
    """:return: How much the model reads of one message."""
    if content is None:
        return Size()
    return _sum(_part_size(part) for part in content.parts or ())


def _part_size(part: types.Part) -> Size:
    chars = len(part.text or "")
    media = 0
    if part.function_call is not None:
        chars += len(part.function_call.name or "") + len(_json(part.function_call.args))
    if part.function_response is not None:
        response = part.function_response
        chars += len(response.name or "") + len(_json(response.response))
        media += sum(
            _media(nested.inline_data, nested.file_data) for nested in response.parts or ()
        )
    if part.executable_code is not None:
        chars += len(part.executable_code.code or "")
    if part.code_execution_result is not None:
        chars += len(part.code_execution_result.output or "")
    blob = part.inline_data
    if blob is not None and (blob.mime_type or "").startswith(("text/", "application/json")):
        chars += len(blob.data or b"")
        blob = None
    return Size(chars, media + _media(blob, part.file_data))


def _media(blob: object, file: object) -> int:
    return MEDIA_TOKENS * ((blob is not None) + (file is not None))


def _instruction_size(instruction: object) -> Size:
    if instruction is None:
        return Size()
    if isinstance(instruction, str):
        return Size(len(instruction))
    if isinstance(instruction, types.Content):
        return content_size(instruction)
    if isinstance(instruction, types.Part):
        return _part_size(instruction)
    if isinstance(instruction, Iterable):
        return _sum(_instruction_size(item) for item in instruction)
    return Size(len(str(instruction)))


def _sum(sizes: Iterable[Size]) -> Size:
    return sum(sizes, Size())


def _json(value: object) -> str:
    if value is None:
        return ""
    dump = getattr(value, "model_dump_json", None)
    if dump is not None:
        return str(dump(exclude_none=True))
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _chars_per_token(value: object) -> float:
    low, high = _CHARS_PER_TOKEN
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return min(high, max(low, float(value)))
    return DEFAULT_CHARS_PER_TOKEN


# ------------------------------------------------------------ what's compacted


@dataclass(frozen=True)
class Span:
    """
    One summary to make.

    :ivar part: ``history``: before the agent's current turn; ``turn``: since it started.
    :ivar events: What it replaces, oldest first: raw events, and earlier
        summaries (compaction events) it folds in.
    """

    part: Part
    events: tuple[Event, ...]

    @property
    def start(self) -> float:
        return min(_start_of(event) for event in self.events)

    @property
    def end(self) -> float:
        return max(_end_of(event) for event in self.events)


def plan_compaction(
    events: Sequence[Event],
    *,
    agent: str,
    branch: str | None = None,
    scope: str | None = None,
    own_turn: bool = False,
    keep_steps: int = 1,
    tail_tokens: int | None = None,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    min_tokens: int = 0,
) -> list[Span]:
    """
    What compacting ``agent``'s view of the session would summarize: the
    events ADK's contents step shows it, on its branch and in its scope.

    :param events: The session's events, and the agent's summaries as
        compaction events.
    :param agent: The agent's name.
    :param branch: Its invocation's branch.
    :param scope: Its isolation scope.
    :param own_turn: It sees only its current turn (``include_contents`` none).
    :param keep_steps: How many of its last model steps stay as they are.
    :param tail_tokens: The most those steps may hold; fewer stay when they'd hold more.
    :param chars_per_token: To estimate tokens with.
    :param min_tokens: A span whose raw events hold fewer isn't worth a summary.
    :return: At most two spans: before the turn's start, then since.
    """
    live = _apply_rewinds(list(events))
    anchor = _turn_start(live, agent, branch, scope)
    if own_turn and anchor is None:
        return []
    view = live[anchor:] if own_turn and anchor is not None else live
    visible = [
        e
        for e in view
        if adk_contents._should_include_event_in_context(branch, e, isolation_scope=scope)
    ]
    summaries = _active([e for e in visible if _is_summary(e)])
    raw = [e for e in visible if not e.actions.compaction and not _covered(e.timestamp, summaries)]
    starts_at = live[anchor].timestamp if anchor is not None else -math.inf

    history = [] if own_turn else [e for e in raw if e.timestamp < starts_at]
    turn = [e for e in raw if e.timestamp > starts_at]
    turn = turn[: _tail(turn, agent, keep_steps, tail_tokens, chars_per_token)]

    parts: tuple[tuple[Part, list[Event], float, float], ...] = (
        ("history", history, -math.inf, starts_at),
        ("turn", turn, starts_at, math.inf),
    )
    spans = []
    for part, chosen, above, below in parts:
        folded = [s for s in summaries if above < _start_of(s) and _end_of(s) < below]
        span = _span(part, chosen, folded, agent)
        if span is not None and _tokens(_raw(span.events), chars_per_token) >= max(min_tokens, 1):
            spans.append(span)
    return spans


def _turn_start(
    events: Sequence[Event], agent: str, branch: str | None, scope: str | None
) -> int | None:
    """Where ``agent``'s current turn starts, as ADK finds it for ``include_contents`` none."""
    unanswered: set[str] = set()
    for i in range(len(events) - 1, -1, -1):
        event = events[i]
        unanswered -= {call.id for call in event.get_function_calls() if call.id}
        submitted = adk_contents._is_submitted_tool_result(event)
        if submitted:
            unanswered |= {
                response.id for response in event.get_function_responses() if response.id
            }
        if (
            not unanswered
            and not submitted
            and not event.actions.compaction
            and adk_contents._should_include_event_in_context(branch, event, isolation_scope=scope)
            and (event.author == "user" or adk_contents._is_other_agent_reply(agent, event))
            and not adk_contents._is_direct_transfer(event)
        ):
            return i
    return None


def _is_summary(event: Event) -> bool:
    return event.actions.compaction is not None


def _start_of(event: Event) -> float:
    compaction = event.actions.compaction
    return compaction.start_timestamp if compaction is not None else event.timestamp


def _end_of(event: Event) -> float:
    compaction = event.actions.compaction
    return compaction.end_timestamp if compaction is not None else event.timestamp


def _active(summaries: Sequence[Event]) -> list[Event]:
    """The summaries no other covers, as ADK's contents step keeps them."""

    def subsumed(i: int, mine: Event) -> bool:
        for j, theirs in enumerate(summaries):
            if j == i:
                continue
            within = _start_of(theirs) <= _start_of(mine) and _end_of(theirs) >= _end_of(mine)
            wider = _start_of(theirs) < _start_of(mine) or _end_of(theirs) > _end_of(mine)
            if within and (wider or j > i):
                return True
        return False

    return [event for i, event in enumerate(summaries) if not subsumed(i, event)]


def _covered(at: float, summaries: Iterable[Event]) -> bool:
    return any(_start_of(s) <= at <= _end_of(s) for s in summaries)


def _tail(
    turn: Sequence[Event], agent: str, keep_steps: int, tail_tokens: int | None, ratio: float
) -> int:
    """
    Where what stays starts: the ``keep_steps``-th last model step, or a
    later one when they'd hold too much.
    """
    steps = [
        i
        for i, event in enumerate(turn)
        if event.author == agent and event.content is not None and event.content.role == "model"
    ]
    for kept in range(min(max(keep_steps, 0), len(steps)), 0, -1):
        start = steps[-kept]
        if tail_tokens is None or _tokens(turn[start:], ratio) <= tail_tokens:
            return start
    return len(turn)


def _span(part: Part, chosen: Sequence[Event], folded: Sequence[Event], agent: str) -> Span | None:
    """
    ``chosen`` and the summaries among it, cut to where none of the agent's
    calls is left unanswered.
    """
    whole = _self_contained(sorted([*folded, *chosen], key=_start_of), agent)
    return Span(part, tuple(whole)) if _raw(whole) else None


def _self_contained(events: list[Event], agent: str) -> list[Event]:
    """
    The longest stretch of ``events`` whose calls by ``agent`` are all answered
    in it, and whose answers' calls are in it. Other agents' calls reach it
    as text, so they needn't pair.
    """

    def calls(event: Event) -> set[str]:
        if event.author != agent:
            return set()
        return {call.id for call in event.get_function_calls() if call.id}

    def answers(event: Event) -> set[str]:
        if event.author != agent and not adk_contents._is_submitted_tool_result(event):
            return set()
        return {response.id for response in event.get_function_responses() if response.id}

    made = set().union(*(calls(event) for event in events))
    first = 0
    for i, event in enumerate(events):
        if answers(event) - made:
            first = i + 1
    events = events[first:]
    unanswered: set[str] = set()
    last = 0
    for i, event in enumerate(events):
        unanswered -= answers(event)
        unanswered |= calls(event)
        if event.author == agent:
            unanswered |= set(event.actions.requested_tool_confirmations)
            unanswered |= set(event.actions.requested_auth_configs)
        if not unanswered:
            last = i + 1
    return events[:last]


def _raw(events: Iterable[Event]) -> list[Event]:
    return [e for e in events if not e.actions.compaction]


def _tokens(events: Iterable[Event], ratio: float) -> int:
    return _sum(content_size(e.content) for e in events).tokens(ratio)


def _as_events(
    summaries: Sequence[Mapping[str, Any]],
    *,
    agent: str,
    branch: str | None,
    scope: str | None,
    invocation_id: str,
) -> list[Event]:
    """An agent's summaries as the compaction events ADK's contents step applies."""
    return [
        Event(
            invocation_id=invocation_id,
            author=agent,
            branch=branch,
            isolation_scope=scope,
            timestamp=float(entry["end"]),
            actions=EventActions(
                compaction=EventCompaction(
                    start_timestamp=float(entry["start"]),
                    end_timestamp=float(entry["end"]),
                    compacted_content=types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                text=_FRAMES.get(str(entry.get("part")), _FRAMES["turn"])
                                + _FRAME_END
                                + str(entry["text"])
                            )
                        ],
                    ),
                )
            ),
        )
        for entry in summaries
    ]


# ------------------------------------------------------------------ the plugin


@dataclass(frozen=True)
class _View:
    """What a request of the agent's is built from: its invocation, and its summaries."""

    ic: InvocationContext
    agent: Any
    key: str
    summaries: list[dict[str, Any]]

    @property
    def own_turn(self) -> bool:
        return getattr(self.agent, "include_contents", "default") == "none"

    def events(self, summaries: Sequence[Mapping[str, Any]] | None = None) -> list[Event]:
        """The session's events, and the agent's summaries after them."""
        return [
            *self.ic.session.events,
            *_as_events(
                self.summaries if summaries is None else summaries,
                agent=self.agent.name,
                branch=self.ic.branch,
                scope=self.ic.isolation_scope,
                invocation_id=self.ic.invocation_id,
            ),
        ]


class ContextWindowPlugin(BasePlugin):
    """
    Keeps each agent's context window in the state, and compacts it mid-turn.

    :param window: A model's window: the one the next request runs on (the
        person's choice in ``model_key``), else the one that last answered the
        agent, else the one its request names; :func:`windows_from` reads
        model_provider.yaml's.
    :param default: The window of a model ``window`` doesn't know.
    :param threshold: How much of what a request may hold makes it compact (0–1].
    :param auto: Compact at the threshold. When False it only keeps count, and
        compacts when asked to (:func:`request_compaction`).
    :param keep_steps: How many of the agent's last model steps stay as they are.
    :param summarizer: Makes the summaries; by default ADK's ``LlmEventSummarizer``
        on the agent's own model, with ``prompt``.
    :param prompt: The default summarizer's prompt, with ``{conversation_history}``.
    :param model_key: The state key the person's choice of model is in, as the
        assistant's and chat agents' model pickers send it; None when nothing
        chooses models through the state.
    :param clock: The time now, in seconds since the epoch (``updated``).
    """

    def __init__(
        self,
        *,
        window: WindowOf | None = None,
        default: Window = DEFAULT_WINDOW,
        threshold: float = 0.8,
        auto: bool = True,
        keep_steps: int = 1,
        summarizer: BaseEventsSummarizer | None = None,
        prompt: str = DEFAULT_PROMPT,
        model_key: str | None = "model",
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(name="forge_context_window")
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be more than 0 and at most 1")
        self.window = window
        self.default = default
        self.threshold = threshold
        self.auto = auto
        self.keep_steps = keep_steps
        self.summarizer = summarizer
        self.prompt = prompt
        self.model_key = model_key
        self.clock = clock
        # The agents being called as tools (AgentTool) right now: how many
        # calls each, by name (each runs a copy of the agent, sharing this
        # plugin, in a session of its own).
        self._called: dict[str, int] = {}

    async def before_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any
    ) -> dict[str, Any] | None:
        if isinstance(tool, AgentTool):
            name = tool.agent.name
            self._called[name] = self._called.get(name, 0) + 1
        return None

    async def after_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, result: Any
    ) -> dict[str, Any] | None:
        self._returned(tool)
        if tool.name == FINISH_TASK_TOOL_NAME and _terminal(result):
            # A task that finished leaves no summaries: its next call starts afresh.
            try:
                ic: InvocationContext = tool_context._invocation_context
                key = summaries_key(tool_context.agent_name, ic.branch, ic.isolation_scope)
                if tool_context.state.get(key):
                    tool_context.state[key] = None
            except Exception:
                log.warning(
                    "context window: %s's summaries weren't cleared",
                    tool_context.agent_name,
                    exc_info=True,
                )
        return None

    async def on_tool_error_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, error: Exception
    ) -> dict[str, Any] | None:
        self._returned(tool)
        return None

    def _returned(self, tool: Any) -> None:
        if not isinstance(tool, AgentTool):
            return
        name = tool.agent.name
        left = self._called.get(name, 0) - 1
        if left > 0:
            self._called[name] = left
        else:
            self._called.pop(name, None)

    def _called_as_tool(self, ic: InvocationContext) -> bool:
        """Whether ``ic``'s run is of an agent another is calling as a tool."""
        root: Any = ic.agent
        while getattr(root, "parent_agent", None) is not None:
            root = root.parent_agent
        return getattr(root, "name", None) in self._called

    async def after_agent_callback(
        self, *, agent: Any, callback_context: CallbackContext
    ) -> types.Content | None:
        # A call as a tool that's over leaves no summaries: the next starts afresh.
        try:
            ic: InvocationContext = callback_context._invocation_context
            key = summaries_key(agent.name, ic.branch, ic.isolation_scope)
            if callback_context.state.get(key) and self._called_as_tool(ic):
                callback_context.state[key] = None
        except Exception:
            log.warning("context window: %s's summaries weren't cleared", agent.name, exc_info=True)
        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        try:
            await self._before(callback_context, llm_request)
        except Exception:
            log.warning(
                "context window: %s's request wasn't measured or compacted",
                callback_context.agent_name,
                exc_info=True,
            )
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        # Streamed fragments carry running counts; the call's last response has them all.
        if llm_response.partial or llm_response.usage_metadata is None:
            return None
        try:
            agent = callback_context.agent_name
            counted = tokens_of(llm_response.usage_metadata)
            if counted.input <= 0:
                return None
            kept = context_of(callback_context.state, agent) or {}
            sent = int(kept.get("sent_chars") or 0)
            text_tokens = counted.input - int(kept.get("sent_media") or 0)
            if sent > 0 and text_tokens > 0:
                # Halfway to what this call says, so one odd request doesn't swing it.
                ratio = (_chars_per_token(kept.get("chars_per_token")) + sent / text_tokens) / 2
                kept["chars_per_token"] = round(_chars_per_token(ratio), 3)
            kept.update(
                tokens=counted.input + counted.output,
                reported=counted.input,
                model=_model_of(llm_response),
                counted="provider",
                updated=round(self.clock(), 3),
            )
            callback_context.state[state_key(agent)] = kept
        except Exception:
            log.warning(
                "context window: %s's count wasn't kept", callback_context.agent_name, exc_info=True
            )
        return None

    # ---------------------------------------------------------- before a call

    async def _before(self, context: CallbackContext, request: LlmRequest) -> None:
        agent_name = context.agent_name
        kept = context_of(context.state, agent_name) or {}
        requested = bool(kept.pop("requested", False))
        ratio = _chars_per_token(kept.get("chars_per_token"))
        # The model the request runs on: one just chosen counts from its first request.
        chosen = context.state.get(self.model_key) if self.model_key else None
        window = self._window_of(chosen, kept.get("model"), request.model)
        limit = window.prompt
        threshold = max(1, math.floor(limit * self.threshold))
        view = self._view(context, request)
        if view is not None and view.summaries:
            # What it summarized before stays summarized.
            self._rebuild(view, request, [])
        size = measure(request)
        tokens = size.tokens(ratio)
        if view is not None and (requested or (self.auto and tokens >= threshold)):
            summaries = await self._compact(view, limit=limit, ratio=ratio, asked=requested)
            if summaries is not None:
                self._rebuild(view, request, view.summaries, summaries)
                context.state[view.key] = summaries
                after = measure(request)
                kept["compactions"] = int(kept.get("compactions") or 0) + 1
                kept["last_compaction"] = {
                    "at": round(self.clock(), 3),
                    "invocation_id": context.invocation_id,
                    "tokens_before": tokens,
                    "tokens_after": after.tokens(ratio),
                    "summaries": len(summaries),
                }
                size, tokens = after, after.tokens(ratio)
        if tokens > limit:
            log.warning(
                "context window: %s's request holds ~%d tokens, more than its %d",
                agent_name,
                tokens,
                limit,
            )
        ic: InvocationContext = context._invocation_context
        kept.update(
            tokens=tokens,
            limit=limit,
            threshold=threshold,
            window=window.tokens,
            reply=window.reply,
            share=self.threshold,
            auto=self.auto,
            apart=bool(ic.branch or ic.isolation_scope) or self._called_as_tool(ic),
            counted="estimate",
            updated=round(self.clock(), 3),
            chars_per_token=round(ratio, 3),
            sent_chars=size.chars,
            sent_media=size.media,
        )
        context.state[state_key(agent_name)] = kept

    def _window_of(self, *models: object) -> Window:
        for model in models:
            if isinstance(model, str) and model and self.window is not None:
                found = self.window(model)
                if found is not None:
                    return found
        return self.default

    def _view(self, context: CallbackContext, request: LlmRequest) -> _View | None:
        """The agent's view, when it's one ADK builds from the session (an LLM agent's)."""
        # A stateful Interactions request carries only its turn: the service keeps the rest.
        if request.previous_interaction_id:
            return None
        # The invocation the request was built in: the callback context's view
        # of it can differ (its isolation scope).
        ic: InvocationContext = context._invocation_context
        agent: Any = ic.agent
        if not hasattr(agent, "canonical_model") or not hasattr(agent, "include_contents"):
            return None
        key = summaries_key(agent.name, ic.branch, ic.isolation_scope)
        kept = summaries_of(context.state, agent.name, ic.branch, ic.isolation_scope)
        # A summary of none of this session's events came in with another's state.
        summaries = [s for s in kept if _covers_any(s, ic.session.events)]
        if len(summaries) != len(kept):
            context.state[key] = summaries or None
        view = _View(ic, agent, key, summaries)
        if view.own_turn and summaries:
            # An agent that sees only its turn sees none of an earlier turn's summaries.
            anchor = _turn_start(ic.session.events, agent.name, ic.branch, ic.isolation_scope)
            starts_at = ic.session.events[anchor].timestamp if anchor is not None else math.inf
            current = [s for s in summaries if s["start"] > starts_at]
            if len(current) != len(summaries):
                context.state[key] = current
                view = _View(ic, agent, key, current)
        return view

    def _rebuild(
        self,
        view: _View,
        request: LlmRequest,
        before: Sequence[Mapping[str, Any]],
        after: Sequence[Mapping[str, Any]] | None = None,
    ) -> bool:
        """
        The request's messages as ADK builds them with the ``after`` summaries
        (the agent's own when None), not ``before``.
        """
        rebuilt = _splice(
            request.contents,
            _contents_of(view.ic, view.agent, view.events(before)),
            _contents_of(view.ic, view.agent, view.events(after)),
        )
        if rebuilt is None:
            log.warning(
                "context window: %s's request couldn't be rebuilt with its summaries; "
                "it goes out as it was",
                view.agent.name,
            )
            return False
        request.contents = rebuilt
        return True

    async def _compact(
        self, view: _View, *, limit: int, ratio: float, asked: bool
    ) -> list[dict[str, Any]] | None:
        """:return: The agent's summaries after compacting, or None when nothing was."""
        name = view.agent.name
        spans = plan_compaction(
            view.events(),
            agent=name,
            branch=view.ic.branch,
            scope=view.ic.isolation_scope,
            own_turn=view.own_turn,
            keep_steps=self.keep_steps,
            tail_tokens=limit // 2,
            chars_per_token=ratio,
            # Asked for: any span; else one worth a twentieth of the window.
            min_tokens=0 if asked else limit // 20,
        )
        made: list[dict[str, Any]] = []
        for span in spans:
            try:
                text = await self._summarize(view.agent, span)
            except Exception:
                log.warning(
                    "context window: %s's %s wasn't summarized", name, span.part, exc_info=True
                )
                continue
            if text is not None:
                made.append(
                    {
                        "part": span.part,
                        "start": span.start,
                        "end": span.end,
                        "text": text,
                        "events": len(_raw(span.events)),
                    }
                )
        if not made:
            log.info("context window: %s has nothing to compact", name)
            return None
        log.info(
            "context window: compacted %s's %s",
            name,
            " and ".join(str(entry["part"]) for entry in made),
        )
        # An earlier summary a new one folded in goes.
        earlier = [
            s
            for s in view.summaries
            if not any(m["start"] <= s["start"] and s["end"] <= m["end"] for m in made)
        ]
        return sorted([*earlier, *made], key=lambda entry: float(entry["start"]))

    async def _summarize(self, agent: Any, span: Span) -> str | None:
        summarizer = self.summarizer or LlmEventSummarizer(
            llm=agent.canonical_model, prompt_template=self.prompt
        )
        made = await summarizer.maybe_summarize_events(
            events=[_as_read(event) for event in span.events]
        )
        if made is None:
            return None
        content = (
            made.actions.compaction.compacted_content if made.actions.compaction else made.content
        )
        parts = content.parts if content is not None and content.parts else []
        return "".join(part.text or "" for part in parts if not part.thought).strip() or None


def _as_read(event: Event) -> Event:
    """An earlier summary as a summarizer reads it: what it says, at its start."""
    compaction = event.actions.compaction
    if compaction is None:
        return event
    return Event(
        invocation_id=event.invocation_id,
        author=event.author or "model",
        timestamp=compaction.start_timestamp,
        content=compaction.compacted_content,
    )


def _covers_any(summary: Mapping[str, Any], events: Sequence[Event]) -> bool:
    start, end = float(summary["start"]), float(summary["end"])
    return any(start <= event.timestamp <= end for event in events)


def _terminal(result: object) -> bool:
    """Whether ``finish_task`` returned this as the task's end (not a retry)."""
    if isinstance(result, Mapping):
        result = result.get("result")
    return result in (FINISH_TASK_SUCCESS_RESULT, FINISH_TASK_ERROR_RESULT)


def _model_of(response: LlmResponse) -> str | None:
    stamped = (response.custom_metadata or {}).get(MODEL_KEY)
    return stamped if isinstance(stamped, str) and stamped else response.model_version


# --------------------------------------------------------- rebuilding requests


def _contents_of(ic: InvocationContext, agent: Any, events: Sequence[Event]) -> list[types.Content]:
    """The messages ADK's contents step builds from ``events``, without what it adds beside them."""
    model = agent.canonical_model
    preserve_ids = (
        model.use_interactions_api
        if isinstance(model, Gemini)
        else isinstance(model, adk_contents._id_pairing_model_types())
    )
    common: dict[str, Any] = {
        "preserve_function_call_ids": preserve_ids,
        "isolation_scope": ic.isolation_scope,
        "is_single_turn": getattr(agent, "mode", None) == "single_turn",
        "user_content": ic.user_content,
    }
    if agent.include_contents == "default":
        run_config = ic.run_config
        return adk_contents._get_contents(
            ic.branch,
            list(events),
            agent.name,
            include_thoughts_from_other_agents=bool(
                run_config and run_config.include_thoughts_from_other_agents
            ),
            **common,
        )
    return adk_contents._get_current_turn_contents(
        ic.branch, list(events), agent.name, include_thoughts_from_other_agents=False, **common
    )


def _splice(
    current: list[types.Content], old: list[types.Content], new: list[types.Content]
) -> list[types.Content] | None:
    """
    ``current`` with what changed from ``old`` to ``new`` changed in it too.
    What else the request carries among them (instructions or context ADK put
    there, what tools added) stays where it was.

    :return: None when ``old`` isn't all in ``current``, in order.
    """
    keys = [_key(c) for c in current]
    old_keys = [_key(c) for c in old]
    where: dict[int, int] = {}
    for tag, i1, i2, j1, _ in SequenceMatcher(None, old_keys, keys, autojunk=False).get_opcodes():
        if tag == "equal":
            where.update((i1 + k, j1 + k) for k in range(i2 - i1))
    if len(where) != len(old):
        return None
    ours = set(where.values())
    result = list(current)
    edits = SequenceMatcher(None, old_keys, [_key(c) for c in new], autojunk=False).get_opcodes()
    # From the end, so the positions of earlier edits hold.
    for tag, i1, i2, j1, j2 in reversed(edits):
        if tag == "equal":
            continue
        if i1 < i2:
            start, stop = where[i1], where[i2 - 1] + 1
        elif i1 < len(old):
            start = stop = where[i1]
        else:
            start = stop = where[len(old) - 1] + 1 if old else len(current)
        besides = [result[k] for k in range(start, stop) if k not in ours]
        result[start:stop] = [*new[j1:j2], *besides]
    return result


def _key(content: types.Content) -> str:
    return content.model_dump_json(exclude_none=True)
