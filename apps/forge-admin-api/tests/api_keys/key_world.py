"""An organization's access, without MySQL: the tables on SQLite, the
policy read by Casbin's real adapter from them (keys write g lines of their
own), people signed in with tokens, and the runtime's agents read from a
folder.

The people: an organization admin, a member and a viewer of Acme, and a
member of Globex. Acme's agent and Globex's agent are both in the runtime.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from casbin_async_sqlalchemy_adapter import Adapter
from fastapi.testclient import TestClient
from forge_agent_runtime import AgentExecutor
from forge_agent_runtime.a2a import task_store
from forge_agent_runtime.executor import DirectoryAgentSource
from forge_common.adk.models import ProviderModels
from forge_common.model_provider import parse_model_provider_yaml
from pydantic import SecretStr
from sqlalchemy import BigInteger, Integer, MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.auth.authorization import get_enforcer, new_enforcer
from forge_admin.auth.tokens import mint_subject_token
from forge_admin.config import Settings
from forge_admin.db.base import Base
from forge_admin.db.session import get_session
from forge_admin.models import CasbinRule, Organization, Permission, Role, User

API = "/api/v1"
RUNTIME = f"{API}/runtime"
ORG = "3f6c0000-0000-4000-8000-000000000001"
OTHER_ORG = "3f6c0000-0000-4000-8000-000000000002"
ADMIN, MEMBER, VIEWER, NEIGHBOUR = "admin-1", "member-1", "viewer-1", "neighbour-1"
PEOPLE = (ADMIN, MEMBER, VIEWER, NEIGHBOUR)
KEYS = f"/organizations/{ORG}/api-keys"
AGENT, OTHER_AGENT = "ca_acmeagent1", "ca_globexagent"

ROLES = {
    "org:admin": (
        "Organization administrator",
        (
            "organizations:read",
            "members:read",
            "members:update",
            "agents:run",
            "agents:approve",
            "agents:manage_runs",
            "api_keys:manage",
            "knowledge_bases:manage",
        ),
    ),
    "org:member": ("Organization member", ("organizations:read", "agents:run")),
    "org:viewer": ("Organization viewer", ("organizations:read",)),
    "org:api": ("API caller", ("agents:run",)),
    # Holds api_keys:manage, but not what API caller grants.
    "org:keykeeper": ("Key keeper", ("api_keys:manage",)),
    "site:admin": ("Site administrator", ("*:*",)),
}
ASSIGNED = {
    ADMIN: ("org:admin", ORG),
    MEMBER: ("org:member", ORG),
    VIEWER: ("org:viewer", ORG),
    NEIGHBOUR: ("org:member", OTHER_ORG),
}
PROVIDERS = """
version: 1
default: {provider: openai, model: gpt-test}
providers:
  openai:
    baseUrl: https://api.openai.com/v1
    api: openai-responses
    auth: {type: apiKey, key: sk-test}
    models: [{id: gpt-test}]
"""
EXAMPLE = (
    Path(__file__).parents[4]
    / "packages/python/agent-runtime/tests/fixtures/support-assistant.chat-agent.json"
)


def agent_document(agent_id: str, organization_id: str, name: str) -> dict[str, Any]:
    """The builder's example agent, offline: without the tools that reach out."""
    doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    kept = {"agent"}
    doc["nodes"] = [n for n in doc["nodes"] if n["id"] in kept]
    doc["edges"] = []
    return {**doc, "id": agent_id, "organization_id": organization_id, "name": name}


def sqlite_tables() -> MetaData:
    """The tables access needs, as SQLite takes them: without MySQL's
    microsecond CURRENT_TIMESTAMP(6) defaults (SQLAlchemy sets the times)."""
    tables = MetaData()
    for name in (
        "organizations",
        "users",
        "casbin_rule",
        "authz_roles",
        "authz_permissions",
        "api_keys",
    ):
        table = Base.metadata.tables[name].to_metadata(tables)
        for column in table.columns:
            column.server_default = None
            # SQLite numbers only INTEGER primary keys itself, not BIGINT.
            if column.primary_key and isinstance(column.type, BigInteger):
                column.type = Integer()
    return tables


@dataclass
class World:
    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    tokens: dict[str, str]

    def call(
        self,
        method: str,
        path: str,
        user: str | None = MEMBER,
        body: Any = None,
        *,
        key: str | None = None,
        header: str = "bearer",
        prefix: str = API,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = dict(headers or {})
        if key is not None:
            if header == "bearer":
                headers["Authorization"] = f"Bearer {key}"
            else:
                headers["X-API-Key"] = key
        elif user is not None:
            headers["Authorization"] = f"Bearer {self.tokens[user]}"
        return self.client.request(
            method, f"{prefix}{path}", headers=headers, json=body
        )

    def make_key(self, user: str = ADMIN, **fields: Any) -> dict[str, Any]:
        made = self.call("POST", KEYS, user, {"name": "CI"} | fields)
        assert made.status_code == 201, made.json()
        return dict(made.json())

    def run[T](self, work: Callable[[AsyncSession], Any]) -> Any:
        async def go() -> Any:
            async with self.sessions() as session:
                return await work(session)

        assert self.client.portal is not None
        return self.client.portal.call(go)


def build_world(
    settings: Settings, tmp_path: Path, *, public: bool = True, **changes: Any
) -> World:
    """
    The admin API over SQLite, its policy read by Casbin's real adapter,
    its runtime's agents from a folder. Its client is started: the caller
    closes it.
    """
    configured = settings.model_copy(
        update={
            "jwt_secret": SecretStr("s" * 40),
            "local_user_id": None,
            "agent_runtime_public": public,
            **changes,
        }
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    enforcer = new_enforcer(Adapter(engine, db_class=CasbinRule))

    folder = tmp_path / ("public" if public else "private")
    folder.mkdir(exist_ok=True)
    for agent_id, org, name in (
        (AGENT, ORG, "Acme helper"),
        (OTHER_AGENT, OTHER_ORG, "Globex helper"),
    ):
        (folder / f"{agent_id}.json").write_text(
            json.dumps(agent_document(agent_id, org, name))
        )
    models = ProviderModels(
        parse_model_provider_yaml(PROVIDERS),
        build=lambda _: None,  # type: ignore[arg-type, return-value]
    )
    app.state.agent_executor = AgentExecutor(
        DirectoryAgentSource(folder), models=models, http=httpx.AsyncClient()
    )
    app.state.a2a_tasks = task_store()

    async def session() -> Any:
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
                    *(
                        User(
                            id=user,
                            first_name=user.split("-")[0].title(),
                            last_name="Person",
                            email=f"{user}@x.io",
                            msid=user,
                        )
                        for user in PEOPLE
                    ),
                ]
            )
            permissions = sorted(
                {p for _, grants in ROLES.values() for p in grants if p != "*:*"}
            )
            made.add_all(
                Permission(resource=p.split(":")[0], action=p.split(":")[1])
                for p in permissions
            )
            for key, (name, grants) in ROLES.items():
                made.add(Role(key=key, name=name))
                made.add_all(
                    CasbinRule(
                        ptype="p",
                        v0=key,
                        v1=grant.split(":")[0],
                        v2=grant.split(":")[1],
                    )
                    for grant in grants
                )
            made.add_all(
                CasbinRule(ptype="g", v0=user, v1=role, v2=f"org:{org}*")
                for user, (role, org) in ASSIGNED.items()
            )
            await made.commit()

    tokens = {
        user: mint_subject_token(configured, user, lifetime=timedelta(minutes=5))
        for user in PEOPLE
    }
    client = TestClient(app)
    client.__enter__()
    # The app makes its own as it starts: these are the test's.
    app.state.sessionmaker = sessions
    app.state.enforcer = enforcer
    assert client.portal is not None
    client.portal.call(create)
    return World(client, sessions, tokens)
