"""The Agents canvas's node catalog: what the runtime's kinds, defaults and tool kinds are read from."""

import json
from importlib import resources
from typing import Any

import pytest
from pydantic import ValidationError

from forge_agent_runtime import TOOL_KINDS
from forge_agent_runtime.catalog import CHAT_NODE_CATALOG, ChatNodeCatalog
from forge_agent_runtime.document import (
    AGENT_LIKE,
    DEFAULTS,
    HANDED_TO,
    HANDS_OFF,
    KINDS,
    TOOLS,
    ChatAgentDocument,
)


def raw() -> dict[str, Any]:
    return json.loads(resources.files("forge_agent_runtime").joinpath("node_kinds.json").read_text("utf-8"))


def test_the_document_reads_its_kinds_from_the_catalog() -> None:
    assert {
        "agent",
        "sub_agent",
        "saved_agent",
        "memory",
        "knowledge_base",
        "http_tool",
        "openapi",
        "mcp",
        "adk_workflow",
        "code_execution",
    } == KINDS
    assert {"agent", "sub_agent"} == AGENT_LIKE
    assert {"sub_agent", "saved_agent"} == HANDED_TO
    assert set(DEFAULTS) == KINDS
    assert DEFAULTS["memory"] == {"mode": "on_demand"}


def test_an_agents_ways_out_are_its_tools_and_who_it_hands_off_to() -> None:
    for kind in CHAT_NODE_CATALOG.kinds:
        ways = [output.id for output in kind.outputs]
        assert ways == ([TOOLS, HANDS_OFF] if kind.kind in AGENT_LIKE else []), kind.kind


def test_what_a_tool_may_be_outside_a_canvas() -> None:
    # Any kind but an agent: an agent is only ever on a canvas.
    assert KINDS - {"agent", "sub_agent"} == TOOL_KINDS
    # A workflow's LLM agent has no memory of past conversations to recall.
    workflow = {kind.kind for kind in CHAT_NODE_CATALOG.kinds if kind.workflow_tool}
    assert workflow == TOOL_KINDS - {"memory"}


def test_a_kind_it_doesnt_have_is_refused() -> None:
    node = {"id": "x", "kind": "teleport", "name": "X", "config": {}}
    with pytest.raises(ValidationError, match="isn't a kind of node Forge knows"):
        ChatAgentDocument.model_validate({"id": "ca_x", "name": "X", "nodes": [node], "edges": []})


def test_a_catalog_without_an_agent_or_with_a_stray_group_is_refused() -> None:
    data = raw()
    data["kinds"] = [kind for kind in data["kinds"] if kind["kind"] != "agent"]
    with pytest.raises(ValidationError, match="it has no agent"):
        ChatNodeCatalog.model_validate(data)
    data = raw()
    data["kinds"][1]["group"] = "elsewhere"
    with pytest.raises(ValidationError, match="isn't one of the groups"):
        ChatNodeCatalog.model_validate(data)
    data = raw()
    data["kinds"].append(data["kinds"][-1])
    with pytest.raises(ValidationError, match="a kind is listed twice"):
        ChatNodeCatalog.model_validate(data)
