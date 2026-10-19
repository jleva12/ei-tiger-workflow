"""The changes the agent builder assistant makes to a chat agent's document,
and what it finds wrong with one."""

import asyncio
import copy
from typing import Any

import pytest
from forge_agent_runtime.document import DEFAULTS, FORMAT, WAYS

from forge_admin.assistant.agent_edits import EditRefused, apply_changes, problems_of
from forge_admin.assistant.building_blocks import building_blocks

ORG = "0b9e3c1a-2f4d-4c6e-8a1b-3d5f7a9c1e2b"
AGENT = "ca_support01"


def node(node_id: str, kind: str, name: str, **config: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "name": name,
        "config": {**copy.deepcopy(DEFAULTS[kind]), **config},
        "outputs": list(WAYS[kind]),
    }


def agent() -> dict[str, Any]:
    """A support agent that searches one knowledge base."""
    return {
        "format": FORMAT,
        "id": AGENT,
        "name": "Support",
        "description": "",
        "organization_id": ORG,
        "nodes": [
            node("agent", "agent", "Support", instruction="Help customers."),
            node(
                "kb",
                "knowledge_base",
                "Search the help centre",
                knowledge_bases=["kb1"],
            ),
        ],
        "edges": [
            {
                "id": "agent:tools->kb",
                "source": "agent",
                "source_output": "tools",
                "target": "kb",
            }
        ],
        "layout": {"agent": {"x": 0, "y": 0}, "kb": {"x": 320, "y": 0}},
    }


def test_a_new_tool_starts_with_its_defaults_and_is_attached_and_placed() -> None:
    edited = apply_changes(
        agent(),
        [
            {
                "action": "add_node",
                "kind": "adk_workflow",
                "id": "refund",
                "name": "Refund an order",
                "settings": {"workflow": "ag_refunds01"},
                "attach_to": {"agent": "agent"},
            }
        ],
    )

    added = edited.document["nodes"][-1]
    assert added["config"] == {
        "workflow": "ag_refunds01",
        "version": None,
        "wait_seconds": 120,
    }
    assert added["outputs"] == []
    assert edited.added == ["refund"]
    # A workflow is only ever called, so the way is worked out.
    assert edited.document["edges"][-1] == {
        "id": "agent:tools->refund",
        "source": "agent",
        "source_output": "tools",
        "target": "refund",
    }
    # Right of the agent, clear of the knowledge base already there.
    assert edited.document["layout"]["refund"] == {"x": 320, "y": 160}
    assert edited.done == [
        "Added Refund an order (refund); Connected: Support (agent) uses as a tool "
        "Refund an order (refund)"
    ]


def test_a_sub_agent_must_be_said_to_be_called_or_handed_off_to() -> None:
    change = {
        "action": "add_node",
        "kind": "sub_agent",
        "id": "billing",
        "name": "Billing specialist",
        "attach_to": {"agent": "agent"},
    }
    with pytest.raises(EditRefused, match="way tools .* or agents"):
        apply_changes(agent(), [change])

    edited = apply_changes(
        agent(), [{**change, "attach_to": {"agent": "agent", "way": "agents"}}]
    )
    assert edited.document["edges"][-1]["source_output"] == "agents"
    assert edited.document["nodes"][-1]["outputs"] == ["tools", "agents"]


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (
            {"action": "connect", "source": "agent", "way": "agents", "target": "kb"},
            "isn't an agent, so nothing hands off to it",
        ),
        (
            {"action": "connect", "source": "kb", "target": "agent"},
            "nothing calls or hands off to the chat agent",
        ),
        (
            {"action": "connect", "source": "kb", "target": "kb"},
            "a node can't use itself",
        ),
        ({"action": "remove_node", "id": "agent"}, "the chat agent can't be removed"),
        (
            {"action": "add_node", "kind": "agent", "name": "Another"},
            "add a sub_agent for another agent",
        ),
        (
            {"action": "update_node", "id": "kb", "settings": {"top_k": 3}},
            "has no setting 'top_k'",
        ),
        (
            {"action": "add_node", "kind": "sub_agent", "name": "support"},
            "is called that already",
        ),
        (
            {"action": "add_node", "kind": "http_tool", "name": "User"},
            "ADK keeps 'user' for the person",
        ),
        (
            {"action": "update_node", "id": "kb", "settings": {"max_results": 50}},
            "doesn't fit Forge's format: node kb settings",
        ),
        ({"action": "update_node", "id": "nope", "name": "X"}, "no node 'nope'"),
    ],
)
def test_a_change_that_cant_be_made_refuses_the_batch(
    change: dict[str, Any], problem: str
) -> None:
    with pytest.raises(EditRefused, match=problem):
        apply_changes(agent(), [{"action": "update_agent", "name": "Fine"}, change])


def test_only_one_agent_hands_off_to_a_sub_agent() -> None:
    doc = apply_changes(
        agent(),
        [
            {
                "action": "add_node",
                "kind": "sub_agent",
                "id": "triage",
                "name": "Triage",
                "attach_to": {"agent": "agent", "way": "agents"},
            },
            {
                "action": "add_node",
                "kind": "sub_agent",
                "id": "billing",
                "name": "Billing",
                "attach_to": {"agent": "agent", "way": "agents"},
            },
        ],
    ).document

    with pytest.raises(EditRefused, match="ADK lets only one agent hand off"):
        apply_changes(
            doc,
            [
                {
                    "action": "connect",
                    "source": "triage",
                    "way": "agents",
                    "target": "billing",
                }
            ],
        )
    # Calling it as a tool is fine.
    edited = apply_changes(
        doc,
        [
            {
                "action": "connect",
                "source": "triage",
                "way": "tools",
                "target": "billing",
            }
        ],
    )
    assert edited.done == [
        "Connected: Triage (triage) uses as a tool Billing (billing)"
    ]


def test_removing_a_node_takes_its_connections_and_place() -> None:
    edited = apply_changes(agent(), [{"action": "remove_node", "id": "kb"}])

    assert [n["id"] for n in edited.document["nodes"]] == ["agent"]
    assert edited.document["edges"] == []
    assert "kb" not in edited.document["layout"]
    assert edited.done == [
        "Removed Search the help centre (kb) and its 1 connection(s)"
    ]


def test_problems_name_the_node_and_what_it_needs() -> None:
    doc = agent()
    doc["nodes"][0]["config"]["instruction"] = ""
    doc["nodes"].append(node("flow", "adk_workflow", "Refund", workflow="ag_gone0001"))
    doc["nodes"].append(node("helper", "sub_agent", "Helper", instruction="Help."))
    doc["edges"].append(
        {"id": "e2", "source": "agent", "source_output": "tools", "target": "flow"}
    )
    blocks = {
        "workflows": [{"id": "ag_refunds01", "published_version": 1}],
        "knowledge_bases": [
            {"id": "kb1", "name": "Help centre", "searchable_documents": 0}
        ],
    }

    found = problems_of(doc, blocks, agent_id=AGENT)

    assert found.problems == [
        "Support (agent): it has no instruction: tell it what to do.",
        "Refund (flow): the organization has no such workflow any more: pick another.",
    ]
    assert found.advice == [
        "Search the help centre (kb): Help centre has no searchable documents yet: "
        "upload some on the Knowledge bases page.",
        "Helper (helper): it has no description: the agent that uses it decides when "
        "to by it.",
        "Helper (helper) isn't connected to any agent, so nothing uses it: connect it, "
        "or remove it.",
    ]


def test_the_runtimes_own_checks_are_problems_too() -> None:
    doc = agent()
    doc["nodes"].append(node("a", "sub_agent", "A", instruction="x", description="x"))
    doc["nodes"].append(node("b", "sub_agent", "B", instruction="x", description="x"))
    doc["edges"] += [
        {"id": "1", "source": "agent", "source_output": "agents", "target": "a"},
        {"id": "2", "source": "a", "source_output": "tools", "target": "b"},
        {"id": "3", "source": "b", "source_output": "tools", "target": "a"},
    ]

    found = problems_of(doc)

    assert any("in a circle" in problem for problem in found.problems)


def test_a_team_component_it_cant_use_is_a_problem() -> None:
    doc = agent()
    doc["nodes"].append(
        node(
            "rich",
            "rich_answers",
            "Rich answers",
            components=["Card", "org.shipment-summary", "org.gone"],
        )
    )
    doc["edges"].append(
        {"id": "e2", "source": "agent", "source_output": "tools", "target": "rich"}
    )
    blocks = {"ui_components": [{"key": "org.shipment-summary", "name": "Shipment"}]}

    found = problems_of(doc, blocks, agent_id=AGENT)

    assert found.problems == [
        "Rich answers (rich): org.gone isn't one of the organization's team "
        "components it can use (published and enabled): pick another."
    ]
    # Unread, none is said to be gone.
    unread = {"ui_components": {"unavailable": "403"}}
    assert problems_of(doc, unread, agent_id=AGENT).problems == []


def test_the_building_blocks_list_the_team_components_agents_may_pick() -> None:
    catalog = {
        "team": [
            {
                "id": "uic_x",
                "key": "org.shipment-summary",
                "display_name": "Shipment summary",
                "description": "Show an order's shipping status.",
                "input_schema": {
                    "type": "object",
                    "properties": {"status": {}, "order_number": {}},
                },
                "capabilities": {"has_choices": False, "has_actions": True},
                "version": 2,
            }
        ]
    }
    asked: list[str] = []

    async def call(method: str, path: str) -> Any:
        asked.append(path)
        if path.endswith("/ui-components/catalog"):
            return catalog
        return {} if path.endswith("/models") else []

    blocks = asyncio.run(building_blocks(call, ORG, ui_components=True))
    assert blocks["ui_components"] == [
        {
            "key": "org.shipment-summary",
            "name": "Shipment summary",
            "description": "Show an order's shipping status.",
            "inputs": ["order_number", "status"],
            "has_choices": False,
            "has_actions": True,
            "note": "Pick it by key in a Rich answers node's components; the model "
            "fills its inputs and the layout is the component's.",
        }
    ]
    # Only the agent builder asks.
    asked.clear()
    assert "ui_components" not in asyncio.run(building_blocks(call, ORG))
    assert not any("ui-components" in path for path in asked)
