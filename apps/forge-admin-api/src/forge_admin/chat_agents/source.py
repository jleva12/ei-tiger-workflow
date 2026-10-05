"""The hosted runtime: organizations' chat agents run by ID.

``forge_agent_runtime`` builds and runs them; this module tells it where they
are and what they may use here:

- :class:`MongoAgentSource`: an agent by ID from the store: ``ca_x`` its latest
  published version, ``ca_x@3`` that one, ``ca_x@draft`` its draft (built
  again whenever the draft is saved).
- :class:`OrganizationMcpServers`: the MCP servers an agent names by ID
  (``forge_admin.mcp_servers``), with their credentials.
- :class:`OrganizationWorkflows`: the workflows an agent calls as tools,
  started on the async worker and waited for.
"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from forge_agent_runtime import (
    AgentExecutor,
    AgentNotFound,
    AgentRef,
    BuildError,
    DocumentError,
    ResolvedAgent,
    RuntimeServices,
    parse_document,
)
from forge_common.adk.models import ProviderModels
from forge_common.adk.usage import price_from
from forge_common.model_provider import load_model_provider_config
from forge_task_adk_workflows.run_store import OPEN, RunStore
from forge_task_adk_workflows.usage_store import UsageStore
from google.adk.artifacts import InMemoryArtifactService
from google.adk.sessions import DatabaseSessionService
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.tool_context import ToolContext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from forge_admin import env_files
from forge_admin.adk_workflows.documents import AgentStore
from forge_admin.adk_workflows.queue import Embedding
from forge_admin.adk_workflows.runs import (
    AdkRunError,
    prepare_run,
    start_adk_run,
    start_schema,
)
from forge_admin.assistant.language_models import gemini_config
from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.config import Settings
from forge_admin.knowledge.search import KnowledgeSearch
from forge_admin.knowledge.tools import OrganizationKnowledgeBases
from forge_admin.mcp_servers.service import McpServers
from forge_admin.mcp_servers.toolset import mcp_toolset
from forge_admin.overview.recording import AgentUsage

logger = logging.getLogger(__name__)

#: How long a chat agent waits for a workflow it called, in seconds.
WORKFLOW_WAIT = 120.0
_POLL = 0.5


class MongoAgentSource:
    """Agents by ID from the chat agent store, at the version a reference names."""

    def __init__(self, store: ChatAgentStore) -> None:
        self.store = store

    async def get(self, ref: AgentRef) -> ResolvedAgent:
        record = await self.store.find(ref.agent_id)
        if record is None:
            raise AgentNotFound(f"There's no agent {ref.agent_id}.")
        if ref.version == "draft":
            if not record.get("has_draft"):
                raise AgentNotFound(
                    f"{ref.agent_id} has no draft: it's published as it is."
                )
            raw = {**record["document"], "version": "draft"}
            key = f"{ref.agent_id}@draft#{record['revision']}"
            version: int | str = "draft"
        else:
            number = (
                ref.version
                if isinstance(ref.version, int)
                else record.get("published_version")
            )
            if number is None:
                raise AgentNotFound(
                    f"{ref.agent_id} hasn't been published yet; "
                    f"run its draft as {ref.agent_id}@draft."
                )
            found = await self.store.find_version(ref.agent_id, int(number))
            if found is None:
                raise AgentNotFound(f"{ref.agent_id} has no version {number}.")
            raw = found["document"]
            key = f"{ref.agent_id}@{number}"
            version = int(number)
        try:
            document = parse_document(raw)
        except DocumentError as error:
            raise BuildError(str(error)) from error
        return ResolvedAgent(document, version, key)  # type: ignore[arg-type]


class OrganizationMcpServers:
    """An agent's MCP tool that names one of its organization's servers."""

    def __init__(
        self, servers: McpServers, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        self.servers = servers
        self.sessions = sessions

    async def __call__(
        self, config: Mapping[str, Any], *, organization_id: str | None
    ) -> BaseToolset:
        if organization_id is None:
            raise LookupError("The agent belongs to no organization.")
        return await mcp_toolset(
            self.servers,
            self.sessions,
            organization_id=organization_id,
            config=dict(config),
        )


class OrganizationWorkflows:
    """
    The workflows a chat agent calls: started as runs on the async worker,
    as the agent, and waited for. A run that pauses (for an approval, a
    person's answer) or takes longer answers with where it got to.
    """

    def __init__(
        self, agents: AgentStore, runs: RunStore, queue: Embedding | None
    ) -> None:
        self.agents = agents
        self.runs = runs
        self.queue = queue

    async def _record(
        self, workflow_id: str, organization_id: str | None
    ) -> dict[str, Any]:
        if organization_id is None:
            raise LookupError("The agent belongs to no organization.")
        record = await self.agents.get(organization_id, workflow_id)
        if record is None:
            raise LookupError(f"The organization has no workflow {workflow_id}.")
        return record

    async def describe(
        self, workflow_id: str, *, organization_id: str | None
    ) -> tuple[str, str, dict[str, Any]]:
        record = await self._record(workflow_id, organization_id)
        document = record["document"]
        return (
            str(document.get("name") or workflow_id),
            str(document.get("description") or ""),
            start_schema(document) or {},
        )

    async def run(
        self,
        workflow_id: str,
        workflow_input: dict[str, Any],
        *,
        organization_id: str | None,
        context: ToolContext,
    ) -> Any:
        if self.queue is None:
            raise RuntimeError(
                "Workflows can't run: the async worker's queue isn't set up."
            )
        record = await self._record(workflow_id, organization_id)

        async def find(other: str) -> dict[str, Any] | None:
            return await self.agents.get(record["organization_id"], other)

        try:
            saved = await prepare_run(record, workflow_input, find)
        except AdkRunError as error:
            raise ValueError(str(error)) from error
        agent = context.agent_name
        run = await start_adk_run(
            self.runs,
            self.queue,
            record,
            saved=saved,
            input=workflow_input,
            run_as=f"chat-agent:{context.session.app_name}",
            run_as_name=f"Chat agent {agent}",
            trigger={
                "type": "chat_agent",
                "agent_id": context.session.app_name,
                "user": context.user_id,
            },
        )
        deadline = asyncio.get_running_loop().time() + WORKFLOW_WAIT
        while True:
            current = await self.runs.get(run["id"])
            status = current["status"] if current else "missing"
            if (
                status not in OPEN
                or status == "paused"
                or asyncio.get_running_loop().time() > deadline
            ):
                break
            await asyncio.sleep(_POLL)
        answer: dict[str, Any] = {"run_id": run["id"], "status": status}
        if current is not None:
            if current.get("result") is not None:
                answer["result"] = current["result"]
            if current.get("error") is not None:
                answer["error"] = current["error"]
        return answer


def chat_agent_models(settings: Settings) -> ProviderModels:
    """:return: Every model the model provider configuration offers (Gemini without one)."""
    if settings.model_provider_config is None:
        return ProviderModels(gemini_config(settings))
    return ProviderModels(
        load_model_provider_config(
            settings.model_provider_config, env_files.environment()
        )
    )


def create_executor(
    settings: Settings,
    engine: AsyncEngine,
    *,
    store: ChatAgentStore,
    sessions: async_sessionmaker[AsyncSession],
    mcp_servers: McpServers | None,
    agents: AgentStore | None,
    runs: RunStore,
    queue: Embedding | None,
    models: ProviderModels | None = None,
    knowledge_search: KnowledgeSearch | None = None,
) -> AgentExecutor:
    """
    The hosted runtime: agents from ``store``, conversations in the admin
    database (ADK's tables, beside the assistant's), what tools may reach as
    the settings say, ``${NAME}`` in tools filled in from the environment.
    """
    services = RuntimeServices(
        environment=env_files.environment(),
        allow_private=settings.agent_tools_allow_private,
        allowed_hosts=tuple(settings.agent_tools_allowed_hosts),
        mcp_servers=OrganizationMcpServers(mcp_servers, sessions)
        if mcp_servers is not None
        else None,
        workflows=OrganizationWorkflows(agents, runs, queue)
        if agents is not None
        else None,
        knowledge_bases=OrganizationKnowledgeBases(sessions, knowledge_search),
    )
    models = models or chat_agent_models(settings)
    return AgentExecutor(
        MongoAgentSource(store),
        models=models,
        sessions=DatabaseSessionService(db_engine=engine),
        artifacts=InMemoryArtifactService(),
        services=services,
        cache_size=settings.agent_runtime_cache,
        # Every turn, and its model and tool calls, for the organization's overview.
        observer=AgentUsage(UsageStore(engine), price=price_from(models)),
    )
