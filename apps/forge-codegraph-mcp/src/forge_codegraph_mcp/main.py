"""``forge-codegraph-mcp``: the server process (also ``python -m forge_codegraph_mcp``).

Takes no arguments; it is configured through the environment and its .env
(core/settings.py). The MCP endpoint is at ``mcp.path`` (``/mcp``), beside
``/health/live`` and ``/health/ready``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from forge_codegraph_query import CodeGraph
from forge_common.logging import configure_logging, get_logger

from forge_codegraph_mcp.access import ForgeAccessVerifier
from forge_codegraph_mcp.core.settings import Settings, get_settings
from forge_codegraph_mcp.routes import HealthRoutes
from forge_codegraph_mcp.server import ServerBuilder
from forge_codegraph_mcp.tools import CodeGraphTools

log = get_logger(__name__)


def graph_lifespan(graph: CodeGraph, settings: Settings) -> Any:
    """
    The code graph for the server's lifetime: startup waits for the worker's
    database (failing when it never shows up or was made for another
    embedding dimension), and shutdown closes it.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await graph.wait(settings.spanner.startup_timeout)
        log.info(
            "graph.ready",
            database=settings.spanner.database,
            vector_length=settings.embedding.vector_length,
            semantic=graph.semantic,
        )
        try:
            yield
        finally:
            await graph.close()

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
    # The graph as every app reads it (forge_codegraph_query).
    graph = CodeGraph.open(settings.codegraph)
    verifier = ForgeAccessVerifier(settings.mcp.auth)
    return (
        ServerBuilder(settings)
        # Forge credentials (an organization's API key, or a token minted for
        # one), checked with the admin API: each caller reads only its
        # organization's repositories (access.py).
        .with_auth(verifier)
        .with_lifespan(graph_lifespan(graph, settings), closing(verifier))
        .with_routes(HealthRoutes(settings, checks={"spanner": graph.live}))
        .with_toolsets(CodeGraphTools(settings, graph))
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
