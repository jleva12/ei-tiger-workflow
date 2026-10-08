"""ApiServer: one class that builds and owns the FastAPI application."""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import APIRouter, Depends, FastAPI
from forge_agent_runtime.a2a import task_store
from forge_common.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from forge_mcp_servers.secrets import SecretBox
from forge_mcp_servers.service import McpServers
from forge_task_adk_workflows.run_store import RunStore
from forge_task_adk_workflows.usage_store import UsageStore
from google.adk.sessions import DatabaseSessionService
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from forge_admin import env_files
from forge_admin.adk_workflows import runtime as workflow_runtime
from forge_admin.adk_workflows.documents import AgentStore
from forge_admin.adk_workflows.files import workflow_artifacts
from forge_admin.adk_workflows.queue import Embedding
from forge_admin.api.routes import health, info
from forge_admin.assistant.runtime import AgentRuntime
from forge_admin.auth.authorization import create_enforcer
from forge_admin.auth.runtime_access import runtime_dependencies
from forge_admin.auth.security import authenticate
from forge_admin.chat_agents import runtime as chat_agent_runtime
from forge_admin.chat_agents.source import create_executor
from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.config import Settings
from forge_admin.db.session import create_engine, create_sessionmaker
from forge_admin.knowledge.graph import codegraph_from
from forge_admin.knowledge.queue import KnowledgeQueue
from forge_admin.knowledge.search import KnowledgeSearch
from forge_admin.knowledge.storage import DocumentStore
from forge_admin.mcp_servers import defaults as mcp_server_defaults

logger = logging.getLogger(__name__)


class ApiServer:
    """
    Wraps the FastAPI setup: settings, the database lifecycle, routers and middleware.

    The health probes are mounted at the root and always open. The info route
    and every router passed in are mounted below ``settings.api_prefix`` and
    require the API key when one is configured. Public routers are mounted at
    the root, without the API key or a user: they check their callers
    themselves.

    :param settings: The application settings.
    :param routers: Routers to mount below the API prefix.
    :param public_routers: Routers to mount at the root, unprotected.
    :param middleware: Extra Starlette middleware, outermost first. Request
        ids and access logs (RequestContextMiddleware) always come first,
        then CORS when ``settings.cors_origins`` is set.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        routers: Sequence[APIRouter] = (),
        public_routers: Sequence[APIRouter] = (),
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self.settings = settings
        self.routers = tuple(routers)
        self.public_routers = tuple(public_routers)
        self.middleware = tuple(middleware)
        self._app: FastAPI | None = None

    def create_app(self) -> FastAPI:
        """
        Build the FastAPI application once and return the same object afterwards.

        :return: The configured application.
        """
        if self._app is None:
            self._app = self._build()
        return self._app

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        """
        Open the database pool and the Casbin enforcer at startup and close
        the pool at shutdown.

        The engine connects lazily and the enforcer reads its policy on use,
        so the application starts while MySQL is down and ``/health/ready``
        reports it.

        :param app: The application, supplied by FastAPI.
        :return: An asynchronous iterator that yields while the app runs.
        """
        engine = create_engine(self.settings)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.enforcer = create_enforcer(engine)
        # ADK workflow runs, kept in this database (adk_runs and
        # adk_run_events, which 0005adk_run_store creates). Tests set their
        # own (SQLite).
        if getattr(app.state, "adk_runs", None) is None:
            app.state.adk_runs = RunStore(engine)
        # What organizations' workflows, agents and assistant used, beside
        # their runs (usage_calls and usage_invocations, 0009usage).
        if getattr(app.state, "usage", None) is None:
            app.state.usage = UsageStore(engine)
        # The async worker's job queues, when set up; None otherwise. It
        # doesn't connect until used. Tests set a fake.
        if getattr(app.state, "embedding", None) is None:
            app.state.embedding = Embedding.from_settings(self.settings)
        # Organizations' agents (their ADK workflows) in MongoDB, when set up;
        # None otherwise. The client connects on first use. Tests set their own
        # (a test database).
        if getattr(app.state, "organization_agents", None) is None:
            app.state.organization_agents = AgentStore.from_settings(self.settings)
        # Organizations' MCP servers, whose credentials are encrypted with
        # secrets_key; None without one. Tests set their own.
        if getattr(app.state, "mcp_servers", None) is None:
            key = self.settings.secrets_key
            app.state.mcp_servers = (
                McpServers(
                    SecretBox(key.get_secret_value()), timeout=self.settings.mcp_timeout
                )
                if key
                else None
            )
        # The MCP servers the web console offers ready-made, checked against
        # the auth methods: a bad file stops the API here. Tests set their own.
        if getattr(app.state, "mcp_server_defaults", None) is None:
            app.state.mcp_server_defaults = mcp_server_defaults.load(
                self.settings.mcp_server_defaults, env_files.environment()
            )
        # Knowledge bases: their documents' files in the documents bucket, the
        # async worker's documents queue that embeds them, and the search over
        # what it embedded; each None when not set up. None connects until
        # used. Tests set their own.
        if getattr(app.state, "documents", None) is None:
            app.state.documents = DocumentStore.from_settings(self.settings)
        if getattr(app.state, "knowledge_queue", None) is None:
            app.state.knowledge_queue = KnowledgeQueue.from_settings(self.settings)
        if getattr(app.state, "knowledge_search", None) is None:
            app.state.knowledge_search = KnowledgeSearch.from_settings(self.settings)
        # Graph knowledge bases' search and the code graph explorer: the code
        # graph worker's API; None when not set up. Tests set their own.
        if getattr(app.state, "codegraph", None) is None:
            app.state.codegraph = codegraph_from(self.settings)
        # The assistant's agents, keeping conversations in the same database.
        # Tests set their own (a scripted model, in-memory sessions).
        if getattr(app.state, "agents", None) is None:
            app.state.agents = AgentRuntime.create(self.settings, engine, app)
        # Organizations' chat agents (drafts and published versions) in MongoDB,
        # and the runtime that runs them by ID; None without MongoDB. Tests set
        # their own.
        if getattr(app.state, "chat_agents", None) is None:
            app.state.chat_agents = ChatAgentStore.from_settings(self.settings)
        if getattr(app.state, "agent_executor", None) is None:
            app.state.agent_executor = (
                create_executor(
                    self.settings,
                    engine,
                    store=app.state.chat_agents,
                    sessions=app.state.sessionmaker,
                    mcp_servers=app.state.mcp_servers,
                    agents=app.state.organization_agents,
                    runs=app.state.adk_runs,
                    queue=app.state.embedding,
                    knowledge_search=app.state.knowledge_search,
                    codegraph=app.state.codegraph,
                )
                if app.state.chat_agents is not None
                else None
            )
        # Chat agents' A2A tasks, in this database (a2a_tasks, 0010a2a). Tests
        # set their own.
        if getattr(app.state, "a2a_tasks", None) is None:
            app.state.a2a_tasks = task_store(engine)
        # ADK workflow runs' sessions, which the async worker's ADK workflows
        # task keeps in this database too: a run's steps are read from them.
        # Tests set their own.
        if getattr(app.state, "adk_run_sessions", None) is None:
            app.state.adk_run_sessions = DatabaseSessionService(db_engine=engine)
        # The files workflow runs start with: ADK artifacts of their sessions,
        # where the async worker's runs keep theirs; None when not set up.
        # Tests set their own.
        if getattr(app.state, "workflow_artifacts", None) is None:
            app.state.workflow_artifacts = workflow_artifacts(self.settings)
        if self.settings.local_user_id:
            logger.warning(
                "LOCAL DEVELOPMENT IDENTITY: API requests without a bearer token "
                "act as user %s (FORGE_ADMIN_LOCAL_USER_ID). Never enable this "
                "in production.",
                self.settings.local_user_id,
            )
        try:
            yield
        finally:
            await app.state.agents.close()
            if app.state.agent_executor is not None:
                await app.state.agent_executor.close()
            if app.state.chat_agents is not None:
                await app.state.chat_agents.aclose()
            if app.state.embedding is not None:
                await app.state.embedding.aclose()
            if app.state.knowledge_queue is not None:
                await app.state.knowledge_queue.aclose()
            if app.state.knowledge_search is not None:
                await app.state.knowledge_search.aclose()
            if app.state.codegraph is not None:
                await app.state.codegraph.aclose()
            if app.state.documents is not None:
                app.state.documents.close()
            if app.state.organization_agents is not None:
                await app.state.organization_agents.aclose()
            await engine.dispose()

    def _build(self) -> FastAPI:
        docs = self.settings.docs_enabled
        app = FastAPI(
            title=self.settings.name,
            version=version("forge-admin"),
            lifespan=self.lifespan,
            middleware=self._middleware(),
            docs_url="/docs" if docs else None,
            openapi_url="/openapi.json" if docs else None,
            redoc_url=None,
        )
        app.state.settings = self.settings

        app.include_router(health.router)
        protected = [Depends(authenticate)]
        for router in (info.router, *self.routers):
            app.include_router(
                router, prefix=self.settings.api_prefix, dependencies=protected
            )
        for router in self.public_routers:
            app.include_router(router)
        # The runtime, over ADK's run API and A2A: public unless the settings
        # say otherwise, and then an API key or a sign-in with agents:run in
        # the organization called (forge_admin.auth.runtime_access).
        runtime = runtime_dependencies(self.settings)
        app.include_router(
            chat_agent_runtime.router,
            prefix=f"{self.settings.api_prefix}/runtime",
            dependencies=runtime,
            tags=["chat agent runtime"],
        )
        app.include_router(
            chat_agent_runtime.a2a_router(self.settings),
            prefix=f"{self.settings.api_prefix}/runtime",
            dependencies=runtime,
            tags=["chat agent runtime: A2A"],
        )
        # The workflows, over REST (their A2A is beside the chat agents').
        app.include_router(
            workflow_runtime.router,
            prefix=f"{self.settings.api_prefix}/runtime",
            dependencies=runtime,
        )
        return app

    def _middleware(self) -> list[Middleware]:
        log_settings = self.settings.logging
        middleware = [
            Middleware(
                RequestContextMiddleware,
                access_log=log_settings.access_log,
                exclude_paths=log_settings.access_log_exclude_paths,
            )
        ]
        if self.settings.cors_origins:
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=self.settings.cors_origins,
                    allow_methods=["*"],
                    allow_headers=["*"],
                    # So the web console can read a failed request's id.
                    expose_headers=[REQUEST_ID_HEADER],
                )
            )
        middleware.extend(self.middleware)
        return middleware
