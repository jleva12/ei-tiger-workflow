import copy
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from forge_agent_runtime.build import BuildError, build_app
from forge_agent_runtime.document import parse_document
from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.tools import GuardedTools, HttpTool, KnowledgeBaseTool, WorkflowTool
from tests.conftest import without


async def build(doc: dict[str, Any], models, web, **services: Any):
    return await build_app(
        parse_document(doc), models=models, services=RuntimeServices(**services), http=web()
    )


async def test_the_example_builds_as_the_canvas_draws_it(example, models, web):
    app = await build(example, models, web)
    root = app.root_agent
    assert isinstance(root, LlmAgent)
    assert (app.name, root.name) == ("ca_1k2cuqsuev", "support_assistant")
    assert root.model is models
    memory, orders, help_center = root.tools
    assert isinstance(memory, PreloadMemoryTool)  # "With every message"
    assert isinstance(orders, GuardedTools) and isinstance(orders.items[0], HttpTool)
    assert orders.items[0].headers == {"Accept": "application/json"}
    assert isinstance(help_center, GuardedTools)

    [billing] = root.sub_agents
    assert isinstance(billing, LlmAgent)
    assert (billing.name, billing.mode, billing.include_contents) == ("billing_specialist", "chat", "default")
    billing_api: Any = billing.tools[0]
    assert len(billing.tools) == 1
    openapi = billing_api.items[0]
    names = sorted(tool.name for tool in await openapi.get_tools())
    assert names == ["get_invoice", "list_charges", "preview_refund"]


async def test_a_sub_agent_on_the_tools_way_is_called_like_a_tool(example, models, web):
    doc = copy.deepcopy(example)
    doc["edges"] = [e for e in doc["edges"] if e["target"] != "billing"]
    doc["edges"].append({"id": "t", "source": "agent", "source_output": "tools", "target": "billing"})
    root = (await build(doc, models, web)).root_agent
    assert root.sub_agents == []
    assert isinstance(root.tools[-1], AgentTool) and root.tools[-1].agent.name == "billing_specialist"


async def test_secrets_are_filled_in_from_the_environment(example, models, web):
    doc = copy.deepcopy(example)
    orders = next(n for n in doc["nodes"] if n["id"] == "orders")
    orders["config"]["headers"].append(
        {"id": "h2", "name": "Authorization", "value": "Bearer ${ORDERS_TOKEN}"}
    )
    app = await build(doc, models, web, environment={"ORDERS_TOKEN": "s3cret"})
    assert app.root_agent.tools[1].items[0].headers["Authorization"] == "Bearer s3cret"
    with pytest.raises(BuildError, match="ORDERS_TOKEN"):
        await build(doc, models, web, environment={})


def saved_node(agent_id: str) -> dict[str, Any]:
    return {
        "id": "saved",
        "kind": "saved_agent",
        "name": "Refunds",
        "config": {"agent": agent_id},
        "outputs": [],
    }


async def test_saved_agents_come_from_the_dependencies_or_the_resolver(example, models, web):
    refunds = without(copy.deepcopy(example), "memory", "orders", "help", "billing", "billing_api")
    refunds.update(id="ca_refunds", name="Refunds")
    refunds["nodes"][0]["name"] = "Refunds desk"

    doc = without(example, "help")
    doc["nodes"].append(saved_node("ca_refunds"))
    doc["edges"].append({"id": "s", "source": "agent", "source_output": "agents", "target": "saved"})

    with pytest.raises(BuildError, match="isn't in this agent's dependencies"):
        await build(doc, models, web)

    bundled = {**doc, "dependencies": {"ca_refunds@2": refunds}}
    root = (await build(bundled, models, web)).root_agent
    assert [a.name for a in root.sub_agents] == ["billing_specialist", "refunds_desk"]

    async def resolve(agent_id: str):
        if agent_id != "ca_refunds":
            raise LookupError("no such agent")
        return parse_document(refunds)

    root = (await build(doc, models, web, resolve_agent=resolve)).root_agent
    assert root.sub_agents[-1].name == "refunds_desk"

    loop = copy.deepcopy(refunds)
    loop["nodes"].append(saved_node("ca_1k2cuqsuev"))
    loop["edges"].append({"id": "s", "source": "agent", "source_output": "tools", "target": "saved"})

    async def resolve_loop(agent_id: str):
        return parse_document(loop if agent_id == "ca_refunds" else doc)

    with pytest.raises(BuildError, match="run each other forever"):
        await build(doc, models, web, resolve_agent=resolve_loop)


async def test_workflows_and_organization_mcp_servers_need_the_hosted_runtime(example, models, web):
    doc = copy.deepcopy(example)
    doc["nodes"].append(
        {
            "id": "wf",
            "kind": "adk_workflow",
            "name": "Triage",
            "config": {"workflow": "ag_triage"},
            "outputs": [],
        }
    )
    doc["edges"].append({"id": "w", "source": "agent", "source_output": "tools", "target": "wf"})
    with pytest.raises(BuildError, match="only the hosted runtime"):
        await build(doc, models, web)

    class Workflows:
        async def describe(self, workflow_id, *, organization_id):
            return (
                "Triage",
                "Sorts a ticket.",
                {"type": "object", "properties": {"ticket": {"type": "string"}}},
            )

        async def run(self, workflow_id, workflow_input, *, organization_id, context):
            return {"status": "succeeded", "output": workflow_input}

    root = (await build(doc, models, web, workflows=Workflows())).root_agent
    workflow: Any = root.tools[-1]
    tool = workflow.items[0]
    assert isinstance(tool, WorkflowTool)
    declaration: Any = tool._get_declaration()
    assert declaration.parameters_json_schema["properties"] == {"ticket": {"type": "string"}}

    help_center = next(n for n in doc["nodes"] if n["id"] == "help")
    help_center["config"]["server"] = "mcp_1"
    with pytest.raises(BuildError, match="organization's MCP servers"):
        await build(doc, models, web, workflows=Workflows())


class KnowledgeBases:
    """Stands in for the hosted runtime's knowledge bases: two of them, and
    what each search was asked."""

    def __init__(self) -> None:
        self.known = {"kb_hr": ("HR policies", "Leave, benefits and expenses."), "kb_it": ("IT runbooks", "")}
        self.searches: list[tuple[list[str], str, str | None, int]] = []

    async def describe(self, knowledge_base_ids, *, organization_id):
        missing = [i for i in knowledge_base_ids if i not in self.known]
        if missing:
            raise LookupError(f"The organization has no knowledge base {missing[0]}")
        return [self.known[i] for i in knowledge_base_ids]

    async def search(self, knowledge_base_ids, query, *, organization_id, limit):
        self.searches.append((list(knowledge_base_ids), query, organization_id, limit))
        return [{"document": "Leave policy.pdf", "section": "Annual leave", "text": "25 days.", "score": 0.9}]


def knowledge_node(**config: Any) -> dict[str, Any]:
    return {
        "id": "kb",
        "kind": "knowledge_base",
        "name": "Policies",
        "config": {"knowledge_bases": ["kb_hr"], "description": "", "max_results": 5, **config},
        "outputs": [],
    }


async def test_a_knowledge_base_tool_searches_the_organizations_knowledge_bases(example, models, web):
    doc = copy.deepcopy(example)
    doc["nodes"].append(knowledge_node(knowledge_bases=["kb_hr", "kb_it"], max_results=3))
    doc["edges"].append({"id": "k", "source": "agent", "source_output": "tools", "target": "kb"})
    with pytest.raises(BuildError, match="only the hosted runtime"):
        await build(doc, models, web)

    knowledge = KnowledgeBases()
    root = (await build(doc, models, web, knowledge_bases=knowledge)).root_agent
    guarded: Any = root.tools[-1]
    tool = guarded.items[0]
    assert isinstance(tool, KnowledgeBaseTool)
    assert tool.name == "policies"
    # Without a description of its own, it says what it searches.
    assert "HR policies (Leave, benefits and expenses.); IT runbooks" in tool.description
    declaration: Any = tool._get_declaration()
    assert declaration.parameters_json_schema["required"] == ["query"]

    answer = await tool.run_async(args={"query": "How many days of leave?"}, tool_context=None)  # type: ignore[arg-type]
    assert answer["passages"][0]["text"] == "25 days."
    assert knowledge.searches == [
        (["kb_hr", "kb_it"], "How many days of leave?", doc.get("organization_id"), 3)
    ]


async def test_a_knowledge_base_tool_names_a_knowledge_base_the_organization_has(example, models, web):
    doc = copy.deepcopy(example)
    doc["nodes"].append(knowledge_node(knowledge_bases=[], description="Company policies."))
    doc["edges"].append({"id": "k", "source": "agent", "source_output": "tools", "target": "kb"})
    with pytest.raises(BuildError, match="doesn't say which knowledge base"):
        await build(doc, models, web, knowledge_bases=KnowledgeBases())

    doc["nodes"][-1]["config"]["knowledge_bases"] = ["kb_gone"]
    with pytest.raises(BuildError, match="no knowledge base kb_gone"):
        await build(doc, models, web, knowledge_bases=KnowledgeBases())

    doc["nodes"][-1]["config"]["knowledge_bases"] = ["kb_hr"]
    root = (await build(doc, models, web, knowledge_bases=KnowledgeBases())).root_agent
    assert root.tools[-1].items[0].description == "Company policies."
