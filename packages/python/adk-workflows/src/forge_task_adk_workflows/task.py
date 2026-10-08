"""The ADK workflows task type: one job, ``run``, which runs one run of an
organization's ADK workflow, start to end, through pauses and restarts.

The admin API starts a run (``POST /organizations/{id}/agents/{id}/runs``) in
the run store (``run_store``), its payload the ADK workflow's document as it
was when the run started (a run is pinned to that revision), the saved ADK
workflows it runs, the input, the member it acts as and the ID of its ADK
session, and queues it on the ``adk_workflows`` queue. The worker runs the
job on the run, under a control backed by it; people decide its approvals
and answer its questions on the run's page (``runs.AdkRun``). The files a
run starts with are ADK artifacts the admin API saved where this worker's
runs keep theirs (``artifacts``; ``files``).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import httpx
from forge_common.model_provider import ModelProviderConfigError
from google.adk.artifacts import BaseArtifactService
from google.adk.sessions import BaseSessionService
from pydantic_settings import SettingsError

from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.files import artifact_service
from forge_task_adk_workflows.graph import RunServices
from forge_task_adk_workflows.graph.agent_tools import AgentServices, DocumentsSearch, WorkerMcpServers
from forge_task_adk_workflows.models import NOT_SET_UP, NoModels, load_models
from forge_task_adk_workflows.runs import APP_NAME, AdkRun, RunPayload
from forge_tasks.control import LocalJobControl, current_control
from forge_tasks.tasks import JobResult, Schedule, TaskContext

log = logging.getLogger(__name__)

TASK_NAME = "adk_workflows"
RUN = "run"
#: Test doubles a TaskContext's extras may carry.
SERVICES = "adk_workflows.services"
SESSIONS = "adk_workflows.sessions"
ARTIFACTS = "adk_workflows.artifacts"
#: The process's session and artifact services, among the TaskContext's
#: shared resources.
SESSIONS_RESOURCE = "forge_task_adk_workflows.sessions"
ARTIFACTS_RESOURCE = "forge_task_adk_workflows.artifacts"
NO_SESSIONS = (
    "ADK workflow runs aren't set up on this worker: set HYBRID_ADK_WORKFLOWS__SESSION_DATABASE_URL "
    "(the admin MySQL, mysql+aiomysql://...)"
)

__all__ = [
    "AdkWorkflowsTask",
    "AdkWorkflowsTaskFactory",
    "RunAdkWorkflowJob",
    "RunPayload",
    "build_artifacts",
    "build_services",
    "build_sessions",
]


class RunAdkWorkflowJob:
    name = RUN
    payload_model = RunPayload

    def __init__(self, task: AdkWorkflowsTask) -> None:
        self.task = task

    def lock_key(self, payload: RunPayload) -> str | None:
        return None  # a run is its own: the run store's lease keeps one worker on it

    def describe(self, payload: RunPayload) -> str:
        return payload.name or payload.agent_id

    async def run(self, payload: RunPayload) -> JobResult:
        # Outside the worker (the CLI, tests) a run keeps its state in memory,
        # and a wait ends it.
        control = current_control() or LocalJobControl()
        if self.task.sessions is None:
            return JobResult.failed(self.task.sessions_unavailable or NO_SESSIONS, outcome="failed")
        run = AdkRun(
            payload,
            control=control,
            services=self.task.services,
            sessions=self.task.sessions,
            settings=self.task.settings,
            artifacts=self.task.artifacts,
            artifacts_unavailable=self.task.artifacts_unavailable,
        )
        return await run.run()


class AdkWorkflowsTask:
    name = TASK_NAME
    queue = TASK_NAME

    def __init__(
        self,
        settings: AdkWorkflowsSettings,
        services: RunServices,
        sessions: BaseSessionService | None,
        *,
        sessions_unavailable: str | None = None,
        artifacts: BaseArtifactService | None = None,
        artifacts_unavailable: str | None = None,
    ) -> None:
        self.settings = settings
        self.services = services
        self.sessions = sessions
        self.sessions_unavailable = sessions_unavailable
        self.artifacts = artifacts
        self.artifacts_unavailable = artifacts_unavailable
        self._jobs = {RUN: RunAdkWorkflowJob(self)}

    @property
    def jobs(self) -> dict[str, RunAdkWorkflowJob]:
        return self._jobs

    async def ensure_schema(self) -> None:
        """ADK creates its session tables on first use; this uses them once,
        so a worker started with ``--ensure-schema`` has them, and says early
        when it can't reach them. It never stops the worker."""
        if self.sessions is None:
            return
        try:
            await self.sessions.list_sessions(app_name=APP_NAME, user_id="forge-ensure-schema")
        except Exception as error:
            log.warning("adk_workflows: the session database can't be used yet: %s", error)

    async def close(self) -> None:
        if self.services.http is not None:
            await self.services.http.aclose()
        agents = self.services.agents
        code = getattr(agents, "code", None)
        if code is not None:
            await code.aclose()
        store = getattr(getattr(agents, "runtime", None), "mcp_servers", None)
        engine = getattr(getattr(store, "store", None), "engine", None)
        if engine is not None:
            await engine.dispose()
        close = getattr(self.sessions, "close", None)
        if close is not None:
            await close()


def build_services(settings: AdkWorkflowsSettings) -> RunServices:
    """What runs' nodes use, once per process: HTTP, and the models LLM nodes
    run on (with each node's model and thinking level), or a stand-in that
    fails an LLM node saying why there are none."""
    unavailable = None
    try:
        models: Any = load_models(settings)
    except (ModelProviderConfigError, SettingsError) as error:
        models = None
        unavailable = f"LLM nodes can't run on this worker: its models can't be read: {error}"
        log.error("adk_workflows: %s", unavailable)
    if models is None:
        models = NoModels(reason=unavailable or NOT_SET_UP)
    http = httpx.AsyncClient(timeout=30, follow_redirects=False)
    return RunServices.of(settings, http=http, model=models, agents=build_agent_services(settings, models, http))


def build_agent_services(
    settings: AdkWorkflowsSettings, models: Any, http: httpx.AsyncClient, *, search: Any = None
) -> AgentServices:
    """
    What LLM agents' tools use on this worker: its models, ``${NAME}`` from
    its environment, where HTTP tools may go (its HTTP steps' rules), the
    organization's MCP servers in the admin MySQL (with the admin's secrets
    key), and knowledge base search when given.
    """
    import os

    from forge_agent_runtime import RuntimeServices

    servers: Any = None
    store: Any = None
    unavailable = None
    if settings.session_database_url is None:
        unavailable = "the organization's MCP servers can't be used: the worker has no admin database"
    elif settings.secrets_key is None:
        unavailable = (
            "the organization's MCP servers can't be used on this worker: set "
            "HYBRID_ADK_WORKFLOWS__SECRETS_KEY to the admin API's FORGE_ADMIN_SECRETS_KEY"
        )
    else:
        from forge_mcp_servers import McpServers, SecretBox, SqlServerStore
        from sqlalchemy.ext.asyncio import create_async_engine

        try:
            servers = McpServers(SecretBox(settings.secrets_key.get_secret_value()), timeout=settings.mcp_timeout)
            store = SqlServerStore(create_async_engine(settings.session_database_url.get_secret_value()))
        except ValueError as error:
            servers, unavailable = None, f"the organization's MCP servers can't be used on this worker: {error}"
    if unavailable:
        log.warning("adk_workflows: %s", unavailable)
    runtime = RuntimeServices(
        environment=os.environ,
        allow_private=settings.http_allow_private,
        allowed_hosts=tuple(settings.http_allowed_hosts),
        max_response_bytes=settings.http_max_response_bytes,
        model_timeout=settings.model_timeout,
        mcp_servers=WorkerMcpServers(servers, store, unavailable=unavailable),
    )
    code = None
    if settings.codegraph_url and settings.codegraph_token:
        from forge_codegraph import CodeGraph

        code = CodeGraph.connect(
            settings.codegraph_url, settings.codegraph_token.get_secret_value(), timeout=settings.codegraph_timeout
        )
    return AgentServices(
        models=models,
        runtime=runtime,
        http=http,
        search=search,
        code=code,
        workflow_wait=settings.workflow_tool_wait,
    )


def knowledge_search(ctx: TaskContext) -> Any:
    """:return: The documents task's search, when this worker has it set up; None otherwise."""
    try:
        from forge_task_documents.task import build_knowledge_search
    except ImportError:
        return None
    try:
        return DocumentsSearch(build_knowledge_search(ctx))
    except Exception as error:  # Its settings, Mongo's URI, an embedding model it can't make.
        log.warning("adk_workflows: knowledge bases can't be searched on this worker: %s", error)
        return None


def build_sessions(settings: AdkWorkflowsSettings) -> tuple[BaseSessionService | None, str | None]:
    """
    :return: Where runs keep their ADK sessions (ADK's
        ``DatabaseSessionService`` on ``session_database_url``), or None and why.
    """
    if settings.session_database_url is None:
        return None, NO_SESSIONS
    from google.adk.sessions import DatabaseSessionService

    try:
        return DatabaseSessionService(db_url=settings.session_database_url.get_secret_value()), None
    except ValueError as error:  # ADK's message names the URL without its password
        return None, f"ADK workflow runs can't keep their sessions: {error}"


def build_artifacts(settings: AdkWorkflowsSettings) -> tuple[BaseArtifactService | None, str | None]:
    """
    :return: Where runs keep their ADK artifacts (``artifacts``, with the S3
        service's settings), or None and why: then a run started with files
        fails.
    """
    if settings.artifacts is None:
        return None, None
    secret = settings.s3_secret_access_key
    try:
        return artifact_service(
            settings.artifacts,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=secret.get_secret_value() if secret else None,
        ), None
    except (ValueError, ImportError) as error:
        return None, f"runs' artifacts can't be kept: {error}"


class AdkWorkflowsTaskFactory:
    name = TASK_NAME
    queue = TASK_NAME
    schedules: list[Schedule] = []
    settings_model = AdkWorkflowsSettings

    def build(self, ctx: TaskContext) -> AdkWorkflowsTask:
        settings: AdkWorkflowsSettings = ctx.options or AdkWorkflowsSettings()
        services: RunServices = ctx.extras.get(SERVICES) or build_services(settings)
        if services.agents is not None and services.agents.search is None and SERVICES not in ctx.extras:
            services = services.but(agents=replace(services.agents, search=knowledge_search(ctx)))
        if isinstance(services.model, NoModels):
            log.warning("adk_workflows: %s", services.model.reason)
        unavailable = None
        if SESSIONS in ctx.extras:
            sessions = ctx.extras[SESSIONS]
        else:
            sessions, unavailable = ctx.shared(SESSIONS_RESOURCE, lambda: build_sessions(settings))
        if sessions is None:
            log.warning("adk_workflows: runs will fail: %s", unavailable or NO_SESSIONS)
        if ARTIFACTS in ctx.extras:
            artifacts, no_artifacts = ctx.extras[ARTIFACTS], None
        else:
            artifacts, no_artifacts = ctx.shared(ARTIFACTS_RESOURCE, lambda: build_artifacts(settings))
        if artifacts is None:
            log.warning("adk_workflows: runs started with files will fail: %s", no_artifacts or "no artifacts are kept")
        return AdkWorkflowsTask(
            settings,
            services,
            sessions,
            sessions_unavailable=unavailable,
            artifacts=artifacts,
            artifacts_unavailable=no_artifacts,
        )
