"""``forge-codegraph-mcp``: the server process (also ``python -m forge_codegraph_mcp``).

Takes no arguments; it is configured through the environment and its .env
(core/settings.py). The MCP endpoint is at ``mcp.path`` (``/mcp``), beside
``/health/live`` and ``/health/ready``.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from forge_common.logging import configure_logging, get_logger

from forge_codegraph_mcp.access import ForgeAccessVerifier
from forge_codegraph_mcp.core.settings import Settings, get_settings
from forge_codegraph_mcp.graph.embedding import Embedder, OpenAIEmbedder
from forge_codegraph_mcp.graph.spanner import SpannerGraphStore, VectorLengthMismatch
from forge_codegraph_mcp.routes import HealthRoutes
from forge_codegraph_mcp.server import ServerBuilder
from forge_codegraph_mcp.tools import CodeGraphTools

log = get_logger(__name__)


def open_store(settings: Settings) -> SpannerGraphStore:
    """
    Open and configure a Spanner-based graph store using the provided settings.

    This function initializes and returns a `SpannerGraphStore` instance using the
    details encapsulated within the given `Settings` object. It ensures that all
    necessary configurations, including the database, scope, cursor signing key,
    vector length, and credentials, are properly set up.

    :param settings: The configuration object containing the necessary parameters
        to initialize the Spanner graph store. Must include the Spanner-specific
        settings such as database, scope, cursor signing key, and optional
        credentials and timeout.
    :type settings: Settings
    :return: A configured and initialized instance of `SpannerGraphStore`.
    :rtype: SpannerGraphStore
    """
    spanner = settings.spanner
    assert spanner.cursor_signing_key is not None  # checked by Settings
    return SpannerGraphStore(
        spanner.database,
        scope=spanner.scope,
        cursor_signing_key=spanner.cursor_signing_key.get_secret_value().encode(),
        vector_length=settings.embedding.vector_length,
        credentials_json=(
            spanner.credentials_json.get_secret_value() if spanner.credentials_json else None
        ),
        timeout=spanner.timeout,
    )


def open_embedder(settings: Settings) -> OpenAIEmbedder | None:
    """
    Initialize and return an instance of OpenAIEmbedder if embedding is enabled
    in the provided settings. If embedding is disabled, it logs a warning and
    returns None. Also ensures that an API key is present for embedding.

    :param settings: The configuration settings that specify embedding parameters.
    :type settings: Settings
    :return: An instance of OpenAIEmbedder if embedding is enabled, otherwise None.
    :rtype: OpenAIEmbedder | None
    """
    embedding = settings.embedding
    if not embedding.enabled:
        if embedding.model:
            log.warning(
                "embedding.disabled",
                detail="an embedding model without an API key: search stays lexical",
            )
        return None
    assert embedding.api_key is not None
    return OpenAIEmbedder(
        embedding.api_key.get_secret_value(),
        embedding.model,
        embedding.vector_length,
        base_url=embedding.base_url,
        max_input_bytes=embedding.max_input_bytes,
    )


async def wait_for_graph(store: SpannerGraphStore, patience: float) -> None:
    """
    Waits for a SpannerGraphStore to respond to a ping request until a specified
    deadline or an exception is raised if the operation fails.

    This function attempts to ping a SpannerGraphStore until it successfully
    responds or the patience deadline is exceeded. If the `VectorLengthMismatch`
    exception is raised, it re-raises the exception immediately. Any other
    exceptions are logged, and the function retries until the deadline.

    :param store: The SpannerGraphStore instance to ping.
    :type store: SpannerGraphStore
    :param patience: The maximum allowed time in seconds to wait for a response
        before giving up.
    :type patience: float
    :return: This function does not return a value. If the operation is
        successful within the deadline, it exits silently.
    :rtype: None
    :raises VectorLengthMismatch: Raised if this specific exception occurs
        during ping attempts.
    :raises Exception: Raised if the patience deadline is exceeded.
    """
    deadline = time.monotonic() + patience
    while True:
        try:
            await store.ping()
            return
        except VectorLengthMismatch:
            raise
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise
            log.warning("graph.waiting", database=store.database, error=str(exc)[:200])
            await asyncio.sleep(2)


def graph_lifespan(
    store: SpannerGraphStore, embedder: OpenAIEmbedder | None, startup_timeout: float
) -> Any:
    """
    Creates a lifespan context manager for a FastAPI application, managing the setup and
    teardown of the graph database and optional embedder. This ensures that the application
    only starts when the graph database is ready and properly configured, and handles
    proper resource cleanup on shutdown.

    :param store: The graph store to manage, implementing interactions with the database.
    :param embedder: An optional embedder to manage for semantic operations.
    :param startup_timeout: The maximum time in seconds to wait for the database to become
        ready during startup.
    :return: An asynchronous context manager for FastAPI lifespan handling.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Fails startup when the database never shows up or was made for
        # another embedding dimension.
        await wait_for_graph(store, startup_timeout)
        log.info(
            "graph.ready",
            database=store.database,
            vector_length=store.vector_length,
            semantic=embedder is not None,
        )
        try:
            yield
        finally:
            await store.close()
            if embedder is not None:
                await embedder.close()

    return lifespan


def closing(verifier: ForgeAccessVerifier) -> Any:
    """
    This function creates an asynchronous context manager for managing the lifespan
    of an application in a FastAPI environment. It uses the provided verifier to
    perform asynchronous cleanup upon shutdown.

    :param verifier: An instance of `ForgeAccessVerifier` used to perform specific
        cleanup operations when the application shuts down.
    :return: An asynchronous context manager that yields control to manage the
        application's lifespan.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await verifier.aclose()

    return lifespan


def build_server(settings: Settings | None = None) -> ServerBuilder:
    """
    Builds and initializes a server using provided or default settings.

    This function constructs a server by leveraging the given settings or default
    settings if none are provided. It configures logging, sets up storage, embeds
    semantic tools, and integrates various server components such as authentication
    (Forge credentials checked with the admin API), lifespan management, health
    routes, and toolsets.

    :param settings: Optional instance of `Settings`. If not provided, default
        settings are used.
    :return: An instance of `ServerBuilder` pre-configured with the necessary
        components based on the provided or default settings.
    :rtype: ServerBuilder
    """
    settings = settings or get_settings()
    configure_logging(settings.logging)
    store = open_store(settings)
    embedder = open_embedder(settings)
    semantic: Embedder | None = embedder
    verifier = ForgeAccessVerifier(settings.mcp.auth)
    return (
        ServerBuilder(settings)
        # Forge credentials (an organization's API key, or a token minted for
        # one), checked with the admin API: each caller reads only its
        # organization's repositories (access.py).
        .with_auth(verifier)
        .with_lifespan(
            graph_lifespan(store, embedder, settings.spanner.startup_timeout),
            closing(verifier),
        )
        .with_routes(HealthRoutes(settings, checks={"spanner": store.live}))
        .with_toolsets(CodeGraphTools(settings, store, semantic))
    )  # fmt: skip


def create_app() -> FastAPI:
    """
    Builds and returns an instance of the FastAPI application. This function sets up and
    initializes the FastAPI server, utilizing additional configurations or components
    provided by the builder.

    :return: An instance of FastAPI application.
    :rtype: FastAPI
    """
    return build_server().build()


def main() -> None:
    build_server().run("forge_codegraph_mcp.main:create_app", factory=True)


if __name__ == "__main__":
    main()
