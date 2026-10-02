"""Running an organization's ADK workflows, against MySQL and MongoDB: a member
runs one as themselves, and the run is kept in the admin database
(``adk_runs``, ``adk_run_events``, made by ``0005adk_run_store``) as the
worker will take it; its steps are read from its ADK session there; the
default grants decide who decides its approvals and who retries, resubmits
and abandons it (``agents:manage_runs``); deleting the organization forgets
its runs. ``test_adk_run_routes.py`` covers every route on SQLite.

A site administrator builds an organization with an admin, a member and a
viewer. Jobs go to a fake of the worker's queue. ADK workflows are kept in a
MongoDB database of the test's own, dropped afterwards.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from embedding_fake import FakeEmbedding
from fastapi.testclient import TestClient
from forge_task_adk_workflows.run_store import Actor, RunStore
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from pymongo import MongoClient

from forge_admin.adk_workflows.build import build_agent
from forge_admin.api.app import PUBLIC_ROUTERS, ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.config import Settings
from forge_admin.db.session import create_engine

pytestmark = pytest.mark.mysql

API = "/api/v1"


def node(
    node_id: str, kind: str, config: dict[str, Any], outputs: list[str] | None = None
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "name": node_id.replace("_", " ").title(),
        "config": config,
        "outputs": ["next"] if outputs is None else outputs,
    }


def document(
    nodes: list[dict[str, Any]], edges: list[tuple[str, str, str]], name: str
) -> dict[str, Any]:
    """A forge.agent/v1 document; the API sets its ID."""
    return {
        "format": "forge.agent/v1",
        "id": "ag_draft00001",
        "name": name,
        "description": "",
        "nodes": nodes,
        "edges": [
            {"id": f"{s}:{o}->{t}", "source": s, "source_output": o, "target": t}
            for s, o, t in edges
        ],
        "layout": {},
    }


def end(node_id: str) -> dict[str, Any]:
    return node(node_id, "end", {"outcome": "succeeded", "result": ""}, [])


def shipping() -> dict[str, Any]:
    """Start, Transform, Approval by the admins, then an End either way."""
    return document(
        [
            node(
                "start",
                "start",
                {
                    "input_schema": {
                        "type": "object",
                        "required": ["key"],
                        "properties": {"key": {"type": "string"}},
                    }
                },
            ),
            node(
                "shape",
                "transform",
                {"expression": '{"loud": $uppercase(input.key)}', "output_schema": {}},
            ),
            node(
                "review",
                "approval",
                {"message": "Ship it?", "approvers": "org:admin", "timeout_hours": 0},
                ["approved", "rejected"],
            ),
            end("yes"),
            end("no"),
        ],
        [
            ("start", "next", "shape"),
            ("shape", "next", "review"),
            ("review", "approved", "yes"),
            ("review", "rejected", "no"),
        ],
        "Ship",
    )


def using(saved_id: str) -> dict[str, Any]:
    """An ADK workflow that runs another, saved one."""
    return document(
        [
            node("start", "start", {"input_schema": {}}),
            node("inner", "saved", {"agent": saved_id}),
            end("done"),
        ],
        [("start", "next", "inner"), ("inner", "next", "done")],
        "Outer",
    )


@dataclass
class Runs:
    organization_id: str
    admin_id: str
    member_id: str
    queue: FakeEmbedding
    admin: TestClient
    member: TestClient
    viewer: TestClient
    outsider: TestClient
    site: TestClient
    settings: Settings

    @property
    def agents(self) -> str:
        return f"{API}/organizations/{self.organization_id}/agents"

    @property
    def adk_runs(self) -> str:
        return f"{API}/organizations/{self.organization_id}/adk-runs"

    def create(self, made: dict[str, Any]) -> dict[str, Any]:
        created = self.member.post(self.agents, json={"document": made})
        assert created.status_code == 201, created.json()
        return dict(created.json())

    def start(self, agent_id: str, input: Any) -> dict[str, Any]:
        started = self.member.post(
            f"{self.agents}/{agent_id}/runs", json={"input": input}
        )
        assert started.status_code == 202, started.json()
        return dict(started.json())

    def in_store[T](self, work: Callable[[RunStore], Awaitable[T]]) -> T:
        """Do something to the runs in MySQL, as the worker would."""

        async def main() -> T:
            engine = create_engine(self.settings)
            try:
                return await work(RunStore(engine))
            finally:
                await engine.dispose()

        return asyncio.run(main())


@pytest.fixture
def runs(
    site_admin: str, client_as: Callable[[str], TestClient], mysql_settings: Settings
) -> Iterator[Runs]:
    if mysql_settings.mongo_uri is None:
        pytest.skip("needs MongoDB: set FORGE_ADMIN_MONGO_URI (make env)")
    site = client_as(site_admin)
    tag = uuid4().hex[:8]
    org = site.post(f"{API}/organizations", json={"name": f"Org {tag}"}).json()["id"]
    lead, member, viewer = f"admin-{tag}", f"member-{tag}", f"viewer-{tag}"
    for user, role in (
        (lead, "org:admin"),
        (member, "org:member"),
        (viewer, "org:viewer"),
    ):
        assigned = site.put(f"{API}/scopes/org:{org}/members/{user}/roles/{role}")
        assert assigned.status_code == 200

    database = f"forge_admin_test_{uuid4().hex[:8]}"
    queue = FakeEmbedding()
    clients: list[TestClient] = []

    def acting_as(user: str) -> TestClient:
        settings = mysql_settings.model_copy(
            update={
                "local_user_id": user,
                "mongo_database": database,
                # Whatever the local .env says: the fake stands in.
                "embedding_redis_url": None,
            }
        )
        app = ApiServer(
            settings, routers=ROUTERS, public_routers=PUBLIC_ROUTERS
        ).create_app()
        app.state.embedding = queue
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield Runs(
        org,
        lead,
        member,
        queue,
        acting_as(lead),
        acting_as(member),
        acting_as(viewer),
        acting_as(f"outsider-{tag}"),
        site,
        mysql_settings,
    )

    for client in clients:
        client.__exit__(None, None, None)
    site.delete(f"{API}/organizations/{org}")
    mongo: MongoClient = MongoClient(
        mysql_settings.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=5000
    )
    mongo.drop_database(database)
    mongo.close()


def test_a_member_runs_an_adk_workflow_kept_in_mysql_as_it_is_saved(
    runs: Runs,
) -> None:
    inner = runs.create(shipping())
    outer = runs.create(using(inner["id"]))
    run = runs.start(outer["id"], {})
    assert (run["agent_id"], run["agent_name"], run["revision"]) == (
        outer["id"],
        "Outer",
        1,
    )
    assert (run["status"], run["requested_by"]["id"]) == (
        "queued",
        runs.member_id,
    )
    assert run["created_at"].endswith("+00:00")
    assert runs.queue.queued == [run["id"]]
    kept = runs.in_store(lambda store: store.get(run["id"]))
    assert kept is not None
    assert kept["payload"]["saved"] == {inner["id"]: inner["document"]}
    assert kept["payload"]["document"] == outer["document"]
    assert kept["payload"]["run_as"] == runs.member_id

    detail = runs.viewer.get(f"{runs.adk_runs}/{run['id']}")
    assert detail.status_code == 200, detail.json()
    assert detail.json()["input"] == {}
    assert [e["kind"] for e in detail.json()["events"]] == ["created"]
    listed = runs.viewer.get(runs.adk_runs, params={"agent_id": outer["id"]}).json()
    assert [r["id"] for r in listed["items"]] == [run["id"]]


def test_a_runs_input_must_fit_its_start_and_its_document_must_build(
    runs: Runs,
) -> None:
    made = runs.create(shipping())
    refused = runs.member.post(
        f"{runs.agents}/{made['id']}/runs", json={"input": {"key": 7}}
    )
    assert refused.status_code == 422
    broken = runs.create(using("ag_nosuchthing"))
    unbuilt = runs.member.post(f"{runs.agents}/{broken['id']}/runs", json={})
    assert unbuilt.status_code == 422
    assert "the organization has no agent ag_nosuchthing" in unbuilt.json()["detail"]
    assert runs.queue.queued == []
    viewed = runs.viewer.post(
        f"{runs.agents}/{made['id']}/runs", json={"input": {"key": "k"}}
    )
    assert viewed.status_code == 403
    assert runs.outsider.get(runs.adk_runs).status_code == 403


def test_the_default_grants_decide_approvals_and_manage_runs(runs: Runs) -> None:
    made = runs.create(shipping())
    run = runs.start(made["id"], {"key": "k-1"})

    async def pause(store: RunStore) -> dict[str, Any]:
        await store.claim(run["id"], owner="w1", lease_seconds=60)
        return await store.pause(
            run["id"],
            owner="w1",
            state={},
            key="review",
            kind="approval",
            reason="Ship it?",
            details={"kind": "approval", "approvers": "org:admin", "step": "review"},
            deadline=None,
        )

    request_id = runs.in_store(pause)["id"]
    decision = {"request_id": request_id, "approved": False, "comment": "Not yet"}
    decisions = f"{runs.adk_runs}/{run['id']}/decisions"
    assert runs.member.post(decisions, json=decision).status_code == 403
    decided = runs.admin.post(decisions, json=decision)
    assert decided.status_code == 202, decided.json()
    assert decided.json()["status"] == "queued"

    async def fail(store: RunStore) -> None:
        await store.claim(run["id"], owner="w1", lease_seconds=60)
        await store.finish(
            run["id"],
            owner="w1",
            state={},
            succeeded=False,
            error={"message": "Rejected", "category": "failed"},
        )

    runs.in_store(fail)
    retry = f"{runs.adk_runs}/{run['id']}/retry"
    refused = runs.member.post(retry)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Requires agents:manage_runs"
    retried = runs.admin.post(retry)
    assert retried.status_code == 202, retried.json()
    assert retried.json()["attempt"] == 2
    abandoned = runs.admin.post(f"{runs.adk_runs}/{run['id']}/abandon")
    assert abandoned.status_code == 200, abandoned.json()
    resubmitted = runs.admin.post(f"{runs.adk_runs}/{run['id']}/resubmit")
    assert resubmitted.status_code == 202, resubmitted.json()
    assert resubmitted.json()["resubmit_of"] == run["id"]


def test_the_run_store_keeps_microseconds_and_json_in_mysql(runs: Runs) -> None:
    when = datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=UTC)

    async def keep(store: RunStore) -> dict[str, Any] | None:
        store.clock = lambda: when
        made = await store.create(
            organization_id=runs.organization_id,
            agent_id="ag_direct0001",
            agent_name="Direct",
            revision=1,
            session_id=str(uuid4()),
            payload={"input": {"é": [1, 2.5, None, True]}, "document": {}},
            requested_by=Actor("someone", "Some One"),
        )
        return await store.get(made["id"])

    kept = runs.in_store(keep)
    assert kept is not None
    assert kept["created_at"] == when
    assert kept["payload"]["input"] == {"é": [1, 2.5, None, True]}
    read = runs.viewer.get(f"{runs.adk_runs}/{kept['id']}").json()
    assert read["created_at"] == "2026-10-01T12:00:00.123456+00:00"


async def run_to_the_approval(
    sessions: DatabaseSessionService,
    made: dict[str, Any],
    member: str,
    session_id: str,
) -> None:
    """Run the shipping ADK workflow in a session, as the worker does, to its
    approval."""
    session = await sessions.create_session(
        app_name="adk_workflows", user_id=member, session_id=session_id
    )
    runner = Runner(
        node=build_agent(made["document"]),
        session_service=sessions,
        app_name="adk_workflows",
    )
    message = types.Content(role="user", parts=[types.Part(text='{"key": "k-1"}')])
    async for _ in runner.run_async(
        user_id=member, session_id=session.id, new_message=message
    ):
        pass


def test_a_runs_steps_are_read_from_the_admin_database(runs: Runs) -> None:
    # The worker keeps the session in the admin MySQL; the API reads it there.
    made = runs.create(shipping())
    run = runs.start(made["id"], {"key": "k-1"})
    member, session_id = runs.member_id, run["session_id"]

    async def in_mysql(work: Callable[[DatabaseSessionService], Any]) -> None:
        engine = create_engine(runs.settings)
        try:
            await work(DatabaseSessionService(db_engine=engine))
        finally:
            await engine.dispose()

    asyncio.run(
        in_mysql(
            lambda sessions: run_to_the_approval(sessions, made, member, session_id)
        )
    )
    try:
        read = runs.member.get(f"{runs.adk_runs}/{run['id']}/steps")
        assert read.status_code == 200, read.json()
        assert [(s["id"], s["status"]) for s in read.json()["steps"]] == [
            ("start", "done"),
            ("shape", "done"),
            ("review", "waiting"),
            ("yes", "not_reached"),
            ("no", "not_reached"),
        ]
    finally:
        asyncio.run(
            in_mysql(
                lambda sessions: sessions.delete_session(
                    app_name="adk_workflows", user_id=member, session_id=session_id
                )
            )
        )


def test_deleting_the_organization_forgets_its_runs(runs: Runs) -> None:
    made = runs.create(shipping())
    run = runs.start(made["id"], {"key": "k-1"})
    deleted = runs.site.delete(f"{API}/organizations/{runs.organization_id}")
    assert deleted.status_code == 204
    assert runs.in_store(lambda store: store.get(run["id"])) is None
    assert runs.in_store(lambda store: store.events(run["id"])) == []
