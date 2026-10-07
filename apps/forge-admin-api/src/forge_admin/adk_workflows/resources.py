"""
What an ADK workflow's LLM agents use from their organization, found as a
run starts (``forge_task_adk_workflows.graph.uses``) so that it's checked at
once and the run carries what the worker needs:

- agents from the Agents page (an LLM node that is one, or calls one as a
  tool): their documents at the version named, with the saved agents they
  use bundled in, as the hosted runtime would run them;
- knowledge bases: their names and descriptions, which their search tools
  describe themselves by, and their kinds; a graph knowledge base's
  repositories with a code graph, which the worker searches;
- MCP servers: that they're the organization's (the worker connects to them
  itself, with their credentials, as they're kept then).
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.chat_agents.bundle import bundle
from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.knowledge.graph import searchable
from forge_admin.models import KnowledgeBase, McpServer
from forge_admin.models.knowledge import GRAPH, RAG


class RunResources:
    """
    The organization's agents, knowledge bases and MCP servers, for a run of
    one of its ADK workflows.

    :param organization_id: The workflow's organization: what's used must be its.
    :param chat_agents: Its agents from the Agents page; None when they
        aren't set up (no MongoDB).
    :param sessions: The admin database.
    """

    def __init__(
        self,
        organization_id: str,
        *,
        chat_agents: ChatAgentStore | None,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self.organization_id = organization_id
        self.chat_agents = chat_agents
        self.sessions = sessions

    async def chat_agent(
        self, agent_id: str, version: int | str | None
    ) -> dict[str, Any]:
        """
        :param version: A published version, ``"draft"``, or None for the latest published.
        :return: Its document at that version (``version`` set), its saved agents bundled.
        :raises LookupError: Why it can't be had: no such agent here, or no such version.
        :raises PyMongoError: The store isn't answering.
        """
        if self.chat_agents is None:
            raise LookupError(
                "agents from the Agents page aren't set up (FORGE_ADMIN_MONGO_URI)"
            )
        store = self.chat_agents
        record = await store.get(self.organization_id, agent_id)
        if record is None:
            raise LookupError(f"the organization has no agent {agent_id}")
        name = record["document"].get("name") or agent_id
        if version == "draft":
            if not record.get("has_draft"):
                raise LookupError(f"{name} has no draft: it's published as it is")
            document = {**record["document"], "version": "draft"}
        else:
            number = (
                version if isinstance(version, int) else record.get("published_version")
            )
            if number is None:
                raise LookupError(
                    f"{name} isn't published yet: publish it, or use its draft"
                )
            found = await store.version(self.organization_id, agent_id, int(number))
            if found is None:
                raise LookupError(f"{name} has no version {number}")
            document = found["document"]
        bundled, _notes = await bundle(store, self.organization_id, document)
        return bundled

    async def knowledge_bases(self, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """
        :return: The organization's knowledge bases of those, by ID: name,
            description and kind (``rag`` or ``graph``); a graph one's
            repositories with a code graph too, ``{id, graph_id, name}``.
        """
        async with self.sessions() as session:
            found = list(
                await session.scalars(
                    select(KnowledgeBase).where(
                        KnowledgeBase.id.in_(list(ids)),
                        KnowledgeBase.organization_id == self.organization_id,
                    )
                )
            )
            repositories = await searchable(
                session, [kb.id for kb in found if kb.kind == GRAPH]
            )
        snapshot: dict[str, dict[str, Any]] = {}
        for kb in found:
            snapshot[kb.id] = {
                "name": kb.name,
                "description": kb.description,
                "kind": kb.kind or RAG,
            }
            if kb.kind == GRAPH:
                snapshot[kb.id]["repositories"] = [
                    {"id": r.id, "graph_id": r.graph_id, "name": r.name}
                    for r in repositories.get(kb.id, [])
                ]
        return snapshot

    async def mcp_servers(self, ids: Sequence[str]) -> set[str]:
        """:return: Those of the IDs that are the organization's MCP servers."""
        async with self.sessions() as session:
            found = await session.scalars(
                select(McpServer.id).where(
                    McpServer.id.in_(list(ids)),
                    McpServer.organization_id == self.organization_id,
                )
            )
            return set(found)
