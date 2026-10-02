"""ApiServer: one class that builds and owns the FastAPI application."""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import APIRouter, Depends, FastAPI
from forge_common.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from forge_task_adk_workflows.run_store import RunStore
from google.adk.sessions import DatabaseSessionService
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from forge_admin.agent_documents import AgentStore
from forge_admin.agents.runtime import AgentRuntime
from forge_admin.api.routes import health, info
from forge_admin.auth.authorization import create_enforcer
from forge_admin.auth.security import authenticate
from forge_admin.config import Settings
from forge_admin.db.session import create_engine, create_sessionmaker
from forge_admin.embedding import Embedding

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
        # The async worker's job queues, when set up; None otherwise. It
        # doesn't connect until used. Tests set a fake.
        if getattr(app.state, "embedding", None) is None:
            app.state.embedding = Embedding.from_settings(self.settings)
        # Organizations' agents (their ADK workflows) in MongoDB, when set up;
        # None otherwise. The client connects on first use. Tests set their own
        # (a test database).
        if getattr(app.state, "organization_agents", None) is None:
            app.state.organization_agents = AgentStore.from_settings(self.settings)
        # The assistant's agents, keeping conversations in the same database.
        # Tests set their own (a scripted model, in-memory sessions).
        if getattr(app.state, "agents", None) is None:
            app.state.agents = AgentRuntime.create(self.settings, engine, app)
        # ADK workflow runs' sessions, which the async worker's ADK workflows
        # task keeps in this database too: a run's steps are read from them.
        # Tests set their own.
        if getattr(app.state, "adk_run_sessions", None) is None:
            app.state.adk_run_sessions = DatabaseSessionService(db_engine=engine)
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
            if app.state.embedding is not None:
                await app.state.embedding.aclose()
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
