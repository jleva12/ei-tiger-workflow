"""What the context window plugin keeps of each agent's context, and how it
compacts an agent that outgrows its window in the middle of a turn: through
ADK's runner, with a scripted model and a tool that reads big files, for one
agent and for branches side by side."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.apps import App
from google.adk.apps.base_events_summarizer import BaseEventsSummarizer
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions, EventCompaction
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, Session
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import Field

from forge_common.adk.context_window import (
    MEDIA_TOKENS,
    ContextWindowPlugin,
    Size,
    Window,
    context_of,
    measure,
    plan_compaction,
    request_compaction,
    state_key,
    summaries_of,
    windows_from,
)
from forge_common.adk.context_window import _splice as splice
from forge_common.adk.usage import MODEL_KEY

APP = "context_test"
USER = "user-ada"
TASK = "Compare the three reports and tell me which quarter grew most."


def file_text(path: str) -> str:
    return f"<{path}> " + "revenue grew in the quarter; " * 100  # ~3,000 characters


def read_file(path: str) -> dict[str, Any]:
    """Reads a file."""
    return {"path": path, "text": file_text(path)}


def call(name: str, **args: Any) -> list[types.Part]:
    return [types.Part(function_call=types.FunctionCall(name=name, args=args))]


def said(text: str) -> list[types.Part]:
    return [types.Part(text=text)]


class Scripted(BaseLlm):
    """Answers each call with its next turn, and keeps what each request held."""

    model: str = "openai/gpt-5.2"
    turns: list[list[types.Part]] = Field(default_factory=list)
    usage: list[types.GenerateContentResponseUsageMetadata | None] = Field(default_factory=list)
    seen: list[list[types.Content]] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.seen.append([content.model_copy(deep=True) for content in llm_request.contents])
        yield LlmResponse(
            content=types.Content(role="model", parts=self.turns.pop(0)),
            usage_metadata=self.usage.pop(0) if self.usage else None,
            custom_metadata={MODEL_KEY: "openai/gpt-5.2"},
        )

    def texts(self, call: int) -> str:
        """Everything request ``call`` (0-based) sent, as text."""
        return "\n".join(
            part.text
            or (str(part.function_response.response) if part.function_response else "")
            or (str(part.function_call.args) if part.function_call else "")
            for content in self.seen[call]
            for part in content.parts or ()
        )


class Summaries(BaseEventsSummarizer):
    """Summarizes as ``summary N``, and keeps what it was given."""

    def __init__(self) -> None:
        self.given: list[list[Event]] = []

    async def maybe_summarize_events(self, *, events: list[Event]) -> Event | None:
        self.given.append(events)
        return Event(
            author="user",
            actions=EventActions(
                compaction=EventCompaction(
                    start_timestamp=events[0].timestamp,
                    end_timestamp=events[-1].timestamp,
                    compacted_content=types.Content(
                        role="model", parts=[types.Part(text=f"summary {len(self.given)}")]
                    ),
                )
            ),
        )


async def ran(
    model: Scripted,
    plugin: ContextWindowPlugin,
    *,
    tools: list[Any] | None = None,
    messages: tuple[str, ...] = (TASK,),
    state: dict[str, Any] | None = None,
    **agent: Any,
) -> Session:
    sessions = InMemorySessionService()
    runner = Runner(
        app=App(
            name=APP,
            root_agent=LlmAgent(
                name="researcher",
                model=model,
                instruction="Answer from the files.",
                tools=tools if tools is not None else [read_file],
                **agent,
            ),
            plugins=[plugin],
        ),
        session_service=sessions,
        auto_create_session=True,
    )
    for text in messages:
        message = types.Content(role="user", parts=[types.Part(text=text)])
        async for _ in runner.run_async(
            user_id=USER, session_id="s-1", new_message=message, state_delta=state
        ):
            pass
    await runner.close()
    session = await sessions.get_session(app_name=APP, user_id=USER, session_id="s-1")
    assert session is not None
    return session


def summaries(session: Session, agent: str = "researcher") -> list[dict[str, Any]]:
    """The agent's summaries, and that the session itself was left as it was."""
    assert not any(event.actions.compaction for event in session.events)
    return summaries_of(session.state, agent)


def reads(*paths: str, answer: str = "Q3 grew most.") -> list[list[types.Part]]:
    return [*(call("read_file", path=path) for path in paths), said(answer)]


#: What a request may hold: 2,000 tokens (compacts at 1,600, ~5,600 characters).
SMALL = Window(2_500, 500)


# ------------------------------------------------------------------- measuring


class TestMeasure:
    def test_it_counts_what_the_model_reads(self) -> None:
        request = LlmRequest(
            contents=[
                types.Content(role="user", parts=[types.Part(text="x" * 100)]),
                types.Content(role="model", parts=call("read_file", path="a")),
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name="read_file", response={"text": "y" * 200}
                            )
                        )
                    ],
                ),
                types.Content(
                    role="user",
                    parts=[types.Part(inline_data=types.Blob(mime_type="image/png", data=b"\x89"))],
                ),
            ],
            config=types.GenerateContentConfig(system_instruction="z" * 50),
        )
        size = measure(request)
        # The texts, the call's name and arguments, the result's name and body, the instruction.
        assert (
            size.chars
            == 100
            + len('read_file{"path": "a"}')
            + len('read_file{"text": "' + "y" * 200 + '"}')
            + 50
        )
        assert size.media == MEDIA_TOKENS

    def test_tool_declarations_count(self) -> None:
        bare = LlmRequest()
        declared = LlmRequest(
            config=types.GenerateContentConfig(
                tools=[
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(name="read_file", description="Reads a file.")
                        ]
                    )
                ]
            )
        )
        assert measure(declared).chars > measure(bare).chars

    def test_tokens_are_characters_over_characters_per_token_and_media(self) -> None:
        assert Size(chars=700, media=10).tokens(3.5) == 210


def test_windows_come_from_model_provider_yaml() -> None:
    from forge_common.model_provider import ModelProviderConfig

    config = ModelProviderConfig.model_validate(
        {
            "version": 1,
            "default": {"provider": "openai", "model": "gpt-5.2"},
            "providers": {
                "openai": {
                    "baseUrl": "https://api.openai.com/v1",
                    "api": "openai-responses",
                    "auth": {"type": "apiKey", "key": "sk-test"},
                    "models": [{"id": "gpt-5.2", "contextWindow": 400_000, "maxTokens": 128_000}],
                }
            },
        }
    )

    class Models:
        pass

    models = Models()
    models.config = config  # type: ignore[attr-defined]
    window = windows_from(models)
    assert window is not None
    assert window("openai/gpt-5.2") == Window(400_000, 128_000)
    assert window("gpt-5.2") == Window(400_000, 128_000)
    assert window("anthropic/claude") is None
    assert windows_from(object()) is None


# -------------------------------------------------------------------- tracking


async def test_each_agent_keeps_its_context_in_the_state() -> None:
    usage = types.GenerateContentResponseUsageMetadata
    model = Scripted(
        turns=reads("q1.txt"),
        usage=[
            usage(prompt_token_count=200, candidates_token_count=20, total_token_count=220),
            usage(prompt_token_count=1_100, candidates_token_count=30, total_token_count=1_130),
        ],
    )
    plugin = ContextWindowPlugin(
        window=lambda name: Window(400_000, 128_000) if name else None, clock=lambda: 1_000.0
    )
    session = await ran(model, plugin)

    kept = context_of(session.state, "researcher")
    assert kept is not None
    # After the last call: the prompt and reply as the provider counted them.
    assert (kept["tokens"], kept["reported"], kept["model"]) == (1_130, 1_100, "openai/gpt-5.2")
    assert (kept["counted"], kept["updated"]) == ("provider", 1_000.0)
    # What it may hold and where it compacts, with what they come from.
    assert (kept["limit"], kept["threshold"]) == (272_000, 217_600)
    assert (kept["window"], kept["reply"], kept["share"], kept["auto"]) == (
        400_000,
        128_000,
        0.8,
        True,
    )
    # Characters per token follow what the provider reported.
    assert kept["chars_per_token"] != 3.5
    assert "compactions" not in kept
    # It's the conversation's own agent.
    assert kept["apart"] is False
    assert state_key("researcher") in session.state


async def test_a_turn_that_outgrows_its_window_is_compacted_before_its_next_call() -> None:
    model = Scripted(turns=reads("q1.txt", "q2.txt", "q3.txt"))
    made = Summaries()
    plugin = ContextWindowPlugin(default=SMALL, summarizer=made)
    session = await ran(model, plugin)

    # Four calls: after q2 the request was too big, after q3 again.
    assert len(model.seen) == 4

    # The third request: the task word for word, a summary of reading q1, q2 as it was.
    third = model.texts(2)
    assert TASK in third
    assert "<q1.txt>" not in third
    assert "<q2.txt>" in third
    assert "Summary of the work on the current request so far" in third
    assert third.count("summary 1") == 1
    # It was summarized from reading q1 (the call and its result), not from what stayed.
    assert [bool(e.get_function_calls()) for e in made.given[0]] == [True, False]

    # The fourth: the second summary folded the first in, q3 as it was.
    fourth = model.texts(3)
    assert TASK in fourth
    assert "<q1.txt>" not in fourth and "<q2.txt>" not in fourth
    assert "<q3.txt>" in fourth
    assert "summary 2" in fourth and "summary 1" not in fourth
    folded = made.given[1][0].content
    assert folded is not None and folded.parts
    assert "summary 1" in (folded.parts[0].text or "")
    # The second took the first's place: one summary, from the first's start.
    (kept_summary,) = summaries(session)
    assert (kept_summary["part"], kept_summary["text"], kept_summary["events"]) == (
        "turn",
        "summary 2",
        2,
    )
    assert kept_summary["start"] == made.given[0][0].timestamp
    # The summary reaches the model as a user message.
    assert any(
        content.role == "user" and "summary 2" in (content.parts[0].text or "")
        for content in model.seen[3]
        if content.parts
    )

    kept = context_of(session.state, "researcher")
    assert kept is not None
    assert kept["compactions"] == 2
    last = kept["last_compaction"]
    assert last["summaries"] == 1
    assert last["tokens_after"] < last["tokens_before"]


async def test_an_agent_that_sees_only_its_turn_keeps_its_task_and_last_step() -> None:
    model = Scripted(turns=reads("q1.txt", "q2.txt"))
    plugin = ContextWindowPlugin(default=SMALL, summarizer=Summaries())
    session = await ran(model, plugin, include_contents="none")

    assert len(summaries(session)) == 1
    third = model.texts(2)
    # ADK finds the turn's start at the user's message, not at the summary.
    assert TASK in third
    assert "summary 1" in third
    assert "<q1.txt>" not in third
    assert "<q2.txt>" in third


async def test_a_model_just_chosen_counts_from_its_first_request() -> None:
    # The agent last answered on a big model; the person picks a small one.
    model = Scripted(turns=reads("q1.txt", "q2.txt"))

    def window(name: str) -> Window | None:
        return SMALL if name == "openai/gpt-5-mini" else Window(400_000, 128_000)

    plugin = ContextWindowPlugin(window=window, summarizer=Summaries())
    session = await ran(model, plugin, state={"model": "openai/gpt-5-mini"})

    assert len(summaries(session)) == 1
    assert "<q1.txt>" not in model.texts(2)
    kept = context_of(session.state, "researcher")
    assert kept is not None
    assert (kept["limit"], kept["window"]) == (2_000, 2_500)


async def test_under_its_threshold_nothing_is_compacted() -> None:
    model = Scripted(turns=reads("q1.txt", "q2.txt", "q3.txt"))
    made = Summaries()
    session = await ran(
        model, ContextWindowPlugin(default=Window(400_000, 128_000), summarizer=made)
    )
    assert summaries(session) == []
    assert made.given == []
    assert "<q1.txt>" in model.texts(3)


async def test_a_compaction_can_be_asked_for_below_the_threshold() -> None:
    def remember(note: str, tool_context: ToolContext) -> dict[str, Any]:
        """Notes something, and asks for the context to be compacted."""
        request_compaction(tool_context)
        return {"noted": note}

    model = Scripted(
        turns=[
            call("read_file", path="q1.txt"),
            call("read_file", path="q2.txt"),
            call("remember", note="Q1 and Q2 read"),
            said("Q2 grew most."),
        ]
    )
    plugin = ContextWindowPlugin(
        default=Window(400_000, 128_000), auto=False, summarizer=Summaries()
    )
    session = await ran(model, plugin, tools=[read_file, remember])

    assert len(summaries(session)) == 1
    last = model.texts(3)
    assert "<q1.txt>" not in last and "<q2.txt>" not in last
    assert "summary 1" in last
    kept = context_of(session.state, "researcher")
    assert kept is not None
    assert kept["compactions"] == 1 and "requested" not in kept


async def test_a_long_conversation_compacts_what_came_before_the_turn_on_its_own() -> None:
    model = Scripted(
        turns=[
            *reads("q1.txt", "q2.txt", answer="Read them. " + "Q2 grew because... " * 130),
            said("Q2."),
        ]
    )
    made = Summaries()
    # The first turn stays under 2,000 tokens; its long answer takes the second over.
    plugin = ContextWindowPlugin(default=Window(3_000, 500), summarizer=made)
    session = await ran(model, plugin, messages=(TASK, "And which one grew least?"))

    # The second turn's only request: the first turn summarized, its own message as it was.
    (history,) = summaries(session)
    assert history["part"] == "history"
    last = model.texts(3)
    assert "And which one grew least?" in last
    assert "<q1.txt>" not in last and "<q2.txt>" not in last and "Read them." not in last
    assert "Summary of the conversation before the current request" in last
    # What it summarized: the first turn, the user's message with it.
    assert made.given[0][0].author == "user"


async def test_an_agent_that_sees_only_its_turn_drops_an_earlier_turns_summary() -> None:
    model = Scripted(turns=[*reads("q1.txt", "q2.txt"), said("Hello again.")])
    plugin = ContextWindowPlugin(default=SMALL, summarizer=Summaries())
    session = await ran(
        model, plugin, include_contents="none", messages=(TASK, "Thanks, that's all.")
    )

    # The second turn's request: its message alone, not the first turn's summary.
    last = model.texts(3)
    assert "Thanks, that's all." in last
    assert "summary 1" not in last and TASK not in last
    assert summaries(session) == []


async def test_a_summarizer_that_fails_never_fails_the_turn() -> None:
    class Broken(BaseEventsSummarizer):
        async def maybe_summarize_events(self, *, events: list[Event]) -> Event | None:
            raise RuntimeError("model down")

    model = Scripted(turns=reads("q1.txt", "q2.txt"))
    session = await ran(model, ContextWindowPlugin(default=SMALL, summarizer=Broken()))
    assert summaries(session) == []
    assert "<q1.txt>" in model.texts(2)


def test_a_threshold_is_a_share_of_the_window() -> None:
    with pytest.raises(ValueError):
        ContextWindowPlugin(threshold=0)
    with pytest.raises(ValueError):
        ContextWindowPlugin(threshold=1.5)


# -------------------------------------------------------------------- planning


def event(author: str, at: float, parts: list[types.Part], role: str = "model") -> Event:
    return Event(
        invocation_id="inv-1",
        author=author,
        timestamp=at,
        content=types.Content(role=role, parts=parts),
    )


def answered(author: str, at: float, call_id: str, text: str) -> Event:
    return event(
        author,
        at,
        [
            types.Part(
                function_response=types.FunctionResponse(
                    id=call_id, name="read_file", response={"text": text}
                )
            )
        ],
        role="user",
    )


def asked(author: str, at: float, call_id: str) -> Event:
    return event(
        author,
        at,
        [
            types.Part(
                function_call=types.FunctionCall(
                    id=call_id, name="read_file", args={"path": call_id}
                )
            )
        ],
    )


def test_a_span_takes_in_what_other_agents_did_without_pairing_their_calls() -> None:
    # Another agent's call never got its answer: the agent reads its calls as text.
    events = [
        event("user", 1, said(TASK), role="user"),
        asked("writer", 2, "w"),
        event("writer", 3, said("W" * 3_000)),
        event("user", 4, said("Now the reports."), role="user"),
        asked("researcher", 5, "a"),
        answered("researcher", 6, "a", "A" * 3_000),
        asked("researcher", 7, "b"),
        answered("researcher", 8, "b", "B" * 3_000),
    ]
    history, turn = plan_compaction(events, agent="researcher")
    assert (history.part, [e.timestamp for e in history.events]) == ("history", [1, 2, 3])
    assert (turn.part, [e.timestamp for e in turn.events]) == ("turn", [5, 6])


def test_a_span_sees_only_the_agents_branch() -> None:
    def on(branch: str, e: Event) -> Event:
        return e.model_copy(update={"branch": branch})

    events = [
        event("user", 1, said(TASK), role="user"),
        on("fan.a", asked("a", 2, "a1")),
        on("fan.b", asked("b", 3, "b1")),
        on("fan.a", answered("a", 4, "a1", "A" * 3_000)),
        on("fan.b", answered("b", 5, "b1", "B" * 3_000)),
        on("fan.a", asked("a", 6, "a2")),
        on("fan.a", answered("a", 7, "a2", "A" * 3_000)),
    ]
    (span,) = plan_compaction(events, agent="a", branch="fan.a")
    # Its sibling's events aren't in its view, so they're neither in its summary
    # nor hidden from anyone: the summary is only ever added to its own view.
    assert [e.timestamp for e in span.events] == [2, 4]


def test_a_span_leaves_no_call_unanswered() -> None:
    events = [
        event("user", 1, said(TASK), role="user"),
        asked("researcher", 2, "a"),
        answered("researcher", 3, "a", "A" * 3_000),
        # Two calls at once, answered apart.
        event(
            "researcher",
            4,
            [
                types.Part(function_call=types.FunctionCall(id="b", name="read_file", args={})),
                types.Part(function_call=types.FunctionCall(id="c", name="read_file", args={})),
            ],
        ),
        answered("researcher", 5, "b", "B" * 3_000),
        answered("researcher", 6, "c", "C" * 3_000),
        asked("researcher", 7, "d"),
        answered("researcher", 8, "d", "D" * 3_000),
    ]
    (span,) = plan_compaction(events, agent="researcher")
    assert [e.timestamp for e in span.events] == [2, 3, 4, 5, 6]


def test_the_last_steps_stay_unless_they_alone_are_too_big() -> None:
    events = [
        event("user", 1, said(TASK), role="user"),
        asked("researcher", 2, "a"),
        answered("researcher", 3, "a", "A" * 3_000),
        asked("researcher", 4, "b"),
        answered("researcher", 5, "b", "B" * 30_000),
    ]
    (kept,) = plan_compaction(events, agent="researcher")
    assert kept.end == 3
    (whole,) = plan_compaction(events, agent="researcher", tail_tokens=1_000)
    assert whole.end == 5


def test_a_span_worth_too_little_isnt_summarized() -> None:
    events = [
        event("user", 1, said(TASK), role="user"),
        asked("researcher", 2, "a"),
        answered("researcher", 3, "a", "tiny"),
        asked("researcher", 4, "b"),
        answered("researcher", 5, "b", "B" * 3_000),
    ]
    assert plan_compaction(events, agent="researcher", min_tokens=500) == []
    assert len(plan_compaction(events, agent="researcher")) == 1


# -------------------------------------------------------------------- branches


class Routed(BaseLlm):
    """Answers each agent (found by its instruction) with its next turn, side by side."""

    model: str = "openai/gpt-5.2"
    routed: dict[str, list[list[types.Part]]] = Field(default_factory=dict)
    seen: dict[str, list[str]] = Field(default_factory=dict)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        instruction = str(llm_request.config.system_instruction or "")
        key = next(key for key in self.routed if key in instruction)
        self.seen.setdefault(key, []).append(
            "\n".join(
                part.text
                or (str(part.function_response.response) if part.function_response else "")
                for content in llm_request.contents
                for part in content.parts or ()
            )
        )
        # Let the other branch take its turn, so the two interleave.
        await asyncio.sleep(0.01)
        yield LlmResponse(content=types.Content(role="model", parts=self.routed[key].pop(0)))


async def test_branches_side_by_side_and_the_agent_after_them_each_compact_their_own_view() -> None:
    model = Routed(
        routed={
            "Read A": reads("a1", "a2", "a3", answer="A: a3 grew most."),
            "Read B": reads("b1", "b2", "b3", answer="B: b2 grew most."),
            "Compare": [said("a3 beats b2.")],
        }
    )
    made = Summaries()
    a = LlmAgent(name="a", model=model, instruction="Read A.", tools=[read_file])
    b = LlmAgent(name="b", model=model, instruction="Read B.", tools=[read_file])
    c = LlmAgent(name="c", model=model, instruction="Compare.")
    root = SequentialAgent(
        name="root", sub_agents=[ParallelAgent(name="fan", sub_agents=[a, b]), c]
    )
    sessions = InMemorySessionService()
    runner = Runner(
        app=App(
            name=APP,
            root_agent=root,
            plugins=[ContextWindowPlugin(default=SMALL, summarizer=made)],
        ),
        session_service=sessions,
        auto_create_session=True,
    )
    message = types.Content(role="user", parts=[types.Part(text=TASK)])
    async for _ in runner.run_async(user_id=USER, session_id="s-1", new_message=message):
        pass
    await runner.close()
    session = await sessions.get_session(app_name=APP, user_id=USER, session_id="s-1")
    assert session is not None
    assert {e.branch for e in session.events if e.author in ("a", "b")} == {"fan.a", "fan.b"}

    # Each branch compacted its own reads, on its own branch, while the other read too.
    for agent, first in (("a", "<a1>"), ("b", "<b1>")):
        (summary,) = summaries_of(session.state, agent, f"fan.{agent}")
        assert summary["part"] == "turn"
        last = model.seen[f"Read {agent.upper()}"][-1]
        assert TASK in last and first not in last and "Summary of the work" in last
        kept = context_of(session.state, agent)
        assert kept is not None and kept["tokens"] < kept["limit"]
        assert kept["apart"] is True
    # Neither branch's summary reached the other.
    (of_a,) = summaries_of(session.state, "a", "fan.a")
    (of_b,) = summaries_of(session.state, "b", "fan.b")
    assert not any(of_a["text"] in sent for sent in model.seen["Read B"])
    assert not any(of_b["text"] in sent for sent in model.seen["Read A"])

    # The agent after them sees both branches (it's on none), and compacted that.
    (after,) = summaries(session, "c")
    assert after["part"] == "history"
    compare = model.seen["Compare"][0]
    assert "<a1>" not in compare and "<b1>" not in compare
    assert "Summary of the conversation before the current request" in compare
    summarized = {e.author for e in made.given[-1]}
    assert {"a", "b"} <= summarized
    assert (context_of(session.state, "c") or {}).get("apart") is False


async def test_a_task_agent_starts_each_call_apart_from_the_conversation() -> None:
    model = Routed(
        routed={
            "Supervise": [call("helper", goal="Read q1"), said("Q1 grew.")],
            "Help": [call("finish_task", result="q1 grew")],
        }
    )
    helper = LlmAgent(name="helper", model=model, mode="task", instruction="Help.")
    root = LlmAgent(name="boss", model=model, instruction="Supervise.", sub_agents=[helper])
    sessions = InMemorySessionService()
    runner = Runner(
        app=App(name=APP, root_agent=root, plugins=[ContextWindowPlugin()]),
        session_service=sessions,
        auto_create_session=True,
    )
    message = types.Content(role="user", parts=[types.Part(text=TASK)])
    async for _ in runner.run_async(user_id=USER, session_id="s-1", new_message=message):
        pass
    await runner.close()
    session = await sessions.get_session(app_name=APP, user_id=USER, session_id="s-1")
    assert session is not None

    # The task reads only what it was handed, not the person's message.
    assert model.seen["Help"] == ['{"goal": "Read q1"}']
    assert (context_of(session.state, "helper") or {}).get("apart") is True
    assert (context_of(session.state, "boss") or {}).get("apart") is False


@pytest.mark.parametrize("called", ["task", "tool"])
async def test_an_agent_called_for_its_help_compacts_and_each_call_starts_afresh(
    called: str,
) -> None:
    """A task (the assistant's specialists) and an agent called as a tool
    (chat agents' sub-agents) compact while they read, and leave no summary
    for their next call."""

    def helping(tag: str) -> list[list[types.Part]]:
        read = [call("read_file", path=f"{tag}{i}") for i in range(5)]
        done = call("finish_task", result="done") if called == "task" else said("done")
        return [*read, done]

    def ask() -> list[types.Part]:
        return call("helper", goal="Read") if called == "task" else call("helper", request="Read")

    model = Routed(
        routed={
            "Supervise": [ask(), said("A."), ask(), said("B.")],
            "Help": [*helping("a"), *helping("b")],
        }
    )
    made = Summaries()
    if called == "task":
        helper = LlmAgent(
            name="helper", model=model, mode="task", instruction="Help.", tools=[read_file]
        )
        root = LlmAgent(name="boss", model=model, instruction="Supervise.", sub_agents=[helper])
    else:
        helper = LlmAgent(name="helper", model=model, instruction="Help.", tools=[read_file])
        root = LlmAgent(
            name="boss", model=model, instruction="Supervise.", tools=[AgentTool(helper)]
        )
    sessions = InMemorySessionService()
    runner = Runner(
        app=App(
            name=APP,
            root_agent=root,
            plugins=[ContextWindowPlugin(default=SMALL, summarizer=made)],
        ),
        session_service=sessions,
        auto_create_session=True,
    )
    for text in ("first", "second"):
        message = types.Content(role="user", parts=[types.Part(text=text)])
        async for _ in runner.run_async(user_id=USER, session_id="s-1", new_message=message):
            pass
    await runner.close()
    session = await sessions.get_session(app_name=APP, user_id=USER, session_id="s-1")
    assert session is not None

    # It compacted while it read, and was counted apart from the conversation.
    seen = model.seen["Help"]
    assert any("Summary of the work" in sent for sent in seen)
    kept = context_of(session.state, "helper")
    assert kept is not None and kept["compactions"] >= 2 and kept["apart"] is True
    # Its second call starts from what it was handed, not the first call's summary.
    assert "Summary of the work" not in seen[6] and "<a" not in seen[6]
    # And neither call's summaries stay in the conversation's state.
    left = {
        key: value for key, value in session.state.items() if key.startswith("forge:summaries:")
    }
    assert left and not any(left.values())


# -------------------------------------------------------------------- splicing


def text(value: str, role: str = "user") -> types.Content:
    return types.Content(role=role, parts=[types.Part(text=value)])


def test_a_rebuilt_request_keeps_what_else_it_carried_in_place() -> None:
    old = [text("task"), text("call a", "model"), text("result a"), text("call b", "model")]
    new = [text("task"), text("summary"), text("call b", "model")]
    # ADK put an instruction among the messages, and a tool added a file at the end.
    current = [old[0], text("instruction"), *old[1:], text("file")]
    assert splice(current, old, new) == [
        text("task"),
        text("instruction"),
        text("summary"),
        text("call b", "model"),
        text("file"),
    ]


def test_extras_inside_what_was_replaced_follow_its_replacement() -> None:
    old = [text("task"), text("a"), text("b"), text("c")]
    new = [text("task"), text("summary"), text("c")]
    current = [text("task"), text("a"), text("extra"), text("b"), text("c")]
    assert splice(current, old, new) == [text("task"), text("summary"), text("extra"), text("c")]


def test_a_request_that_doesnt_hold_what_it_was_built_from_isnt_rebuilt() -> None:
    old = [text("task"), text("a")]
    assert splice([text("task"), text("changed")], old, [text("summary")]) is None
