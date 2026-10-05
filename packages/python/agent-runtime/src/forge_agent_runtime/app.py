"""A chat agent as a web app, put together with a builder. Needs ``forge-agent-runtime[server]``.

:class:`AgentServer` collects what the app is made of (the agent, the models it
runs on, where conversations are kept, your own routes and lifespans), and
:meth:`AgentServer.build` makes one FastAPI app of it::

    from forge_agent_runtime import AgentServer

    server = (
        AgentServer()                                   # FORGE_AGENT_* settings and .env
        .with_agent("agent/support-assistant.chat-agent.json")
        .with_models("model_provider.yaml")
        .with_web("web/dist")                           # the built UI, served at /
    )
    app = server.build()

    if __name__ == "__main__":
        server.run()

The app serves:

- ``{api_prefix}/run_sse`` and the sessions: ADK's run API (:mod:`.server`),
  the agent named by ``appName``;
- ``{api_prefix}/agent``: the agent it serves (its ID, name, version and the
  state it takes), which a UI reads instead of hard-coding them;
- ``/healthz``;
- the built UI at ``/``, a single-page app: paths it doesn't have answer its
  ``index.html``.

What it keeps is pluggable: conversations in memory or a database
(:meth:`AgentServer.with_sessions`), files agents make and people send in
memory, a folder or S3 (:meth:`AgentServer.with_artifacts`), long-term memory
in memory or MongoDB Atlas (:meth:`AgentServer.with_memory`). Replies stream
as the model writes them, or arrive whole (:meth:`AgentServer.with_streaming`),
and the API can ask for a key (:meth:`AgentServer.with_api_keys`).

Every setting has a ``FORGE_AGENT_*`` variable (:class:`AgentServerSettings`);
a builder method overrides it. :func:`env` reads your own settings in
``main.py``, from the environment or ``.env``. The ``${NAME}``s in the model provider config
and the agent's tools are filled in from the environment, and from ``.env``
beside it for what the environment doesn't have. :func:`build` builds the agent once as the app
starts, so a missing key or a tool it can't set up stops it there, saying why.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from forge_agent_runtime.document import ChatAgentDocument
from forge_agent_runtime.executor import (
    AgentExecutor,
    AgentRef,
    AgentSource,
    DirectoryAgentSource,
    DocumentAgentSource,
    FileAgentSource,
)
from forge_agent_runtime.services import RuntimeServices

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.params import Depends
    from google.adk.artifacts import BaseArtifactService
    from google.adk.memory import BaseMemoryService
    from google.adk.sessions import BaseSessionService

    from forge_common.adk.models import ProviderModels
    from forge_common.model_provider import ModelProviderConfig

log = logging.getLogger(__name__)

Lifespan = Callable[[Any], AbstractAsyncContextManager[Any]]


def read_dotenv(path: Path) -> dict[str, str]:
    """
    :return: A ``.env`` file's ``NAME=value`` lines (``export``, quotes and
        comments allowed); empty when there's none.
    """
    values: dict[str, str] = {}
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[name.strip()] = value
    return values


class MissingSetting(LookupError):
    """A setting ``main.py`` needs is in neither the environment nor ``.env``."""


def env(name: str, default: str | None = None, *, env_file: Path = Path(".env")) -> str:
    """
    One of your own settings, for ``main.py``: from the environment, or from
    ``.env`` for what the environment doesn't have. ::

        .with_sessions(env("DATABASE_URL"))

    :param default: What it is when neither has it (or it's blank); without
        one, it must be set.
    :raises MissingSetting: It isn't set, and there's no default.
    """
    value = os.environ.get(name) or read_dotenv(env_file).get(name)
    if value:
        return value
    if default is None:
        raise MissingSetting(f"{name} isn't set: add it to .env, or to the environment.")
    return default


#: Database URLs as hosts hand them out, and the async driver ADK needs for each.
ASYNC_DRIVERS = {
    "postgres": "postgresql+asyncpg",
    "postgresql": "postgresql+asyncpg",
    "mysql": "mysql+aiomysql",
    "sqlite": "sqlite+aiosqlite",
}


def database_url(url: str) -> str:
    """
    A database URL as ADK's session service takes it: ``postgres://`` and
    ``postgresql://`` (as hosts hand them out) use asyncpg, ``mysql://``
    aiomysql, and ``sqlite://`` aiosqlite; ``sslmode`` is asyncpg's ``ssl``.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    scheme = ASYNC_DRIVERS.get(scheme, scheme)
    if scheme == "postgresql+asyncpg":
        rest = re.sub(r"([?&])sslmode=", r"\1ssl=", rest)
    return f"{scheme}://{rest}"


def artifact_service(
    where: str, environment: Mapping[str, str], *, create_bucket: bool = False
) -> BaseArtifactService:
    """
    :param where: ``memory``, a folder (``./artifacts`` or ``file://...``), or
        ``s3://bucket/prefix`` (``forge-agent-runtime[artifacts-s3]``).
    :param environment: Where S3's ``AWS_*`` settings are.
    :param create_bucket: Make the S3 bucket on first use if it isn't there.
    """
    from google.adk.artifacts import FileArtifactService, InMemoryArtifactService

    if where == "memory":
        return InMemoryArtifactService()
    if where.startswith("s3://"):
        try:
            import boto3  # noqa: F401

            from forge_common.adk.artifacts import S3ArtifactService
        except ImportError as error:
            raise ImportError(
                "Artifacts in S3 need boto3: pip install 'forge-agent-runtime[artifacts-s3]'"
            ) from error
        return S3ArtifactService.from_url(where, environment, create_bucket=create_bucket)
    scheme = where.partition("://")[0] if "://" in where else "file"
    if scheme != "file":
        raise ValueError(f"Artifacts are kept in memory, a folder or s3://..., not {scheme}://")
    return FileArtifactService(root_dir=where.removeprefix("file://"))


def _shown(url: str) -> str:
    """A database URL for the log, its password hidden."""
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return url.split("://", 1)[0] + "://…"


class AgentServerSettings(BaseSettings):
    """
    An agent server's settings: ``FORGE_AGENT_<NAME>`` variables, or a ``.env``
    file beside it. Lists are JSON (``FORGE_AGENT_CORS_ORIGINS=["https://…"]``).
    """

    model_config = SettingsConfigDict(env_prefix="FORGE_AGENT_", env_file=".env", extra="ignore")

    #: The agent's exported JSON, or a folder of them.
    definition: Path | None = None
    #: The model_provider.yaml its agents run on; its keys are ${NAME}s from the environment.
    model_provider_config: Path | None = None
    #: Where conversations are kept: memory, or a database URL (sqlite:///sessions.db,
    #: postgresql://…, mysql://…; each with its async driver: forge-agent-runtime[sessions-…]).
    sessions: str = "memory"
    #: Where files agents make and people send are kept: memory, a folder, or
    #: s3://bucket/prefix (forge-agent-runtime[artifacts-s3]; AWS_* from the environment).
    artifacts: str = "memory"
    #: Make the S3 bucket on first use if it isn't there (a local S3 in development).
    artifacts_create_bucket: bool = False
    #: Long-term memory in MongoDB Atlas (forge-agent-runtime[memory-atlas]); in memory without it.
    memory_atlas_uri: SecretStr | None = None
    memory_database: str = "forge_agents"
    #: The model provider whose embeddings Atlas memory uses.
    embedding_provider: str = "openai"
    #: What HTTP tools may reach besides the internet: private networks, or these hosts.
    allow_private: bool = False
    allowed_hosts: list[str] = []
    #: Whether replies stream as the model writes them (true) or arrive whole
    #: (false); as each request asks by default.
    streaming: bool | None = None
    #: Keys the API asks for (Authorization: Bearer <key>, or X-API-Key); none, and it's open.
    api_keys: list[SecretStr] = []
    #: Where the run API is: /api/run_sse, /api/apps/…
    api_prefix: str = "/api"
    #: The built UI, served at /; none when it's served elsewhere (Vite in development).
    web_dir: Path | None = None
    #: Origins a browser may call the API from; none (the default) when the UI is served here.
    cors_origins: list[str] = []
    #: The app's title (its OpenAPI page); the agent's name by default.
    title: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    #: Restart on code changes (development); needs the app's import string (main:app).
    reload: bool = False
    log_level: str = "info"
    #: Build the agent as the app starts, so what's wrong shows then.
    prebuild: bool = True
    #: Where ${NAME}s come from besides the environment.
    env_file: Path = Path(".env")


def spa_files(directory: Path) -> Any:
    """A built single-page app: its files, and its ``index.html`` for every other path."""
    from starlette.exceptions import HTTPException
    from starlette.staticfiles import StaticFiles

    class SpaFiles(StaticFiles):
        async def get_response(self, path: str, scope: Any) -> Any:
            try:
                return await super().get_response(path, scope)
            except HTTPException as exc:
                # A route of the app, not a file: the app shows it.
                if exc.status_code == 404 and "." not in Path(path).name:
                    return await super().get_response("index.html", scope)
                raise

    return SpaFiles(directory=directory, html=True)


def _api_key_check(keys: list[SecretStr]) -> Depends:
    """A dependency refusing requests without one of the keys (compared in constant time)."""
    from fastapi import Depends, Header, HTTPException

    accepted = [key.get_secret_value().encode() for key in keys]

    # Headers, not Request: annotations here are strings (from __future__), and
    # FastAPI resolves them in this module, where fastapi's names aren't.
    async def require_api_key(
        authorization: str = Header("", include_in_schema=False),
        x_api_key: str = Header("", include_in_schema=False),
    ) -> None:
        scheme, _, token = authorization.partition(" ")
        given = x_api_key or (token.strip() if scheme.lower() == "bearer" else "")
        if not any(hmac.compare_digest(given.encode(), key) for key in accepted):
            raise HTTPException(
                401,
                "This API asks for a key: Authorization: Bearer <key>",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return Depends(require_api_key)


class AgentServer:
    """
    Builds an agent's web app. Each ``with_*`` method returns the builder;
    what a method doesn't set comes from :class:`AgentServerSettings`.

    :param settings: The settings; read from the environment and ``.env`` by default.
    """

    def __init__(self, settings: AgentServerSettings | None = None) -> None:
        self.settings = settings or AgentServerSettings()
        self._source: AgentSource | None = None
        self._models: ProviderModels | None = None
        self._config: ModelProviderConfig | None = None
        self._sessions: BaseSessionService | None = None
        self._artifacts: BaseArtifactService | None = None
        self._memory: BaseMemoryService | None = None
        self._services: dict[str, Any] = {}
        self._auth: list[Depends] = []
        self._routes: list[Any] = []
        self._lifespans: list[Lifespan] = []
        self._middleware: list[tuple[type, dict[str, Any]]] = []
        self._executor: AgentExecutor | None = None
        self._app: FastAPI | None = None

    # --- What it's made of ----------------------------------------------------

    def with_agent(self, agent: str | Path | ChatAgentDocument | AgentSource) -> Self:
        """The agent: its exported JSON (a file, or a folder of them), a document, or any source."""
        if isinstance(agent, (str, Path)):
            path = Path(agent)
            self._source = DirectoryAgentSource(path) if path.is_dir() else FileAgentSource(path)
        elif isinstance(agent, ChatAgentDocument):
            self._source = DocumentAgentSource(agent)
        else:
            self._source = agent
        return self

    def with_models(self, models: str | Path | ModelProviderConfig | ProviderModels) -> Self:
        """The models agents run on: a model_provider.yaml, its parsed config, or ProviderModels."""
        from forge_common.adk.models import ProviderModels
        from forge_common.model_provider import ModelProviderConfig

        if isinstance(models, ProviderModels):
            self._models = models
        elif isinstance(models, ModelProviderConfig):
            self._config = models
        else:
            self.settings = self.settings.model_copy(update={"model_provider_config": Path(models)})
        return self

    def with_sessions(self, sessions: str | BaseSessionService) -> Self:
        """
        Where conversations are kept: ``memory``, a database URL
        (``sqlite:///data/sessions.db``, ``postgresql://…``, ``mysql://…``), or a
        session service.
        """
        if isinstance(sessions, str):
            self.settings = self.settings.model_copy(update={"sessions": sessions})
            self._sessions = None
        else:
            self._sessions = sessions
        return self

    def with_artifacts(self, artifacts: str | Path | BaseArtifactService) -> Self:
        """
        Where files agents make and people send are kept: ``memory``, a folder,
        ``s3://bucket/prefix``, or an artifact service.
        """
        if isinstance(artifacts, (str, Path)):
            self.settings = self.settings.model_copy(update={"artifacts": str(artifacts)})
            self._artifacts = None
        else:
            self._artifacts = artifacts
        return self

    def with_memory(self, memory: str | BaseMemoryService) -> Self:
        """
        Long-term memory, for agents with a Memory tool: ``memory``, a MongoDB
        Atlas URI (searched by meaning, embedded by the model provider config's
        OpenAI provider; ``forge-agent-runtime[memory-atlas]``), or a memory service.
        """
        if isinstance(memory, str):
            if memory != "memory" and not memory.startswith(("mongodb://", "mongodb+srv://")):
                raise ValueError("Memory is kept in memory, or MongoDB Atlas (mongodb+srv://…)")
            uri = None if memory == "memory" else SecretStr(memory)
            self.settings = self.settings.model_copy(update={"memory_atlas_uri": uri})
            self._memory = None
        else:
            self._memory = memory
        return self

    def with_streaming(self, streaming: bool | None) -> Self:
        """Replies stream as the model writes them (True), or arrive whole (False); None: as asked."""
        self.settings = self.settings.model_copy(update={"streaming": streaming})
        return self

    def with_api_keys(self, *keys: str) -> Self:
        """
        Keys the API asks for: callers send one as ``Authorization: Bearer
        <key>`` (or ``X-API-Key``). For servers calling it, not browsers: a page
        can't keep a key secret.
        """
        given = [SecretStr(key.strip()) for key in keys if key.strip()]
        self.settings = self.settings.model_copy(update={"api_keys": [*self.settings.api_keys, *given]})
        return self

    def with_services(self, **services: Any) -> Self:
        """
        What agents may use beyond their JSON (:class:`RuntimeServices` fields):
        ``workflows``, ``mcp_servers``, ``knowledge_bases``, ``resolve_agent``,
        ``environment``, timeouts.
        """
        known = {f.name for f in fields(RuntimeServices)}
        unknown = sorted(set(services) - known)
        if unknown:
            raise TypeError(f"RuntimeServices has no {', '.join(unknown)}")
        self._services.update(services)
        return self

    def with_auth(self, *dependencies: Depends) -> Self:
        """FastAPI dependencies run before each run API route, e.g. one checking a token."""
        self._auth.extend(dependencies)
        return self

    def with_routes(self, *routers: Any) -> Self:
        """Your own routes (``APIRouter``), beside the agent's."""
        self._routes.extend(routers)
        return self

    def with_lifespan(self, *lifespans: Lifespan) -> Self:
        """What starts with the app and stops with it, in order (stopped in reverse)."""
        self._lifespans.extend(lifespans)
        return self

    def with_middleware(self, middleware: type, **options: Any) -> Self:
        """ASGI middleware, as ``app.add_middleware`` takes it; the first added is outermost."""
        self._middleware.append((middleware, options))
        return self

    def with_web(self, directory: str | Path) -> Self:
        """The built UI (``npm run build``'s dist), served at ``/``."""
        self.settings = self.settings.model_copy(update={"web_dir": Path(directory)})
        return self

    def with_cors(self, *origins: str) -> Self:
        """Origins a browser may call the API from, when the UI is served elsewhere."""
        self.settings = self.settings.model_copy(
            update={"cors_origins": [*self.settings.cors_origins, *origins]}
        )
        return self

    # --- Building ---------------------------------------------------------------

    @property
    def executor(self) -> AgentExecutor:
        """Builds and runs the agents; made on first use."""
        if self._executor is None:
            self._executor = self._make_executor()
        return self._executor

    def _make_executor(self) -> AgentExecutor:
        from google.adk.memory import InMemoryMemoryService
        from google.adk.sessions import DatabaseSessionService, InMemorySessionService

        from forge_common.adk.models import ProviderModels
        from forge_common.model_provider import default_model_provider_config_path, load_model_provider_config

        settings = self.settings
        # The environment wins over .env, as for the settings themselves.
        environment = {**read_dotenv(settings.env_file), **os.environ}
        source = self._source
        if source is None and settings.definition is not None:
            self.with_agent(settings.definition)
            source = self._source
        if source is None:
            raise ValueError("The server has no agent: call with_agent(), or set FORGE_AGENT_DEFINITION.")
        config = self._config
        if self._models is None and config is None:
            config = load_model_provider_config(
                settings.model_provider_config or default_model_provider_config_path(), environment
            )
        models = self._models or ProviderModels(config)
        sessions = self._sessions
        if sessions is None and settings.sessions == "memory":
            sessions = InMemorySessionService()
        elif sessions is None:
            url = database_url(settings.sessions)
            if url.startswith("sqlite+aiosqlite:///"):
                # SQLite makes the file, not the folder it's in.
                path = url.removeprefix("sqlite+aiosqlite:///").split("?", 1)[0]
                if path and path != ":memory:":
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
            sessions = DatabaseSessionService(db_url=url)
        artifacts = self._artifacts or artifact_service(
            settings.artifacts, environment, create_bucket=settings.artifacts_create_bucket
        )
        memory = self._memory
        if memory is None and settings.memory_atlas_uri is not None:
            from forge_common.adk.memory import AtlasVectorMemoryService, OpenAIEmbedder

            if config is None:
                raise ValueError("Atlas memory embeds with a model provider config: use with_models(path).")
            memory = AtlasVectorMemoryService.from_uri(
                settings.memory_atlas_uri.get_secret_value(),
                database=settings.memory_database,
                embedder=OpenAIEmbedder.from_provider(config, settings.embedding_provider),
            )
        services = RuntimeServices(
            environment=environment,
            allow_private=settings.allow_private,
            allowed_hosts=tuple(settings.allowed_hosts),
        ).but(**self._services)
        return AgentExecutor(
            source,
            models=models,
            sessions=sessions,
            artifacts=artifacts,
            memory=memory or InMemoryMemoryService(),
            services=services,
            streaming=settings.streaming,
        )

    def _kept(self) -> str:
        """Where it keeps what it keeps, and how it replies, for the log."""
        settings = self.settings
        sessions = (
            type(self._sessions).__name__
            if self._sessions is not None
            else "in memory"
            if settings.sessions == "memory"
            else _shown(database_url(settings.sessions))
        )
        artifacts = (
            type(self._artifacts).__name__
            if self._artifacts is not None
            else "in memory"
            if settings.artifacts == "memory"
            else settings.artifacts
        )
        memory = (
            type(self._memory).__name__
            if self._memory is not None
            else "MongoDB Atlas"
            if settings.memory_atlas_uri is not None
            else "in memory"
        )
        replies = {None: "as each request asks", True: "streamed", False: "whole"}[settings.streaming]
        return f"conversations {sessions}; files {artifacts}; memory {memory}; replies {replies}" + (
            "; API keys asked for" if settings.api_keys else ""
        )

    def agent(self) -> ChatAgentDocument | None:
        """The agent it serves, when it serves one (a file or a document); None for a folder."""
        source = self._source
        if isinstance(source, FileAgentSource):
            return source.document()
        if isinstance(source, DocumentAgentSource):
            return source.doc
        return None

    def build(self) -> FastAPI:
        """:return: The app (the same one each time it's asked)."""
        if self._app is not None:
            return self._app
        from fastapi import APIRouter, FastAPI, HTTPException

        from forge_agent_runtime.build import describe
        from forge_agent_runtime.server import create_router

        settings = self.settings
        executor = self.executor
        served = self.agent()
        prefix = settings.api_prefix.rstrip("/")

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            async with contextlib.AsyncExitStack() as stack:
                # The executor closes last: what the app's own lifespans use stays up until they stop.
                stack.push_async_callback(executor.close)
                if settings.prebuild and served is not None:
                    await executor.runner(str(AgentRef(served.id)))
                    log.info("Serving %s (%s) at %s/run_sse", served.name, served.id, prefix or "/")
                log.info("Keeping %s", self._kept())
                for each in self._lifespans:
                    await stack.enter_async_context(each(app))
                yield

        app = FastAPI(title=settings.title or (served.name if served else "Forge agents"), lifespan=lifespan)

        auth = list(self._auth)
        if settings.api_keys:
            auth.append(_api_key_check(settings.api_keys))
            if settings.web_dir is not None:
                log.warning("The API asks for a key, so the UI served here can't call it")
        agent_routes = APIRouter(dependencies=auth)

        @agent_routes.get("/agent")
        async def get_agent() -> dict[str, Any]:
            """The agent this server runs: its ID (the run API's appName), name, version and the state it takes."""
            doc = self.agent()
            if doc is None:
                raise HTTPException(404, "This server runs several agents: GET /apps/{id} for one.")
            return dict(describe(doc))

        app.include_router(agent_routes, prefix=prefix)
        app.include_router(create_router(executor, dependencies=auth), prefix=prefix)

        @app.get("/healthz", include_in_schema=False)
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        for router in self._routes:
            app.include_router(router)
        # add_middleware puts each outside the last: add in reverse so the first given is outermost.
        for middleware, options in reversed(self._middleware):
            app.add_middleware(middleware, **options)
        if settings.cors_origins:
            from starlette.middleware.cors import CORSMiddleware

            app.add_middleware(
                CORSMiddleware,
                allow_origins=settings.cors_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        web = settings.web_dir
        if web is not None:
            if (web / "index.html").is_file():
                # Last: every path the API and routes don't have is the UI's.
                app.mount("/", spa_files(web), name="web")
            else:
                log.warning("No built UI at %s (npm run build in web/); serving the API only", web)
        self._app = app
        return app

    def run(self, import_string: str = "main:app") -> None:
        """
        Serve the app with uvicorn, where the settings say.

        :param import_string: Where the app is, for ``reload``: uvicorn imports it again on each change.
        """
        import uvicorn

        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=self.settings.log_level.upper(), format="%(levelname)s %(name)s: %(message)s"
            )
        target: Any = import_string if self.settings.reload else self.build()
        uvicorn.run(
            target,
            host=self.settings.host,
            port=self.settings.port,
            reload=self.settings.reload,
            log_level=self.settings.log_level,
        )
