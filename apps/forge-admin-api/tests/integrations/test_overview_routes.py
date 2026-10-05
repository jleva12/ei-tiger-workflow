"""The organization overview's route, without MySQL or MongoDB: the run store
and the usage store on SQLite, the organization's access in memory (a Casbin
enforcer over the default grants), its workflows and agents stood in. Who may
read it, and what it answers."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest
from casbin.persist.adapter import load_policy_line
from casbin.persist.adapters.asyncio import AsyncAdapter
from fastapi.testclient import TestClient
from forge_common.adk.usage import Attribution, UsageCall
from forge_task_adk_workflows import run_store, usage_store
from forge_task_adk_workflows.run_store import Actor, RunStore
from forge_task_adk_workflows.usage_store import UsageStore
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.auth.authorization import get_enforcer, new_enforcer
from forge_admin.auth.tokens import mint_subject_token
from forge_admin.config import Settings
from forge_admin.db.session import get_session
from forge_admin.models import Organization, User

API = "/api/v1"
ORG = "3f6c0000-0000-4000-8000-000000000001"
VIEWER, OUTSIDER = "viewer-1", "outsider-1"
NOW = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)
OVERVIEW = f"/organizations/{ORG}/overview"


class Grants(AsyncAdapter):
    """The default grant the overview checks, and the viewer's role."""

    lines = [
        "p, org:viewer, organizations, read",
        f"g, {VIEWER}, org:viewer, org:{ORG}*",
    ]

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


class Directory:
    """Stands in for the request's MySQL session: the organization, and the people's names."""

    async def get(self, model: type, key: str) -> object | None:
        if model is Organization and key == ORG:
            return Organization(id=key, name="Org", description="")
        if model is User and key == "user-ada":
            return User(
                id=key, first_name="Ada", last_name="Lovelace", email="ada@x.io"
            )
        return None


class Documents:
    """Stands in for a MongoDB store of the organization's documents."""

    def __init__(self, *named: tuple[str, str]) -> None:
        self.records = [
            {"_id": doc_id, "organization_id": ORG, "document": {"name": name}}
            for doc_id, name in named
        ]

    async def list(self, organization_id: str) -> list[dict[str, Any]]:
        return self.records if organization_id == ORG else []

    async def aclose(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def client(settings: Settings) -> Iterator[tuple[TestClient, Callable[..., Any]]]:
    configured = settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 40), "local_user_id": None}
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    clock = Clock()
    runs, usage = RunStore(engine, clock=clock), UsageStore(engine, clock=clock)
    app.state.adk_runs = runs
    app.state.usage = usage
    app.state.organization_agents = Documents(("wf_refunds", "Refunds"))
    app.state.chat_agents = Documents(("ca_support", "Support desk"))
    enforcer = new_enforcer(Grants())
    app.dependency_overrides[get_session] = Directory
    app.dependency_overrides[get_enforcer] = lambda: enforcer

    with TestClient(app) as client:
        assert client.portal is not None
        call = partial(client.portal.call)

        async def seed() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(run_store.metadata.create_all)
                await conn.run_sync(usage_store.metadata.create_all)
            clock.now = NOW - timedelta(hours=2)
            await runs.create(
                organization_id=ORG,
                agent_id="wf_refunds",
                agent_name="Refunds",
                revision=1,
                session_id="s-1",
                payload={},
                requested_by=Actor("user-bob", "Bob Builder"),
            )
            await usage.record(
                UsageCall(
                    attribution=Attribution(ORG, "agent", "ca_support", "Support desk"),
                    call="model",
                    name="openai/gpt-5.2",
                    author="triage",
                    at=NOW - timedelta(hours=1),
                    user_id="user-ada",
                    input_tokens=1200,
                    cached_tokens=200,
                    output_tokens=300,
                    cost=0.004,
                )
            )
            clock.now = NOW

        call(seed)
        yield client, call
        call(engine.dispose)


def get(client: TestClient, user: str, settings: Settings, **params: Any) -> Any:
    configured = settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 40), "local_user_id": None}
    )
    token = mint_subject_token(configured, user, lifetime=timedelta(minutes=5))
    return client.get(
        f"{API}{OVERVIEW}", headers={"Authorization": f"Bearer {token}"}, params=params
    )


def test_a_member_reads_the_period_in_their_days(
    client: tuple[TestClient, Any], settings: Settings
) -> None:
    answered = get(client[0], VIEWER, settings, period="7d", time_zone="Europe/Oslo")
    assert answered.status_code == 200, answered.text
    overview = answered.json()
    assert (overview["period"], overview["unit"], overview["time_zone"]) == (
        "7d",
        "day",
        "Europe/Oslo",
    )
    assert len(overview["buckets"]) == 7
    assert overview["buckets"][0]["start"] == "2026-09-30T22:00:00+00:00"
    assert {s["key"]: s["name"] for s in overview["subjects"]} == {
        "agent:ca_support": "Support desk",
        "workflow:wf_refunds": "Refunds",
    }
    [usage] = overview["usage"]
    assert usage == {
        "bucket": 6,
        "subject": "agent:ca_support",
        "model": "openai/gpt-5.2",
        "calls": 1,
        "failed": 0,
        "input": 1200,
        "cached": 200,
        "output": 300,
        "thinking": 0,
        "cost": pytest.approx(0.004),
        "unpriced": 0,
    }
    [run] = overview["activity"]
    assert (run["subject"], run["started"], run["open"]) == (
        "workflow:wf_refunds",
        1,
        1,
    )
    assert overview["open"]["queued"] == 1
    assert {m["id"]: m["name"] for m in overview["members"]} == {
        "user-ada": "Ada Lovelace",
        "user-bob": "Bob Builder",
    }


def test_ninety_days_are_by_week(
    client: tuple[TestClient, Any], settings: Settings
) -> None:
    overview = get(client[0], VIEWER, settings, period="90d").json()
    assert overview["unit"] == "week"
    assert overview["time_zone"] == "UTC"


def test_an_outsider_cant_read_it(
    client: tuple[TestClient, Any], settings: Settings
) -> None:
    assert get(client[0], OUTSIDER, settings).status_code == 403


def test_a_period_it_doesnt_offer_is_refused(
    client: tuple[TestClient, Any], settings: Settings
) -> None:
    assert get(client[0], VIEWER, settings, period="1y").status_code == 422
