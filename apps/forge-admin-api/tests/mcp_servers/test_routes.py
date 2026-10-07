"""An organization's MCP servers' routes, without MySQL: the tables on
SQLite, the organization's access in memory (the default grants), servers'
tools listed by a stand-in, and OAuth against the fake authorization server.
What each route answers, what it keeps (encrypted), and what it refuses.

The people: an organization admin, a member and a viewer of the
organization, a member of another, and an outsider.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from casbin.persist.adapter import load_policy_line
from casbin.persist.adapters.asyncio import AsyncAdapter
from fastapi.testclient import TestClient
from forge_mcp_servers.client import Listing, McpConnectionError
from forge_mcp_servers.secrets import SecretBox
from forge_mcp_servers.service import McpServers
from forge_mcp_servers.testing import MCP, FakeOAuth
from google.adk.tools.mcp_tool import McpToolset
from pydantic import SecretStr
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.auth.authorization import get_enforcer, new_enforcer
from forge_admin.auth.tokens import mint_subject_token
from forge_admin.config import Settings
from forge_admin.db.base import Base
from forge_admin.db.session import get_session
from forge_admin.mcp_servers.toolset import McpServerGone, mcp_toolset
from forge_admin.models import McpOAuthFlow, McpServer, Organization, User

API = "/api/v1"
ORG = "3f6c0000-0000-4000-8000-000000000001"
OTHER_ORG = "3f6c0000-0000-4000-8000-000000000002"
ADMIN, MEMBER, VIEWER, NEIGHBOUR, OUTSIDER = (
    "admin-1",
    "member-1",
    "viewer-1",
    "neighbour-1",
    "outsider-1",
)
SERVERS = f"/organizations/{ORG}/mcp-servers"
CALLBACK = "http://localhost:5190/oauth/mcp/callback"

GRANTS = {
    "org:admin": ("organizations:read", "mcp_servers:manage"),
    "org:member": ("organizations:read", "mcp_servers:manage"),
    "org:viewer": ("organizations:read",),
}
ROLES = {
    ADMIN: ("org:admin", ORG),
    MEMBER: ("org:member", ORG),
    VIEWER: ("org:viewer", ORG),
    NEIGHBOUR: ("org:member", OTHER_ORG),
}


class Grants(AsyncAdapter):
    """The policy casbin_rule would hold: the default grants, and each
    person's role."""

    def __init__(self) -> None:
        self.lines = [
            f"p, {role}, {permission.replace(':', ', ')}"
            for role, permissions in GRANTS.items()
            for permission in permissions
        ] + [f"g, {user}, {role}, org:{org}*" for user, (role, org) in ROLES.items()]

    async def load_policy(self, model: Any) -> None:
        for line in self.lines:
            load_policy_line(line, model)

    async def save_policy(self, model: Any) -> bool:
        return True

    async def add_policy(self, sec: str, ptype: str, rule: list[str]) -> None:
        pass

    async def remove_policy(self, sec: str, ptype: str, rule: list[str]) -> None:
        pass

    async def remove_filtered_policy(
        self, sec: str, ptype: str, field_index: int, *field_values: str
    ) -> None:
        pass


@dataclass
class Lister:
    """Stands in for listing a server's tools: what it was sent, and what
    it answers (a listing, or an error to raise)."""

    answer: Listing | Exception = field(
        default_factory=lambda: Listing(
            tools=[
                {"name": "search", "title": "Search", "description": "Finds things."}
            ],
            server_info={"name": "Help center", "version": "2.0"},
        )
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self, url: str, *, headers: dict[str, str], seconds: float
    ) -> Listing:
        self.calls.append({"url": url, "headers": headers, "seconds": seconds})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@dataclass
class Workspace:
    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    servers: McpServers
    lister: Lister
    fake: FakeOAuth
    tokens: dict[str, str]

    def call(self, method: str, path: str, user: str = MEMBER, body: Any = None) -> Any:
        return self.client.request(
            method,
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self.tokens[user]}"},
            json=body,
        )

    def add(self, user: str = MEMBER, **fields: Any) -> dict[str, Any]:
        body = {
            "name": "Help center",
            "url": MCP,
            "auth": {"kind": "api_key", "secrets": {"key": "sk-123"}},
        } | fields
        made = self.call("POST", SERVERS, user, body)
        assert made.status_code == 201, made.json()
        return dict(made.json())

    def row(self, server_id: str) -> McpServer:
        async def get() -> McpServer:
            async with self.sessions() as session:
                found = await session.get(McpServer, server_id)
                assert found is not None
                return found

        assert self.client.portal is not None
        return self.client.portal.call(get)


def sqlite_tables() -> MetaData:
    """The tables the routes use, as SQLite takes them: without MySQL's
    microsecond CURRENT_TIMESTAMP(6) defaults (SQLAlchemy sets the times)."""
    tables = MetaData()
    for name in ("organizations", "users", "mcp_servers", "mcp_oauth_flows"):
        table = Base.metadata.tables[name].to_metadata(tables)
        for column in table.columns:
            column.server_default = None
    return tables


@pytest.fixture
def workspace(settings: Settings) -> Iterator[Workspace]:
    configured = settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 40), "local_user_id": None}
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    lister, fake = Lister(), FakeOAuth()
    servers = McpServers(SecretBox("k" * 40), lister=lister, transport=fake.transport)
    app.state.mcp_servers = servers
    enforcer = new_enforcer(Grants())

    async def session() -> AsyncIterator[AsyncSession]:
        async with sessions() as made:
            yield made

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_enforcer] = lambda: enforcer

    async def create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(sqlite_tables().create_all)
        async with sessions() as made:
            made.add_all(
                [
                    Organization(id=ORG, name="Acme", description=""),
                    Organization(id=OTHER_ORG, name="Globex", description=""),
                    User(
                        id=ADMIN,
                        first_name="Ada",
                        last_name="Admin",
                        email="ada@x.io",
                        msid="ada",
                    ),
                    User(
                        id=MEMBER,
                        first_name="Mia",
                        last_name="Member",
                        email="mia@x.io",
                        msid="mia",
                    ),
                ]
            )
            await made.commit()

    tokens = {
        user: mint_subject_token(configured, user, lifetime=timedelta(minutes=5))
        for user in (ADMIN, MEMBER, VIEWER, NEIGHBOUR, OUTSIDER)
    }
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(create)
        yield Workspace(client, sessions, servers, lister, fake, tokens)
        client.portal.call(engine.dispose)


# ------------------------------------------------------------ the methods


def test_the_auth_methods_are_listed_with_their_forms(workspace: Workspace) -> None:
    listed = workspace.call("GET", "/mcp-auth-methods")
    assert listed.status_code == 200
    methods = {method["kind"]: method for method in listed.json()}
    assert list(methods) == [
        "none",
        "api_key",
        "bearer",
        "oauth",
        "oauth_client_credentials",
    ]
    assert methods["oauth"]["interactive"] is True
    key = next(f for f in methods["api_key"]["fields"] if f["name"] == "key")
    assert key["secret"] is True and key["required"] is True


def test_the_default_servers_are_listed(workspace: Workspace) -> None:
    listed = workspace.call("GET", "/mcp-server-defaults")
    assert listed.status_code == 200
    explorer = next(s for s in listed.json() if s["id"] == "code-explorer")
    assert explorer["auth"]["kind"] == "bearer"
    assert explorer["url"].endswith("/mcp")
    assert explorer["auth"]["fields"]["token"]["label"] == "Organization API key"


# ----------------------------------------------------- adding and reading


def test_a_member_adds_a_server_whose_secrets_are_never_answered(
    workspace: Workspace,
) -> None:
    made = workspace.add(
        headers=[{"name": "X-Tenant", "value": "acme"}], description="Articles"
    )
    assert made["organization_id"] == ORG
    assert made["status"] == "unchecked" and made["tools"] == []
    assert made["auth"] == {
        "kind": "api_key",
        "settings": {"header": "X-API-Key", "scheme": ""},
        "secrets_set": ["key"],
        "interactive": False,
        "connected": None,
        "connected_by": None,
        "connected_by_name": None,
        "connected_at": None,
        "expires_at": None,
        "scope": None,
    }
    assert "sk-123" not in str(workspace.call("GET", SERVERS, VIEWER).json())
    # Encrypted where it's kept.
    sealed = workspace.row(made["id"]).auth_secrets
    assert sealed and "sk-123" not in sealed
    assert workspace.servers.box.open(sealed) == {"key": "sk-123"}


def test_only_the_organizations_people_read_its_servers(workspace: Workspace) -> None:
    made = workspace.add()
    assert [s["name"] for s in workspace.call("GET", SERVERS, VIEWER).json()] == [
        "Help center"
    ]
    assert workspace.call("GET", SERVERS, OUTSIDER).status_code == 403
    assert workspace.call("GET", SERVERS, NEIGHBOUR).status_code == 403
    # Not found through another organization.
    other = f"/organizations/{OTHER_ORG}/mcp-servers/{made['id']}"
    assert workspace.call("GET", other, NEIGHBOUR).status_code == 404


def test_only_those_who_manage_mcp_servers_change_them(workspace: Workspace) -> None:
    made = workspace.add()
    path = f"{SERVERS}/{made['id']}"
    body = {"name": "x", "url": MCP}
    assert workspace.call("POST", SERVERS, VIEWER, body).status_code == 403
    assert workspace.call("PATCH", path, VIEWER, {"name": "y"}).status_code == 403
    assert workspace.call("POST", f"{path}/check", VIEWER).status_code == 403
    assert workspace.call("DELETE", path, VIEWER).status_code == 403
    assert (
        workspace.call("PATCH", path, ADMIN, {"name": "Docs"}).json()["name"] == "Docs"
    )


def test_names_are_unique_in_an_organization(workspace: Workspace) -> None:
    workspace.add()
    again = workspace.call("POST", SERVERS, MEMBER, {"name": "Help center", "url": MCP})
    assert again.status_code == 409
    assert (
        again.json()["detail"]
        == "The organization already has an MCP server by that name"
    )


@pytest.mark.parametrize(
    ("auth", "message"),
    [
        ({"kind": "saml"}, "There's no auth method 'saml'"),
        ({"kind": "api_key"}, "secrets aren't right: key: Field required"),
        (
            {"kind": "api_key", "settings": {"header": "a b"}, "secrets": {"key": "k"}},
            "header",
        ),
        (
            {"kind": "bearer", "secrets": {"token": "t", "pin": "1"}},
            "has no secret pin",
        ),
    ],
)
def test_auth_a_method_doesnt_take_is_refused(
    workspace: Workspace, auth: dict[str, Any], message: str
) -> None:
    refused = workspace.call(
        "POST", SERVERS, MEMBER, {"name": "Help center", "url": MCP, "auth": auth}
    )
    assert refused.status_code == 422
    assert message in str(refused.json()["detail"])


def test_a_url_that_isnt_http_is_refused(workspace: Workspace) -> None:
    refused = workspace.call(
        "POST", SERVERS, MEMBER, {"name": "Local", "url": "file:///etc/passwd"}
    )
    assert refused.status_code == 422


# ------------------------------------------------------------- changing


def test_secrets_left_out_are_kept_and_a_new_method_needs_its_own(
    workspace: Workspace,
) -> None:
    made = workspace.add()
    path = f"{SERVERS}/{made['id']}"
    kept = workspace.call(
        "PATCH",
        path,
        MEMBER,
        {"auth": {"kind": "api_key", "settings": {"scheme": "Token"}}},
    )
    assert kept.status_code == 200, kept.json()
    assert kept.json()["auth"]["secrets_set"] == ["key"]
    assert workspace.servers.secrets(workspace.row(made["id"])) == {"key": "sk-123"}

    # Another method doesn't inherit the key.
    switched = workspace.call("PATCH", path, MEMBER, {"auth": {"kind": "bearer"}})
    assert switched.status_code == 422
    switched = workspace.call(
        "PATCH", path, MEMBER, {"auth": {"kind": "bearer", "secrets": {"token": "t-1"}}}
    )
    assert switched.json()["auth"]["secrets_set"] == ["token"]
    assert workspace.servers.secrets(workspace.row(made["id"])) == {"token": "t-1"}


def test_a_new_url_or_auth_leaves_a_checked_server_unchecked(
    workspace: Workspace,
) -> None:
    made = workspace.add()
    path = f"{SERVERS}/{made['id']}"
    assert workspace.call("POST", f"{path}/check").json()["status"] == "ok"
    renamed = workspace.call("PATCH", path, MEMBER, {"name": "Docs"}).json()
    assert renamed["status"] == "ok"
    moved = workspace.call("PATCH", path, MEMBER, {"url": f"{MCP}/v2"}).json()
    assert moved["status"] == "unchecked" and moved["checked_at"] is None


def test_a_deleted_server_is_gone(workspace: Workspace) -> None:
    made = workspace.add()
    path = f"{SERVERS}/{made['id']}"
    assert workspace.call("DELETE", path).status_code == 204
    assert workspace.call("GET", path).status_code == 404
    assert workspace.call("GET", SERVERS).json() == []


# -------------------------------------------------------------- checking


def test_a_check_connects_with_its_headers_and_keeps_its_tools(
    workspace: Workspace,
) -> None:
    made = workspace.add(
        headers=[{"name": "X-Tenant", "value": "acme"}], timeout_seconds=12
    )
    checked = workspace.call("POST", f"{SERVERS}/{made['id']}/check")
    assert checked.status_code == 200
    assert workspace.lister.calls == [
        {
            "url": MCP,
            "headers": {"X-Tenant": "acme", "X-API-Key": "sk-123"},
            "seconds": 12,
        }
    ]
    body = checked.json()
    assert body["status"] == "ok" and body["last_error"] is None
    assert body["tools"] == [
        {"name": "search", "title": "Search", "description": "Finds things."}
    ]
    assert body["server_info"] == {"name": "Help center", "version": "2.0"}
    assert body["checked_at"]


def test_a_server_that_fails_its_check_says_why(workspace: Workspace) -> None:
    made = workspace.add()
    workspace.lister.answer = McpConnectionError(
        "The server answered 401: it refused the credentials", status=401
    )
    checked = workspace.call("POST", f"{SERVERS}/{made['id']}/check").json()
    assert checked["status"] == "error"
    assert (
        checked["last_error"] == "The server answered 401: it refused the credentials"
    )


# ----------------------------------------------------------------- OAuth


def oauth_server(workspace: Workspace) -> dict[str, Any]:
    return workspace.add(name="Linear", auth={"kind": "oauth"})


def test_an_oauth_server_is_unconnected_until_someone_signs_in(
    workspace: Workspace,
) -> None:
    made = oauth_server(workspace)
    assert made["auth"]["interactive"] is True
    assert made["auth"]["connected"] is False
    checked = workspace.call("POST", f"{SERVERS}/{made['id']}/check").json()
    assert checked["status"] == "needs_auth"
    assert workspace.lister.calls == []


def test_signing_in_connects_the_server_and_checks_it(workspace: Workspace) -> None:
    made = oauth_server(workspace)
    path = f"{SERVERS}/{made['id']}"
    started = workspace.call("POST", f"{path}/oauth/start")
    assert started.status_code == 200, started.json()
    url = started.json()["authorization_url"]
    assert url.startswith("https://auth.example.com/authorize?")
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A5190%2Foauth%2Fmcp%2Fcallback" in url
    state = url.split("state=")[1].split("&")[0]

    code = workspace.fake.authorize(url)
    # Only who began it may finish it.
    stranger = workspace.call(
        "POST", "/mcp-oauth/callback", ADMIN, {"state": state, "code": code}
    )
    assert stranger.status_code == 400

    finished = workspace.call(
        "POST", "/mcp-oauth/callback", MEMBER, {"state": state, "code": code}
    )
    assert finished.status_code == 200, finished.json()
    body = finished.json()
    assert body["id"] == made["id"]
    assert body["auth"]["connected"] is True
    assert body["auth"]["connected_by"] == MEMBER
    assert body["auth"]["connected_by_name"] == "Mia Member"
    assert body["auth"]["scope"] == "tools:read"
    assert body["status"] == "ok"
    assert workspace.lister.calls[-1]["headers"] == {"Authorization": "Bearer access-1"}
    # The tokens are kept encrypted.
    grant = workspace.row(made["id"]).auth_grant
    assert grant and "access-1" not in grant

    # Once.
    again = workspace.call(
        "POST", "/mcp-oauth/callback", MEMBER, {"state": state, "code": code}
    )
    assert again.status_code == 400

    disconnected = workspace.call("DELETE", f"{path}/oauth").json()
    assert disconnected["auth"]["connected"] is False
    assert disconnected["status"] == "unchecked"


def test_a_sign_in_the_person_declined_says_so(workspace: Workspace) -> None:
    made = oauth_server(workspace)
    url = workspace.call("POST", f"{SERVERS}/{made['id']}/oauth/start").json()[
        "authorization_url"
    ]
    state = url.split("state=")[1].split("&")[0]
    declined = workspace.call(
        "POST",
        "/mcp-oauth/callback",
        MEMBER,
        {
            "state": state,
            "error": "access_denied",
            "error_description": "The user said no",
        },
    )
    assert declined.status_code == 400
    assert declined.json()["detail"] == "The sign-in didn't finish: The user said no"


def test_a_sign_in_that_expired_isnt_finished(workspace: Workspace) -> None:
    made = oauth_server(workspace)
    url = workspace.call("POST", f"{SERVERS}/{made['id']}/oauth/start").json()[
        "authorization_url"
    ]
    state = url.split("state=")[1].split("&")[0]

    async def expire() -> None:
        async with workspace.sessions() as session:
            flow = await session.scalar(select(McpOAuthFlow))
            assert flow is not None
            flow.expires_at = flow.expires_at - timedelta(hours=1)
            await session.commit()

    assert workspace.client.portal is not None
    workspace.client.portal.call(expire)
    late = workspace.call(
        "POST", "/mcp-oauth/callback", MEMBER, {"state": state, "code": "code-0"}
    )
    assert late.status_code == 400
    assert "expired" in late.json()["detail"]


def test_only_oauth_servers_are_signed_in_to(workspace: Workspace) -> None:
    made = workspace.add()
    refused = workspace.call("POST", f"{SERVERS}/{made['id']}/oauth/start")
    assert refused.status_code == 422
    assert "isn't connected by signing in" in refused.json()["detail"]


def test_without_a_secrets_key_mcp_servers_arent_set_up(workspace: Workspace) -> None:
    workspace.client.app.state.mcp_servers = None  # type: ignore[attr-defined]
    answered = workspace.call("GET", SERVERS)
    assert answered.status_code == 503
    assert "FORGE_ADMIN_SECRETS_KEY" in answered.json()["detail"]


# ---------------------------------------------------------------- agents


def test_an_agent_gets_a_server_as_a_toolset_with_fresh_headers(
    workspace: Workspace,
) -> None:
    made = workspace.add(headers=[{"name": "X-Tenant", "value": "acme"}])

    async def build() -> tuple[McpToolset, dict[str, str]]:
        toolset = await mcp_toolset(
            workspace.servers,
            workspace.sessions,
            organization_id=ORG,
            config={"server": made["id"], "tools": "search, fetch", "confirm": True},
        )
        headers = await toolset._header_provider(None)  # type: ignore[misc]
        return toolset, headers

    toolset, headers = asyncio.run(build())
    assert toolset.tool_filter == ["search", "fetch"]
    assert headers == {"X-Tenant": "acme", "X-API-Key": "sk-123"}

    with pytest.raises(McpServerGone):
        asyncio.run(
            mcp_toolset(
                workspace.servers,
                workspace.sessions,
                organization_id=OTHER_ORG,
                config={"server": made["id"]},
            )
        )
