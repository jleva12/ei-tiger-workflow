"""The HTTP surface: probes, and the MCP endpoint behind Forge credentials,
which the admin API checks."""

from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx
import pytest
from conftest import MCP_HEADERS, REPO, SHOP_URL, Shop, make_settings
from fastapi.testclient import TestClient

from forge_codegraph_mcp.access import ForgeAccessVerifier
from forge_codegraph_mcp.core.settings import Settings
from forge_codegraph_mcp.main import build_server
from forge_codegraph_mcp.routes import HealthRoutes
from forge_codegraph_mcp.server import ServerBuilder
from forge_codegraph_mcp.tools import CodeGraphTools

ORG = "3f6c0000-0000-4000-8000-000000000001"
KEY = "fk_" + "a" * 43
CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "repository_state", "arguments": {"repository": REPO}},
}


class Admin:
    """The admin API's GET /code-graph/access, as the tests have it answer."""

    def __init__(self) -> None:
        self.answers: dict[str, httpx.Response] = {}
        self.asked: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        credential = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return self.answers.get(credential) or httpx.Response(
            401, json={"detail": "Unknown API key"}
        )

    def reads(self, credential: str, *urls: str) -> None:
        self.answers[credential] = httpx.Response(
            200,
            json={
                "subject": "apikey:k1",
                "organization_id": ORG,
                "repositories": [
                    {"url": url, "owner": "acme", "name": "x", "branch": "main"} for url in urls
                ],
            },
        )


@pytest.fixture
def admin() -> Admin:
    return Admin()


@pytest.fixture
def serve(shop: Shop, admin: Admin) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(
        settings: Settings | None = None,
        ready: Callable[[], Awaitable[Any]] | None = None,
    ) -> TestClient:
        settings = settings or make_settings()

        async def ok() -> None:
            return None

        verifier = ForgeAccessVerifier(
            settings.mcp.auth, transport=httpx.MockTransport(admin.handle)
        )
        app = (
            ServerBuilder(settings)
            .with_auth(verifier)
            .with_routes(HealthRoutes(settings, checks={"spanner": ready or ok}))
            .with_toolsets(CodeGraphTools(settings, shop.store, None))
            .build()
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def bearer(credential: str) -> dict[str, str]:
    return {**MCP_HEADERS, "Authorization": f"Bearer {credential}"}


def test_probes(serve: Callable[..., TestClient]) -> None:
    client = serve()
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["checks"] == {"spanner": "ok"}

    async def down() -> None:
        raise ConnectionError("no spanner")

    unready = serve(ready=down).get("/health/ready")
    assert unready.status_code == 503 and unready.json()["checks"] == {"spanner": "unavailable"}


def test_a_key_of_an_organization_with_the_repository_reads_it(
    serve: Callable[..., TestClient], admin: Admin
) -> None:
    admin.reads(KEY, SHOP_URL)
    response = serve().post("/mcp", json=CALL, headers=bearer(KEY))
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["structuredContent"]["repository_id"] == REPO
    assert response.headers["x-request-id"]
    # The credential went to the admin API as it came.
    [asked] = admin.asked
    assert asked.url.path == "/api/v1/code-graph/access"
    assert asked.headers["Authorization"] == f"Bearer {KEY}"
    assert "X-API-Key" not in asked.headers


def test_another_organizations_repository_isnt_there(
    serve: Callable[..., TestClient], admin: Admin
) -> None:
    admin.reads(KEY, "https://github.com/globex/radar")
    response = serve().post("/mcp", json=CALL, headers=bearer(KEY))
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == f"not found: repository {REPO}"


def test_credentials_the_admin_api_refuses_are_refused(
    serve: Callable[..., TestClient], admin: Admin
) -> None:
    client = serve()
    assert client.post("/mcp", json=CALL, headers=MCP_HEADERS).status_code == 401
    assert client.post("/mcp", json=CALL, headers=bearer("fk_unknown")).status_code == 401
    # A credential without repositories:read, or naming no organization.
    admin.answers["token"] = httpx.Response(403, json={"detail": "Requires repositories:read"})
    refused = client.post("/mcp", json=CALL, headers=bearer("token"))
    assert refused.status_code == 403
    assert "insufficient_scope" in refused.headers["www-authenticate"]
    # The probes stay open.
    assert client.get("/health/ready").status_code == 200


def test_the_answer_is_kept_for_a_while(serve: Callable[..., TestClient], admin: Admin) -> None:
    admin.reads(KEY, SHOP_URL)
    client = serve()
    for _ in range(3):
        assert client.post("/mcp", json=CALL, headers=bearer(KEY)).status_code == 200
    assert len(admin.asked) == 1
    uncached = serve(make_settings(mcp={"auth": {"cache_seconds": 0}}))
    for _ in range(2):
        assert uncached.post("/mcp", json=CALL, headers=bearer(KEY)).status_code == 200
    assert len(admin.asked) == 3


def test_without_the_admin_api_the_endpoint_is_unavailable(
    serve: Callable[..., TestClient], admin: Admin
) -> None:
    admin.answers[KEY] = httpx.Response(500)
    client = serve()
    response = client.post("/mcp", json=CALL, headers=bearer(KEY))
    assert response.status_code == 503
    assert response.json()["error"] == "temporarily_unavailable"
    # Nothing was kept: once it answers, the key works.
    admin.reads(KEY, SHOP_URL)
    assert client.post("/mcp", json=CALL, headers=bearer(KEY)).status_code == 200


def test_the_deployment_key_goes_with_every_question(
    serve: Callable[..., TestClient], admin: Admin
) -> None:
    admin.reads(KEY, SHOP_URL)
    settings = make_settings(
        mcp={"auth": {"admin_url": "http://admin:8091/api/v1/", "admin_api_key": "deploy"}}
    )
    assert serve(settings).post("/mcp", json=CALL, headers=bearer(KEY)).status_code == 200
    [asked] = admin.asked
    assert str(asked.url) == "http://admin:8091/api/v1/code-graph/access"
    assert asked.headers["X-API-Key"] == "deploy"


def test_build_server_registers_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    # The emulator's address keeps the client off Google's credentials; nothing
    # is read until the app starts.
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "localhost:19030")
    # Without entering the client, the lifespan (and its database check) doesn't run.
    client = TestClient(build_server(make_settings()).build())
    assert client.get("/health/live").json() == {"status": "ok"}
    assert {"/health/live", "/health/ready"} <= set(client.get("/openapi.json").json()["paths"])


async def test_startup_waits_for_the_workers_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from forge_codegraph_mcp import main
    from forge_codegraph_mcp.graph.spanner import VectorLengthMismatch

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)

    class Store:
        database = "projects/p/instances/i/databases/d"

        def __init__(self, failures: list[Exception]) -> None:
            self.failures = failures
            self.pings = 0

        async def ping(self) -> None:
            self.pings += 1
            if self.failures:
                raise self.failures.pop(0)

    # Not there yet, then there: startup goes on.
    store = Store([ConnectionError("no database"), ConnectionError("no schema")])
    await main.wait_for_graph(store, 60)  # type: ignore[arg-type]
    assert store.pings == 3
    # A database made for other embeddings never will be.
    store = Store([VectorLengthMismatch("3072, configured 1024")])
    with pytest.raises(VectorLengthMismatch):
        await main.wait_for_graph(store, 60)  # type: ignore[arg-type]
    assert store.pings == 1
    # Out of patience, the last failure stops startup.
    store = Store([ConnectionError("down")] * 3)
    with pytest.raises(ConnectionError):
        await main.wait_for_graph(store, 0)  # type: ignore[arg-type]
