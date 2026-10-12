"""Follow-up questions after a reply: asked for with the run, written by the
reply's model once the turn ends, kept in the state for the chat, and cleared
when the next turn starts. Through ADK's runner, with a scripted model."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.agent_tool import AgentTool
from google.genai import types
from pydantic import Field

from forge_common.adk.ask_person import ASK_PERSON, ask_person
from forge_common.adk.follow_ups import (
    REQUEST_KEY,
    STATE_KEY,
    FollowUpsPlugin,
    follow_ups_of,
    parse_follow_ups,
    requested_count,
)
from forge_common.adk.usage import MODEL_KEY

APP = "follow_test"
USER = "user-ada"
SUGGESTED = '["Deploy to production", "Roll it back", "Show the logs"]'


class Scripted(BaseLlm):
    """Answers each call with its next turn, and keeps each request."""

    model: str = "scripted"
    turns: list[list[types.Part]] = Field(default_factory=list)
    seen: list[LlmRequest] = Field(default_factory=list)
    stamp: str | None = None

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.seen.append(llm_request.model_copy(deep=True))
        parts = self.turns.pop(0)
        metadata = {MODEL_KEY: self.stamp} if self.stamp else None
        yield LlmResponse(
            content=types.Content(role="model", parts=parts), custom_metadata=metadata
        )


class Choosing(Scripted):
    """Like ``ProviderModels``: a request's model and thinking are chosen first."""

    chosen: list[tuple[object, object]] = Field(default_factory=list)

    def select(
        self, request: LlmRequest, model: object = None, thinking_level: object = None
    ) -> None:
        self.chosen.append((model, thinking_level))


def said(text: str) -> list[types.Part]:
    return [types.Part(text=text)]


def suggestions(model: Scripted) -> str:
    """What the follow-ups' request read: its instruction and the conversation."""
    request = model.seen[-1]
    (part,) = request.contents[0].parts or [types.Part()]
    return f"{request.config.system_instruction}\n---\n{part.text}"


class Chat:
    def __init__(self, root: LlmAgent, plugin: FollowUpsPlugin | None = None) -> None:
        self.sessions = InMemorySessionService()
        self.runner = Runner(
            app=App(name=APP, root_agent=root, plugins=[plugin or FollowUpsPlugin()]),
            session_service=self.sessions,
            auto_create_session=True,
        )

    async def send(
        self, *parts: types.Part, ask: Any = True, state: dict[str, Any] | None = None
    ) -> list[Event]:
        delta = dict(state or {})
        if ask is not None:
            delta[REQUEST_KEY] = ask
        message = types.Content(role="user", parts=list(parts))
        return [
            event
            async for event in self.runner.run_async(
                user_id=USER, session_id="s-1", new_message=message, state_delta=delta or None
            )
        ]

    async def state(self) -> dict[str, Any]:
        session = await self.sessions.get_session(app_name=APP, user_id=USER, session_id="s-1")
        assert session is not None
        return dict(session.state)


def kept(events: list[Event]) -> list[Any]:
    """Each follow-ups value the run's events set, in order."""
    return [
        e.actions.state_delta[STATE_KEY]
        for e in events
        if STATE_KEY in (e.actions.state_delta or {})
    ]


async def test_a_turn_that_asks_ends_with_follow_ups_from_the_replys_model() -> None:
    model = Scripted(turns=[said("Deployed main to staging."), said(SUGGESTED)])
    chat = Chat(LlmAgent(name="helper", model=model))

    events = await chat.send(*said("Deploy main"))

    # They're the run's last event, so the chat has them when the turn ends.
    assert events[-1].actions.state_delta == {
        STATE_KEY: {"suggestions": ["Deploy to production", "Roll it back", "Show the logs"]}
    }
    assert follow_ups_of(await chat.state()) == [
        "Deploy to production",
        "Roll it back",
        "Show the logs",
    ]
    read = suggestions(model)
    assert "write 3 short follow-up messages" in read
    assert "Person: Deploy main\n\nAssistant: Deployed main to staging." in read
    # No tools, nothing of the agent's own instruction: a call of its own.
    assert not model.seen[-1].tools_dict


async def test_a_run_says_how_many() -> None:
    model = Scripted(turns=[said("Done."), said('["One", "Two", "Three", "Four"]')])
    chat = Chat(LlmAgent(name="helper", model=model))
    await chat.send(*said("Go"), ask={"count": 2})
    assert "write 2 short" in suggestions(model)
    assert follow_ups_of(await chat.state()) == ["One", "Two"]


async def test_a_run_that_doesnt_ask_gets_none_and_costs_nothing() -> None:
    model = Scripted(turns=[said("Deployed.")])
    chat = Chat(LlmAgent(name="helper", model=model))
    events = await chat.send(*said("Deploy main"), ask=None)
    assert kept(events) == []
    assert len(model.seen) == 1


async def test_the_next_turn_starts_by_clearing_the_last_ones() -> None:
    model = Scripted(
        turns=[said("Deployed."), said(SUGGESTED), said("Rolled back."), said('["Why?"]')]
    )
    chat = Chat(LlmAgent(name="helper", model=model))
    await chat.send(*said("Deploy main"))

    events = await chat.send(*said("Roll it back"))
    # Cleared before anything else happens, then this reply's.
    assert kept(events) == [None, {"suggestions": ["Why?"]}]
    assert kept(events[:1]) == [None]

    # A turn that doesn't ask still clears them: they'd follow an older reply.
    model.turns.append(said("Hello."))
    assert kept(await chat.send(*said("Hi"), ask=None)) == [None]
    assert follow_ups_of(await chat.state()) == []


async def test_a_turn_waiting_on_the_person_gets_none_until_it_carries_on() -> None:
    question = types.FunctionCall(name=ASK_PERSON, args={"message": "Which environment?"})
    model = Scripted(
        turns=[[types.Part(function_call=question)], said("Deploying to staging."), said(SUGGESTED)]
    )
    chat = Chat(LlmAgent(name="helper", model=model, tools=[ask_person]))

    paused = await chat.send(*said("Deploy main"))
    assert kept(paused) == []
    assert len(model.seen) == 1

    (call,) = [c for e in paused for c in e.get_function_calls()]
    answer = types.FunctionResponse(id=call.id, name=call.name, response={"result": "staging"})
    resumed = await chat.send(types.Part(function_response=answer))
    assert kept(resumed)[-1] == {
        "suggestions": ["Deploy to production", "Roll it back", "Show the logs"]
    }


async def test_only_the_agent_that_took_the_turn_suggests() -> None:
    # The researcher runs as a tool: it's given the caller's state (the
    # request too) but not its message, so it doesn't suggest.
    inner = Scripted(turns=[said("Found three deploys.")])
    researcher = LlmAgent(name="researcher", model=inner, description="Looks things up.")
    tool_call = types.FunctionCall(name="researcher", args={"request": "Find deploys"})
    model = Scripted(
        turns=[[types.Part(function_call=tool_call)], said("There were three."), said(SUGGESTED)]
    )
    chat = Chat(LlmAgent(name="helper", model=model, tools=[AgentTool(researcher)]))

    events = await chat.send(*said("How many deploys?"))
    assert len(inner.seen) == 1
    assert kept(events) == [
        {"suggestions": ["Deploy to production", "Roll it back", "Show the logs"]}
    ]


async def test_provider_models_run_them_on_the_replys_model_without_thinking() -> None:
    model = Choosing(turns=[said("Done."), said(SUGGESTED)], stamp="openai/gpt-5-mini")
    await Chat(LlmAgent(name="helper", model=model)).send(*said("Go"))
    assert model.chosen == [("openai/gpt-5-mini", "off")]

    other = Choosing(turns=[said("Done."), said(SUGGESTED)], stamp="openai/gpt-5-mini")
    plugin = FollowUpsPlugin(model="google/gemini-2.5-flash", thinking_level="low")
    await Chat(LlmAgent(name="helper", model=other), plugin).send(*said("Go"))
    assert other.chosen == [("google/gemini-2.5-flash", "low")]


async def test_a_failure_or_a_slow_model_ends_the_turn_without_them() -> None:
    model = Scripted(turns=[said("Done."), said("I can't think of any.")])
    chat = Chat(LlmAgent(name="helper", model=model))
    events = await chat.send(*said("Go"))
    assert events[-1].content and events[-1].content.parts
    assert kept(events) == []

    class Slow(Scripted):
        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse]:
            if len(self.seen) == 1:
                await asyncio.sleep(5)
            async for response in super().generate_content_async(llm_request, stream):
                yield response

    slow = Slow(turns=[said("Done."), said(SUGGESTED)])
    chat = Chat(LlmAgent(name="helper", model=slow), FollowUpsPlugin(timeout=0.05))
    assert kept(await chat.send(*said("Go"))) == []


def test_reading_requests_and_answers() -> None:
    assert requested_count(True, 3) == 3
    assert requested_count({"count": 9}, 3) == 5
    assert requested_count({"count": 0}, 3) == 1
    assert requested_count({}, 4) == 4
    assert requested_count(False, 3) is None
    assert requested_count(None, 3) is None

    assert parse_follow_ups('Sure:\n```json\n["A", "B", "A", " ", 3]\n```', 3) == ["A", "B"]
    assert parse_follow_ups('{"a": 1}', 3) == []
    assert parse_follow_ups("[not json]", 3) == []

    assert follow_ups_of({STATE_KEY: {"suggestions": ["A", "", 2]}}) == ["A"]
    assert follow_ups_of({STATE_KEY: None}) == []
    assert follow_ups_of({}) == []
