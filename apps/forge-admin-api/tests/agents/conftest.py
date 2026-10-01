"""API clients whose agents run on a scripted model (scripted_llm.py)."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.adk.apps import App
from google.adk.artifacts import InMemoryArtifactService
from google.adk.sessions import InMemorySessionService
from scripted_llm import USER

from forge_admin.agents.person import PersonLookup
from forge_admin.agents.runtime import AgentRuntime
from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.config import Settings


@pytest.fixture
def agent_client(settings: Settings) -> Iterator[Callable[..., TestClient]]:
    """
    Make API clients, acting as ``USER``, whose agents are the given ADK apps
    with in-memory sessions and artifacts.
    """
    clients: list[TestClient] = []

    def make(
        *apps: App,
        unavailable: str | None = None,
        people: PersonLookup | None = None,
        **overrides: Any,
    ) -> TestClient:
        configured = settings.model_copy(update={"local_user_id": USER, **overrides})
        app = ApiServer(configured, routers=ROUTERS).create_app()
        app.state.agents = AgentRuntime.of(
            apps,
            sessions=InMemorySessionService(),
            artifacts=InMemoryArtifactService(),
            unavailable=unavailable,
            people=people,
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)
