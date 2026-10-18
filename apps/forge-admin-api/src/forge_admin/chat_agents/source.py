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
- ``OrganizationSkills`` (``forge_admin.skills.vault``): the skills its
  Skills tools name, from the organization's vault as they are now.
"""

import hashlib
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from forge_agent_runtime import (
    AgentExecutor,
    AgentNotFound,
    AgentRef,
    BuildError,
    DocumentError,
    ResolvedAgent,
    RuntimeServices,
    code_executor,
    parse_document,
)
from forge_codegraph_query import CodeGraph
from forge_common.adk.models import ProviderModels
from forge_common.adk.usage import price_from
from forge_common.model_provider import load_model_provider_config
from forge_mcp_servers.api_credentials import ApiCredentials
from forge_mcp_servers.service import McpServers
from forge_task_adk_workflows.run_store import RunStore
from forge_task_adk_workflows.runs import STATE_KEY
from forge_task_adk_workflows.steps import run_progress
from forge_task_adk_workflows.usage_store import UsageStore
from google.adk.artifacts import InMemoryArtifactService
from google.adk.sessions import BaseSessionService, DatabaseSessionService
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.tool_context import ToolContext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from forge_admin import env_files
from forge_admin.adk_workflows.documents import AgentStore
from forge_admin.adk_workflows.queue import Embedding
from forge_admin.adk_workflows.resources import RunResources
from forge_admin.adk_workflows.runs import (
    APP_NAME,
    AdkRunError,
    prepare_run,
    settle,
    start_adk_run,
    start_schema,
)
from forge_admin.adk_workflows.versions import (
    ResolvedWorkflow,
    WorkflowGone,
    resolve_workflow,
    workflow_finder,
)
from forge_admin.api_credentials import (
    TOOL_ENVIRONMENT_NOTE,
    OrganizationCredentials,
    tool_environment,
)
from forge_admin.assistant.language_models import gemini_config
from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.config import Settings
from forge_admin.db.session import adk_session_engine
from forge_admin.knowledge.search import KnowledgeSearch
from forge_admin.knowledge.tools import OrganizationKnowledgeBases
from forge_admin.mcp_servers.toolset import mcp_toolset
from forge_admin.overview.recording import AgentUsage
from forge_admin.skills.vault import OrganizationSkills, SkillStamps

logger = logging.getLogger(__name__)

#: How long a chat agent waits for a workflow it called, in seconds.
WORKFLOW_WAIT = 120.0

#: What a chat sees of each step of a workflow run it started: not what the
#: step was handed, handed on or called, which only the organization's
#: members see on the run's page.
CHAT_STEP_FIELDS = (
    "id",
    "name",
    "kind",
    "status",
    "started_at",
    "finished_at",
    "handed_to",
)


def chat_run_id(agent_id: str, session_id: str, call_id: str) -> str:
    """
    :return: The ID of the run a chat agent's call of a workflow starts,
        made from the conversation and the call: the chat follows the run by
        its call, and a call answered again finds the run it started.
    """
    made = f"{agent_id}\n{session_id}\n{call_id}".encode()
    return hashlib.sha256(made).hexdigest()[:32]


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


class MongoAgentSource:
    """
    Agents by ID from the chat agent store, at the version a reference names.

    :param skill_stamps: When the skills an agent uses last changed: part of
        its cache key, so a skill changed in the vault is used from the next turn.
    """

    def __init__(
        self, store: ChatAgentStore, skill_stamps: SkillStamps | None = None
    ) -> None:
        self.store = store
        self.skill_stamps = skill_stamps

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
        if self.skill_stamps is not None:
            stamp = await self.skill_stamps(
                record.get("organization_id") or raw.get("organization_id"), raw
            )
            if stamp:
                key = f"{key}#skills:{stamp}"
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
    Meanwhile the chat follows it by its call (``progress``): each call
    starts the run :func:`chat_run_id` names.
    """

    def __init__(
        self,
        agents: AgentStore,
        runs: RunStore,
        queue: Embedding | None,
        resources: Callable[[str], RunResources] | None = None,
        run_sessions: BaseSessionService | None = None,
    ) -> None:
        self.agents = agents
        self.runs = runs
        self.queue = queue
        #: What a workflow's LLM agents use from its organization, by organization.
        self.resources = resources
        #: Where the worker keeps runs' ADK sessions, which their steps are
        #: read from; without it, a run's progress has no steps.
        self.run_sessions = run_sessions

    async def _resolved(
        self, ref: str, organization_id: str | None
    ) -> ResolvedWorkflow:
        """:param ref: ``ag_x`` (its latest published), ``ag_x@3``, ``ag_x@draft``."""
        if organization_id is None:
            raise LookupError("The agent belongs to no organization.")
        try:
            return await resolve_workflow(
                self.agents, ref, organization_id=organization_id
            )
        except WorkflowGone:
            raise LookupError(
                f"The organization has no workflow {ref.partition('@')[0]}."
            ) from None

    async def describe(
        self, workflow_id: str, *, organization_id: str | None
    ) -> tuple[str, str, dict[str, Any]]:
        document = (await self._resolved(workflow_id, organization_id)).document
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
        session = context.session
        call_id = context.function_call_id
        run_id = chat_run_id(session.app_name, session.id, call_id) if call_id else None
        started = (
            await self.runs.get(run_id, organization_id=organization_id)
            if run_id
            else None
        )
        if started is None:
            started = await self._start(
                workflow_id, workflow_input, organization_id, context, run_id
            )
        current = await settle(self.runs, started["id"], wait=WORKFLOW_WAIT)
        status = current["status"] if current else "missing"
        answer: dict[str, Any] = {"run_id": started["id"], "status": status}
        if current is not None:
            if current.get("result") is not None:
                answer["result"] = current["result"]
            if current.get("error") is not None:
                answer["error"] = current["error"]
        return answer

    async def _start(
        self,
        workflow_id: str,
        workflow_input: dict[str, Any],
        organization_id: str | None,
        context: ToolContext,
        run_id: str | None,
    ) -> dict[str, Any]:
        """:return: A new run of the workflow, for the call, queued."""
        assert self.queue is not None
        resolved = await self._resolved(workflow_id, organization_id)
        record = resolved.run_record
        find = workflow_finder(self.agents, record["organization_id"])
        resources = (
            self.resources(record["organization_id"]) if self.resources else None
        )
        try:
            prepared = await prepare_run(record, workflow_input, find, resources)
        except AdkRunError as error:
            raise ValueError(str(error)) from error
        agent = context.agent_name
        return await start_adk_run(
            self.runs,
            self.queue,
            record,
            prepared=prepared,
            input=workflow_input,
            run_as=f"chat-agent:{context.session.app_name}",
            run_as_name=f"Chat agent {agent}",
            trigger={
                "type": "chat_agent",
                "agent_id": context.session.app_name,
                "user": context.user_id,
                # The conversation and call it ran for, which follow it (``progress``).
                "session_id": context.session.id,
                "call_id": context.function_call_id,
            },
            version=resolved.version,
            run_id=run_id,
        )

    async def progress(
        self,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        call_id: str,
        organization_id: str | None,
    ) -> dict[str, Any] | None:
        if organization_id is None:
            return None
        run = await self.runs.get(
            chat_run_id(agent_id, session_id, call_id), organization_id=organization_id
        )
        payload = run.get("payload") if run is not None else None
        if run is None or not isinstance(payload, dict):
            return None
        trigger = payload.get("trigger")
        # Only the person whose conversation started it follows it.
        if not isinstance(trigger, dict) or (
            trigger.get("type"),
            trigger.get("agent_id"),
            trigger.get("user"),
            trigger.get("session_id"),
        ) != ("chat_agent", agent_id, user_id, session_id):
            return None
        state = run.get("state")
        kept = state.get(STATE_KEY) if isinstance(state, dict) else None
        kept = kept if isinstance(kept, dict) else {}
        found = None
        if self.run_sessions is not None:
            found = await self.run_sessions.get_session(
                app_name=APP_NAME,
                user_id=str(payload.get("run_as") or ""),
                session_id=str(kept.get("session_id") or run["session_id"]),
            )
        document = payload.get("document")
        steps = (
            run_progress(
                found,
                document,
                # Before it starts, it has no invocation, and so no events of its own.
                invocation_id=str(kept.get("invocation_id") or ""),
            )["steps"]
            if isinstance(document, dict)
            else []
        )
        error = run.get("error")
        return {
            "run_id": run["id"],
            "workflow": run["agent_name"],
            "status": run["status"],
            "created_at": _iso(run.get("created_at")),
            "started_at": _iso(run.get("started_at")),
            "finished_at": _iso(run.get("finished_at")),
            "waiting_reason": run.get("waiting_reason"),
            "error": {"message": str(error.get("message") or "")}
            if isinstance(error, dict)
            else None,
            # The steps it reached, and its start (where a run that hasn't
            # recorded anything yet is): not the others, ways it didn't take.
            "steps": [
                {key: step[key] for key in CHAT_STEP_FIELDS}
                for step in steps
                if step["status"] != "not_reached"
                or step["handed_to"]
                or step["kind"] == "start"
            ],
        }


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
    codegraph: CodeGraph | None = None,
    api_credentials: ApiCredentials | None = None,
    run_sessions: BaseSessionService | None = None,
) -> AgentExecutor:
    """
    The hosted runtime: agents from ``store``, conversations in the admin
    database (ADK's tables, beside the assistant's), what tools may reach as
    the settings say, their requests signed with the organization's
    credentials, and ``${NAME}`` in tools filled in only from the variables
    the settings list (``agent_tool_environment``).
    """
    services = RuntimeServices(
        environment=tool_environment(
            settings.agent_tool_environment, env_files.environment()
        ),
        environment_note=TOOL_ENVIRONMENT_NOTE,
        allow_private=settings.agent_tools_allow_private,
        allowed_hosts=tuple(settings.agent_tools_allowed_hosts),
        code_executor=code_executor(
            settings.agent_sandbox_mode, settings.agent_openshell
        ),
        mcp_servers=OrganizationMcpServers(mcp_servers, sessions)
        if mcp_servers is not None
        else None,
        credentials=OrganizationCredentials(api_credentials, sessions)
        if api_credentials is not None
        else None,
        workflows=OrganizationWorkflows(
            agents,
            runs,
            queue,
            lambda organization: RunResources(
                organization, chat_agents=store, sessions=sessions
            ),
            run_sessions=run_sessions
            or DatabaseSessionService(db_engine=adk_session_engine(engine)),
        )
        if agents is not None
        else None,
        knowledge_bases=OrganizationKnowledgeBases(
            sessions, knowledge_search, codegraph
        ),
        skills=OrganizationSkills(sessions),
    )
    models = models or chat_agent_models(settings)
    return AgentExecutor(
        MongoAgentSource(store, SkillStamps(sessions)),
        models=models,
        sessions=DatabaseSessionService(db_engine=adk_session_engine(engine)),
        artifacts=InMemoryArtifactService(),
        services=services,
        cache_size=settings.agent_runtime_cache,
        # Every turn, and its model and tool calls, for the organization's overview.
        observer=AgentUsage(UsageStore(engine), price=price_from(models)),
    )
