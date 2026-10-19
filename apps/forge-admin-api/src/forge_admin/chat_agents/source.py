"""The hosted runtime: organizations' chat agents run by ID.

``forge_agent_runtime`` builds and runs them; this module tells it where they
are and what they may use here:

- :class:`MongoAgentSource`: an agent by ID from the store: ``ca_x`` its latest
  published version, ``ca_x@3`` that one, ``ca_x@draft`` its draft (built
  again whenever the draft is saved); and, before any of that is read, its
  organization (``locate``), which callers are let in by.
- :class:`OrganizationMcpServers`: the MCP servers an agent names by ID
  (``forge_admin.mcp_servers``), with their credentials.
- :class:`OrganizationWorkflows`: the workflows an agent calls as tools,
  started on the async worker and waited for.
- ``OrganizationSkills`` (``forge_admin.skills.vault``): the skills its
  Skills tools name, from the organization's vault as they are now.
- ``OrganizationUiComponents`` (``forge_admin.ui_components.library``): the
  team UI components its Rich answers pick, at their latest published versions.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from forge_agent_runtime import (
    INTAKE,
    AgentExecutor,
    AgentNotFound,
    AgentRef,
    BuildError,
    DocumentError,
    LocatedAgent,
    ResolvedAgent,
    RuntimeServices,
    code_executor,
    parse_document,
    running_agent,
    workflow_starter,
)
from forge_agent_runtime.services import StartSource, WorkflowStart
from forge_codegraph_query import CodeGraph
from forge_common.adk.models import ProviderModels
from forge_common.adk.usage import price_from
from forge_common.model_provider import load_model_provider_config
from forge_mcp_servers.api_credentials import ApiCredentials
from forge_mcp_servers.service import McpServers
from forge_task_adk_workflows.run_store import Actor, Provenance, RunStore
from forge_task_adk_workflows.runs import STATE_KEY
from forge_task_adk_workflows.steps import run_progress
from forge_task_adk_workflows.usage_store import UsageStore
from google.adk.agents.callback_context import CallbackContext
from google.adk.artifacts import InMemoryArtifactService
from google.adk.sessions import BaseSessionService, DatabaseSessionService
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.tool_context import ToolContext
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from forge_admin import env_files
from forge_admin.adk_workflows.documents import AgentStore
from forge_admin.adk_workflows.provenance import chat_start
from forge_admin.adk_workflows.queue import Embedding
from forge_admin.adk_workflows.resources import RunResources
from forge_admin.adk_workflows.runs import (
    APP_NAME,
    AdkRunError,
    AdkRunRefused,
    prepare_run,
    refuse_start,
    settle,
    start_adk_run_once,
    start_schema,
)
from forge_admin.adk_workflows.versions import (
    ResolvedWorkflow,
    WorkflowGone,
    resolve_workflow,
    workflow_finder,
)
from forge_admin.api.routes.common import name_of
from forge_admin.api_credentials import (
    TOOL_ENVIRONMENT_NOTE,
    OrganizationCredentials,
    tool_environment,
)
from forge_admin.assistant.language_models import gemini_config
from forge_admin.chat_agents.access import (
    AgentUser,
    OrganizationAccess,
    as_strict_as_live,
    current_agent_user,
)
from forge_admin.chat_agents.requests import OrganizationRequests
from forge_admin.chat_agents.starts import StartIntents
from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.chat_agents.turn_leases import SqlTurnLeases
from forge_admin.config import Settings
from forge_admin.db.session import adk_session_engine
from forge_admin.knowledge.search import KnowledgeSearch
from forge_admin.knowledge.tools import OrganizationKnowledgeBases
from forge_admin.mcp_servers.toolset import McpServerStamps, mcp_toolset
from forge_admin.overview.recording import AgentUsage, current_channel
from forge_admin.skills.vault import OrganizationSkills, SkillStamps
from forge_admin.ui_components.library import (
    OrganizationUiComponents,
    UiComponentStamps,
)
from forge_admin.ui_components.store import UiComponentStore

logger = logging.getLogger(__name__)

#: How long a chat agent waits for a workflow it called, in seconds, when its
#: Workflow tool doesn't say (``wait_seconds``); at most the deployment's cap
#: (``agent_workflow_wait_max``).
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


def chat_run_id(agent_id: str, user_id: str, session_id: str, call_id: str) -> str:
    """
    :return: The ID of the run a chat agent's call of a workflow starts,
        made from the conversation (its agent, whose it is, its ID) and the
        call: the chat follows the run by its call, and a call answered
        again finds the run it started. Whose it is is part of it: two
        people's conversations may have the same ID.
    """
    made = f"{agent_id}\n{user_id}\n{session_id}\n{call_id}".encode()
    return hashlib.sha256(made).hexdigest()[:32]


def started_for(
    run: Mapping[str, Any], agent_id: str, user_id: str, session_id: str
) -> bool:
    """
    :return: Whether a run was started by a call in this conversation: its
        agent's, its person's.
    """
    payload = run.get("payload")
    trigger = payload.get("trigger") if isinstance(payload, dict) else None
    return isinstance(trigger, dict) and (
        trigger.get("type"),
        trigger.get("agent_id"),
        trigger.get("user"),
        trigger.get("session_id"),
    ) == ("chat_agent", agent_id, user_id, session_id)


#: What the call IDs of the runs intakes start begin with: made from the
#: record's or request's ID (``forge_agent_runtime.intake``), not a model's call.
INTAKE_CALLS = ("intake-", "request-")


def start_run_id(agent_id: str, call_id: str) -> str:
    """
    :return: The ID of a run an intake starts: made from the agent and the
        start's call ID (its record's or request's, and the workflow), not
        the conversation or its person, so however often and wherever it's
        asked for, it's one run. Who may follow it is still checked
        (:func:`started_for`).
    """
    return chat_run_id(agent_id, "", "", call_id)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


class MongoAgentSource:
    """
    Agents by ID from the chat agent store, at the version a reference names.

    :param skill_stamps: When the skills an agent uses last changed: part of
        its cache key, so a skill changed in the vault is used from the next turn.
    :param mcp_stamps: The MCP endpoint settings captured by its toolsets,
        so changing an endpoint rebuilds the toolsets on the next turn.
    :param ui_stamps: The latest versions of the team UI components its Rich
        answers pick (``forge_admin.ui_components``), so a publish reaches it
        from the next turn.
    """

    def __init__(
        self,
        store: ChatAgentStore,
        skill_stamps: SkillStamps | None = None,
        mcp_stamps: McpServerStamps | None = None,
        ui_stamps: UiComponentStamps | None = None,
    ) -> None:
        self.store = store
        self.skill_stamps = skill_stamps
        self.mcp_stamps = mcp_stamps
        self.ui_stamps = ui_stamps

    async def locate(self, ref: AgentRef) -> LocatedAgent:
        """
        :return: The agent and its organization, from its record alone:
            what the runtime lets callers in by before reading its document.
        """
        record = await self.store.find(ref.agent_id)
        if record is None:
            raise AgentNotFound(f"There's no agent {ref.agent_id}.")
        return LocatedAgent(ref, str(record["organization_id"]))

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
            # Which version it is: a published one never runs a workflow's draft.
            raw = {**found["document"], "version": int(number)}
            key = f"{ref.agent_id}@{number}"
            version = int(number)
        # An old version, or the draft, is never open to more people than
        # the live one: held to the rules that guard what it reaches there.
        raw, stricter = await as_strict_as_live(
            self.store, str(record["organization_id"]), record, raw, version
        )
        if stricter:
            key = f"{key}#access:{stricter}"
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
        if self.mcp_stamps is not None:
            stamp = await self.mcp_stamps(
                record.get("organization_id") or raw.get("organization_id"), raw
            )
            if stamp:
                key = f"{key}#mcp:{stamp}"
        if self.ui_stamps is not None:
            stamp = await self.ui_stamps(
                record.get("organization_id") or raw.get("organization_id"), raw
            )
            if stamp:
                key = f"{key}#ui:{stamp}"
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
    starts the run :func:`chat_run_id` names, once.

    The runs its intakes start go through an outbox (``starts``): written
    down first (:meth:`intend`), started once the change completing the
    intake is kept (:meth:`dispatch`), each under :func:`start_run_id`.
    """

    def __init__(
        self,
        agents: AgentStore,
        runs: RunStore,
        queue: Embedding | None,
        resources: Callable[[str, AgentUser], RunResources] | None = None,
        run_sessions: BaseSessionService | None = None,
        intents: StartIntents | None = None,
    ) -> None:
        self.agents = agents
        self.runs = runs
        self.queue = queue
        #: What a workflow's LLM agents use from its organization, by
        #: organization, for whoever the chat's call is for.
        self.resources = resources
        #: Where the worker keeps runs' ADK sessions, which their steps are
        #: read from; without it, a run's progress has no steps.
        self.run_sessions = run_sessions
        #: The runs intakes start, written down until they're started.
        self.intents = intents

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
        wait: float | None = None,
    ) -> Any:
        if self.queue is None:
            raise RuntimeError(
                "Workflows can't run: the async worker's queue isn't set up."
            )
        session = context.session
        # A call of this workflow with this input left unanswered by an
        # earlier turn (stopped as it waited) started a run: this is that
        # run, not another.
        call_id = (
            orphan_of(context, workflow_input)
            or context.function_call_id
            or f"adk-{uuid4()}"
        )
        run_id = chat_run_id(session.app_name, context.user_id, session.id, call_id)
        found = await self.runs.get(run_id, organization_id=organization_id)
        # Only a run this conversation's call started is its own to carry on.
        if found is not None and not started_for(
            found, session.app_name, context.user_id, session.id
        ):
            raise ValueError(
                "This call's workflow run belongs to another conversation."
            )
        started = await self._start(
            workflow_id,
            workflow_input,
            organization_id,
            origin_of(context),
            run_id=run_id,
            call_id=call_id,
        )
        return await self._answer(
            started["id"], WORKFLOW_WAIT if wait is None else wait
        )

    async def intend(
        self,
        starts: Sequence[WorkflowStart],
        *,
        organization_id: str | None,
        context: CallbackContext,
        source: StartSource,
    ) -> list[str]:
        if self.intents is None or self.queue is None:
            raise RuntimeError(
                "Workflows can't run: the async worker's queue isn't set up."
            )
        if organization_id is None:
            raise LookupError("The agent belongs to no organization.")
        session = context.session
        # Who and what starts them, as this turn has it: they may be started
        # by another process, after it.
        origin = origin_of(context)
        written = [
            {
                "id": start_run_id(session.app_name, start.call_id),
                "organization_id": organization_id,
                "agent_id": session.app_name,
                "user_id": context.user_id,
                "session_id": session.id,
                "call_id": start.call_id,
                "workflow": start.ref,
                "workflow_name": start.name[:255],
                "input": start.input,
                "origin": origin,
                "source": source.kind,
                "source_id": source.id,
                "state_key": source.state_key,
            }
            for start in starts
        ]
        await self.intents.write(written)
        return [intent["id"] for intent in written]

    async def dispatch(
        self, run_id: str, *, organization_id: str | None, wait: float
    ) -> dict[str, Any]:
        if self.intents is None:
            raise RuntimeError("Workflows can't run here.")
        intent = await self.intents.get(run_id)
        if intent is None or intent["organization_id"] != organization_id:
            raise LookupError(f"There's no run {run_id[:8]} to start.")
        try:
            await self._start(
                intent["workflow"],
                intent["input"],
                organization_id,
                intent["origin"],
                run_id=run_id,
                call_id=intent["call_id"],
            )
        except ValueError as refused:
            # Refused for good (it doesn't fit, they may not): never started.
            await self.intents.dispatched(run_id, error=str(refused))
            return {"run_id": run_id, "status": "refused", "error": str(refused)}
        await self.intents.dispatched(run_id)
        return await self._answer(run_id, wait)

    async def _answer(self, run_id: str, wait: float) -> dict[str, Any]:
        """:return: Where a run got to, once it stops going or ``wait`` runs out."""
        current = await settle(self.runs, run_id, wait=wait)
        answer: dict[str, Any] = {
            "run_id": run_id,
            "status": current["status"] if current else "missing",
        }
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
        origin: dict[str, Any],
        *,
        run_id: str,
        call_id: str,
    ) -> dict[str, Any]:
        """
        :return: The run of the workflow under ``run_id``: the one started
            already, or a new one, queued.
        :raises ValueError: Its start was refused (kept for the audit).
        """
        if self.queue is None:
            raise RuntimeError(
                "Workflows can't run: the async worker's queue isn't set up."
            )
        resolved = await self._resolved(workflow_id, organization_id)
        record = resolved.run_record
        agent_id = str(origin["agent_id"])
        name = str(origin.get("agent_name") or "")
        run_as = Actor(
            f"chat-agent:{agent_id}",
            f"Chat agent {name or origin.get('context_agent') or agent_id}",
        )
        found = await self.runs.started_as(
            run_id,
            organization_id=record["organization_id"],
            agent_id=record["_id"],
            requested_by=run_as,
        )
        if found is not None:
            return found
        caller = origin.get("caller") or {}
        user = AgentUser(
            caller.get("subject"),
            tuple(caller.get("groups") or ()),
            bool(caller.get("manager")),
        )
        resources = (
            self.resources(record["organization_id"], user) if self.resources else None
        )
        started = Provenance(**origin["provenance"])
        intake = bool(origin.get("intake"))
        # Named only when they're who called: anyone else's ID is only what was passed on.
        if started.on_behalf_of_verified:
            started = replace(
                started,
                on_behalf_of_name=await self._name_of(resources, origin["user_id"]),
            )
        find = workflow_finder(self.agents, record["organization_id"])
        try:
            prepared = await prepare_run(record, workflow_input, find, resources)
        except AdkRunError as error:
            await refuse_start(
                self.runs,
                organization_id=record["organization_id"],
                agent_id=record["_id"],
                version=resolved.version,
                status_code=403 if isinstance(error, AdkRunRefused) else 422,
                reason=str(error),
                requested_by=run_as,
                provenance=started,
            )
            raise ValueError(str(error)) from error
        run, _ = await start_adk_run_once(
            self.runs,
            self.queue,
            record,
            run_id=run_id,
            prepared=prepared,
            input=workflow_input,
            run_as=str(run_as.id),
            run_as_name=run_as.name,
            trigger={
                "type": "chat_agent",
                "agent_id": agent_id,
                **({"agent_name": name} if name else {}),
                **(
                    {"agent_version": started.via_agent_version}
                    if started.via_agent_version
                    else {}
                ),
                "user": origin["user_id"],
                # The conversation and call it ran for, which follow it (``progress``).
                "session_id": origin["session_id"],
                "call_id": call_id,
                **({"intake": True} if intake else {}),
            },
            version=resolved.version,
            provenance=started,
        )
        return run

    @staticmethod
    async def _name_of(resources: RunResources | None, user_id: str) -> str:
        """:return: A conversation's person's name (or a key's), when the
        site knows them; empty otherwise."""
        if resources is None:
            return ""
        try:
            async with resources.sessions() as session:
                name = await name_of(session, user_id)
        except SQLAlchemyError:
            return ""
        # Someone the site doesn't know is named by their ID: no name.
        return "" if name == user_id else name

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
            chat_run_id(agent_id, user_id, session_id, call_id),
            organization_id=organization_id,
        )
        if run is None and call_id.startswith(INTAKE_CALLS):
            # An intake's: known by its record or request, not the conversation.
            run = await self.runs.get(
                start_run_id(agent_id, call_id), organization_id=organization_id
            )
        # Only the person whose conversation started it follows it.
        if run is None or not started_for(run, agent_id, user_id, session_id):
            return None
        payload = run["payload"]
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


def origin_of(context: CallbackContext) -> dict[str, Any]:
    """
    :return: Who and what starts a run from a chat agent's turn, as JSON:
        the agent (and its version, as the turn resolved it), the
        conversation and its person, who called (``current_agent_user``),
        how (the channel, the request's origin), and whether an intake
        completing starts it. What a start made later, by another process
        (the upkeep), needs of the turn.
    """
    session = context.session
    agent_id = session.app_name
    # Which version of the agent runs, and its name, as the turn resolved it.
    running = running_agent()
    if running is not None and running.document.id != agent_id:
        running = None
    user = current_agent_user()
    started = chat_start(
        user_id=context.user_id,
        user_name="",
        caller=user.subject,
        channel=current_channel(),
        agent_id=agent_id,
        agent_version=None
        if running is None or running.version is None
        else str(running.version),
        intake=workflow_starter() == INTAKE,
    )
    return {
        "agent_id": agent_id,
        "agent_name": running.document.name if running is not None else "",
        "context_agent": context.agent_name,
        "session_id": session.id,
        "user_id": context.user_id,
        "intake": workflow_starter() == INTAKE,
        "caller": {
            "subject": user.subject,
            "groups": list(user.groups),
            "manager": user.manager,
        },
        "provenance": asdict(started),
    }


def orphan_of(context: ToolContext, workflow_input: dict[str, Any]) -> str | None:
    """
    :return: The ID of an earlier call of the same Workflow tool, with the
        same input, an earlier turn made and never answered (it stopped as
        it waited for the run, and the chat dropped the call): the model's
        call again of the run it started. None when there's none.
    """
    events = list(getattr(context.session, "events", None) or ())
    name = None
    for event in events:
        for call in event.get_function_calls():
            if call.id == context.function_call_id:
                name = call.name
    if name is None:
        return None
    answered = {
        response.id for event in events for response in event.get_function_responses()
    }
    wanted = json.dumps(workflow_input, sort_keys=True, default=str)
    for event in reversed(events):
        if event.invocation_id == context.invocation_id:
            continue
        for call in event.get_function_calls():
            if (
                call.name == name
                and call.id
                and call.id not in answered
                and json.dumps(dict(call.args or {}), sort_keys=True, default=str)
                == wanted
            ):
                return call.id
    return None


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
    ui_components: UiComponentStore | None = None,
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
        workflow_wait=WORKFLOW_WAIT,
        workflow_wait_max=settings.agent_workflow_wait_max,
        workflows=OrganizationWorkflows(
            agents,
            runs,
            queue,
            # A chat's workflow runs for whoever the chat's call is for.
            lambda organization, user: RunResources(
                organization,
                chat_agents=store,
                ui_components=ui_components,
                sessions=sessions,
                user=user,
                workflows=agents,
            ),
            run_sessions=run_sessions
            or DatabaseSessionService(db_engine=adk_session_engine(engine)),
            # Its intakes' runs, written down until they're started.
            intents=StartIntents(sessions),
        )
        if agents is not None
        else None,
        knowledge_bases=OrganizationKnowledgeBases(
            sessions, knowledge_search, codegraph
        ),
        skills=OrganizationSkills(sessions),
        # The team UI components their Rich answers pick: published, enabled.
        ui_components=OrganizationUiComponents(ui_components)
        if ui_components is not None
        else None,
        # Requests agents file to their intakes, kept for the person beyond a conversation.
        intake_requests=OrganizationRequests(sessions),
        # What whoever each call is for holds, which agents' rules are checked against.
        access=OrganizationAccess(sessions),
    )
    models = models or chat_agent_models(settings)
    return AgentExecutor(
        MongoAgentSource(
            store,
            SkillStamps(sessions),
            McpServerStamps(sessions),
            UiComponentStamps(ui_components) if ui_components is not None else None,
        ),
        models=models,
        sessions=DatabaseSessionService(db_engine=adk_session_engine(engine)),
        artifacts=InMemoryArtifactService(),
        services=services,
        cache_size=settings.agent_runtime_cache,
        # Every turn, and its model and tool calls, for the organization's overview.
        observer=AgentUsage(UsageStore(engine), price=price_from(models)),
        # One turn at a time in a conversation, across every process.
        leases=SqlTurnLeases(sessions),
        turn_wait=settings.agent_turn_wait,
    )
