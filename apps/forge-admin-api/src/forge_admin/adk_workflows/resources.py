"""
What an ADK workflow's LLM agents use from their organization, found as a
run starts (``forge_task_adk_workflows.graph.uses``) so that it's checked at
once and the run carries what the worker needs:

- agents from the Agents page (an LLM node that is one, or calls one as a
  tool): their documents at the version named, with the saved agents they
  use bundled in, as the hosted runtime would run them, each node's rule
  as strict as the live version's; and what whoever the run is for holds
  of the permissions their rules name, which the worker checks them
  against (``forge_admin.chat_agents.access``);
- knowledge bases: their names and descriptions, which their search tools
  describe themselves by, and their kinds; a system design knowledge base's
  repositories with a code graph, which the worker searches, and how they
  connect;
- MCP servers: that they're the organization's (the worker connects to them
  itself, with their credentials, as they're kept then);
- skills: each whole, as it is when the run starts.
- team UI components: the chat agents' Rich answers' picks, carried at
  their latest published versions when the run starts.

And that whoever the run is for may run the workflow and the saved workflows
it runs, those limited to some of the organization's agent permissions
(``forge_admin.chat_agents.access``).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from forge_agent_runtime.access import Caller
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.adk_workflows.documents import AgentStore
from forge_admin.chat_agents.access import (
    AgentUser,
    as_strict_as_live,
    caller_of,
    check_agent_use,
)
from forge_admin.chat_agents.bundle import bundle
from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.knowledge.designs import designs_of
from forge_admin.knowledge.graph import searchable
from forge_admin.models import KnowledgeBase, McpServer
from forge_admin.models.knowledge import RAG, SYSTEM
from forge_admin.skills.vault import bundles_of
from forge_admin.ui_components.store import UiComponentStore


class RunResources:
    """
    The organization's agents, knowledge bases and MCP servers, for a run of
    one of its ADK workflows.

    :param organization_id: The workflow's organization: what's used must be its.
    :param chat_agents: Its agents from the Agents page; None when they
        aren't set up (no MongoDB).
    :param sessions: The admin database.
    :param user: Who the run is for, whose use of the agents and workflows
        limited to some of the organization's agent permissions is checked;
        None for what runs nothing (checking a workflow, publishing it).
    :param workflows: Its workflows, whose records say who may run the saved
        ones a run runs; None for what runs nothing.
    :param ui_components: Its team UI components, which its agents carry at
        their latest published versions; None when they aren't set up.
    """

    def __init__(
        self,
        organization_id: str,
        *,
        chat_agents: ChatAgentStore | None,
        sessions: async_sessionmaker[AsyncSession],
        user: AgentUser | None,
        workflows: AgentStore | None,
        ui_components: UiComponentStore | None = None,
    ) -> None:
        self.organization_id = organization_id
        self.chat_agents = chat_agents
        self.sessions = sessions
        self.user = user
        self.workflows = workflows
        self.ui_components = ui_components

    async def check_workflows(
        self, record: Mapping[str, Any], saved: Mapping[str, Mapping[str, Any]]
    ) -> None:
        """
        Check that whoever the run is for may run the workflow, and each
        saved workflow it runs (sub-workflows, workflow tools), as their
        records say now.

        :param record: The workflow's record.
        :param saved: The saved workflows' documents it runs, by reference.
        :raises AgentUseRefused: They may not run one.
        :raises PyMongoError: The store isn't answering.
        """
        if self.user is None:
            return
        records = [record]
        if self.workflows is not None:
            ids = dict.fromkeys(str(doc.get("id") or "") for doc in saved.values())
            for workflow_id in ids:
                found = await self.workflows.get(self.organization_id, workflow_id)
                if found is not None and found["_id"] != record.get("_id"):
                    records.append(found)
        async with self.sessions() as session:
            await check_agent_use(session, self.organization_id, records, self.user)

    async def chat_agent(
        self, agent_id: str, version: int | str | None
    ) -> dict[str, Any]:
        """
        :param version: A published version, ``"draft"``, or None for the latest published.
        :return: Its document at that version (``version`` set), its saved
            agents bundled, its nodes' rules as strict as the live version's.
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
            which: int | str = "draft"
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
            which = int(number)
        document, _ = await as_strict_as_live(
            store, self.organization_id, record, document, which
        )
        bundled, _notes = await bundle(
            store,
            self.organization_id,
            document,
            skills=self.skills,
            ui_components=self.drawn_components if self.ui_components else None,
        )
        return bundled

    async def caller(self, wanted: frozenset[int]) -> Caller | None:
        """
        :param wanted: The agent permissions the run's agents' rules name.
        :return: What whoever the run is for holds of them, which its agents'
            rules are checked against as it runs; None when it's for nobody
            (its agents with rules refuse) or no rule names any.
        """
        if self.user is None or not wanted:
            return None
        async with self.sessions() as session:
            return await caller_of(session, self.organization_id, self.user, wanted)

    async def knowledge_bases(self, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """
        :return: The organization's knowledge bases of those, by ID: name,
            description and kind (``rag`` or ``system``); a system design one's
            repositories with a code graph too, ``{id, graph_id, name}``, how
            its applications and components connect, ``{source, kind,
            target, description}``, and its whole ``design``
            (``forge_admin.knowledge.designs``: its groups, applications,
            components and connections).
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
            systems = [kb.id for kb in found if kb.kind == SYSTEM]
            repositories = await searchable(session, systems)
            designs = await designs_of(session, systems)
        snapshot: dict[str, dict[str, Any]] = {}
        for kb in found:
            snapshot[kb.id] = {
                "name": kb.name,
                "description": kb.description,
                "kind": kb.kind or RAG,
            }
            if kb.kind == SYSTEM:
                snapshot[kb.id]["repositories"] = [
                    {"id": r.id, "graph_id": r.graph_id, "name": r.name}
                    for r in repositories.get(kb.id, [])
                ]
                # Workers from before designs read the connections alone.
                snapshot[kb.id]["connections"] = designs[kb.id]["connections"]
                snapshot[kb.id]["design"] = designs[kb.id]
        return snapshot

    async def drawn_components(self, keys: Sequence[str]) -> dict[str, Any]:
        """:return: Those of the keys its agents may use, each team UI
        component's latest published definition."""
        assert self.ui_components is not None
        return await self.ui_components.find_live(self.organization_id, keys)

    async def skills(self, ids: Sequence[str]) -> dict[str, Any]:
        """:return: Those of the IDs that are the organization's skills, whole, by ID."""
        async with self.sessions() as session:
            return await bundles_of(session, self.organization_id, ids)

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
