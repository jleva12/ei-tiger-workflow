from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from forge_admin.api.server import ApiServer
from forge_admin.config import Settings

router = APIRouter(prefix="/projects")


@router.get("")
async def list_projects() -> list[str]:
    return ["forge"]


class HeaderMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message["headers"], (b"x-test", b"1")]
            await send(message)

        await self.app(scope, receive, send_with_header)


def test_health_probes_and_info(settings: Settings) -> None:
    with TestClient(ApiServer(settings).create_app()) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        ready = client.get("/health/ready")
        info = client.get("/api/v1/info")
    # MySQL is unreachable in this fixture.
    assert ready.status_code == 503
    assert ready.json() == {"status": "unavailable"}
    assert info.json()["name"] == "forge-admin"


def test_routers_are_mounted_below_the_api_prefix(settings: Settings) -> None:
    server = ApiServer(settings, routers=[router])
    with TestClient(server.create_app()) as client:
        assert client.get("/api/v1/projects").json() == ["forge"]
        assert client.get("/projects").status_code == 404


def test_create_app_returns_the_same_app(settings: Settings) -> None:
    server = ApiServer(settings, routers=[router])
    assert server.create_app() is server.create_app()
    assert ApiServer(settings).create_app() is not server.create_app()


def test_lifespan_owns_the_engine(settings: Settings) -> None:
    app = ApiServer(settings).create_app()
    with TestClient(app):
        assert app.state.engine is not None
        assert app.state.sessionmaker is not None


def test_api_key_protects_api_routes_but_not_health(settings: Settings) -> None:
    protected = settings.model_copy(update={"api_key": SecretStr("secret-key")})
    server = ApiServer(protected, routers=[router])
    with TestClient(server.create_app()) as client:
        assert client.get("/api/v1/projects").status_code == 401
        wrong = client.get("/api/v1/projects", headers={"X-API-Key": "nope"})
        right = client.get("/api/v1/projects", headers={"X-API-Key": "secret-key"})
        live = client.get("/health/live")
    assert wrong.status_code == 401
    assert right.json() == ["forge"]
    assert live.status_code == 200


def test_openapi_documents_the_api_key(settings: Settings) -> None:
    schema = ApiServer(settings, routers=[router]).create_app().openapi()
    assert (
        schema["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-API-Key"
    )
    assert "/health/live" not in schema["paths"]


def test_docs_can_be_disabled(settings: Settings) -> None:
    hidden = settings.model_copy(update={"docs_enabled": False})
    with TestClient(ApiServer(hidden).create_app()) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_cors_and_custom_middleware(settings: Settings) -> None:
    configured = settings.model_copy(update={"cors_origins": ["http://localhost:5180"]})
    server = ApiServer(configured, middleware=[Middleware(HeaderMiddleware)])
    with TestClient(server.create_app()) as client:
        response = client.get(
            "/api/v1/info", headers={"Origin": "http://localhost:5180"}
        )
    assert response.headers["access-control-allow-origin"] == "http://localhost:5180"
    assert response.headers["x-test"] == "1"
