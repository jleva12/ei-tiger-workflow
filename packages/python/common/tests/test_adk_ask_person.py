"""Asking the person mid-turn: the tool pauses the turn on ADK's
``adk_request_input``, the answer carries it on, and a schema a model got
wrong is repaired first. Through ADK's runner, with a scripted model."""

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
from google.genai import types
from pydantic import Field

from forge_common.adk.ask_person import ASK_PERSON, ask_person, repair_input_schemas
from forge_common.adk.ask_person import _repair_schema as repair

APP = "ask_test"
USER = "user-ada"

DEPLOY = {
    "type": "object",
    "properties": {
        "environment": {"type": "string", "enum": ["staging", "production"]},
        "replicas": {"type": "integer"},
        "notify": {"type": "boolean"},
    },
    "required": ["environment"],
}


class Scripted(BaseLlm):
    """Answers each call with its next turn, and keeps what each request held."""

    model: str = "scripted"
    turns: list[list[types.Part]] = Field(default_factory=list)
    seen: list[list[types.Content]] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.seen.append([content.model_copy(deep=True) for content in llm_request.contents])
        yield LlmResponse(content=types.Content(role="model", parts=self.turns.pop(0)))


def asks(schema: dict[str, Any] | None) -> list[types.Part]:
    args: dict[str, Any] = {"message": "Where should I deploy main?"}
    if schema is not None:
        args["response_schema"] = schema
    return [types.Part(function_call=types.FunctionCall(name=ASK_PERSON, args=args))]


def said(text: str) -> list[types.Part]:
    return [types.Part(text=text)]


class Chat:
    def __init__(self, root: LlmAgent) -> None:
        self.sessions = InMemorySessionService()
        self.runner = Runner(
            app=App(name=APP, root_agent=root),
            session_service=self.sessions,
            auto_create_session=True,
        )

    async def send(self, *parts: types.Part) -> list[Event]:
        message = types.Content(role="user", parts=list(parts))
        return [
            event
            async for event in self.runner.run_async(
                user_id=USER, session_id="s-1", new_message=message
            )
        ]

    async def answer(self, call: types.FunctionCall, value: Any) -> list[Event]:
        # As the chat answers it (assistant-ui's useAdkSubmitInput).
        response = types.FunctionResponse(id=call.id, name=call.name, response={"result": value})
        return await self.send(types.Part(function_response=response))


def question(events: list[Event]) -> types.FunctionCall:
    (event,) = [e for e in events if e.long_running_tool_ids]
    (call,) = event.get_function_calls()
    assert call.id in (event.long_running_tool_ids or set())
    return call


def answered(contents: list[types.Content]) -> Any:
    for content in contents:
        for part in content.parts or ():
            if part.function_response and part.function_response.name == ASK_PERSON:
                return part.function_response.response
    raise AssertionError("the request carried no answer")


async def test_a_question_pauses_the_turn_and_the_answer_carries_it_on() -> None:
    model = Scripted(turns=[asks(DEPLOY), said("Deploying to staging.")])
    chat = Chat(LlmAgent(name="helper", model=model, tools=[ask_person]))

    first = await chat.send(*said("Deploy main"))
    call = question(first)
    assert call.args is not None and call.args["response_schema"] == DEPLOY
    # The turn stopped there: nothing answered it, and the model wasn't asked again.
    assert len(model.seen) == 1

    second = await chat.answer(call, {"environment": "staging", "replicas": 2, "notify": True})
    assert any(e.content and e.content.parts and e.content.parts[0].text for e in second)
    assert answered(model.seen[1]) == {
        "result": {"environment": "staging", "replicas": 2, "notify": True}
    }


async def test_an_agent_with_sub_agents_checks_the_answer_against_the_schema() -> None:
    # The Forge assistant's supervisor has specialists: ADK checks answers then.
    model = Scripted(turns=[asks(DEPLOY), said("Deploying to production.")])
    specialist = LlmAgent(name="runs", model=model, description="Runs things.", mode="task")
    root = LlmAgent(
        name="supervisor",
        model=model,
        tools=[ask_person],
        sub_agents=[specialist],
        after_model_callback=repair_input_schemas,
    )
    chat = Chat(root)

    call = question(await chat.send(*said("Deploy main")))
    await chat.answer(call, {"environment": "production"})
    result = answered(model.seen[1])["result"]
    assert result["environment"] == "production"


async def test_a_schema_with_required_among_its_fields_is_repaired_before_its_kept() -> None:
    broken = {
        "type": "object",
        "properties": {
            "environment": {"type": "string"},
            "required": ["environment", "region"],
            "additionalProperties": False,
        },
    }
    model = Scripted(turns=[asks(broken), said("Done.")])
    chat = Chat(
        LlmAgent(
            name="helper",
            model=model,
            tools=[ask_person],
            after_model_callback=repair_input_schemas,
        )
    )
    call = question(await chat.send(*said("Deploy main")))
    assert call.args is not None
    assert call.args["response_schema"] == {
        "type": "object",
        "properties": {"environment": {"type": "string"}},
        "required": ["environment"],
    }


def test_repairs_reach_nested_fields_and_items_and_leave_good_schemas_alone() -> None:
    nested = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "required": ["name"]},
        },
    }
    assert repair(nested) == {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    }
    assert repair(DEPLOY) == DEPLOY
    assert repair({"type": "string"}) == {"type": "string"}
    assert repair(None) is None


def test_the_tool_is_adks_input_request_with_a_description_of_the_form() -> None:
    declaration = ask_person._get_declaration()
    assert declaration is not None
    assert declaration.name == ASK_PERSON == "adk_request_input"
    assert ask_person.is_long_running
    assert "one question per field" in (declaration.description or "")
