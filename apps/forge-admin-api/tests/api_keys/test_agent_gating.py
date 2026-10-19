"""Who may use each of a chat agent's agents: those holding what the rule on
its node needs (through a linked company group, or an API key linked to
it), and those managing the organization's agents; nobody unknown. The
hosted runtime checks the rules as the agents run, for whoever the call is
for, so a coordinator may answer someone a sub-agent of it doesn't. Chat
pages, older versions and workflow runs hold to them too."""

import json
from collections.abc import AsyncGenerator
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx2 as httpx
import jwt
import pytest
from agent_access_world import (
    ACCESS,
    BUILDER,
    FINANCE,
    add_builder,
    limited_to,
    link,
    permission,
    with_groups,
)
from forge_agent_runtime import AgentExecutor, DirectoryAgentSource, RuntimeServices
from forge_agent_runtime.document import parse_document
from forge_common.adk.models import ProviderModels
from forge_common.model_provider import parse_model_provider_yaml
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from key_world import (
    ADMIN,
    AGENT,
    MEMBER,
    NEIGHBOUR,
    ORG,
    OTHER_AGENT,
    OTHER_ORG,
    PROVIDERS,
    RUNTIME,
    World,
    agent_document,
)
from pydantic import Field

from forge_admin.adk_workflows.resources import RunResources
from forge_admin.adk_workflows.runs import used_resources
from forge_admin.chat_agents.access import (
    AgentUser,
    OrganizationAccess,
    as_strict_as_live,
    rules_in,
    stricter,
    with_record_rule,
)
from forge_admin.chat_agents.store import new_page_token

LINK = new_page_token()


class ScriptedLlm(BaseLlm):
    """Answers each model call with its next turn: a list of parts."""

    model: str = "gpt-test"
    turns: list[Any] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        yield LlmResponse(content=types.Content(role="model", parts=self.turns.pop(0)))


def say(value: str) -> types.Part:
    return types.Part(text=value)


def hand_to(agent: str) -> types.Part:
    return types.Part(
        function_call=types.FunctionCall(
            name="transfer_to_agent", args={"agent_name": agent}
        )
    )


class Agents:
    """The chat agents' store, as far as the runtime and the builder's reads
    use it: each agent's versions, its latest published the last."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.kept: dict[tuple[str, int], dict[str, Any]] = {}

    def publish(self, document: dict[str, Any], **record: Any) -> dict[str, Any]:
        agent_id = document["id"]
        number = 1 + max((n for a, n in self.kept if a == agent_id), default=0)
        self.kept[agent_id, number] = deepcopy({**document, "version": number})
        self.records[agent_id] = {
            "_id": agent_id,
            "organization_id": document["organization_id"],
            "revision": number,
            "document": deepcopy(document),
            "has_draft": False,
            "draft_version": None,
            "published_version": number,
            "published_at": "2026-10-17T00:00:00Z",
            "created_at": "2026-10-17T00:00:00Z",
            "created_by": ADMIN,
            "updated_at": "2026-10-17T00:00:00Z",
            "updated_by": ADMIN,
            "updated_by_name": None,
            **record,
        }
        return self.records[agent_id]

    async def get(self, organization_id: str, agent_id: str) -> dict[str, Any] | None:
        record = self.records.get(agent_id)
        if record is None or record["organization_id"] != organization_id:
            return None
        return record

    async def find(self, agent_id: str) -> dict[str, Any] | None:
        return self.records.get(agent_id)

    async def version(
        self, organization_id: str, agent_id: str, number: int
    ) -> dict[str, Any] | None:
        if await self.get(organization_id, agent_id) is None:
            return None
        found = self.kept.get((agent_id, number))
        return {"document": deepcopy(found)} if found else None

    async def find_version(self, agent_id: str, number: int) -> dict[str, Any] | None:
        found = self.kept.get((agent_id, number))
        return {"document": deepcopy(found)} if found else None

    async def versions(
        self, organization_id: str, agent_id: str
    ) -> list[dict[str, Any]]:
        return []

    async def find_chat_page(self, token: str) -> dict[str, Any] | None:
        for record in self.records.values():
            if (record.get("chat_page") or {}).get("token") == token:
                return record
        return None

    async def aclose(self) -> None:
        pass


def desk(
    agent_id: str = AGENT,
    organization_id: str = ORG,
    *,
    entry: Any = None,
    billing: Any = None,
) -> dict[str, Any]:
    """An organization's helper (Support assistant) handing off to Billing."""
    doc = agent_document(agent_id, organization_id, "Acme helper")
    doc["nodes"][0]["config"]["access"] = entry
    doc["nodes"].append(
        {
            "id": "sub_billing",
            "kind": "sub_agent",
            "name": "Billing",
            "config": {
                "description": "Invoices.",
                "instruction": "Bill.",
                "access": billing,
            },
        }
    )
    doc["edges"].append(
        {
            "id": "e_billing",
            "source": "agent",
            "source_output": "agents",
            "target": "sub_billing",
        }
    )
    return doc


@pytest.fixture
def llm(world: World, tmp_path: Path) -> ScriptedLlm:
    """The runtime as it's hosted: agents from a folder, checking their rules
    against Acme's agent permissions, on a scripted model; and someone who
    builds Acme's agents."""
    scripted = ScriptedLlm()
    models = ProviderModels(
        parse_model_provider_yaml(PROVIDERS), build=lambda _: scripted
    )
    world.client.app.state.agent_executor = AgentExecutor(  # type: ignore[attr-defined]
        DirectoryAgentSource(tmp_path / "agents"),
        models=models,
        http=httpx.AsyncClient(),
        services=RuntimeServices(access=OrganizationAccess(world.sessions)),
    )
    add_builder(world)
    return scripted


def put(tmp_path: Path, document: dict[str, Any]) -> None:
    (tmp_path / "agents" / f"{document['id']}.json").write_text(json.dumps(document))


def signed_in(kw: dict[str, Any]) -> str | None:
    """:return: Who the headers a call sends sign in, when they're a person's token."""
    sent = (kw.get("headers") or {}).get("Authorization", "")
    if not sent.startswith("Bearer ") or sent.startswith("Bearer fk_"):
        return None
    claims = jwt.decode(
        sent.removeprefix("Bearer "), options={"verify_signature": False}
    )
    return str(claims["sub"])


def chat(
    world: World,
    user: str | None,
    message: str = "Hi",
    *,
    agent: str = AGENT,
    who: str | None = None,
    **kw: Any,
) -> list[tuple[str, str]]:
    """
    A turn through the runtime's run API: who said what (text, and tools'
    errors); a turn refused before it runs, as said by ``refused``. A person
    names themself; a key, one of its end users.
    """
    who = who or ("someone" if kw.get("key") else user or signed_in(kw) or "someone")
    created = world.call(
        "POST", f"/apps/{agent}/users/{who}/sessions", user, {}, prefix=RUNTIME, **kw
    )
    assert created.status_code == 201, created.text
    ran = world.call(
        "POST",
        "/run_sse",
        user,
        {
            "appName": agent,
            "userId": who,
            "sessionId": created.json()["id"],
            "newMessage": {"role": "user", "parts": [{"text": message}]},
        },
        prefix=RUNTIME,
        **kw,
    )
    if ran.status_code == 403:
        return [("refused", ran.json()["detail"])]
    assert ran.status_code == 200, ran.text
    said = []
    for chunk in ran.text.split("\n\n"):
        if not chunk.startswith("data: "):
            continue
        event = json.loads(chunk.removeprefix("data: "))
        for part in (event.get("content") or {}).get("parts") or []:
            if part.get("text"):
                said.append((event["author"], part["text"]))
            if "error" in ((part.get("functionResponse") or {}).get("response") or {}):
                said.append(
                    (event["author"], part["functionResponse"]["response"]["error"])
                )
    return said


# ------------------------------------------------------------ the runtime


def test_an_agent_without_rules_answers_anyone(
    world: World, llm: ScriptedLlm, tmp_path: Path
) -> None:
    put(tmp_path, desk())
    llm.turns = [[say("Hello!")], [say("Hello, app.")]]
    assert chat(world, MEMBER) == [("support_assistant", "Hello!")]
    key = world.make_key(name="Outside app")["secret"]
    assert chat(world, None, key=key) == [("support_assistant", "Hello, app.")]


def test_a_chat_agent_with_a_rule_answers_only_those_holding_it(
    world: World, llm: ScriptedLlm, tmp_path: Path
) -> None:
    helper = permission(world, "helper:use", [FINANCE])
    put(tmp_path, desk(entry={"permission": helper}))

    # Refused as the turn starts, before anything runs.
    assert chat(world, MEMBER) == [
        (
            "refused",
            "You can't use Support assistant: it needs helper:use. "
            "Ask your organization's admins for access.",
        )
    ]
    assert llm.requests == []

    # Through a linked company group; and those who build its agents may.
    llm.turns = [[say("Hello, Finance.")], [say("Hello, builder.")]]
    assert chat(world, None, headers=with_groups(world, MEMBER, [FINANCE])) == [
        ("support_assistant", "Hello, Finance.")
    ]
    assert chat(world, BUILDER) == [("support_assistant", "Hello, builder.")]


def test_access_taken_away_stops_the_next_turn_wherever_the_conversation_is(
    world: World, llm: ScriptedLlm, tmp_path: Path
) -> None:
    helper = permission(world, "helper:use", [FINANCE])
    put(tmp_path, desk(entry={"permission": helper}))
    finance = with_groups(world, MEMBER, [FINANCE])
    made = world.call(
        "POST",
        f"/apps/{AGENT}/users/{MEMBER}/sessions",
        None,
        {},
        prefix=RUNTIME,
        headers=finance,
    )
    body = {
        "appName": AGENT,
        "userId": MEMBER,
        "sessionId": made.json()["id"],
        "newMessage": {"role": "user", "parts": [{"text": "My invoice"}]},
    }
    # Handed to Billing, which has no rule of its own.
    llm.turns = [[hand_to("billing")], [say("Billing here.")]]
    ran = world.call("POST", "/run_sse", None, body, prefix=RUNTIME, headers=finance)
    assert ran.status_code == 200 and "Billing here." in ran.text
    asked = len(llm.requests)
    # Finance no longer holds it: the next turn, which would go straight to
    # Billing, is refused as it starts.
    link(world, helper, groups=[])
    again = world.call("POST", "/run_sse", None, body, prefix=RUNTIME, headers=finance)
    assert again.status_code == 403
    assert "it needs helper:use" in again.json()["detail"]
    assert len(llm.requests) == asked


def test_a_coordinator_may_answer_someone_a_sub_agent_of_it_doesnt(
    world: World, llm: ScriptedLlm, tmp_path: Path
) -> None:
    approve = permission(world, "invoices:approve")
    read = permission(world, "finance:read", [FINANCE])
    write = permission(world, "finance:write")
    rule = {
        "any": [
            {"permission": approve},
            {"all": [{"permission": read}, {"permission": write}]},
        ]
    }
    put(tmp_path, desk(billing=rule))
    finance = with_groups(world, MEMBER, [FINANCE])

    llm.turns = [[hand_to("billing")], [say("Billing isn't open to you.")]]
    said = chat(world, None, "My invoice", headers=finance)
    # What Billing needs goes to the model, which tells the person: the hand-off's
    # answer isn't theirs to see (forge_agent_runtime.views).
    assert said == [("support_assistant", "Billing isn't open to you.")]
    told = llm.requests[-1].model_dump_json()
    assert (
        "You can't use Billing: it needs invoices:approve or both finance:read "
        "and finance:write."
    ) in told

    # finance:write too: both of the second way hold.
    link(world, write, groups=[FINANCE])
    llm.turns = [[hand_to("billing")], [say("Billing here.")]]
    assert chat(world, None, "My invoice", headers=finance) == [
        ("billing", "Billing here.")
    ]


def test_an_api_key_linked_to_the_permission_may_use_it(
    world: World, llm: ScriptedLlm, tmp_path: Path
) -> None:
    helper = permission(world, "helper:use")
    put(tmp_path, desk(entry={"permission": helper}))
    made = world.make_key(name="Outside app")
    other = world.make_key(name="Other app")
    link(world, helper, api_keys=[made["id"]])
    llm.turns = [[say("Hello, app.")]]
    assert chat(world, None, key=made["secret"]) == [
        ("support_assistant", "Hello, app.")
    ]
    [(_, refused)] = chat(world, None, key=other["secret"])
    assert refused.startswith("You can't use Support assistant: it needs helper:use.")


def test_a_deleted_permission_is_held_by_nobody(
    world: World, llm: ScriptedLlm, tmp_path: Path
) -> None:
    helper = permission(world, "helper:use", [FINANCE])
    put(tmp_path, desk(entry={"permission": helper}))
    assert (
        world.call("DELETE", f"{ACCESS}/permissions/{helper}", ADMIN).status_code == 204
    )
    [(_, refused)] = chat(world, None, headers=with_groups(world, MEMBER, [FINANCE]))
    assert f"it needs a deleted permission (#{helper})" in refused
    llm.turns = [[say("Hello, builder.")]]
    assert chat(world, BUILDER) == [("support_assistant", "Hello, builder.")]


def test_another_organizations_permission_counts_for_nothing(
    world: World, llm: ScriptedLlm, tmp_path: Path
) -> None:
    helper = permission(world, "helper:use", [FINANCE])
    put(tmp_path, desk(OTHER_AGENT, OTHER_ORG, entry={"permission": helper}))
    # Acme's permission ID means nothing in Globex: nobody holds it there.
    [(_, refused)] = chat(
        world, None, agent=OTHER_AGENT, headers=with_groups(world, NEIGHBOUR, [FINANCE])
    )
    assert "it needs a deleted permission" in refused


# ------------------------------------------------------------ chat pages


def test_a_page_doesnt_answer_for_an_agent_whose_chat_agent_has_a_rule(
    world: World, llm: ScriptedLlm, tmp_path: Path
) -> None:
    helper = permission(world, "helper:use")
    agents = Agents()
    world.client.app.state.chat_agents = agents  # type: ignore[attr-defined]
    page = f"/pages/{LINK}"

    # A sub-agent's rule only tells visitors what it needs.
    document = desk(billing={"permission": helper})
    agents.publish(document, chat_page={"token": LINK, "enabled": True})
    (tmp_path / "agents" / f"{AGENT}@1.json").write_text(
        json.dumps({**document, "version": 1})
    )
    assert world.call("GET", page, None, prefix=RUNTIME).status_code == 200

    # The chat agent's: visitors never hold it.
    document = desk(entry={"permission": helper})
    agents.publish(document, chat_page={"token": LINK, "enabled": True})
    (tmp_path / "agents" / f"{AGENT}@2.json").write_text(
        json.dumps({**document, "version": 2})
    )
    refused = world.call("GET", page, None, prefix=RUNTIME)
    assert refused.status_code == 403
    assert "chat page can't be used" in refused.json()["detail"]


# ------------------------------------------------------------ versions


def test_an_old_version_is_never_open_to_more_people_than_the_live_one(
    world: World,
) -> None:
    agents = Agents()
    agents.publish(desk())
    agents.publish(desk(billing={"permission": 7}, entry={"permission": 8}))
    record = agents.records[AGENT]

    async def as_run(
        version: int | str, document: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        return await as_strict_as_live(agents, ORG, record, document, version)  # type: ignore[arg-type]

    old, stamp = world.run(lambda _: as_run(1, agents.kept[AGENT, 1]))
    assert stamp.startswith("live:2:")
    # Its nodes' own rules stay as they were; what it's held to is beside them.
    assert [n["config"].get("access") for n in old["nodes"]] == [None, None]
    assert old["required_rules"] == {
        "agent": {"permission": 8},
        "sub_billing": {"all": [{"permission": 8}, {"permission": 7}]},
    }
    # They run as the runtime builds them: held to both.
    assert parse_document(old).required_rules == old["required_rules"]
    # The live one is as it is; any it came with are the runtime's to say.
    live, stamp = world.run(lambda _: as_run(2, agents.kept[AGENT, 2]))
    assert stamp == "" and live == agents.kept[AGENT, 2]
    sneaked = {**agents.kept[AGENT, 2], "required_rules": {"agent": {"any": []}}}
    live, _ = world.run(lambda _: as_run(2, sneaked))
    assert "required_rules" not in live


def refund(node_id: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "adk_workflow",
        "name": "Refund",
        "config": {"workflow": "ag_refund00001"},
    }


def test_a_draft_cant_reach_what_the_live_version_guards_by_moving_it() -> None:
    # Live: only those holding finance:write (7) reach the refunds, through Billing.
    live = desk(billing={"permission": 7})
    live["nodes"].append(refund("refund"))
    live["edges"].append(
        {
            "id": "e_r",
            "source": "sub_billing",
            "source_output": "tools",
            "target": "refund",
        }
    )
    # The draft hands the refunds to the chat agent itself, under a node of a new ID.
    draft = desk()
    draft["nodes"].append(refund("refund_again"))
    draft["edges"].append(
        {
            "id": "e_r2",
            "source": "agent",
            "source_output": "tools",
            "target": "refund_again",
        }
    )
    required = stricter(draft, live)
    assert required["agent"] == {"permission": 7}
    # The live version's own is as open as its chat agent: nothing to hold to.
    assert stricter(live, live)["sub_billing"] == {"permission": 7}
    assert "agent" not in stricter(live, live)


def test_an_agent_its_record_limited_shows_and_keeps_that_rule(world: World) -> None:
    agents = Agents()
    record = agents.publish(desk(), access=limited_to(4, 5))
    world.client.app.state.chat_agents = agents  # type: ignore[attr-defined]
    rule = {"any": [{"permission": 4}, {"permission": 5}]}

    # The builder shows it on the chat agent node, which it's published with.
    assert (
        with_record_rule(record["document"], record)["nodes"][0]["config"]["access"]
        == rule
    )
    read = world.call("GET", f"/organizations/{ORG}/chat-agents/{AGENT}", MEMBER)
    assert read.status_code == 200, read.text
    assert read.json()["document"]["nodes"][0]["config"]["access"] == rule
    assert "access" not in read.json()
    # Until then, every version holds to it as it runs.
    made, stamp = world.run(
        lambda _: as_strict_as_live(agents, ORG, record, record["document"], 1)  # type: ignore[arg-type]
    )
    assert stamp.startswith("old:")
    assert made["required_rules"]["agent"] == rule
    # Who may use an agent isn't said apart from its nodes any more.
    gone = world.call(
        "PUT",
        f"/organizations/{ORG}/chat-agents/{AGENT}/access",
        ADMIN,
        {"permission_ids": []},
    )
    assert gone.status_code in (404, 405)


# ------------------------------------------------------------ workflow runs


def test_a_run_carries_what_its_starter_holds_of_its_agents_rules(world: World) -> None:
    helper = permission(world, "helper:use")
    other = permission(world, "other:use", [FINANCE])
    agents = Agents()
    agents.publish(desk(billing={"permission": helper}, entry={"permission": other}))
    workflow = {
        "nodes": [
            {
                "id": "helper",
                "kind": "llm",
                "name": "Ask the helper",
                "config": {"source": "agent", "agent": AGENT},
            }
        ]
    }

    def resources(user: AgentUser | None) -> RunResources:
        return RunResources(
            ORG,
            chat_agents=agents,  # type: ignore[arg-type]
            sessions=world.sessions,
            user=user,
            workflows=None,
        )

    async def start(user: AgentUser | None) -> Any:
        found, _, _ = await used_resources([workflow], resources(user))
        return await resources(user).caller(rules_in(found.values()))

    held = world.run(lambda _: start(AgentUser(MEMBER, (FINANCE,))))
    assert held.held == {other} and held.names == {
        helper: "helper:use",
        other: "other:use",
    }
    assert world.run(lambda _: start(AgentUser(None))).known is False
    assert world.run(lambda _: start(AgentUser(BUILDER, manager=True))).everything
    # Checking or publishing a workflow runs nothing, so holds nothing.
    assert world.run(lambda _: start(None)) is None


# ------------------------------------------------------------ what callers see

CONSOLE_CLIENT = {"X-Forge-Client": "console"}


def turn_events(
    world: World, user: str | None, message: str = "My invoice", **kw: Any
) -> list[dict[str, Any]]:
    """A turn's events as its caller is shown them (forge_agent_runtime.views)."""
    who = "someone" if kw.get("key") else user or "someone"
    created = world.call(
        "POST", f"/apps/{AGENT}/users/{who}/sessions", user, {}, prefix=RUNTIME, **kw
    )
    assert created.status_code == 201, created.text
    ran = world.call(
        "POST",
        "/run_sse",
        user,
        {
            "appName": AGENT,
            "userId": who,
            "sessionId": created.json()["id"],
            "newMessage": {"role": "user", "parts": [{"text": message}]},
        },
        prefix=RUNTIME,
        **kw,
    )
    assert ran.status_code == 200, ran.text
    return [
        json.loads(chunk.removeprefix("data: "))
        for chunk in ran.text.split("\n\n")
        if chunk.startswith("data: ")
    ]


def calls_carried(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """:return: What each call in the events was made with, as shown."""
    return [
        part["functionCall"].get("args", {})
        for event in events
        for part in (event.get("content") or {}).get("parts") or []
        if "functionCall" in part
    ]


def test_whoever_builds_the_agents_sees_what_its_calls_carry_in_the_console(
    world: World, llm: ScriptedLlm, tmp_path: Path
) -> None:
    put(tmp_path, desk())
    llm.turns = [[hand_to("billing")], [say("Billing here.")]]
    events = turn_events(world, BUILDER, headers=CONSOLE_CLIENT)
    assert calls_carried(events) == [{"agent_name": "billing"}]


def test_everyone_else_sees_a_call_but_not_what_it_carries(
    world: World, llm: ScriptedLlm, tmp_path: Path
) -> None:
    put(tmp_path, desk())
    key = world.make_key(name="Outside app")["secret"]
    callers: list[tuple[str | None, dict[str, Any]]] = [
        # A member chatting in the console, who doesn't build the agents.
        (MEMBER, {"headers": CONSOLE_CLIENT}),
        # Whoever builds them, from anywhere but the console (the desktop app).
        (BUILDER, {}),
        # An outside app.
        (None, {"key": key}),
    ]
    for user, kw in callers:
        llm.turns = [[hand_to("billing")], [say("Billing here.")]]
        events = turn_events(world, user, **kw)
        assert calls_carried(events) == [{}], (user, kw)
        assert any(
            part.get("text") == "Billing here."
            for event in events
            for part in (event.get("content") or {}).get("parts") or []
        )
