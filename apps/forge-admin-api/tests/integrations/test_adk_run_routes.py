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
from forge_task_adk_workflows.runs import MAX_NOTES, STATE_KEY
from google.adk.artifacts import InMemoryArtifactService
from google.adk.events import Event, EventActions
from google.adk.events.event import NodeInfo
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai import types
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.adk_workflows.build import build_agent
from forge_admin.api.app import ROUTERS
from forge_admin.api.routes.adk_workflow_runs import a2a_task
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
    "version",
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
    "cancel_requested_at",
    "parent_run_id",
    "next_attempt_at",
}
DETAIL_FIELDS = RUN_FIELDS | {
    "input",
    "files",
    "result",
    "document",
    "trigger",
    "events",
    "activity_incomplete",
    "children",
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
PAPERS = "ag_papers0001"
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
    app.state.workflow_artifacts = InMemoryArtifactService()
    app.state.organization_agents = Agents(
        SHIPPING,
        running(OUTER, SHIP, "Outer"),
        running(BROKEN, "ag_nosuchthing", "Broken"),
        document(
            PAPERS,
            "Papers",
            [
                node(
                    "start",
                    "start",
                    {"input_schema": {}, "allow_files": True, "file_types": ["pdf"]},
                ),
                end("done"),
            ],
            [("start", "next", "done")],
        ),
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
    assert run["version"] == "draft"
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
        # Never published: its draft.
        "version": "draft",
        "name": "Outer",
        "document": running(OUTER, SHIP, "Outer"),
        "saved": {SHIP: SHIPPING},
        "chat_agents": {},
        "knowledge_bases": {},
        "skills": {},
        "input": {"n": 1},
        "files": [],
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
    sessions: BaseSessionService,
    made: dict[str, Any],
    member: str,
    session_id: str,
    invocation_id: str,
) -> None:
    """Run the shipping ADK workflow in a session, as the worker does (one
    invocation, its run's), to its approval."""
    session = await sessions.create_session(
        app_name="adk_workflows", user_id=member, session_id=session_id
    )
    runner = Runner(
        node=build_agent(made), session_service=sessions, app_name="adk_workflows"
    )
    message = types.Content(role="user", parts=[types.Part(text='{"key": "k-1"}')])
    async for _ in runner.run_async(
        user_id=member,
        session_id=session.id,
        new_message=message,
        invocation_id=invocation_id,
    ):
        pass


async def kept_by_the_worker(runs: RunStore, run_id: str, kept: dict[str, Any]) -> None:
    """What the worker keeps of a run's ADK workflow (its session, invocation,
    the sessions it ran in before), as it checkpoints it."""
    await runs.claim(run_id, owner="worker-1", lease_seconds=60)
    await runs.checkpoint(run_id, owner="worker-1", state={"adk_workflow": kept})


def test_a_runs_steps_are_read_from_its_adk_session(workspace: Workspace) -> None:
    run = workspace.start()
    # Before the worker made its session, nothing is reached yet.
    before = workspace.get(f"{RUNS}/{run['id']}/steps", VIEWER)
    assert before.status_code == 200, before.json()
    assert before.json()["session_id"] == run["session_id"]
    assert {step["status"] for step in before.json()["steps"]} == {"not_reached"}

    workspace.call(
        run_to_the_approval,
        workspace.sessions,
        SHIPPING,
        MEMBER,
        run["session_id"],
        "e-this-run",
    )
    workspace.call(
        kept_by_the_worker,
        workspace.runs,
        run["id"],
        {"session_id": run["session_id"], "invocation_id": "e-this-run"},
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
    # The edges it went along, and the step it waits at was handed to.
    assert set(read["taken_edges"]) >= {"start:next->shape", "shape:next->review"}
    assert read["steps"][2]["handed_to"] is True
    outsider = workspace.get(f"{RUNS}/{run['id']}/steps", OUTSIDER)
    assert outsider.status_code == 403


def test_a_runs_steps_are_its_own_from_the_session_it_runs_in_now(
    workspace: Workspace,
) -> None:
    run = workspace.start()
    later = f"{run['session_id']}-r1"
    # Another run of its first session (resubmitted before resubmissions had
    # sessions of their own) waits there; this one started over in another.
    for session_id, invocation in (
        (run["session_id"], "e-another-run"),
        (later, "e-this-run"),
    ):
        workspace.call(
            run_to_the_approval,
            workspace.sessions,
            SHIPPING,
            MEMBER,
            session_id,
            invocation,
        )
    workspace.call(
        kept_by_the_worker,
        workspace.runs,
        run["id"],
        {
            "session_id": later,
            "invocation_id": "e-this-run",
            "earlier_sessions": [run["session_id"]],
        },
    )

    now = workspace.get(f"{RUNS}/{run['id']}/steps", VIEWER).json()

    assert (now["session_id"], now["earlier_sessions"]) == (later, [run["session_id"]])
    assert [s["status"] for s in now["steps"]][:3] == ["done", "done", "waiting"]
    # Its first session holds the other run's steps, and none of its own.
    first = workspace.get(
        f"{RUNS}/{run['id']}/steps", VIEWER, session_id=run["session_id"]
    )
    assert first.status_code == 200 and first.json()["session_id"] == run["session_id"]
    assert {step["status"] for step in first.json()["steps"]} == {"not_reached"}
    never = workspace.get(f"{RUNS}/{run['id']}/steps", VIEWER, session_id="elsewhere")
    assert never.status_code == 404
    assert never.json()["detail"] == "The run never ran in that session"


def test_a_runs_trace_lists_its_events_and_state_for_its_admins(
    workspace: Workspace,
) -> None:
    run = workspace.start()
    path = f"{RUNS}/{run['id']}/trace"
    # Before the worker made its session, there's nothing to list.
    before = workspace.get(path, ADMIN)
    assert before.status_code == 200, before.json()
    assert (before.json()["events"], before.json()["invocation_id"]) == ([], None)

    workspace.call(
        run_to_the_approval,
        workspace.sessions,
        SHIPPING,
        MEMBER,
        run["session_id"],
        "e-this-run",
    )
    kept = {"session_id": run["session_id"], "invocation_id": "e-this-run"}
    workspace.call(kept_by_the_worker, workspace.runs, run["id"], kept)

    read = workspace.get(path, ADMIN).json()
    assert read["invocation_id"] == "e-this-run"
    assert read["session_id"] == run["session_id"]
    assert read["worker"] == kept
    events = read["events"]
    assert events[0]["author"] == "user" and events[0]["text"] == '{"key": "k-1"}'
    shape = next(event for event in events if event["step"] == "shape")
    assert shape["output"] == {"loud": "K-1"}
    assert read["state_before"] == {}
    assert read["state_after"]["input"] == {"key": "k-1"}
    # The steps say what each was handed, and what it set.
    steps = workspace.get(f"{RUNS}/{run['id']}/steps", VIEWER).json()["steps"]
    by_id = {step["id"]: step for step in steps}
    assert (by_id["shape"]["input_from"], by_id["shape"]["input"]) == (
        "start",
        {"key": "k-1"},
    )
    assert [change["key"] for change in by_id["start"]["state_changes"]] == ["input"]

    # Only those who manage the organization's runs read it.
    for who in (MEMBER, VIEWER, OUTSIDER):
        assert workspace.get(path, who).status_code == 403
    missing = workspace.get(f"{RUNS}/{'0' * 32}/trace", ADMIN)
    assert missing.status_code == 404
    never = workspace.get(path, ADMIN, session_id="elsewhere")
    assert never.status_code == 404


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


def test_only_a_run_an_a2a_task_started_is_its_tasks() -> None:
    # Its task ended for good when it failed: it isn't retried.
    a2a = {"type": "runtime", "protocol": "a2a", "task_id": "t-1", "context_id": "c-1"}
    assert a2a_task({"payload": {"trigger": a2a}, "resubmit_of": None})
    # Its resubmission is a run of its own; a run over the REST API has no task.
    assert not a2a_task({"payload": {"trigger": a2a}, "resubmit_of": "r" * 32})
    assert not a2a_task(
        {"payload": {"trigger": {"type": "runtime", "protocol": "rest"}}}
    )
    assert not a2a_task({"payload": {"trigger": {"type": "manual", "by": "u-1"}}})
    assert not a2a_task({"payload": {}})


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
    # A session of its own: two runs of one session would take each other's answers.
    assert again["session_id"] != done["session_id"]
    assert again["requested_by"] == {"id": ADMIN, "name": "Ada Admin"}
    assert workspace.queue.queued == [again["id"]]
    original = workspace.call(workspace.runs.get, done["id"])
    copy = workspace.call(workspace.runs.get, again["id"])
    assert copy["payload"] == {**original["payload"], "session_id": again["session_id"]}


def test_a_run_is_abandoned_unless_it_succeeded_or_was_abandoned(
    workspace: Workspace,
) -> None:
    done = workspace.finished(succeeded=True)
    refused = workspace.post(f"{RUNS}/{done['id']}/abandon", ADMIN)
    assert refused.status_code == 409
    assert (
        refused.json()["detail"]
        == "A run that succeeded or was abandoned can't be abandoned"
    )
    paused = workspace.paused()
    workspace.queue.queued.clear()
    abandoned = workspace.post(f"{RUNS}/{paused['id']}/abandon", ADMIN)
    assert abandoned.status_code == 200, abandoned.json()
    run = abandoned.json()
    assert (run["status"], run["pause"], run["cancel_requested_at"]) == (
        "abandoned",
        None,
        None,
    )
    assert run["finished_at"] is not None
    # Nothing carries it on, and it's abandoned for good.
    assert workspace.queue.queued == []
    assert workspace.post(f"{RUNS}/{paused['id']}/abandon", ADMIN).status_code == 409
    failed = workspace.finished(succeeded=False)
    assert workspace.post(f"{RUNS}/{failed['id']}/abandon", ADMIN).status_code == 200


def test_a_running_run_is_stopped_by_its_worker_then_abandoned(
    workspace: Workspace,
) -> None:
    running = workspace.claimed()
    detail = workspace.get(f"{RUNS}/{running['id']}").json()
    assert detail["actions"]["abandon"] is True
    assert detail["cancel_requested_at"] is None

    asked = workspace.post(f"{RUNS}/{running['id']}/abandon", ADMIN)

    assert asked.status_code == 202, asked.json()
    run = asked.json()
    assert run["status"] == "running" and run["cancel_requested_at"] is not None
    detail = workspace.get(f"{RUNS}/{running['id']}").json()
    assert detail["actions"]["abandon"] is False  # asked already
    [*_, event] = detail["events"]
    assert (event["kind"], event["actor"]["id"]) == ("cancel_requested", ADMIN)
    # Asking again changes nothing.
    again = workspace.post(f"{RUNS}/{running['id']}/abandon", ADMIN)
    assert again.status_code == 202
    assert again.json()["cancel_requested_at"] == run["cancel_requested_at"]
    # Its worker stops it, and abandons it.
    workspace.call(workspace.runs.abandon_running, running["id"], owner="w1", state={})
    stopped = workspace.get(f"{RUNS}/{running['id']}").json()
    assert stopped["status"] == "abandoned"
    assert stopped["actions"]["abandon"] is False


def test_a_run_shows_the_runs_it_started_and_abandoning_it_abandons_them(
    workspace: Workspace,
) -> None:
    parent = workspace.claimed()
    child, _ = workspace.call(
        workspace.runs.create_child,
        run_id="c" * 32,
        parent_run_id=parent["id"],
        owner="w1",
        organization_id=ORG,
        agent_id=SHIP,
        agent_name="Refunds",
        revision=0,
        session_id="child-session",
        payload={"input": {}, "trigger": {"type": "workflow", "run_id": parent["id"]}},
        requested_by=Actor(MEMBER, "Mia Member"),
    )

    detail = workspace.get(f"{RUNS}/{parent['id']}").json()
    assert [(c["id"], c["parent_run_id"]) for c in detail["children"]] == [
        (child["id"], parent["id"])
    ]
    assert detail["parent_run_id"] is None
    assert (
        workspace.get(f"{RUNS}/{child['id']}").json()["parent_run_id"] == parent["id"]
    )

    asked = workspace.post(f"{RUNS}/{parent['id']}/abandon", ADMIN)

    assert asked.status_code == 202, asked.json()
    abandoned = workspace.get(f"{RUNS}/{child['id']}").json()
    assert abandoned["status"] == "abandoned"
    [*_, event] = abandoned["events"]
    assert event["message"] == "Abandoned: the run that started it was abandoned"


def test_a_runs_page_says_when_its_activity_misses_notes(
    workspace: Workspace,
) -> None:
    run = workspace.claimed()
    assert workspace.get(f"{RUNS}/{run['id']}").json()["activity_incomplete"] is False
    for kept, incomplete in (
        ({"notes": MAX_NOTES}, False),
        ({"notes": MAX_NOTES + 1}, True),  # its last note said the rest aren't noted
        ({"notes": 3, "notes_lost": 1}, True),  # one couldn't be written
    ):
        workspace.call(
            workspace.runs.checkpoint, run["id"], owner="w1", state={STATE_KEY: kept}
        )
        detail = workspace.get(f"{RUNS}/{run['id']}").json()
        assert detail["activity_incomplete"] is incomplete, kept


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


# ------------------------------------------------------------------ files


def test_a_run_started_with_files_lists_them_and_each_downloads(
    workspace: Workspace,
) -> None:
    pdf = b"%PDF-1.7 the quarter"
    started = workspace.client.post(
        f"{API}/organizations/{ORG}/agents/{PAPERS}/runs",
        headers=workspace._as(MEMBER),
        data={"input": json.dumps({"quarter": 3})},
        files=[("files", ("Q3 report.pdf", pdf, "application/pdf"))],
    )
    assert started.status_code == 202, started.json()
    run = started.json()
    detail = workspace.get(f"{RUNS}/{run['id']}").json()
    assert detail["input"] == {"quarter": 3}
    assert detail["files"] == [
        {
            "name": "Q3 report.pdf",
            "media_type": "application/pdf",
            "size_bytes": len(pdf),
            "type": "pdf",
            "version": 0,
        }
    ]
    path = f"{RUNS}/{run['id']}/files/Q3 report.pdf"
    # Anyone who reads the organization's runs downloads it, as it was sent.
    for reader in (MEMBER, VIEWER):
        got = workspace.get(path, reader)
        assert got.status_code == 200
        assert got.content == pdf
        assert got.headers["content-type"] == "application/pdf"
        assert got.headers["content-disposition"] == (
            "attachment; filename=\"Q3 report.pdf\"; filename*=UTF-8''Q3%20report.pdf"
        )
        assert got.headers["x-content-type-options"] == "nosniff"
    assert workspace.get(path, OUTSIDER).status_code == 403
    missing = workspace.get(f"{RUNS}/{run['id']}/files/other.pdf")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "The run has no such file"
    # A resubmitted run, in a session of its own, has the original's file.
    assert workspace.call(workspace.runs.claim, run["id"], owner="w1", lease_seconds=60)
    workspace.call(
        workspace.runs.finish, run["id"], owner="w1", state=None, succeeded=True
    )
    again = workspace.post(f"{RUNS}/{run['id']}/resubmit", ADMIN).json()
    assert again["session_id"] != run["session_id"]
    assert workspace.get(f"{RUNS}/{again['id']}/files/Q3 report.pdf").content == pdf


async def saved_by_an_agent(
    sessions: BaseSessionService,
    artifacts: InMemoryArtifactService,
    session_id: str,
    content: bytes,
) -> None:
    """A file the run's ``done`` step saved, as ``save_file`` does: the
    artifact, and the event that says so."""
    found = await sessions.get_session(
        app_name="adk_workflows", user_id=MEMBER, session_id=session_id
    ) or await sessions.create_session(
        app_name="adk_workflows", user_id=MEMBER, session_id=session_id
    )
    version = await artifacts.save_artifact(
        app_name="adk_workflows",
        user_id=MEMBER,
        session_id=session_id,
        filename="summary.md",
        artifact=types.Part(
            inline_data=types.Blob(data=content, mime_type="text/markdown")
        ),
        custom_metadata={"size_bytes": len(content), "saved_by": "done"},
    )
    await sessions.append_event(
        found,
        Event(
            invocation_id="e-this-run",
            author="done",
            actions=EventActions(artifact_delta={"summary.md": version}),
            node_info=NodeInfo(path="papers@1/done@1"),
        ),
    )


def test_the_files_a_run_saved_are_listed_and_each_version_downloads(
    workspace: Workspace,
) -> None:
    pdf = b"%PDF-1.7 the quarter"
    run = workspace.client.post(
        f"{API}/organizations/{ORG}/agents/{PAPERS}/runs",
        headers=workspace._as(MEMBER),
        data={"input": json.dumps({})},
        files=[("files", ("Q3.pdf", pdf, "application/pdf"))],
    ).json()
    artifacts = workspace.client.app.state.workflow_artifacts  # type: ignore[attr-defined]
    for content in (b"# First", b"# Second"):
        workspace.call(
            saved_by_an_agent, workspace.sessions, artifacts, run["session_id"], content
        )
    workspace.call(
        kept_by_the_worker,
        workspace.runs,
        run["id"],
        {"session_id": run["session_id"], "invocation_id": "e-this-run"},
    )

    listed = workspace.get(f"{RUNS}/{run['id']}/session-files", VIEWER)

    assert listed.status_code == 200, listed.json()
    body = listed.json()
    assert (body["session_id"], body["kept"]) == (run["session_id"], True)
    by_name = {file["name"]: file for file in body["files"]}
    assert by_name["Q3.pdf"]["origin"] == "input"
    assert by_name["Q3.pdf"]["versions"][0]["size_bytes"] == len(pdf)
    summary = by_name["summary.md"]
    assert (summary["origin"], summary["media_type"]) == ("run", "text/markdown")
    assert [
        (v["version"], v["size_bytes"], v["step"], v["saved_by"])
        for v in summary["versions"]
    ] == [(0, 7, "done", "done"), (1, 8, "done", "done")]
    assert all(v["saved_at"] for v in summary["versions"])
    # Its latest version downloads, or the one asked for.
    path = f"{RUNS}/{run['id']}/files/summary.md"
    latest = workspace.get(path, VIEWER)
    assert (latest.status_code, latest.content) == (200, b"# Second")
    assert latest.headers["content-type"].startswith("text/markdown")
    assert workspace.get(path, VIEWER, version=0).content == b"# First"
    assert workspace.get(path, VIEWER, version=7).status_code == 404
    assert (
        workspace.get(f"{RUNS}/{run['id']}/session-files", OUTSIDER).status_code == 403
    )


def test_without_a_place_for_files_a_run_lists_none(workspace: Workspace) -> None:
    run = workspace.start()
    workspace.client.app.state.workflow_artifacts = None  # type: ignore[attr-defined]
    listed = workspace.get(f"{RUNS}/{run['id']}/session-files").json()
    assert (listed["files"], listed["kept"]) == ([], False)


def test_files_a_start_doesnt_take_are_refused(workspace: Workspace) -> None:
    def send(agent_id: str, name: str) -> Any:
        return workspace.client.post(
            f"{API}/organizations/{ORG}/agents/{agent_id}/runs",
            headers=workspace._as(MEMBER),
            data={"input": json.dumps({"key": "k-1"})},
            files=[("files", (name, b"data", "application/octet-stream"))],
        )

    wrong_type = send(PAPERS, "notes.txt")
    assert wrong_type.status_code == 422
    assert wrong_type.json()["detail"] == (
        "notes.txt: the workflow's start takes pdf files only"
    )
    none_taken = send(SHIP, "report.pdf")
    assert none_taken.status_code == 422
    assert none_taken.json()["detail"] == "The workflow's start doesn't take files"
    assert workspace.queue.queued == []
