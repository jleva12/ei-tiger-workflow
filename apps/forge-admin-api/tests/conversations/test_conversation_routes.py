"""
The conversation audit's routes, over SQLite: real ADK sessions (ADK's own
tables, as the hosted runtime and the assistant keep them) and their usage
rows, the policy read by Casbin's real adapter.

Acme's agent has conversations from each channel (the builder's chat, the
run API with an API key, A2A, and one from before channels were kept); Globex's
agent has one of its own; a member asked the assistant once from inside Acme
and once from nowhere.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from casbin_async_sqlalchemy_adapter import Adapter
from fastapi.testclient import TestClient
from forge_task_adk_workflows.usage_store import calls, invocations
from forge_task_adk_workflows.usage_store import metadata as usage_tables
from google.adk.events import Event, EventActions
from google.adk.sessions import DatabaseSessionService
from google.genai import types
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
from forge_admin.models import ApiKey, CasbinRule, Organization, Permission, Role, User

API = "/api/v1"
ACME = "3f6c0000-0000-4000-8000-000000000001"
GLOBEX = "3f6c0000-0000-4000-8000-000000000002"
ADMIN, MEMBER, ROOT = "admin-1", "member-1", "root-1"
HELPER, OTHER = "ca_acmehelper", "ca_globexhelper"
KEY = "7a1d0000-0000-4000-8000-0000000000aa"
ORG_LIST = f"/organizations/{ACME}/conversations"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

ROLES = {
    "org:admin": ("organizations:read", "agents:run", "agents:audit"),
    "org:member": ("organizations:read", "agents:run"),
    "site:admin": ("*:*",),
}
ASSIGNED = {
    ADMIN: ("org:admin", f"org:{ACME}*"),
    MEMBER: ("org:member", f"org:{ACME}*"),
    ROOT: ("site:admin", "*"),
}


class Agents:
    """The organizations' chat agents, as ChatAgentStore lists them."""

    async def list(self, organization_id: str) -> list[dict[str, Any]]:
        found = {ACME: (HELPER, "Acme helper"), GLOBEX: (OTHER, "Globex helper")}[
            organization_id
        ]
        return [{"_id": found[0], "document": {"name": found[1]}}]

    async def aclose(self) -> None:
        pass


def tables() -> MetaData:
    """The access tables, as SQLite takes them."""
    made = MetaData()
    for name in (
        "organizations",
        "users",
        "casbin_rule",
        "authz_roles",
        "authz_permissions",
        "api_keys",
    ):
        table = Base.metadata.tables[name].to_metadata(made)
        for column in table.columns:
            column.server_default = None
            if column.primary_key and isinstance(column.type, BigInteger):
                column.type = Integer()
    return made


def text(role: str, said: str) -> types.Content:
    return types.Content(role=role, parts=[types.Part(text=said)])


@dataclass
class World:
    client: TestClient
    tokens: dict[str, str]

    def get(self, path: str, caller: str = ADMIN, **params: Any) -> httpx.Response:
        return self.client.get(
            f"{API}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self.tokens[caller]}"},
        )

    def ids(self, path: str = ORG_LIST, caller: str = ADMIN, **params: Any) -> set[str]:
        answer = self.get(path, caller, **params)
        assert answer.status_code == 200, answer.json()
        return {item["session_id"] for item in answer.json()["items"]}


async def seed(
    engine: Any, sessions: async_sessionmaker[AsyncSession], adk: DatabaseSessionService
) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(tables().create_all)
        await conn.run_sync(usage_tables.create_all)
    async with sessions() as made:
        made.add_all(
            [
                Organization(id=ACME, name="Acme", description=""),
                Organization(id=GLOBEX, name="Globex", description=""),
                *(
                    User(
                        id=user,
                        first_name=user.split("-")[0].title(),
                        last_name="Person",
                        email=f"{user}@x.io",
                        msid=user,
                    )
                    for user in (ADMIN, MEMBER, ROOT)
                ),
                ApiKey(
                    id=KEY,
                    organization_id=ACME,
                    name="Billing app",
                    secret_sha256="0" * 64,
                    hint="fk_abcd",
                ),
            ]
        )
        grants = sorted(
            {grant for granted in ROLES.values() for grant in granted if grant != "*:*"}
        )
        made.add_all(
            Permission(resource=g.split(":")[0], action=g.split(":")[1]) for g in grants
        )
        for role, granted in ROLES.items():
            made.add(Role(key=role, name=role))
            made.add_all(
                CasbinRule(ptype="p", v0=role, v1=g.split(":")[0], v2=g.split(":")[1])
                for g in granted
            )
        made.add_all(
            CasbinRule(ptype="g", v0=user, v1=role, v2=domain)
            for user, (role, domain) in ASSIGNED.items()
        )
        await made.commit()

    async def conversation(
        app: str, user: str, session_id: str, *turns: tuple[str, list[Event]]
    ) -> None:
        session = await adk.create_session(
            app_name=app, user_id=user, session_id=session_id
        )
        for invocation, events in turns:
            for event in events:
                event.invocation_id = invocation
                await adk.append_event(session, event)

    def said(author: str, content: types.Content, **actions: Any) -> Event:
        return Event(author=author, content=content, actions=EventActions(**actions))

    looked_up = types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(
                    id="c-1", name="look_up_order", args={"id": "A1"}
                )
            )
        ],
    )
    answered = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id="c-1", name="look_up_order", response={"status": "shipped"}
                )
            )
        ],
    )
    await conversation(
        HELPER,
        MEMBER,
        "s-console",
        (
            "e-console",
            [
                said("user", text("user", "Where is A1?")),
                said("helper", looked_up),
                said("helper", answered),
                said(
                    "helper",
                    text("model", "It shipped."),
                    state_delta={"last_order": "A1"},
                ),
            ],
        ),
    )
    await conversation(
        HELPER,
        "outside-42",
        "s-api",
        ("e-api", [said("user", text("user", "Refund me"))]),
    )
    await conversation(
        HELPER,
        "A2A_USER_ctx9",
        "s-a2a",
        ("e-a2a", [said("user", text("user", "Status?"))]),
    )
    await conversation(
        HELPER, "old-user", "s-old", ("e-old", [said("user", text("user", "Hello"))])
    )
    await conversation(
        OTHER,
        MEMBER,
        "s-globex",
        ("e-globex", [said("user", text("user", "Globex only"))]),
    )
    await conversation(
        "forge",
        MEMBER,
        "s-assist",
        ("e-in-acme", [said("user", text("user", "Show Acme's runs"))]),
        ("e-elsewhere", [said("user", text("user", "Something private elsewhere"))]),
    )

    def turn(
        org: str,
        kind: str,
        subject: str,
        session_id: str,
        channel: str | None,
        caller: str | None,
        n: int,
    ) -> dict[str, Any]:
        at = (NOW + timedelta(minutes=n)).replace(tzinfo=None)
        return {
            "id": f"inv-{n}",
            "organization_id": org,
            "kind": kind,
            "subject_id": subject,
            "subject_name": subject,
            "version": "2",
            "session_id": session_id,
            "user_id": caller,
            "channel": channel,
            "status": "succeeded",
            "started_at": at,
            "hour": at.replace(minute=0, second=0),
            "finished_at": at,
            "duration_ms": 1200,
        }

    def call(
        org: str,
        kind: str,
        subject: str,
        session_id: str,
        invocation: str,
        name: str,
        n: int,
        **tokens: Any,
    ) -> dict[str, Any]:
        at = (NOW + timedelta(minutes=n)).replace(tzinfo=None)
        return {
            "organization_id": org,
            "kind": kind,
            "subject_id": subject,
            "subject_name": subject,
            "session_id": session_id,
            "invocation_id": invocation,
            "user_id": None,
            "author": "helper",
            "call": "model" if "/" in name else "tool",
            "name": name,
            "at": at,
            "hour": at.replace(minute=0, second=0),
            "input_tokens": tokens.get("tokens_in", 0),
            "cached_tokens": 0,
            "output_tokens": tokens.get("tokens_out", 0),
            "thinking_tokens": 0,
            "cost": tokens.get("cost"),
            "failed": False,
        }

    async with engine.begin() as conn:
        await conn.execute(
            sa.insert(invocations),
            [
                turn(ACME, "agent", HELPER, "s-console", "console", MEMBER, 1),
                turn(ACME, "agent", HELPER, "s-api", "api", f"apikey:{KEY}", 2),
                turn(ACME, "agent", HELPER, "s-a2a", "a2a", None, 3),
                turn(ACME, "agent", HELPER, "s-old", None, "old-user", 4),
                turn(GLOBEX, "agent", OTHER, "s-globex", "console", MEMBER, 5),
                turn(
                    ACME, "assistant", "assistant", "s-assist", "assistant", MEMBER, 6
                ),
                turn("", "assistant", "assistant", "s-assist", "assistant", MEMBER, 7),
            ],
        )
        await conn.execute(
            sa.insert(calls),
            [
                call(
                    ACME,
                    "agent",
                    HELPER,
                    "s-console",
                    "e-console",
                    "openai/gpt-test",
                    1,
                    tokens_in=100,
                    tokens_out=20,
                    cost=0.01,
                ),
                call(
                    ACME, "agent", HELPER, "s-console", "e-console", "look_up_order", 1
                ),
                call(
                    ACME,
                    "agent",
                    HELPER,
                    "s-console",
                    "e-console",
                    "openai/gpt-test",
                    1,
                    tokens_in=150,
                    tokens_out=10,
                    cost=0.02,
                ),
                call(
                    ACME,
                    "assistant",
                    "assistant",
                    "s-assist",
                    "e-in-acme",
                    "openai/gpt-test",
                    6,
                    tokens_in=50,
                    tokens_out=5,
                ),
                call(
                    "",
                    "assistant",
                    "assistant",
                    "s-assist",
                    "e-elsewhere",
                    "openai/gpt-test",
                    7,
                    tokens_in=70,
                    tokens_out=7,
                ),
            ],
        )


@pytest.fixture
def world(settings: Settings) -> Iterator[World]:
    configured = settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 40), "local_user_id": None}
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    enforcer = new_enforcer(Adapter(engine, db_class=CasbinRule))
    adk = DatabaseSessionService(db_engine=engine)

    async def session() -> Any:
        async with sessions() as made:
            yield made

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_enforcer] = lambda: enforcer
    client = TestClient(app)
    client.__enter__()
    # The app makes its own as it starts: these are the test's.
    app.state.engine = engine
    app.state.sessionmaker = sessions
    app.state.enforcer = enforcer
    app.state.adk_run_sessions = adk
    app.state.chat_agents = Agents()
    assert client.portal is not None
    client.portal.call(seed, engine, sessions, adk)
    tokens = {
        user: mint_subject_token(configured, user, lifetime=timedelta(minutes=5))
        for user in (ADMIN, MEMBER, ROOT)
    }
    yield World(client, tokens)
    client.__exit__(None, None, None)


def test_an_organizations_admins_list_its_agents_and_assistants_conversations(
    world: World,
) -> None:
    answer = world.get(ORG_LIST)
    assert answer.status_code == 200, answer.json()
    page = answer.json()
    by_id = {item["session_id"]: item for item in page["items"]}
    # Every channel of its agent, and the assistant asked from inside it; never Globex's.
    assert set(by_id) == {"s-console", "s-api", "s-a2a", "s-old", "s-assist"}
    assert page["total"] == 5
    assert page["agents"] == {HELPER: "Acme helper"}
    console = by_id["s-console"]
    assert (console["agent_name"], console["user_id"], console["channels"]) == (
        "Acme helper",
        MEMBER,
        ["console"],
    )
    assert (console["events"], console["turns"], console["last_status"]) == (
        4,
        1,
        "succeeded",
    )
    assert (console["model_calls"], console["tool_calls"]) == (2, 1)
    assert (console["input_tokens"], console["output_tokens"]) == (250, 30)
    assert console["cost"] == pytest.approx(0.03)
    assert by_id["s-api"]["callers"] == [f"apikey:{KEY}"]
    assert by_id["s-old"]["channels"] == ["unknown"]
    # The assistant's: only its turn asked from inside Acme counts here.
    assist = by_id["s-assist"]
    assert (assist["agent_name"], assist["turns"], assist["input_tokens"]) == (
        "Assistant",
        1,
        50,
    )
    # The people and keys it names.
    assert page["people"][MEMBER] == {
        "kind": "person",
        "name": "Member Person",
        "email": "member-1@x.io",
    }
    assert page["people"][f"apikey:{KEY}"]["name"] == "Billing app"


def test_conversations_are_found_by_channel_agent_and_who(world: World) -> None:
    assert world.ids(channel="api") == {"s-api"}
    assert world.ids(channel="a2a") == {"s-a2a"}
    assert world.ids(channel="console") == {"s-console"}
    assert world.ids(channel="unknown") == {"s-old"}
    assert world.ids(agent="assistant") == {"s-assist"}
    assert world.ids(agent=HELPER) == {"s-console", "s-api", "s-a2a", "s-old"}
    # By a person's name, an API key's name, and a caller's or session's ID.
    assert world.ids(q="member person") == {"s-console", "s-assist"}
    assert world.ids(q="Billing") == {"s-api"}
    assert world.ids(q="A2A_USER") == {"s-a2a"}
    assert world.ids(q="s-ol") == {"s-old"}
    assert world.ids(q="nobody at all") == set()
    paged = world.get(ORG_LIST, limit=2, offset=0).json()
    assert (len(paged["items"]), paged["total"]) == (2, 5)


def test_a_conversation_reads_whole(world: World) -> None:
    answer = world.get(f"{ORG_LIST}/s-console", app=HELPER, user=MEMBER)
    assert answer.status_code == 200, answer.json()
    found = answer.json()
    assert (found["agent_name"], found["partial"]) == ("Acme helper", False)
    texts = [event["text"] for event in found["events"] if event["text"]]
    assert texts == ["Where is A1?", "It shipped."]
    [call] = [c for event in found["events"] for c in event["calls"]]
    assert (call["name"], call["args"]) == ("look_up_order", {"id": "A1"})
    [response] = [r for event in found["events"] for r in event["responses"]]
    assert response["response"] == {"status": "shipped"}
    assert found["state_after"] == {"last_order": "A1"}
    [turn] = found["turns"]
    assert (turn["channel"], turn["caller"], turn["status"]) == (
        "console",
        MEMBER,
        "succeeded",
    )
    assert [c["name"] for c in found["calls"]] == [
        "openai/gpt-test",
        "look_up_order",
        "openai/gpt-test",
    ]
    assert found["people"][MEMBER]["name"] == "Member Person"
    # As ADK's session API serves them, for the chat window.
    raw = found["adk_events"]
    assert [event["invocationId"] for event in raw] == ["e-console"] * 4
    assert raw[0]["content"] == {"role": "user", "parts": [{"text": "Where is A1?"}]}
    assert raw[1]["content"]["parts"][0]["functionCall"]["name"] == "look_up_order"


def test_an_organization_reads_only_the_assistant_turns_asked_from_inside_it(
    world: World,
) -> None:
    found = world.get(f"{ORG_LIST}/s-assist", app="forge", user=MEMBER).json()
    assert found["partial"] is True
    assert [event["text"] for event in found["events"]] == ["Show Acme's runs"]
    assert [turn["organization_id"] for turn in found["turns"]] == [ACME]
    assert [event["invocationId"] for event in found["adk_events"]] == ["e-in-acme"]
    # The site's admins read all of it.
    whole = world.get("/conversations/s-assist", ROOT, app="forge", user=MEMBER).json()
    assert whole["partial"] is False
    assert [event["text"] for event in whole["events"]] == [
        "Show Acme's runs",
        "Something private elsewhere",
    ]
    assert len(whole["turns"]) == 2


def test_only_the_organizations_conversations_are_found(world: World) -> None:
    for session_id, app, user in (
        ("s-globex", OTHER, MEMBER),
        ("s-nothing", HELPER, MEMBER),
    ):
        missing = world.get(f"{ORG_LIST}/{session_id}", app=app, user=user)
        assert missing.status_code == 404
        assert missing.json()["detail"] == "There's no such conversation"


def test_the_site_lists_every_assistant_conversation(world: World) -> None:
    assert world.ids("/conversations", ROOT) == {"s-assist"}
    page = world.get("/conversations", ROOT).json()
    # Both turns, wherever they were asked from.
    assert page["items"][0]["turns"] == 2 and page["items"][0]["input_tokens"] == 120


def test_auditing_needs_agents_audit(world: World) -> None:
    assert world.get(ORG_LIST, MEMBER).status_code == 403
    assert (
        world.get(f"{ORG_LIST}/s-console", MEMBER, app=HELPER, user=MEMBER).status_code
        == 403
    )
    # An organization's admins aren't the site's.
    assert world.get("/conversations", ADMIN).status_code == 403
    assert world.get(f"/organizations/{GLOBEX}/conversations", ADMIN).status_code == 403
