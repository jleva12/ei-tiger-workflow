"""Approvals ask the person in each tool's words (``AskInWords``), and several
asked in one turn resume together when they're answered together."""

import asyncio
from collections.abc import Mapping
from typing import Any
from unittest.mock import MagicMock

import httpx2 as httpx
import pytest
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.events import Event
from google.adk.runners import InMemoryRunner
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import SecretStr
from scripted_llm import ScriptedLlm
from test_admin_tools import Api, context

from forge_admin.assistant.access_tools import AccessToolset
from forge_admin.assistant.admin_tools import AdministrationToolset
from forge_admin.assistant.agent_tools import AgentToolset
from forge_admin.assistant.knowledge_tools import KnowledgeToolset
from forge_admin.assistant.route_tools import AskInWords, RouteToolset
from forge_admin.assistant.workflow_tools import WorkflowToolset
from forge_admin.config import Settings

ADK_HINT = (
    "Please approve or reject the tool call {}() by responding with a "
    "FunctionResponse with an expected ToolConfirmation payload."
)


@pytest.fixture
def signing(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 32), "api_key": SecretStr("k" * 32)}
    )


class Lights(RouteToolset):
    """Two changes: one with a question of its own, one whose question fails."""

    CHANGE_TOOLS = ("switch_off", "switch_on")

    def __init__(self) -> None:
        super().__init__(MagicMock())
        self.switched: list[str] = []

    async def switch_off(self, room: str, tool_context: ToolContext) -> dict[str, str]:
        """Switch the lights off in a room."""
        self.switched.append(f"off: {room}")
        return {"lights": "off"}

    async def switch_on(self, room: str, tool_context: ToolContext) -> dict[str, str]:
        """Switch the lights on in a room."""
        self.switched.append(f"on: {room}")
        return {"lights": "on"}

    async def confirmation_hint(
        self, name: str, args: Mapping[str, Any], tool_context: ToolContext
    ) -> str | None:
        if name == "switch_off":
            room = args["room"]
            return f"Switch the lights off in the {room}?\nAnyone there is in the dark."
        raise RuntimeError("No words for that")


def call(call_id: str, name: str, **args: Any) -> types.Part:
    return types.Part(
        function_call=types.FunctionCall(id=call_id, name=name, args=args)
    )


def approval(request_id: str, confirmed: bool = True) -> types.Part:
    """What the chat's approval card sends."""
    return types.Part(
        function_response=types.FunctionResponse(
            id=request_id,
            name="adk_request_confirmation",
            response={"confirmed": confirmed},
        )
    )


def test_each_approval_asks_in_its_tools_words_and_several_resume_together() -> None:
    lights = Lights()
    deployed: list[str] = []

    def deploy(environment: str) -> dict[str, str]:
        """Deploy the application."""
        deployed.append(environment)
        return {"status": "deployed"}

    model = ScriptedLlm(
        turns=[
            # One turn, three changes that each wait for the person.
            [
                call("call-1", "switch_off", room="kitchen"),
                call("call-2", "switch_on", room="hall"),
                call("call-3", "deploy", environment="prod"),
            ],
            [types.Part.from_text(text="Done.")],
        ]
    )
    agent = LlmAgent(
        name="forge",
        model=model,
        tools=[lights, FunctionTool(deploy, require_confirmation=True)],
    )
    runner = InMemoryRunner(
        app=App(name="forge", root_agent=agent, plugins=[AskInWords()])
    )

    async def turn(session_id: str, *parts: types.Part) -> list[Event]:
        message = types.Content(role="user", parts=list(parts))
        return [
            event
            async for event in runner.run_async(
                user_id="ada", session_id=session_id, new_message=message
            )
        ]

    async def main() -> tuple[list[Event], list[str], list[Event]]:
        session = await runner.session_service.create_session(
            app_name="forge", user_id="ada"
        )
        asked = await turn(session.id, types.Part.from_text(text="Lights, then ship"))
        requests = {
            fc.args["originalFunctionCall"]["id"]: fc.id
            for event in asked
            for fc in event.get_function_calls()
            if fc.name == "adk_request_confirmation"
        }
        ran_before = [*lights.switched, *deployed]
        # Every card answered, then sent at once, as the chat sends them.
        done = await turn(
            session.id,
            approval(requests["call-1"]),
            approval(requests["call-2"], confirmed=False),
            approval(requests["call-3"]),
        )
        return asked, ran_before, done

    asked, ran_before, done = asyncio.run(main())
    hints = {
        fc.args["originalFunctionCall"]["name"]: fc.args["toolConfirmation"]["hint"]
        for event in asked
        for fc in event.get_function_calls()
        if fc.name == "adk_request_confirmation"
    }
    assert hints == {
        # The toolset's question, in place of ADK's.
        "switch_off": "Switch the lights off in the kitchen?\n"
        "Anyone there is in the dark.",
        # A question that fails to word, and a tool without one, keep ADK's.
        "switch_on": ADK_HINT.format("switch_on"),
        "deploy": ADK_HINT.format("deploy"),
    }
    # Nothing ran before the person answered.
    assert ran_before == []

    # The approved calls ran in one resumed run; the denied one didn't.
    assert lights.switched == ["off: kitchen"]
    assert deployed == ["prod"]
    answers = {
        fr.name: fr.response for event in done for fr in event.get_function_responses()
    }
    assert answers["switch_off"]["payload"] == {"lights": "off"}
    # The model hears the person chose not to, not ADK's "rejected".
    assert answers["switch_on"]["status"] == "failed"
    assert answers["switch_on"]["reason"].startswith("The person chose not to")
    assert answers["deploy"] == {"status": "deployed"}
    assert [part.text for part in done[-1].content.parts] == ["Done."]
    # The model was asked once more, for the whole turn: not once per approval.
    assert model.turns == [] and len(model.requests) == 2


@pytest.mark.parametrize(
    "toolset_class",
    [
        AccessToolset,
        AdministrationToolset,
        AgentToolset,
        KnowledgeToolset,
        WorkflowToolset,
    ],
)
def test_every_change_that_asks_first_has_a_question(
    signing: Settings, toolset_class: type[RouteToolset]
) -> None:
    """Even when nothing it names can be read, nor its arguments make sense."""
    api = Api(signing, lambda request: httpx.Response(404, json={"detail": "No"}))
    toolset = toolset_class(api.person_api)

    async def main() -> dict[str, str | None]:
        found = await toolset.get_tools()
        return {
            tool.name: await tool.confirmation_hint({}, context("ada"))
            for tool in found
            if getattr(tool, "_require_confirmation", False)
        }

    questions = asyncio.run(main())
    assert questions
    for name, question in questions.items():
        asked, _, why = (question or "").partition("\n")
        assert asked.endswith("?") and why, name
        assert "()" not in question and "_" not in asked, name
