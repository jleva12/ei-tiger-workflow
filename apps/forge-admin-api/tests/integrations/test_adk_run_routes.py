"""ADK workflow runs' routes, without MySQL, MongoDB or Redis: the run store on
SQLite, the organization's access in memory (a Casbin enforcer over the
default grants), and its ADK workflows, the worker's queue and the runs' ADK
sessions stood in. What each route answers, and what it refuses.

The people: an organization admin, a member and a viewer of the
organization, and an outsider. A run is put where a test needs it (paused,
failed, succeeded) as the worker would put it, through the run store.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest
from casbin.persist.adapter import load_policy_line
from casbin.persist.adapters.asyncio import AsyncAdapter
from embedding_fake import FakeEmbedding
from fastapi.testclient import TestClient
from forge_task_adk_workflows.run_store import Actor, RunStore, metadata
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai import types
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.adk_workflows.build import build_agent
from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.auth.authorization import get_enforcer, new_enforcer
from forge_admin.auth.tokens import mint_subject_token
from forge_admin.config import Settings
from forge_admin.db.session import get_session
from forge_admin.models import Organization, User

API = "/api/v1"
ORG = "3f6c0000-0000-4000-8000-000000000001"
OTHER_ORG = "3f6c0000-0000-4000-8000-000000000002"
ADMIN, MEMBER, VIEWER, OUTSIDER = "admin-1", "member-1", "viewer-1", "outsider-1"
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
RUNS = f"/organizations/{ORG}/adk-runs"
NO_SUCH_RUN = "The organization has no such ADK workflow run"
UNAVAILABLE = "The ADK workflow runs' database isn't answering; try again shortly"

# The default roles' grants that ADK workflow runs check (the migrations').
GRANTS = {
    "org:admin": (
        "organizations:read",
        "agents:run",
        "agents:approve",
        "agents:manage_runs",
    ),
    "org:member": ("organizations:read", "agents:run"),
    "org:viewer": ("organizations:read",),
}
ROLES = {ADMIN: "org:admin", MEMBER: "org:member", VIEWER: "org:viewer"}
PEOPLE = {ADMIN: ("Ada", "Admin"), MEMBER: ("Mia", "Member")}

RUN_FIELDS = {
    "id",
    "organization_id",
    "agent_id",
    "agent_name",
    "revision",
    "session_id",
    "status",
    "attempt",
    "requested_by",
    "resubmit_of",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "duration_ms",
    "waiting_until",
    "waiting_reason",
    "pause",
    "error",
}
DETAIL_FIELDS = RUN_FIELDS | {
    "input",
    "result",
    "document",
    "trigger",
    "events",
    "actions",
}


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
    agent_id: str,
    name: str,
    nodes: list[dict[str, Any]],
    edges: list[tuple[str, str, str]],
) -> dict[str, Any]:
    return {
        "format": "forge.agent/v1",
        "id": agent_id,
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


SHIP = "ag_ship000001"
OUTER = "ag_outer00001"
BROKEN = "ag_broken0001"
SHIPPING = document(
    SHIP,
    "Ship",
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
)


def running(agent_id: str, saved_id: str, name: str) -> dict[str, Any]:
    """An ADK workflow that runs another, saved one."""
    return document(
        agent_id,
        name,
        [
            node("start", "start", {"input_schema": {}}),
            node("inner", "saved", {"agent": saved_id}),
            end("done"),
        ],
        [("start", "next", "inner"), ("inner", "next", "done")],
    )


QUESTION = {
    "type": "object",
    "required": ["send"],
    "properties": {"send": {"type": "boolean"}, "changes": {"type": "string"}},
}


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Grants(AsyncAdapter):
    """The policy casbin_rule would hold: the default grants, and each
    person's role in the organization."""

    def __init__(self) -> None:
        self.lines = [
            f"p, {role}, {permission.replace(':', ', ')}"
            for role, permissions in GRANTS.items()
            for permission in permissions
        ] + [f"g, {user}, {role}, org:{ORG}*" for user, role in ROLES.items()]

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
    """Stands in for the request's MySQL session: the organizations there
    are, and the people's names."""

    async def get(self, model: type, key: str) -> object | None:
        if model is Organization and key in (ORG, OTHER_ORG):
            return Organization(id=key, name=f"Org {key[-1]}", description="")
        if model is User and key in PEOPLE:
            first, last = PEOPLE[key]
            return User(id=key, first_name=first, last_name=last, email=f"{key}@x.io")
        return None


class Agents:
    """Stands in for the organization's ADK workflows in MongoDB."""

    def __init__(self, *documents: dict[str, Any]) -> None:
        self.records = {
            (ORG, made["id"]): {
                "_id": made["id"],
                "organization_id": ORG,
                "revision": 2,
                "document": made,
            }
            for made in documents
        }

    async def get(self, organization_id: str, agent_id: str) -> dict[str, Any] | None:
        return self.records.get((organization_id, agent_id))

    async def aclose(self) -> None:
        pass


@dataclass
class Workspace:
    client: TestClient
    runs: RunStore
    queue: FakeEmbedding
    sessions: InMemorySessionService
    clock: Clock
    tokens: Callable[[str], str]

    def call(self, function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run a coroutine function on the app's event loop (where the run
        store's connection lives)."""
        assert self.client.portal is not None
        return self.client.portal.call(partial(function, *args, **kwargs))

    def get(self, path: str, user: str = MEMBER, **params: Any) -> Any:
        return self.client.get(f"{API}{path}", headers=self._as(user), params=params)

    def post(self, path: str, user: str = MEMBER, body: Any = None) -> Any:
        return self.client.post(f"{API}{path}", headers=self._as(user), json=body)

    def start(self, agent_id: str = SHIP, user: str = MEMBER, **input: Any) -> dict:
        started = self.post(
            f"/organizations/{ORG}/agents/{agent_id}/runs",
            user,
            {"input": input or {"key": "k-1"}},
        )
        assert started.status_code == 202, started.json()
        return dict(started.json())

    def claimed(self, user: str = MEMBER) -> dict[str, Any]:
        """A run a worker has."""
        run = self.start(user=user)
        self.clock.advance(1)
        assert self.call(self.runs.claim, run["id"], owner="w1", lease_seconds=60)
        return run

    def paused(self, kind: str = "approval", **details: Any) -> dict[str, Any]:
        """A run waiting at an approval, or a question."""
        run = self.claimed()
        pause = self.call(
            self.runs.pause,
            run["id"],
            owner="w1",
            state={},
            key="review",
            kind=kind,
            reason="Ship it?",
            details={"kind": kind, "step": "review", "step_name": "Review", **details},
            deadline=None,
        )
        return {**run, "request_id": pause["id"]}

    def finished(self, succeeded: bool) -> dict[str, Any]:
        run = self.claimed()
        self.clock.advance(2.5)
        self.call(
            self.runs.finish,
            run["id"],
            owner="w1",
            state={},
            succeeded=succeeded,
            result={"outcome": "succeeded", "result": "shipped"} if succeeded else None,
            error=None if succeeded else {"message": "No route", "step": "shape"},
        )
        return run

    def _as(self, user: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens(user)}"}


@pytest.fixture
def workspace(settings: Settings) -> Iterator[Workspace]:
    configured = settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 40), "local_user_id": None}
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    clock = Clock()
    runs = RunStore(
        create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool), clock=clock
    )
    queue, sessions = FakeEmbedding(), InMemorySessionService()
    app.state.adk_runs = runs
    app.state.embedding = queue
    app.state.adk_run_sessions = sessions
    app.state.organization_agents = Agents(
        SHIPPING,
        running(OUTER, SHIP, "Outer"),
        running(BROKEN, "ag_nosuchthing", "Broken"),
    )
    enforcer = new_enforcer(Grants())
    app.dependency_overrides[get_session] = Directory
    app.dependency_overrides[get_enforcer] = lambda: enforcer

    def token(user: str) -> str:
        return mint_subject_token(configured, user, lifetime=timedelta(minutes=5))

    with TestClient(app) as client:
        made = Workspace(client, runs, queue, sessions, clock, token)

        async def create_tables() -> None:
            async with runs.engine.begin() as conn:
                await conn.run_sync(metadata.create_all)

        made.call(create_tables)
        yield made
        made.call(runs.engine.dispose)


# ------------------------------------------------------------------ starting


def test_a_run_starts_queued_with_its_job_as_its_adk_workflow_is_saved(
    workspace: Workspace,
) -> None:
    started = workspace.post(
        f"/organizations/{ORG}/agents/{OUTER}/runs", body={"input": {"n": 1}}
    )
    assert started.status_code == 202, started.json()
    run = started.json()
    assert set(run) == RUN_FIELDS
    assert len(run["id"]) == 32
    assert {k: run[k] for k in ("organization_id", "agent_id", "agent_name")} == {
        "organization_id": ORG,
        "agent_id": OUTER,
        "agent_name": "Outer",
    }
    assert (run["revision"], run["status"], run["attempt"]) == (2, "queued", 1)
    assert run["requested_by"] == {"id": MEMBER, "name": "Mia Member"}
    assert run["created_at"] == run["updated_at"] == "2026-10-01T12:00:00+00:00"
    for empty in ("resubmit_of", "started_at", "finished_at", "duration_ms"):
        assert run[empty] is None
    assert (run["waiting_until"], run["pause"], run["error"]) == (None, None, None)
    # A job takes it.
    assert workspace.queue.queued == [run["id"]]
    # What the worker runs: the ADK workflow and the saved one as they're
    # saved now, in a session of its own, as the member.
    kept = workspace.call(workspace.runs.get, run["id"])
    assert kept["payload"] == {
        "tenant_id": ORG,
        "agent_id": OUTER,
        "revision": 2,
        "name": "Outer",
        "document": running(OUTER, SHIP, "Outer"),
        "saved": {SHIP: SHIPPING},
        "input": {"n": 1},
        "session_id": run["session_id"],
        "run_as": MEMBER,
        "run_as_name": "Mia Member",
        "trigger": {"type": "manual", "by": MEMBER},
    }
    again = workspace.start(OUTER)
    assert again["session_id"] != run["session_id"]


def test_a_run_starts_only_with_input_that_fits_and_a_document_that_builds(
    workspace: Workspace,
) -> None:
    refused = workspace.post(
        f"/organizations/{ORG}/agents/{SHIP}/runs", body={"input": {"key": 7}}
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "The input doesn't fit the start: key: 7 is not of type 'string'"
    )
    unbuilt = workspace.post(f"/organizations/{ORG}/agents/{BROKEN}/runs", body={})
    assert unbuilt.status_code == 422
    assert "the organization has no agent ag_nosuchthing" in unbuilt.json()["detail"]
    assert workspace.queue.queued == []
    assert workspace.get(RUNS).json()["total"] == 0


def test_only_those_who_may_run_adk_workflows_run_them(workspace: Workspace) -> None:
    path = f"/organizations/{ORG}/agents/{SHIP}/runs"
    body = {"input": {"key": "k-1"}}
    viewed = workspace.post(path, VIEWER, body)
    assert viewed.status_code == 403
    assert viewed.json()["detail"] == "Requires agents:run"
    assert workspace.post(path, OUTSIDER, body).status_code == 403
    missing = workspace.post(
        f"/organizations/{ORG}/agents/ag_nosuchthing/runs", body={}
    )
    assert missing.status_code == 404
    unknown = workspace.post(
        f"/organizations/{OTHER_ORG[:-1]}9/agents/{SHIP}/runs", body={}
    )
    assert unknown.status_code == 404
    assert workspace.queue.queued == []


def test_a_run_whose_job_cant_be_queued_stays_queued(workspace: Workspace) -> None:
    workspace.queue.refuse = True
    run = workspace.start()
    assert run["status"] == "queued"
    assert workspace.queue.queued == []
    assert workspace.get(f"{RUNS}/{run['id']}").status_code == 200
    # Without the worker's queue set up at all, nothing starts.
    workspace.client.app.state.embedding = None  # type: ignore[attr-defined]
    down = workspace.post(
        f"/organizations/{ORG}/agents/{SHIP}/runs", body={"input": {"key": "k"}}
    )
    assert down.status_code == 503
    assert "FORGE_ADMIN_EMBEDDING_REDIS_URL" in down.json()["detail"]
    assert workspace.get(RUNS).json()["total"] == 1


# ------------------------------------------------------------------- reading


def test_runs_list_newest_first_by_adk_workflow_and_status(
    workspace: Workspace,
) -> None:
    first = workspace.start()
    workspace.clock.advance(1)
    second = workspace.start(OUTER)
    workspace.clock.advance(1)
    third = workspace.finished(succeeded=False)
    # Another organization's run.
    workspace.call(
        workspace.runs.create,
        organization_id=OTHER_ORG,
        agent_id=SHIP,
        agent_name="Ship",
        revision=1,
        session_id="s-other",
        payload={},
        requested_by=Actor("someone", "Someone"),
    )
    listed = workspace.get(RUNS, VIEWER)
    assert listed.status_code == 200
    page = listed.json()
    assert [r["id"] for r in page["items"]] == [third["id"], second["id"], first["id"]]
    assert page["total"] == 3
    assert set(page["items"][0]) == RUN_FIELDS
    failed = page["items"][0]
    assert failed["status"] == "failed"
    assert failed["error"] == {
        "message": "No route",
        "category": "error",
        "step": "shape",
        "occurred_at": "2026-10-01T12:00:05.500000+00:00",
    }
    assert failed["started_at"] == "2026-10-01T12:00:03+00:00"
    assert failed["duration_ms"] == 2500

    ship = workspace.get(RUNS, agent_id=SHIP).json()
    assert [r["id"] for r in ship["items"]] == [third["id"], first["id"]]
    queued = workspace.get(RUNS, status=["queued", "running"]).json()
    assert [r["id"] for r in queued["items"]] == [second["id"], first["id"]]
    both = workspace.get(RUNS, status=["failed", "queued"]).json()
    assert both["total"] == 3
    paged = workspace.get(RUNS, limit=1, offset=1).json()
    assert ([r["id"] for r in paged["items"]], paged["total"]) == ([second["id"]], 3)

    assert workspace.get(RUNS, OUTSIDER).status_code == 403
    for wrong in ({"status": "done"}, {"agent_id": "wf_1"}, {"limit": 0}):
        assert workspace.get(RUNS, **wrong).status_code == 422


def test_a_runs_page_has_what_it_runs_its_activity_and_what_it_allows(
    workspace: Workspace,
) -> None:
    run = workspace.start(key="k-9")
    page = workspace.get(f"{RUNS}/{run['id']}", VIEWER)
    assert page.status_code == 200
    detail = page.json()
    assert set(detail) == DETAIL_FIELDS
    assert detail["input"] == {"key": "k-9"}
    assert detail["document"] == SHIPPING
    assert detail["trigger"] == {"type": "manual", "by": MEMBER}
    assert detail["result"] is None
    assert detail["events"] == [
        {
            "id": 1,
            "at": "2026-10-01T12:00:00+00:00",
            "kind": "created",
            "message": "Started",
            "actor": {"id": MEMBER, "name": "Mia Member"},
            "attributes": None,
        }
    ]
    assert detail["actions"] == {
        "retry": False,
        "resubmit": False,
        "abandon": True,
        "decide": False,
        "answer": False,
    }

    done = workspace.finished(succeeded=True)
    finished = workspace.get(f"{RUNS}/{done['id']}").json()
    assert finished["status"] == "succeeded"
    assert finished["result"] == {"outcome": "succeeded", "result": "shipped"}
    assert finished["duration_ms"] == 2500
    assert [(e["kind"], e["actor"]) for e in finished["events"]] == [
        ("created", {"id": MEMBER, "name": "Mia Member"}),
        ("started", None),
        ("succeeded", None),
    ]
    assert finished["actions"] == {
        "retry": False,
        "resubmit": True,
        "abandon": False,
        "decide": False,
        "answer": False,
    }


def test_only_the_organizations_runs_are_found(workspace: Workspace) -> None:
    theirs = workspace.call(
        workspace.runs.create,
        organization_id=OTHER_ORG,
        agent_id=SHIP,
        agent_name="Ship",
        revision=1,
        session_id="s-other",
        payload={"document": SHIPPING, "run_as": "x"},
        requested_by=Actor("someone", "Someone"),
    )
    for run_id in (theirs["id"], "0" * 32, "an-old-task-id"):
        for path in ("", "/steps"):
            missing = workspace.get(f"{RUNS}/{run_id}{path}")
            assert missing.status_code == 404
            assert missing.json()["detail"] == NO_SUCH_RUN
    mine = workspace.start()
    assert workspace.get(f"{RUNS}/{mine['id']}", OUTSIDER).status_code == 403
    assert workspace.get(f"{RUNS}/not%20an%20id").status_code == 422


# --------------------------------------------------------------------- steps


async def run_to_the_approval(
    sessions: BaseSessionService, made: dict[str, Any], member: str, session_id: str
) -> None:
    """Run the shipping ADK workflow in its session, as the worker does, to
    its approval."""
    session = await sessions.create_session(
        app_name="adk_workflows", user_id=member, session_id=session_id
    )
    runner = Runner(
        node=build_agent(made), session_service=sessions, app_name="adk_workflows"
    )
    message = types.Content(role="user", parts=[types.Part(text='{"key": "k-1"}')])
    async for _ in runner.run_async(
        user_id=member, session_id=session.id, new_message=message
    ):
        pass


def test_a_runs_steps_are_read_from_its_adk_session(workspace: Workspace) -> None:
    run = workspace.start()
    # Before the worker made its session, nothing is reached yet.
    before = workspace.get(f"{RUNS}/{run['id']}/steps", VIEWER)
    assert before.status_code == 200, before.json()
    assert before.json()["session_id"] == run["session_id"]
    assert {step["status"] for step in before.json()["steps"]} == {"not_reached"}

    workspace.call(
        run_to_the_approval, workspace.sessions, SHIPPING, MEMBER, run["session_id"]
    )
    read = workspace.get(f"{RUNS}/{run['id']}/steps", VIEWER).json()
    assert [(s["id"], s["status"]) for s in read["steps"]] == [
        ("start", "done"),
        ("shape", "done"),
        ("review", "waiting"),
        ("yes", "not_reached"),
        ("no", "not_reached"),
    ]
    assert read["steps"][1]["output"] == {"loud": "K-1"}
    outsider = workspace.get(f"{RUNS}/{run['id']}/steps", OUTSIDER)
    assert outsider.status_code == 403


# ----------------------------------------------------------------- decisions


def test_approvals_are_decided_by_whom_the_step_names(workspace: Workspace) -> None:
    run = workspace.paused(approvers="org:admin")
    decisions = f"{RUNS}/{run['id']}/decisions"
    paused = workspace.get(f"{RUNS}/{run['id']}").json()
    assert paused["status"] == "paused"
    assert paused["pause"] == {
        "id": run["request_id"],
        "kind": "approval",
        "reason": "Ship it?",
        "details": {
            "kind": "approval",
            "step": "review",
            "step_name": "Review",
            "approvers": "org:admin",
        },
        "requested_at": "2026-10-01T12:00:01+00:00",
        "deadline": None,
    }
    assert paused["actions"]["decide"] and not paused["actions"]["answer"]
    decision = {"request_id": run["request_id"], "approved": True, "comment": " ok "}
    by_member = workspace.post(decisions, MEMBER, decision)
    assert by_member.status_code == 403
    assert by_member.json()["detail"] == "Requires agents:approve"
    workspace.queue.queued.clear()
    by_admin = workspace.post(decisions, ADMIN, decision)
    assert by_admin.status_code == 202, by_admin.json()
    decided = by_admin.json()
    assert set(decided) == RUN_FIELDS
    assert (decided["status"], decided["pause"]) == ("queued", None)
    assert workspace.queue.queued == [run["id"]]
    kept = workspace.call(workspace.runs.get, run["id"])
    assert kept["decisions"]["review"]["approved"] is True
    assert kept["decisions"]["review"]["comment"] == "ok"
    assert kept["decisions"]["review"]["actor_name"] == "Ada Admin"
    [*_, event] = workspace.get(f"{RUNS}/{run['id']}").json()["events"]
    assert (event["kind"], event["message"]) == ("decided", "Approved: Review")
    assert event["actor"] == {"id": ADMIN, "name": "Ada Admin"}
    # Decided already: it isn't open any more.
    again = workspace.post(decisions, ADMIN, decision)
    assert again.status_code == 409
    assert "doesn't wait at that approval" in again.json()["detail"]

    # One any member may decide.
    members = workspace.paused(approvers="org:member")
    path = f"{RUNS}/{members['id']}/decisions"
    viewed = workspace.post(
        path, VIEWER, {"request_id": members["request_id"], "approved": True}
    )
    assert viewed.status_code == 403
    assert viewed.json()["detail"] == "Requires agents:run"
    stale = workspace.post(path, MEMBER, {"request_id": "req-old", "approved": True})
    assert stale.status_code == 409
    rejected = workspace.post(
        path, MEMBER, {"request_id": members["request_id"], "approved": False}
    )
    assert rejected.status_code == 202
    kept = workspace.call(workspace.runs.get, members["id"])
    assert kept["decisions"]["review"]["approved"] is False
    assert (
        workspace.post(
            path, OUTSIDER, {"request_id": "x", "approved": True}
        ).status_code
        == 403
    )


def test_a_question_isnt_decided_nor_an_approval_answered(
    workspace: Workspace,
) -> None:
    asks = workspace.paused("human_input", response_schema=QUESTION)
    decided = workspace.post(
        f"{RUNS}/{asks['id']}/decisions",
        ADMIN,
        {"request_id": asks["request_id"], "approved": True},
    )
    assert decided.status_code == 409
    assert "waits for an answer" in decided.json()["detail"]
    approval = workspace.paused(approvers="org:member")
    answered = workspace.post(
        f"{RUNS}/{approval['id']}/answers",
        MEMBER,
        {"request_id": approval["request_id"], "answer": {"send": True}},
    )
    assert answered.status_code == 409
    assert "waits for a decision" in answered.json()["detail"]
    # Neither is paused: nothing to decide or answer.
    queued = workspace.start()
    for route, body in (
        ("decisions", {"request_id": "x", "approved": True}),
        ("answers", {"request_id": "x", "answer": {}}),
    ):
        refused = workspace.post(f"{RUNS}/{queued['id']}/{route}", ADMIN, body)
        assert refused.status_code == 409
        missing = workspace.post(f"{RUNS}/{'0' * 32}/{route}", ADMIN, body)
        assert missing.status_code == 404


# ------------------------------------------------------------------- answers


def test_questions_are_answered_with_what_they_ask_for(workspace: Workspace) -> None:
    asks = workspace.paused("human_input", response_schema=QUESTION)
    answers = f"{RUNS}/{asks['id']}/answers"
    detail = workspace.get(f"{RUNS}/{asks['id']}").json()
    assert detail["pause"]["kind"] == "human_input"
    assert detail["actions"]["answer"] and not detail["actions"]["decide"]
    bad = workspace.post(
        answers,
        MEMBER,
        {"request_id": asks["request_id"], "answer": {"send": "yes", "changes": 1}},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"] == (
        "The answer doesn't fit what the question asks: changes: 1 is not of type "
        "'string'; send: 'yes' is not of type 'boolean'"
    )
    viewed = workspace.post(
        answers, VIEWER, {"request_id": asks["request_id"], "answer": {"send": True}}
    )
    assert viewed.status_code == 403
    assert viewed.json()["detail"] == "Requires agents:run"
    workspace.queue.queued.clear()
    good = workspace.post(
        answers,
        MEMBER,
        {"request_id": asks["request_id"], "answer": {"send": True, "changes": "Less"}},
    )
    assert good.status_code == 202, good.json()
    assert good.json()["status"] == "queued"
    assert workspace.queue.queued == [asks["id"]]
    kept = workspace.call(workspace.runs.get, asks["id"])["decisions"]["review"]
    assert kept["approved"] is True and kept["actor_id"] == MEMBER
    assert json.loads(kept["comment"]) == {"send": True, "changes": "Less"}
    events = workspace.get(f"{RUNS}/{asks['id']}").json()["events"]
    assert events[-1]["kind"] == "answered"
    stale = workspace.post(
        answers, MEMBER, {"request_id": asks["request_id"], "answer": {"send": True}}
    )
    assert stale.status_code == 409


# ---------------------------------------------- retrying, resubmitting, abandoning


def test_retry_resubmit_and_abandon_need_agents_manage_runs(
    workspace: Workspace,
) -> None:
    failed = workspace.finished(succeeded=False)
    for action in ("retry", "resubmit", "abandon"):
        path = f"{RUNS}/{failed['id']}/{action}"
        refused = workspace.post(path, MEMBER)
        assert refused.status_code == 403
        assert refused.json()["detail"] == "Requires agents:manage_runs"
        assert workspace.post(path, OUTSIDER).status_code == 403
        missing = workspace.post(f"{RUNS}/{'0' * 32}/{action}", ADMIN)
        assert missing.status_code == 404
        assert missing.json()["detail"] == NO_SUCH_RUN


def test_a_failed_run_is_retried_as_its_next_attempt(workspace: Workspace) -> None:
    queued = workspace.start()
    refused = workspace.post(f"{RUNS}/{queued['id']}/retry", ADMIN)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Only a failed run can be retried"
    failed = workspace.finished(succeeded=False)
    detail = workspace.get(f"{RUNS}/{failed['id']}").json()
    assert detail["actions"] == {
        "retry": True,
        "resubmit": True,
        "abandon": True,
        "decide": False,
        "answer": False,
    }
    workspace.queue.queued.clear()
    retried = workspace.post(f"{RUNS}/{failed['id']}/retry", ADMIN)
    assert retried.status_code == 202, retried.json()
    run = retried.json()
    assert (run["id"], run["status"], run["attempt"]) == (failed["id"], "queued", 2)
    assert run["finished_at"] is None
    assert workspace.queue.queued == [failed["id"]]
    [*_, event] = workspace.get(f"{RUNS}/{failed['id']}").json()["events"]
    assert (event["kind"], event["actor"]["id"]) == ("retried", ADMIN)


def test_a_finished_run_is_resubmitted_as_a_new_run(workspace: Workspace) -> None:
    queued = workspace.start()
    refused = workspace.post(f"{RUNS}/{queued['id']}/resubmit", ADMIN)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Only a finished run can be resubmitted"
    done = workspace.finished(succeeded=True)
    workspace.queue.queued.clear()
    resubmitted = workspace.post(f"{RUNS}/{done['id']}/resubmit", ADMIN)
    assert resubmitted.status_code == 202, resubmitted.json()
    again = resubmitted.json()
    assert again["id"] != done["id"] and again["resubmit_of"] == done["id"]
    assert (again["status"], again["attempt"]) == ("queued", 1)
    assert again["session_id"] == done["session_id"]
    assert again["requested_by"] == {"id": ADMIN, "name": "Ada Admin"}
    assert workspace.queue.queued == [again["id"]]
    original = workspace.call(workspace.runs.get, done["id"])
    copy = workspace.call(workspace.runs.get, again["id"])
    assert copy["payload"] == original["payload"]


def test_a_run_is_abandoned_unless_its_running_or_finished(
    workspace: Workspace,
) -> None:
    running_run = workspace.claimed()
    refused = workspace.post(f"{RUNS}/{running_run['id']}/abandon", ADMIN)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "A running or finished run can't be abandoned"
    done = workspace.finished(succeeded=True)
    assert workspace.post(f"{RUNS}/{done['id']}/abandon", ADMIN).status_code == 409
    paused = workspace.paused()
    workspace.queue.queued.clear()
    abandoned = workspace.post(f"{RUNS}/{paused['id']}/abandon", ADMIN)
    assert abandoned.status_code == 200, abandoned.json()
    run = abandoned.json()
    assert (run["status"], run["pause"]) == ("abandoned", None)
    assert run["finished_at"] is not None
    # Nothing carries it on.
    assert workspace.queue.queued == []
    failed = workspace.finished(succeeded=False)
    assert workspace.post(f"{RUNS}/{failed['id']}/abandon", ADMIN).status_code == 200


# ------------------------------------------------------------- unavailability


def test_a_run_store_that_isnt_answering_is_a_503(workspace: Workspace) -> None:
    run = workspace.start()
    down = RunStore(create_async_engine("sqlite+aiosqlite:////nonexistent/dir/runs.db"))
    workspace.client.app.state.adk_runs = down  # type: ignore[attr-defined]
    for answer in (
        workspace.get(RUNS),
        workspace.get(f"{RUNS}/{run['id']}"),
        workspace.post(f"{RUNS}/{run['id']}/abandon", ADMIN),
        workspace.post(
            f"/organizations/{ORG}/agents/{SHIP}/runs", body={"input": {"key": "k"}}
        ),
    ):
        assert answer.status_code == 503
        assert answer.json()["detail"] == UNAVAILABLE
