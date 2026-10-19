"""Access rules on agents' nodes: checked for whoever the call is for, as each agent runs."""

from __future__ import annotations

from typing import Any, cast

import pytest
from google.adk.agents import LlmAgent
from google.adk.events.event import Event
from google.genai import types

from forge_agent_runtime.access import (
    Caller,
    HeldAccess,
    RuleError,
    caller_from_json,
    caller_json,
    holds,
    parse_rule,
    permissions_of,
    words,
)
from forge_agent_runtime.build import BuildError, build_app, build_tools
from forge_agent_runtime.document import DocumentError, parse_document
from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.tools import CalledAgent
from tests.conftest import call, text
from tests.test_intake import Chat, answers

APPROVE, READ, WRITE = 1, 2, 3
NAMES = {APPROVE: "invoices:approve", READ: "finance:read", WRITE: "finance:write"}
#: invoices:approve, or both finance:read and finance:write.
BILLING = {"any": [{"permission": APPROVE}, {"all": [{"permission": READ}, {"permission": WRITE}]}]}


def holding(*held: int, **flags: Any) -> RuntimeServices:
    return RuntimeServices(access=HeldAccess(Caller(frozenset(held), NAMES, **flags)))


def document(
    *, entry: Any = None, billing: Any = BILLING, mode: str = "chat", way: str = "agents"
) -> dict[str, Any]:
    """A coordinator handing off to (or calling) a billing specialist."""
    return {
        "format": "forge.chat_agent/v1",
        "id": "ca_desk",
        "name": "Desk",
        "organization_id": "org_1",
        "nodes": [
            {
                "id": "agent",
                "kind": "agent",
                "name": "Coordinator",
                "config": {"instruction": "Help.", "access": entry},
            },
            {
                "id": "sub_billing",
                "kind": "sub_agent",
                "name": "Billing",
                "config": {
                    "description": "Invoices.",
                    "instruction": "Bill.",
                    "mode": mode,
                    "access": billing,
                },
            },
        ],
        "edges": [{"id": "e1", "source": "agent", "source_output": way, "target": "sub_billing"}],
    }


def replies(events: list[Event]) -> list[tuple[str, str]]:
    return [
        (event.author, part.text)
        for event in events
        for part in (event.content.parts if event.content and event.content.parts else [])
        if part.text and not event.partial
    ]


# Rules ---------------------------------------------------------------------------


def test_a_rule_holds_as_its_groups_say() -> None:
    assert holds(BILLING, frozenset({APPROVE}))
    assert holds(BILLING, frozenset({READ, WRITE}))
    assert not holds(BILLING, frozenset({READ}))
    assert not holds(BILLING, frozenset())
    assert holds(None, frozenset())
    assert permissions_of(BILLING) == {APPROVE, READ, WRITE}


def test_a_rule_is_said_in_words() -> None:
    assert words(BILLING, NAMES) == "invoices:approve or both finance:read and finance:write"
    three = {"all": [{"permission": 1}, {"permission": 2}, {"any": [{"permission": 3}, {"permission": 9}]}]}
    assert words(three, NAMES) == (
        "invoices:approve, finance:read, and either finance:write or a deleted permission (#9)"
    )


@pytest.mark.parametrize(
    ("rule", "problem"),
    [
        ({"permission": 0}, "access.permission should be a permission's ID"),
        ({"permission": True}, "access.permission should be a permission's ID"),
        ({"any": []}, "access.any should list at least one rule"),
        ({"none": [{"permission": 1}]}, "access should be"),
        ({"any": [{"permission": 1}], "all": [{"permission": 2}]}, "access should be"),
        (
            {"any": [{"all": [{"any": [{"all": [{"permission": 1}]}]}]}]},
            r"access.any\[0\].all\[0\].any\[0\] nests groups more than 3 deep",
        ),
        ({"any": [{"permission": i} for i in range(1, 52)]}, "names more than 50 permissions"),
    ],
)
def test_what_isnt_a_rule_is_refused(rule: Any, problem: str) -> None:
    with pytest.raises(RuleError, match=problem):
        parse_rule(rule)


def test_a_document_checks_its_rules_against_the_format() -> None:
    with pytest.raises(DocumentError):
        parse_document(document(billing={"any": []}))
    assert parse_document(document()).nodes[1].config["access"] == BILLING
    # Older documents have none: anyone may.
    old = document()
    del old["nodes"][1]["config"]["access"]
    assert parse_document(old).nodes[1].config["access"] is None


def test_a_caller_goes_to_a_workflows_worker_as_json() -> None:
    caller = Caller(frozenset({READ}), NAMES, everything=False, known=True)
    assert caller_from_json(caller_json(caller)) == caller


# As the agents run -----------------------------------------------------------------


async def test_the_chat_agent_refuses_someone_without_what_it_needs(models, llm) -> None:
    app = await build_app(
        parse_document(document(entry={"permission": APPROVE})), models=models, services=holding()
    )
    events = await Chat(app).say(text("Hi"))
    assert replies(events) == [
        (
            "coordinator",
            "You can't use Coordinator: it needs invoices:approve. Ask your organization's admins for access.",
        )
    ]
    assert llm.requests == []


async def test_a_hand_off_to_an_agent_they_may_not_use_stays_with_the_agent_handing_off(models, llm) -> None:
    app = await build_app(parse_document(document()), models=models, services=holding(READ))
    chat = Chat(app)
    llm.turns = [
        [call("transfer_to_agent", agent_name="billing")],
        [text("Sorry, billing needs a permission.")],
    ]
    events = await chat.say(text("My invoice"))
    [refused] = answers(events, "transfer_to_agent")
    assert refused["error"].startswith(
        "You can't use Billing: it needs invoices:approve or both finance:read and finance:write."
    )
    assert replies(events)[-1] == ("coordinator", "Sorry, billing needs a permission.")
    # The conversation is still the coordinator's.
    llm.turns = [[text("Anything else?")]]
    events = await chat.say(text("OK"))
    assert replies(events) == [("coordinator", "Anything else?")]


async def test_someone_holding_what_it_needs_is_handed_to_it(models, llm) -> None:
    app = await build_app(parse_document(document()), models=models, services=holding(READ, WRITE))
    llm.turns = [[call("transfer_to_agent", agent_name="billing")], [text("Billing here.")]]
    events = await Chat(app).say(text("My invoice"))
    assert replies(events) == [("billing", "Billing here.")]


async def test_those_who_manage_the_agents_may_use_them_all(models, llm) -> None:
    app = await build_app(
        parse_document(document(entry={"permission": APPROVE})),
        models=models,
        services=holding(everything=True),
    )
    llm.turns = [[call("transfer_to_agent", agent_name="billing")], [text("Billing here.")]]
    events = await Chat(app).say(text("My invoice"))
    assert replies(events) == [("billing", "Billing here.")]


async def test_an_agent_called_as_a_tool_answers_what_it_needs(models, llm) -> None:
    app = await build_app(parse_document(document(way="tools")), models=models, services=holding())
    llm.turns = [[call("billing", request="Total?")], [text("Billing isn't yours to use.")]]
    events = await Chat(app).say(text("My invoice"))
    [refused] = answers(events, "billing")
    assert "You can't use Billing: it needs invoices:approve" in refused["error"]
    assert replies(events)[-1] == ("coordinator", "Billing isn't yours to use.")


@pytest.mark.parametrize("mode", ["task", "single_turn"])
async def test_a_task_given_to_an_agent_they_may_not_use_is_answered_what_it_needs(models, llm, mode) -> None:
    app = await build_app(parse_document(document(mode=mode)), models=models, services=holding(READ))
    chat = Chat(app)
    llm.turns = [[text("Let me ask billing."), call("billing", request="Total?")]]
    events = await chat.say(text("My invoice"))
    # The call is said as what Billing needs, and no task is left open.
    assert [said for _, said in replies(events)] == [
        "Let me ask billing.",
        "You can't use Billing: it needs invoices:approve or both finance:read and finance:write. "
        "Ask your organization's admins for access.",
    ]
    assert {author for author, _ in replies(events)} == {"coordinator"}
    assert not [c for e in events for c in e.get_function_calls()]
    llm.turns = [[text("Anything else?")]]
    assert replies(await chat.say(text("OK"))) == [("coordinator", "Anything else?")]


@pytest.mark.parametrize("mode", ["task", "single_turn"])
async def test_a_task_given_to_an_agent_they_may_use_runs(models, llm, mode) -> None:
    app = await build_app(parse_document(document(mode=mode)), models=models, services=holding(APPROVE))
    llm.turns = [[call("billing", request="Total?")], [text("Billing: 12 EUR.")], [text("It's 12 EUR.")]]
    await Chat(app).say(text("My invoice"))
    # Billing itself was asked: its instruction went to the model.
    assert any("Bill." in str(request.config.system_instruction) for request in llm.requests[1:])


async def test_without_a_check_agents_with_a_rule_refuse(models, llm) -> None:
    app = await build_app(parse_document(document(entry={"permission": APPROVE})), models=models)
    events = await Chat(app).say(text("Hi"))
    assert replies(events) == [
        (
            "coordinator",
            "Coordinator needs permission #1, which this runtime can't check, so it can't be used here.",
        )
    ]


async def test_someone_unknown_is_told_to_sign_in(models) -> None:
    app = await build_app(
        parse_document(document(entry={"permission": APPROVE})), models=models, services=holding(known=False)
    )
    events = await Chat(app).say(text("Hi"))
    [(_, said)] = replies(events)
    assert said.startswith(
        "Coordinator is only for people its organization lets use it: it needs invoices:approve."
    )


# Saved agents ------------------------------------------------------------------------


def saved(entry: Any, node: Any) -> dict[str, Any]:
    """A desk handing off to a saved billing agent, whose own entry may have a rule too."""
    billing = {
        "format": "forge.chat_agent/v1",
        "id": "ca_billing",
        "name": "Billing",
        "organization_id": "org_1",
        "nodes": [{"id": "agent", "kind": "agent", "name": "Billing desk", "config": {"access": entry}}],
        "edges": [],
    }
    return {
        "format": "forge.chat_agent/v1",
        "id": "ca_desk",
        "name": "Desk",
        "organization_id": "org_1",
        "nodes": [
            {"id": "agent", "kind": "agent", "name": "Coordinator", "config": {}},
            {
                "id": "saved_b",
                "kind": "saved_agent",
                "name": "Billing",
                "config": {"agent": "ca_billing", "access": node},
            },
        ],
        "edges": [{"id": "e1", "source": "agent", "source_output": "agents", "target": "saved_b"}],
        "dependencies": {"ca_billing@1": billing},
    }


@pytest.mark.parametrize(
    ("entry", "node", "needs"),
    [
        ({"permission": READ}, None, "finance:read"),
        (None, {"permission": WRITE}, "finance:write"),
        ({"permission": READ}, {"permission": WRITE}, "finance:write and finance:read"),
    ],
)
async def test_a_saved_agents_rule_and_its_nodes_both_hold(models, llm, entry, node, needs) -> None:
    app = await build_app(parse_document(saved(entry, node)), models=models, services=holding())
    # It's called after the node it's placed at.
    llm.turns = [[call("transfer_to_agent", agent_name="billing")], [text("No.")]]
    events = await Chat(app).say(text("My invoice"))
    [refused] = answers(events, "transfer_to_agent")
    assert f"it needs {needs}." in refused["error"]


async def test_a_workflows_saved_agent_tool_checks_its_rule(models, llm) -> None:
    [tool] = await build_tools(
        "Billing",
        "saved_agent",
        {"agent": "ca_billing", "access": {"permission": READ}},
        models=models,
        services=holding(READ),
        organization_id="org_1",
        dependencies=saved(None, None)["dependencies"],
    )
    assert isinstance(tool, CalledAgent) and tool.tool.name == "billing"
    with pytest.raises(BuildError, match="'Billing''s access should be"):
        await build_tools(
            "Billing",
            "saved_agent",
            {"agent": "ca_billing", "access": {"nope": 1}},
            models=models,
            organization_id="org_1",
            dependencies=saved(None, None)["dependencies"],
        )


def two_saved(way: str = "tools", *, billing: Any = None, hr: Any = None) -> dict[str, Any]:
    """A desk using two saved agents, placed as Billing and HR, whose entries are both called Assistant."""

    def assistant(agent_id: str, instruction: str, rule: Any) -> dict[str, Any]:
        return {
            "format": "forge.chat_agent/v1",
            "id": agent_id,
            "name": agent_id,
            "organization_id": "org_1",
            "nodes": [
                {
                    "id": "agent",
                    "kind": "agent",
                    "name": "Assistant",
                    "config": {"description": instruction, "instruction": instruction, "access": rule},
                }
            ],
            "edges": [],
        }

    return {
        "format": "forge.chat_agent/v1",
        "id": "ca_desk",
        "name": "Desk",
        "organization_id": "org_1",
        "nodes": [
            {"id": "agent", "kind": "agent", "name": "Coordinator", "config": {"instruction": "Help."}},
            {"id": "saved_b", "kind": "saved_agent", "name": "Billing", "config": {"agent": "ca_billing"}},
            {"id": "saved_h", "kind": "saved_agent", "name": "HR", "config": {"agent": "ca_hr"}},
        ],
        "edges": [
            {"id": "e1", "source": "agent", "source_output": way, "target": "saved_b"},
            {"id": "e2", "source": "agent", "source_output": way, "target": "saved_h"},
        ],
        "dependencies": {
            "ca_billing@1": assistant("ca_billing", "Bill.", billing),
            "ca_hr@1": assistant("ca_hr", "People.", hr),
        },
    }


def root(app: Any) -> LlmAgent:
    return cast(LlmAgent, app.root_agent)


def called(agent: LlmAgent) -> list[str]:
    """:return: The names of the agents it calls as tools."""
    return [tool.tool.name for tool in agent.tools if isinstance(tool, CalledAgent)]


def instructions(llm: Any) -> list[str]:
    return [str(request.config.system_instruction) for request in llm.requests]


async def test_saved_agents_whose_entries_share_a_name_are_called_after_their_places(models, llm) -> None:
    app = await build_app(parse_document(two_saved()), models=models, services=holding())
    assert called(root(app)) == ["billing", "hr"]

    llm.turns = [[call("billing", request="Total?")], [text("12 EUR.")], [text("It's 12 EUR.")]]
    await Chat(app).say(text("My invoice"))
    assert instructions(llm)[1].startswith("Bill.")

    llm.requests.clear()
    llm.turns = [[call("hr", request="Leave?")], [text("25 days.")], [text("You have 25 days.")]]
    await Chat(app).say(text("My leave"))
    assert instructions(llm)[1].startswith("People.")


async def test_saved_agents_whose_entries_share_a_name_are_handed_to_after_their_places(models, llm) -> None:
    app = await build_app(parse_document(two_saved("agents")), models=models, services=holding())
    assert [agent.name for agent in root(app).sub_agents] == ["billing", "hr"]
    llm.turns = [[call("transfer_to_agent", agent_name="hr")], [text("HR here.")]]
    events = await Chat(app).say(text("My leave"))
    assert replies(events) == [("hr", "HR here.")]
    assert instructions(llm)[1].startswith("People.")


async def test_saved_agents_whose_entries_share_a_name_keep_their_own_rules(models, llm) -> None:
    doc = two_saved(billing={"permission": READ}, hr={"permission": WRITE})
    app = await build_app(parse_document(doc), models=models, services=holding(READ))

    llm.turns = [[call("hr", request="Leave?")], [text("HR isn't yours.")]]
    events = await Chat(app).say(text("My leave"))
    [refused] = answers(events, "hr")
    assert refused["error"].startswith("You can't use HR: it needs finance:write.")

    # Billing needs only its own rule: HR's isn't merged into it.
    llm.requests.clear()
    llm.turns = [[call("billing", request="Total?")], [text("12 EUR.")], [text("It's 12 EUR.")]]
    events = await Chat(app).say(text("My invoice"))
    assert "error" not in answers(events, "billing")[0]
    assert instructions(llm)[1].startswith("Bill.")


async def test_agents_left_with_the_same_name_are_refused(models) -> None:
    # A saved agent placed under a name another of the desk's agents has.
    doc = two_saved()
    doc["nodes"][2]["name"] = "Billing"
    with pytest.raises(BuildError, match="'Billing' and 'Billing' have the same ADK name, 'billing'"):
        await build_app(parse_document(doc), models=models, services=holding())

    # One with an agent of its own named as another of the desk's.
    doc = two_saved()
    hr = doc["dependencies"]["ca_hr@1"]
    hr["nodes"].append(
        {"id": "pay", "kind": "sub_agent", "name": "Billing", "config": {"instruction": "Pay."}}
    )
    hr["edges"].append({"id": "p", "source": "agent", "source_output": "tools", "target": "pay"})
    with pytest.raises(BuildError, match="'Billing' and 'Billing' in 'HR' have the same ADK name"):
        await build_app(parse_document(doc), models=models, services=holding())

    # A place ADK can't name it after.
    doc = two_saved()
    doc["nodes"][2]["name"] = "2"
    with pytest.raises(BuildError, match="'2' needs a letter in its name"):
        await build_app(parse_document(doc), models=models, services=holding())
    doc["nodes"][2]["name"] = "User"
    with pytest.raises(BuildError, match="ADK keeps that name for the person"):
        await build_app(parse_document(doc), models=models, services=holding())


async def test_one_saved_agent_two_others_use_is_built_for_each(models) -> None:
    # Billing and HR both hand off to a saved Ledger, which has a sub-agent of its own.
    doc = two_saved("agents")
    doc["dependencies"]["ca_ledger@1"] = {
        "format": "forge.chat_agent/v1",
        "id": "ca_ledger",
        "name": "Ledger",
        "organization_id": "org_1",
        "nodes": [
            {"id": "agent", "kind": "agent", "name": "Ledger", "config": {}},
            {"id": "audit", "kind": "sub_agent", "name": "Audit", "config": {}},
        ],
        "edges": [{"id": "a", "source": "agent", "source_output": "tools", "target": "audit"}],
    }
    for agent_id, place in (("ca_billing@1", "Billing ledger"), ("ca_hr@1", "HR ledger")):
        used = doc["dependencies"][agent_id]
        used["nodes"].append(
            {"id": "ledger", "kind": "saved_agent", "name": place, "config": {"agent": "ca_ledger"}}
        )
        used["edges"].append({"id": "l", "source": "agent", "source_output": "tools", "target": "ledger"})
    # As tools, each copy runs apart: Audit is the same agent in both.
    app = await build_app(parse_document(doc), models=models, services=holding())
    billing, hr = cast(list[LlmAgent], root(app).sub_agents)
    assert called(billing) == ["billing_ledger"]
    assert called(hr) == ["hr_ledger"]

    # Handed to, both copies' Audit would be in one tree.
    for agent_id in ("ca_billing@1", "ca_hr@1"):
        doc["dependencies"][agent_id]["edges"][0]["source_output"] = "agents"
    ledger = doc["dependencies"]["ca_ledger@1"]
    ledger["edges"][0]["source_output"] = "agents"
    with pytest.raises(BuildError, match="'audit' is reached twice, by two saved agents that both use it"):
        await build_app(parse_document(doc), models=models, services=holding())


def test_types_parts() -> None:
    # The replies helper reads text parts only.
    assert replies([Event(author="x", content=types.Content(parts=[types.Part(text="a")]))]) == [("x", "a")]
