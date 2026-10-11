"""An organization's API credentials' routes, without MySQL: the tables on
SQLite, the organization's access in memory (the default grants), OAuth
against a fake token endpoint. What each route answers, what it keeps
(encrypted), and what it refuses; how the hosted runtime signs a tool's
request with one; and the OpenAPI spec route that lists a spec's operations.

The people: an organization admin, a member and a viewer of the
organization, a member of another, and an outsider.
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from casbin.persist.adapter import load_policy_line
from casbin.persist.adapters.asyncio import AsyncAdapter
from fastapi.testclient import TestClient
from forge_mcp_servers import ApiCredentials, CredentialUnavailable
from forge_mcp_servers.secrets import SecretBox
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
from forge_admin.api_credentials import OrganizationCredentials, tool_environment
from forge_admin.auth.authorization import get_enforcer, new_enforcer
from forge_admin.auth.tokens import mint_subject_token
from forge_admin.config import Settings
from forge_admin.db.base import Base
from forge_admin.db.session import get_session
from forge_admin.models import (
    ApiCredential,
    ApiCredentialOAuthFlow,
    Organization,
    User,
)

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
CREDENTIALS = f"/organizations/{ORG}/credentials"
ORDERS = "https://api.example.com/v1"
TOKEN_URL = "https://auth.example.com/oauth/token"

GRANTS = {
    "org:admin": ("organizations:read", "credentials:manage", "agents:manage"),
    "org:member": ("organizations:read", "credentials:manage", "agents:manage"),
    "org:viewer": ("organizations:read",),
}
ROLES = {
    ADMIN: ("org:admin", ORG),
    MEMBER: ("org:member", ORG),
    VIEWER: ("org:viewer", ORG),
    NEIGHBOUR: ("org:member", OTHER_ORG),
}

BILLING_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Billing", "version": "2"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/invoices/{id}": {
            "get": {
                "operationId": "getInvoice",
                "summary": "One invoice",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/charges": {
            "get": {
                "operationId": "listCharges",
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
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


class TokenEndpoint:
    """An authorization server's token endpoint: a new access token each time."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if str(request.url) != TOKEN_URL:
            return httpx2.Response(404)
        issued = len(self.requests)
        return httpx2.Response(
            200,
            json={
                "access_token": f"access-{issued}",
                "refresh_token": f"refresh-{issued}",
                "expires_in": 3600,
                "scope": "orders",
            },
        )


@dataclass
class Workspace:
    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    credentials: ApiCredentials
    tokens: dict[str, str]
    endpoint: TokenEndpoint
    spec_requests: list[httpx.Request]

    def call(self, method: str, path: str, user: str = MEMBER, body: Any = None) -> Any:
        return self.client.request(
            method,
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self.tokens[user]}"},
            json=body,
        )

    def add(self, user: str = MEMBER, **fields: Any) -> dict[str, Any]:
        body = {
            "name": "Orders API",
            "urls": [ORDERS],
            "auth": {"kind": "bearer", "secrets": {"token": "t0k-123"}},
        } | fields
        made = self.call("POST", CREDENTIALS, user, body)
        assert made.status_code == 201, made.json()
        return dict(made.json())

    def row(self, credential_id: str) -> ApiCredential:
        async def get() -> ApiCredential:
            async with self.sessions() as session:
                found = await session.get(ApiCredential, credential_id)
                assert found is not None
                return found

        assert self.client.portal is not None
        return self.client.portal.call(get)

    def run(self, work: Any) -> Any:
        assert self.client.portal is not None
        return self.client.portal.call(work)


def sqlite_tables() -> MetaData:
    """The tables the routes use, as SQLite takes them: without MySQL's
    microsecond CURRENT_TIMESTAMP(6) defaults (SQLAlchemy sets the times)."""
    tables = MetaData()
    for name in (
        "organizations",
        "users",
        "api_credentials",
        "api_credential_oauth_flows",
    ):
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
    endpoint = TokenEndpoint()
    credentials = ApiCredentials(
        SecretBox("k" * 40), transport=httpx2.MockTransport(endpoint.handle)
    )
    app.state.api_credentials = credentials
    spec_requests: list[httpx.Request] = []

    def serve_spec(request: httpx.Request) -> httpx.Response:
        spec_requests.append(request)
        if request.url.path == "/v1/openapi.json":
            return httpx.Response(200, json=BILLING_SPEC)
        return httpx.Response(404)

    app.state.spec_transport = httpx.MockTransport(serve_spec)
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
        yield Workspace(client, sessions, credentials, tokens, endpoint, spec_requests)
        client.portal.call(engine.dispose)


# ------------------------------------------------------------ the methods


def test_the_methods_are_listed_with_their_forms(workspace: Workspace) -> None:
    listed = workspace.call("GET", "/credential-auth-methods")
    assert listed.status_code == 200
    methods = {method["kind"]: method for method in listed.json()}
    assert list(methods) == [
        "api_key",
        "api_key_query",
        "bearer",
        "basic",
        "oauth_client_credentials",
        "oauth",
    ]
    assert methods["oauth"]["interactive"] is True
    fields = {
        field["name"]: field for field in methods["oauth_client_credentials"]["fields"]
    }
    assert fields["client_secret"]["secret"] is True
    assert fields["client_authentication"]["choices"] == ["basic", "body"]


# ------------------------------------------------------------ credentials


def test_a_member_adds_a_credential_whose_secrets_are_never_answered(
    workspace: Workspace,
) -> None:
    made = workspace.add(description="Orders, read only")
    assert made["name"] == "Orders API"
    assert made["urls"] == [ORDERS]
    assert made["auth"] == {
        "kind": "bearer",
        "settings": {},
        "secrets_set": ["token"],
        "interactive": False,
        "connected": None,
        "connected_by": None,
        "connected_by_name": None,
        "connected_at": None,
        "expires_at": None,
        "scope": None,
    }
    assert "t0k-123" not in str(made)
    # Encrypted at rest.
    secrets = workspace.row(made["id"]).auth_secrets
    assert secrets and "t0k-123" not in secrets
    listed = workspace.call("GET", CREDENTIALS, VIEWER).json()
    assert [credential["id"] for credential in listed] == [made["id"]]


def test_only_the_organizations_people_read_its_credentials(
    workspace: Workspace,
) -> None:
    made = workspace.add()
    for user in (NEIGHBOUR, OUTSIDER):
        assert workspace.call("GET", CREDENTIALS, user).status_code == 403
        assert (
            workspace.call("GET", f"{CREDENTIALS}/{made['id']}", user).status_code
            == 403
        )


def test_only_those_who_manage_credentials_change_them(workspace: Workspace) -> None:
    made = workspace.add()
    path = f"{CREDENTIALS}/{made['id']}"
    assert workspace.call("PATCH", path, VIEWER, {"name": "X"}).status_code == 403
    assert workspace.call("DELETE", path, VIEWER).status_code == 403
    refused = workspace.call(
        "POST",
        CREDENTIALS,
        VIEWER,
        {
            "name": "B",
            "urls": [ORDERS],
            "auth": {"kind": "bearer", "secrets": {"token": "x"}},
        },
    )
    assert refused.status_code == 403


def test_names_are_unique_and_urls_say_where_it_may_go(workspace: Workspace) -> None:
    workspace.add()
    taken = workspace.call(
        "POST",
        CREDENTIALS,
        body={
            "name": "Orders API",
            "urls": [ORDERS],
            "auth": {"kind": "bearer", "secrets": {"token": "x"}},
        },
    )
    assert taken.status_code == 409
    for urls in (
        [],
        ["ftp://x.io"],
        ["https://api.example.com/v1?key=1"],
        ["not a url"],
    ):
        refused = workspace.call(
            "POST",
            CREDENTIALS,
            body={
                "name": "Other",
                "urls": urls,
                "auth": {"kind": "bearer", "secrets": {"token": "x"}},
            },
        )
        assert refused.status_code == 422, urls


@pytest.mark.parametrize(
    ("auth", "says"),
    [
        ({"kind": "none"}, "There's no auth method 'none'"),
        ({"kind": "bearer"}, "token"),
        ({"kind": "bearer", "secrets": {"key": "x"}}, "Bearer token has no secret key"),
        ({"kind": "basic", "settings": {}}, "username"),
        (
            {
                "kind": "oauth_client_credentials",
                "settings": {"client_id": "c"},
                "secrets": {"client_secret": "s"},
            },
            "token_url",
        ),
    ],
)
def test_auth_a_method_doesnt_take_is_refused(
    workspace: Workspace, auth: dict[str, Any], says: str
) -> None:
    refused = workspace.call(
        "POST", CREDENTIALS, body={"name": "X", "urls": [ORDERS], "auth": auth}
    )
    assert refused.status_code == 422
    assert says in str(refused.json()["detail"])


def test_secrets_left_out_are_kept_and_a_new_method_needs_its_own(
    workspace: Workspace,
) -> None:
    made = workspace.add()
    path = f"{CREDENTIALS}/{made['id']}"
    renamed = workspace.call(
        "PATCH",
        path,
        body={
            "name": "Orders",
            "urls": [ORDERS, "https://uploads.example.com"],
            "auth": {"kind": "bearer"},
        },
    )
    assert renamed.status_code == 200, renamed.json()
    assert renamed.json()["auth"]["secrets_set"] == ["token"]
    assert renamed.json()["urls"] == [ORDERS, "https://uploads.example.com"]
    other = workspace.call("PATCH", path, body={"auth": {"kind": "api_key"}})
    assert other.status_code == 422
    keyed = workspace.call(
        "PATCH",
        path,
        body={
            "auth": {
                "kind": "api_key",
                "settings": {"header": "X-Key"},
                "secrets": {"key": "k"},
            }
        },
    )
    assert keyed.json()["auth"]["settings"] == {"header": "X-Key", "scheme": ""}
    assert keyed.json()["auth"]["secrets_set"] == ["key"]


def test_a_deleted_credential_is_gone(workspace: Workspace) -> None:
    made = workspace.add()
    path = f"{CREDENTIALS}/{made['id']}"
    assert workspace.call("DELETE", path).status_code == 204
    assert workspace.call("GET", path).status_code == 404
    assert workspace.call("DELETE", path).status_code == 404


def test_without_a_secrets_key_credentials_arent_set_up(workspace: Workspace) -> None:
    workspace.client.app.state.api_credentials = None  # type: ignore[attr-defined]
    answered = workspace.call("GET", CREDENTIALS)
    assert answered.status_code == 503
    assert "FORGE_ADMIN_SECRETS_KEY" in answered.json()["detail"]


# ----------------------------------------------------------------- OAuth


def oauth_credential(workspace: Workspace) -> dict[str, Any]:
    return workspace.add(
        name="Orders sign-in",
        auth={
            "kind": "oauth",
            "settings": {
                "authorization_url": "https://auth.example.com/authorize",
                "token_url": TOKEN_URL,
                "client_id": "forge",
                "scopes": "orders",
            },
            "secrets": {"client_secret": "s3cret"},
        },
    )


def test_signing_in_connects_the_credential(workspace: Workspace) -> None:
    made = oauth_credential(workspace)
    assert made["auth"]["interactive"] is True
    assert made["auth"]["connected"] is False
    path = f"{CREDENTIALS}/{made['id']}"
    started = workspace.call("POST", f"{path}/oauth/start")
    assert started.status_code == 200, started.json()
    url = started.json()["authorization_url"]
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith("https://auth.example.com/authorize?")
    assert query["redirect_uri"].endswith("/oauth/credentials/callback")
    state = query["state"]

    # Only who began it may finish it.
    stranger = workspace.call(
        "POST", "/credential-oauth/callback", ADMIN, {"state": state, "code": "c-1"}
    )
    assert stranger.status_code == 400
    finished = workspace.call(
        "POST", "/credential-oauth/callback", MEMBER, {"state": state, "code": "c-1"}
    )
    assert finished.status_code == 200, finished.json()
    body = finished.json()
    assert body["auth"]["connected"] is True
    assert body["auth"]["connected_by_name"] == "Mia Member"
    assert body["auth"]["scope"] == "orders"
    grant = workspace.row(made["id"]).auth_grant
    assert grant and "access-1" not in grant
    # Once.
    again = workspace.call(
        "POST", "/credential-oauth/callback", MEMBER, {"state": state, "code": "c-1"}
    )
    assert again.status_code == 400

    disconnected = workspace.call("DELETE", f"{path}/oauth").json()
    assert disconnected["auth"]["connected"] is False


def test_a_sign_in_that_expired_isnt_finished(workspace: Workspace) -> None:
    made = oauth_credential(workspace)
    url = workspace.call("POST", f"{CREDENTIALS}/{made['id']}/oauth/start").json()[
        "authorization_url"
    ]
    state = parse_qs(urlsplit(url).query)["state"][0]

    async def expire() -> None:
        async with workspace.sessions() as session:
            flow = await session.scalar(select(ApiCredentialOAuthFlow))
            assert flow is not None
            flow.expires_at = flow.expires_at - timedelta(hours=1)
            await session.commit()

    workspace.run(expire)
    late = workspace.call(
        "POST", "/credential-oauth/callback", MEMBER, {"state": state, "code": "c-0"}
    )
    assert late.status_code == 400
    assert "expired" in late.json()["detail"]


def test_only_sign_in_credentials_are_signed_in_with(workspace: Workspace) -> None:
    made = workspace.add()
    refused = workspace.call("POST", f"{CREDENTIALS}/{made['id']}/oauth/start")
    assert refused.status_code == 422
    assert "isn't signed in with" in refused.json()["detail"]


# ------------------------------------------------- the hosted runtime's tools


def test_a_tool_signs_its_request_with_its_organizations_credential(
    workspace: Workspace,
) -> None:
    made = workspace.add(
        auth={
            "kind": "oauth_client_credentials",
            "settings": {"client_id": "forge", "token_url": TOKEN_URL},
            "secrets": {"client_secret": "s3cret"},
        }
    )
    provider = OrganizationCredentials(workspace.credentials, workspace.sessions)

    async def sign(organization: str, url: str) -> Any:
        return await provider(made["id"], organization_id=organization, url=url)

    signed = workspace.run(lambda: sign(ORG, f"{ORDERS}/orders/7"))
    assert dict(signed.headers) == {"Authorization": "Bearer access-1"}
    # The token is kept, encrypted: the next request reuses it.
    workspace.run(lambda: sign(ORG, ORDERS))
    assert len(workspace.endpoint.requests) == 1
    for organization, url in ((ORG, "https://evil.example.com/x"), (OTHER_ORG, ORDERS)):
        with pytest.raises(CredentialUnavailable):
            workspace.run(lambda o=organization, u=url: sign(o, u))


def test_hosted_tools_read_only_the_environment_names_listed() -> None:
    environment = {"ORDERS_TOKEN": "t", "FORGE_ADMIN_SECRETS_KEY": "never"}
    assert tool_environment([], environment) == {}
    assert tool_environment(["ORDERS_TOKEN", "MISSING"], environment) == {
        "ORDERS_TOKEN": "t"
    }


# ------------------------------------------------------------- OpenAPI specs

SPECS = f"/organizations/{ORG}/openapi/operations"


def test_a_pasted_spec_lists_its_operations_by_their_tools_names(
    workspace: Workspace,
) -> None:
    import json

    found = workspace.call(
        "POST", SPECS, body={"source": "inline", "spec": json.dumps(BILLING_SPEC)}
    )
    assert found.status_code == 200, found.json()
    body = found.json()
    assert (body["title"], body["version"], body["servers"]) == (
        "Billing",
        "2",
        [ORDERS],
    )
    assert [
        (o["name"], o["operation_id"], o["method"], o["path"], o["summary"])
        for o in body["operations"]
    ] == [
        ("get_invoice", "getInvoice", "GET", "/invoices/{id}", "One invoice"),
        ("list_charges", "listCharges", "GET", "/charges", ""),
    ]
    assert body["security"] == [
        {
            "name": "bearer",
            "type": "http",
            "location": "",
            "parameter": "",
            "scheme": "bearer",
        }
    ]


def test_a_spec_at_a_url_is_fetched_with_the_credential_where_it_may_go(
    workspace: Workspace,
) -> None:
    made = workspace.add()
    workspace.client.app.state.settings.agent_tools_allowed_hosts = ["api.example.com"]  # type: ignore[attr-defined]
    found = workspace.call(
        "POST",
        SPECS,
        body={
            "source": "url",
            "url": f"{ORDERS}/openapi.json",
            "credential": made["id"],
        },
    )
    assert found.status_code == 200, found.json()
    assert len(found.json()["operations"]) == 2
    assert workspace.spec_requests[0].headers["authorization"] == "Bearer t0k-123"


def test_a_spec_that_cant_be_had_says_why(workspace: Workspace) -> None:
    for body, says in (
        ({"source": "url", "url": "http://169.254.169.254/latest"}, "private network"),
        ({"source": "url", "url": ""}, "Give the spec's URL"),
        (
            {"source": "url", "url": "https://x.io/${SECRET}"},
            "FORGE_ADMIN_AGENT_TOOL_ENVIRONMENT",
        ),
        ({"source": "inline", "spec": "{nope"}, "JSON or YAML"),
        ({"source": "inline", "spec": ""}, "Paste the spec"),
    ):
        refused = workspace.call("POST", SPECS, body=body)
        assert refused.status_code == 422, body
        assert says in refused.json()["detail"], refused.json()
    assert (
        workspace.call(
            "POST", SPECS, VIEWER, {"source": "inline", "spec": "{}"}
        ).status_code
        == 403
    )
