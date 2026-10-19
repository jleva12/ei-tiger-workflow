"""The ``organization`` toolset: what the organization a helper runs for has,
read-only.

Its kinds of tool (the Agents canvas's node catalog), its MCP servers and
their tools as last listed, its knowledge bases (and a search of one), its
workflows and what they take, its agents and their tools, its API
credentials (names and where they go, never secrets) and its skills. Every
read is of that organization's own records, so a helper writes about real
tools and resources.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from forge_agent_runtime import SkillBundle
from forge_agent_runtime.catalog import CHAT_NODE_CATALOG
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from sqlalchemy import select
from sqlalchemy.orm import undefer

from forge_admin.helpers.kit import HelperRun, HelperToolset
from forge_admin.knowledge.tools import OrganizationKnowledgeBases
from forge_admin.models import ApiCredential, KnowledgeBase, McpServer, Skill

logger = logging.getLogger(__name__)

#: How much of a description or a passage a listing shows.
CLIP = 400
#: The most records a listing shows.
MOST = 50


def clip(text: object, most: int = CLIP) -> str:
    said = " ".join(str(text or "").split())
    return said if len(said) <= most else said[: most - 1] + "…"


def _fields(schema: object) -> list[str]:
    """A JSON Schema's top-level fields: ``name (type, required)``."""
    if not isinstance(schema, Mapping):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return []
    required = set(schema.get("required") or [])
    return [
        f"{name} ({(spec or {}).get('type', 'any') if isinstance(spec, Mapping) else 'any'}"
        f"{', required' if name in required else ''})"
        for name, spec in list(properties.items())[:20]
    ]


async def _records(store: Any, organization_id: str) -> list[Mapping[str, Any]]:
    if store is None:
        return []
    try:
        return list(await store.list(organization_id))
    except Exception:
        logger.warning("A helper couldn't list a store", exc_info=True)
        return []


def organization_tools(run: HelperRun) -> list[BaseTool]:
    """The toolset's tools, reading ``run``'s organization."""
    org = run.organization_id
    state = run.state
    sessions = run.sessions
    knowledge = OrganizationKnowledgeBases(
        sessions,
        getattr(state, "knowledge_search", None),
        getattr(state, "codegraph", None),
    )

    async def list_tool_kinds() -> dict[str, Any]:
        """Lists the kinds of tool a Forge agent can be given (on the Agents
        canvas and in workflows' LLM agents), each with what it does."""
        return {
            "tool_kinds": [
                {"kind": kind.kind, "label": kind.label, "does": kind.summary}
                for kind in CHAT_NODE_CATALOG.kinds
                if kind.group == "tools" or kind.kind == "saved_agent"
            ]
        }

    async def list_mcp_servers() -> dict[str, Any]:
        """Lists the organization's MCP servers, each with its tools (their
        exact names and what they do), as last checked."""
        async with sessions() as session:
            rows = await session.scalars(
                select(McpServer)
                .where(McpServer.organization_id == org)
                .order_by(McpServer.name)
                .limit(MOST)
            )
            servers = [
                {
                    "name": row.name,
                    "description": clip(row.description),
                    "tools": [
                        {
                            "name": str(tool.get("name")),
                            "does": clip(tool.get("description"), 200),
                        }
                        for tool in (row.tools or [])[:60]
                        if isinstance(tool, Mapping)
                    ],
                }
                for row in rows
            ]
        return {"mcp_servers": servers}

    async def list_knowledge_bases() -> dict[str, Any]:
        """Lists the organization's knowledge bases: named sets of documents
        (or, for a system design one, code and how a system fits together)
        that agents search."""
        async with sessions() as session:
            rows = await session.scalars(
                select(KnowledgeBase)
                .where(KnowledgeBase.organization_id == org)
                .order_by(KnowledgeBase.name)
                .limit(MOST)
            )
            bases = [
                {
                    "name": row.name,
                    "description": clip(row.description),
                    "kind": row.kind or "rag",
                }
                for row in rows
            ]
        return {"knowledge_bases": bases}

    async def search_knowledge_base(knowledge_base: str, query: str) -> dict[str, Any]:
        """Searches one of the organization's knowledge bases, by its name, for
        the passages that best match the query: to follow the organization's
        own documents.

        Args:
          knowledge_base: The knowledge base's name, as list_knowledge_bases has it.
          query: What to look for, in a few words.
        """
        async with sessions() as session:
            row = await session.scalar(
                select(KnowledgeBase).where(
                    KnowledgeBase.organization_id == org,
                    KnowledgeBase.name == knowledge_base,
                )
            )
        if row is None:
            return {"error": f"There's no knowledge base called {knowledge_base!r}."}
        try:
            passages = await knowledge.search(
                [row.id], query, organization_id=org, limit=5
            )
        except (LookupError, RuntimeError) as error:
            return {"error": str(error)}
        return {
            "passages": [
                {
                    "document": p.get("document"),
                    "where": p.get("location") or p.get("section"),
                    "text": clip(p.get("text"), 1_200),
                }
                for p in passages
            ]
        }

    async def list_workflows() -> dict[str, Any]:
        """Lists the organization's workflows, each with what it does and the
        input its start takes: an agent can run one as a tool."""
        found = []
        workflows = getattr(state, "organization_agents", None)
        for record in (await _records(workflows, org))[:MOST]:
            document = record.get("document") or {}
            start = next(
                (n for n in document.get("nodes") or [] if n.get("kind") == "start"),
                {},
            )
            found.append(
                {
                    "name": document.get("name"),
                    "description": clip(document.get("description")),
                    "input": _fields((start.get("config") or {}).get("input_schema")),
                }
            )
        return {"workflows": found}

    async def list_agents() -> dict[str, Any]:
        """Lists the organization's agents (from the Agents page), each with
        what it's for and the tools it has."""
        found = []
        for record in (await _records(getattr(state, "chat_agents", None), org))[:MOST]:
            document = record.get("document") or {}
            nodes = [n for n in document.get("nodes") or [] if isinstance(n, Mapping)]
            found.append(
                {
                    "name": document.get("name"),
                    "description": clip(document.get("description")),
                    "tools": [
                        f"{n.get('name')} ({n.get('kind')})"
                        for n in nodes
                        if n.get("kind") not in ("agent", "sub_agent")
                    ][:20],
                }
            )
        return {"agents": found}

    async def list_credentials() -> dict[str, Any]:
        """Lists the organization's API credentials: how its HTTP and OpenAPI
        tools sign in to APIs, and the addresses each may be sent to. Never
        their secrets."""
        async with sessions() as session:
            rows = await session.scalars(
                select(ApiCredential)
                .where(ApiCredential.organization_id == org)
                .order_by(ApiCredential.name)
                .limit(MOST)
            )
            found = [
                {
                    "name": row.name,
                    "description": clip(row.description),
                    "sent_to": list(row.urls or []),
                }
                for row in rows
            ]
        return {"credentials": found}

    async def list_skills() -> dict[str, Any]:
        """Lists the skills the organization already has, by name and description."""
        async with sessions() as session:
            rows = await session.execute(
                select(Skill.name, Skill.description)
                .where(Skill.organization_id == org)
                .order_by(Skill.name)
                .limit(200)
            )
            return {
                "skills": [
                    {"name": name, "description": clip(description)}
                    for name, description in rows
                ]
            }

    async def read_skill(name: str) -> dict[str, Any]:
        """Reads one of the organization's skills whole: its instructions and files.

        Args:
          name: The skill's name, as list_skills has it.
        """
        async with sessions() as session:
            row = await session.scalar(
                select(Skill)
                .options(undefer(Skill.bundle))
                .where(Skill.organization_id == org, Skill.name == name)
            )
        if row is None:
            return {"error": f"There's no skill called {name!r}."}
        skill = SkillBundle.model_validate(row.bundle)
        return {
            "name": skill.name,
            "description": skill.description,
            "instructions": skill.instructions,
            "files": [
                {
                    "path": f.path,
                    "content": f.content[:8_000]
                    if f.encoding == "utf-8"
                    else "(binary)",
                }
                for f in skill.files[:30]
            ],
        }

    return [
        FunctionTool(tool)
        for tool in (
            list_tool_kinds,
            list_mcp_servers,
            list_knowledge_bases,
            search_knowledge_base,
            list_workflows,
            list_agents,
            list_credentials,
            list_skills,
            read_skill,
        )
    ]


TOOLSET = HelperToolset(
    name="organization",
    description="Reads what the organization has: its tool kinds, MCP servers, "
    "knowledge bases (and searches them), workflows, agents, credentials and skills.",
    tools=frozenset(
        {
            "list_tool_kinds",
            "list_mcp_servers",
            "list_knowledge_bases",
            "search_knowledge_base",
            "list_workflows",
            "list_agents",
            "list_credentials",
            "list_skills",
            "read_skill",
        }
    ),
    build=organization_tools,
)
