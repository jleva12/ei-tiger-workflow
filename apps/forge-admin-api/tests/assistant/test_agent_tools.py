"""The assistant's agent builder tools, against a stand-in for the admin API:
what each sends (as whom, where, with what), and what the model gets back."""

import asyncio
import json
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import SecretStr
from test_admin_tools import Api, Handler, context
from test_agent_edits import AGENT, ORG, agent

from forge_admin.assistant.agent_tools import (
    CONFIRMED,
    TOOL_NAMES,
    AgentToolset,
    turn_report,
)
from forge_admin.config import Settings

BASE = f"/api/v1/organizations/{ORG}/chat-agents/{AGENT}"


@pytest.fixture
def signing(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 32), "api_key": SecretStr("k" * 32)}
    )


def record(revision: int = 4, *, has_draft: bool = True) -> dict[str, Any]:
    return {
        "id": AGENT,
        "organization_id": ORG,
        "revision": revision,
        "document": agent(),
        "status": "published+draft" if has_draft else "published",
        "has_draft": has_draft,
        "draft_version": 2 if has_draft else None,
        "published_version": 1,
    }


def sse(*events: dict[str, Any]) -> str:
    return ":ok\n\n" + "".join(f"data: {json.dumps(event)}\n\n" for event in events)


class Agents:
    """The chat agent routes, as the API answers them, saving drafts."""

    def __init__(self, *, has_draft: bool = True) -> None:
        self.current = record(has_draft=has_draft)
        self.stream = sse()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/versions") and request.method == "POST":
            self.current = {**record(self.current["revision"] + 1), "draft_version": 2}
            return httpx.Response(200, json=self.current)
        if path.endswith("/draft") and request.method == "PUT":
            body = json.loads(request.content)
            if body["revision"] != self.current["revision"]:
                return httpx.Response(409, json={"detail": "It was saved since."})
            self.current = {
                **self.current,
                "revision": self.current["revision"] + 1,
                "document": body["document"],
            }
            return httpx.Response(200, json=self.current)
        if path.endswith(AGENT):
            return httpx.Response(200, json=self.current)
        if path.endswith("/chat-agents") and request.method == "POST":
            document = json.loads(request.content)["document"]
            return httpx.Response(
                201,
                json={**record(1), "id": "ca_newone001", "document": document},
            )
        if path.endswith("/knowledge-bases"):
            return httpx.Response(
                200,
                json=[{"id": "kb1", "name": "Help centre", "kind": "rag", "ready": 12}],
            )
        if path.endswith("/helpers/instruction_writer"):
            return httpx.Response(
                200,
                json={
                    "helper": "instruction_writer",
                    "result": {
                        "instruction": "You help customers. Search with "
                        "`search_the_help_centre`."
                    },
                    "notes": ["Add a refunds workflow to handle refunds."],
                    "model": "openai/gpt-5",
                },
            )
        if path.endswith("/sessions") and request.method == "POST":
            return httpx.Response(201, json={"id": "s1", "state": {}})
        if path.endswith("/run_sse"):
            return httpx.Response(
                200, text=self.stream, headers={"content-type": "text/event-stream"}
            )
        if path.endswith("/models"):
            return httpx.Response(200, json={"defaultModel": None, "models": []})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "Not found"})


def tools(signing: Settings, handler: Handler) -> tuple[AgentToolset, Api]:
    api = Api(signing, handler)
    return AgentToolset(api.person_api), api


def call(toolset: AgentToolset, tool_name: str, /, **args: Any) -> Any:
    async def main() -> Any:
        found = {tool.name: tool for tool in await toolset.get_tools()}
        return await found[tool_name].run_async(args=args, tool_context=context("ada"))

    return asyncio.run(main())


def test_it_offers_its_tools_and_asks_first_only_for_a_test(signing: Settings) -> None:
    toolset, _ = tools(signing, Agents())

    async def main() -> dict[str, Any]:
        return {tool.name: tool for tool in await toolset.get_tools()}

    found = asyncio.run(main())
    assert set(found) == TOOL_NAMES
    asking = {
        name
        for name, tool in found.items()
        if getattr(tool, "_require_confirmation", False)
    }
    assert asking == CONFIRMED == {"test_agent"}


def test_an_agent_reads_as_its_nodes_connections_and_problems(
    signing: Settings,
) -> None:
    toolset, api = tools(signing, Agents())

    found = call(toolset, "get_agent", organization_id=ORG, agent_id=AGENT)

    assert found["status"] == "success", found
    view = found["payload"]
    assert api.calls[0].url.path == BASE
    # The organization's records, to check what its nodes pick.
    assert f"/api/v1/organizations/{ORG}/agent-access/permissions" in {
        call.url.path for call in api.calls
    }
    assert view["revision"] == 4
    assert view["editing"] == "its draft (version 2)"
    assert [(n["id"], n["is"]) for n in view["nodes"]] == [
        ("agent", "the chat agent people talk to"),
        ("kb", "a tool"),
    ]
    # Empty settings say nothing.
    assert view["nodes"][1]["settings"] == {
        "knowledge_bases": ["kb1"],
        "max_results": 5,
    }
    assert view["connections"] == [
        {
            "from": "agent (Support)",
            "way": "tools (uses as a tool)",
            "to": "kb (Search the help centre)",
        }
    ]
    assert view["problems"] == []
    assert view["link"] == f"/organizations/{ORG}/chat-agents/{AGENT}"


def test_an_edit_saves_the_draft_from_the_revision_read_and_says_what_it_did(
    signing: Settings,
) -> None:
    routes = Agents()
    toolset, api = tools(signing, routes)

    found = call(
        toolset,
        "edit_agent",
        organization_id=ORG,
        agent_id=AGENT,
        revision=4,
        changes=[
            {
                "action": "add_node",
                "kind": "memory",
                "id": "memory",
                "name": "Remember them",
                "attach_to": {"agent": "agent"},
            },
            {"action": "update_agent", "name": "Customer support"},
        ],
    )

    assert found["status"] == "success", found
    answer = found["payload"]
    # The builder reloads the draft by these.
    assert (answer["agent_id"], answer["organization_id"], answer["revision"]) == (
        AGENT,
        ORG,
        5,
    )
    assert answer["added"] == ["memory"]
    assert answer["problems"] == []
    put = next(call for call in api.calls if call.method == "PUT")
    assert put.url.path == f"{BASE}/draft"
    assert api.body(api.calls.index(put))["revision"] == 4
    saved = routes.current["document"]
    assert [n["id"] for n in saved["nodes"]] == ["agent", "kb", "memory"]
    # The chat agent goes by the agent's name.
    assert saved["name"] == saved["nodes"][0]["name"] == "Customer support"


def test_an_edit_made_from_an_old_revision_is_refused(signing: Settings) -> None:
    toolset, api = tools(signing, Agents())

    found = call(
        toolset,
        "edit_agent",
        organization_id=ORG,
        agent_id=AGENT,
        revision=2,
        changes=[{"action": "update_agent", "name": "X"}],
    )

    assert found["status"] == "failed"
    assert "is at revision 4 now, not 2" in found["reason"]
    assert all(call.method == "GET" for call in api.calls)


def test_an_edit_to_a_published_agent_starts_its_draft(signing: Settings) -> None:
    routes = Agents(has_draft=False)
    toolset, api = tools(signing, routes)

    found = call(
        toolset,
        "edit_agent",
        organization_id=ORG,
        agent_id=AGENT,
        changes=[{"action": "update_agent", "description": "Answers customers."}],
    )

    assert found["status"] == "success", found
    assert "started draft version 2" in found["payload"]["started_draft"]
    assert [c.method for c in api.calls][:3] == ["GET", "POST", "PUT"]


def test_a_refused_change_saves_nothing(signing: Settings) -> None:
    toolset, api = tools(signing, Agents())

    found = call(
        toolset,
        "edit_agent",
        organization_id=ORG,
        agent_id=AGENT,
        changes=[{"action": "remove_node", "id": "agent"}],
    )

    assert found["status"] == "failed"
    assert "can't be removed" in found["reason"]
    assert all(call.method == "GET" for call in api.calls)


def test_an_instruction_is_written_from_the_agents_brief_and_saved(
    signing: Settings,
) -> None:
    routes = Agents()
    toolset, api = tools(signing, routes)

    found = call(
        toolset,
        "write_instruction",
        organization_id=ORG,
        agent_id=AGENT,
        node_id="agent",
        ask="Answer customers' questions from the help centre, kindly.",
    )

    assert found["status"] == "success", found
    sent = next(
        api.body(index)
        for index, c in enumerate(api.calls)
        if c.url.path.endswith("/helpers/instruction_writer")
    )
    assert sent["current"] == "Help customers."
    brief = sent["brief"]
    assert (brief["where"], brief["role"], brief["name"]) == (
        "chat",
        "chat_agent",
        "Support",
    )
    assert brief["tools"] == [
        {
            "kind": "knowledge_base",
            "name": "Search the help centre",
            "config": {"knowledge_bases": ["kb1"], "description": "", "max_results": 5},
        }
    ]
    assert "state.model" in [ref["path"] for ref in brief["references"]]
    answer = found["payload"]
    assert answer["revision"] == 5
    assert answer["done"] == ["Wrote the instruction of Support (agent)"]
    assert answer["notes"] == ["Add a refunds workflow to handle refunds."]
    assert routes.current["document"]["nodes"][0]["config"]["instruction"].startswith(
        "You help customers."
    )


def test_a_tool_node_has_no_instruction_to_write(signing: Settings) -> None:
    toolset, api = tools(signing, Agents())

    found = call(
        toolset,
        "write_instruction",
        organization_id=ORG,
        agent_id=AGENT,
        node_id="kb",
        ask="Anything.",
    )

    assert found["status"] == "failed"
    assert "isn't the chat agent or one of its sub-agents" in found["reason"]
    assert not any(c.url.path.endswith("instruction_writer") for c in api.calls)


def test_a_new_agent_is_just_its_chat_agent(signing: Settings) -> None:
    toolset, api = tools(signing, Agents())

    found = call(
        toolset,
        "create_agent",
        organization_id=ORG,
        name="HR helpdesk",
        description="Answers staff questions about HR policies.",
    )

    assert found["status"] == "success", found
    sent = api.body()["document"]
    assert sent["format"] == "forge.chat_agent/v1"
    assert [(n["id"], n["kind"], n["name"]) for n in sent["nodes"]] == [
        ("agent", "agent", "HR helpdesk")
    ]
    assert found["payload"]["link"] == f"/organizations/{ORG}/chat-agents/ca_newone001"


def test_a_test_talks_to_the_draft_as_the_person_and_reports_each_turn(
    signing: Settings,
) -> None:
    routes = Agents()
    routes.stream = sse(
        {"author": "support", "customMetadata": {"forge:turn": "started"}},
        {
            "author": "support",
            "content": {
                "role": "model",
                "parts": [
                    {"text": "Let me look.", "thought": True},
                    {
                        "functionCall": {
                            "id": "c1",
                            "name": "search_the_help_centre",
                            "args": {"query": "refund window"},
                        }
                    },
                ],
            },
        },
        {
            "author": "support",
            "content": {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "c1",
                            "name": "search_the_help_centre",
                            "response": {"passages": ["30 days"]},
                        }
                    }
                ],
            },
        },
        {
            "author": "support",
            "content": {"role": "model", "parts": [{"text": "You have 30 days."}]},
        },
    )
    toolset, api = tools(signing, routes)

    found = call(
        toolset,
        "test_agent",
        organization_id=ORG,
        agent_id=AGENT,
        messages=["How long do I have to return something?"],
    )

    assert found["status"] == "success", found
    session = next(c for c in api.calls if c.url.path.endswith("/sessions"))
    assert session.url.path == f"/api/v1/runtime/apps/{AGENT}@draft/users/ada/sessions"
    run = next(
        api.body(index)
        for index, c in enumerate(api.calls)
        if c.url.path.endswith("/run_sse")
    )
    assert run["appName"] == f"{AGENT}@draft"
    assert run["userId"] == "ada" and run["sessionId"] == "s1"
    [[turn]] = found["payload"]["conversations"]
    assert turn == {
        "message": "How long do I have to return something?",
        "answered_by": ["support"],
        "tool_calls": [
            {
                "tool": "search_the_help_centre",
                "by": "support",
                "args": {"query": "refund window"},
                "answer": {"passages": ["30 days"]},
            }
        ],
        "reply": "You have 30 days.",
    }


def test_a_turn_that_asks_the_person_or_fails_says_so() -> None:
    asked = turn_report(
        "Book it",
        sse(
            {
                "author": "booker",
                "longRunningToolIds": ["q1"],
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "id": "q1",
                                "name": "adk_request_input",
                                "args": {"message": "Which day?"},
                            }
                        }
                    ]
                },
            }
        ),
    )
    assert asked["waiting_for_the_person"] == {"message": "Which day?"}
    assert "tool_calls" not in asked

    failed = turn_report(
        "Hi",
        sse(
            {"author": "support", "errorCode": "MODEL_ERROR", "errorMessage": "No key"}
        ),
    )
    assert failed["error"] == "No key"


def test_separate_scenarios_are_conversations_of_their_own(signing: Settings) -> None:
    routes = Agents()
    routes.stream = sse(
        {
            "author": "support",
            "content": {"role": "model", "parts": [{"text": "Sure."}]},
        }
    )
    toolset, api = tools(signing, routes)

    found = call(
        toolset,
        "test_agent",
        organization_id=ORG,
        agent_id=AGENT,
        messages=["How do I return this?", "I was charged twice."],
        separately=True,
    )

    assert found["status"] == "success", found
    conversations = found["payload"]["conversations"]
    assert [[turn["message"] for turn in turns] for turns in conversations] == [
        ["How do I return this?"],
        ["I was charged twice."],
    ]
    assert sum(c.url.path.endswith("/sessions") for c in api.calls) == 2


def test_a_test_asks_the_person_in_words_naming_the_agent(
    signing: Settings,
) -> None:
    def ask(handler: Handler, **args: Any) -> str | None:
        toolset, _ = tools(signing, handler)
        where = {"organization_id": ORG, "agent_id": AGENT}
        return asyncio.run(
            toolset.confirmation_hint("test_agent", where | args, context("ada"))
        )

    real = "the tools it uses act on real systems, so what they do really happens."
    assert ask(Agents(), messages=["Where's my order?"]) == (
        f"Try “Support” with this message?\nIts draft answers as in a real chat, and {real}"
    )
    assert ask(Agents(), messages=["A return", "A bill"], separately=True) == (
        "Try “Support” with 2 separate messages?\nEach starts a new conversation. "
        f"Its draft answers as in a real chat, and {real}"
    )
    assert ask(Agents(has_draft=False), messages=["Hi", "My order"]) == (
        "Try “Support” with a conversation of 2 messages?\nThey're sent in order. "
        f"Its published version answers as in a real chat, and {real}"
    )
    # An agent it can't read is still asked about, by what it is.
    refused = ask(
        lambda request: httpx.Response(404, json={"detail": "Not found"}),
        messages=["Hi"],
    )
    assert refused == (
        f"Try the agent with this message?\nIt answers as in a real chat, and {real}"
    )
    # Other changes don't ask, so they have no question.
    toolset, _ = tools(signing, Agents())
    assert (
        asyncio.run(toolset.confirmation_hint("edit_agent", {}, context("ada"))) is None
    )
