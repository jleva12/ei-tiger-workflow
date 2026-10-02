"""Running an organization's ADK workflows, against MySQL and MongoDB: a member
runs one as themselves, on its own queue; its runs are the organization's
background tasks labelled with it; a run's steps are read from its ADK
session; its approvals are decided by whom the step names, with the ADK
workflows' permissions; its questions are answered with what they ask for.

A site administrator builds an organization with an admin, a member and a
viewer. Runs go to a fake of the worker's queues, background tasks to a fake
of its API, and ADK sessions are kept in memory. ADK workflows are kept in a
MongoDB database of the test's own, dropped afterwards.
"""

import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from background_tasks_fake import FakeBackgroundTasks, task
from embedding_fake import FakeEmbedding
from fastapi.testclient import TestClient
from google.adk.runners import Runner
from google.adk.sessions import (
    BaseSessionService,
    DatabaseSessionService,
    InMemorySessionService,
)
from google.genai import types
from pymongo import MongoClient

from forge_admin.agent_build import build_agent
from forge_admin.api.app import PUBLIC_ROUTERS, ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.config import Settings
from forge_admin.db.session import create_engine

pytestmark = pytest.mark.mysql

API = "/api/v1"
NO_SUCH_RUN = "The organization has no such ADK workflow run"


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


START = node(
    "start",
    "start",
    {
        "input_schema": {
            "type": "object",
            "required": ["key"],
            "properties": {"key": {"type": "string"}},
        }
    },
)


def end(node_id: str, result: str = "") -> dict[str, Any]:
    return node(node_id, "end", {"outcome": "succeeded", "result": result}, [])


def shipping() -> dict[str, Any]:
    """Start, Transform, Approval by the admins, then an End either way."""
    return document(
        [
            START,
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


QUESTION = {
    "type": "object",
    "required": ["send"],
    "properties": {"send": {"type": "boolean"}, "changes": {"type": "string"}},
}


@dataclass
class Organization:
    id: str
    admin_id: str
    member_id: str


@dataclass
class Runs:
    organization: Organization
    queue: FakeEmbedding
    tasks: FakeBackgroundTasks
    sessions: InMemorySessionService
    admin: TestClient
    member: TestClient
    viewer: TestClient
    outsider: TestClient
    # A client as a user whose ADK sessions are the admin database's own.
    with_database_sessions: Callable[[str], TestClient]
    settings: Settings

    @property
    def agents(self) -> str:
        return f"{API}/organizations/{self.organization.id}/agents"

    @property
    def adk_runs(self) -> str:
        return f"{API}/organizations/{self.organization.id}/adk-runs"

    def create(self, made: dict[str, Any]) -> dict[str, Any]:
        created = self.member.post(self.agents, json={"document": made})
        assert created.status_code == 201, created.json()
        return dict(created.json())

    def waiting(self, task_id: str, **details: Any) -> None:
        """A run of this organization waiting at an approval or question."""
        self.tasks.add(
            task(
                task_id,
                self.organization.id,
                "AWAITING_VALIDATION",
                task_type="adk_workflows",
                labels={
                    "tenant": self.organization.id,
                    "task_type": "adk_workflows",
                    "adk_workflow": "ag_whatever01",
                    "adk_session": f"s-{task_id}",
                },
                approval={
                    "id": f"req-{task_id}",
                    "reason": "Ship it?",
                    "details": {"step": "review", "step_name": "Review", **details},
                },
            )
        )


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
    queue, tasks, sessions = (
        FakeEmbedding(),
        FakeBackgroundTasks(),
        InMemorySessionService(),
    )
    clients: list[TestClient] = []

    def acting_as(user: str, in_memory: bool = True) -> TestClient:
        settings = mysql_settings.model_copy(
            update={
                "local_user_id": user,
                "mongo_database": database,
                # Whatever the local .env says: the fakes stand in.
                "embedding_redis_url": None,
                "async_worker_url": None,
                "async_worker_token": None,
            }
        )
        app = ApiServer(
            settings, routers=ROUTERS, public_routers=PUBLIC_ROUTERS
        ).create_app()
        app.state.embedding = queue
        app.state.background_tasks = tasks
        if in_memory:
            app.state.adk_run_sessions = sessions
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield Runs(
        Organization(org, lead, member),
        queue,
        tasks,
        sessions,
        acting_as(lead),
        acting_as(member),
        acting_as(viewer),
        acting_as(f"outsider-{tag}"),
        lambda user: acting_as(user, in_memory=False),
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


# ------------------------------------------------------------------ starting


def test_a_member_runs_an_adk_workflow_as_it_is_saved_on_its_own_queue(
    runs: Runs,
) -> None:
    inner = runs.create(shipping())
    outer = runs.create(using(inner["id"]))
    started = runs.member.post(f"{runs.agents}/{outer['id']}/runs", json={"input": {}})
    assert started.status_code == 202, started.json()
    answer = started.json()
    assert set(answer) == {"queue", "key", "agent_id", "revision", "session_id"}
    assert (answer["queue"], answer["agent_id"], answer["revision"]) == (
        "adk_workflows",
        outer["id"],
        1,
    )
    assert answer["key"].startswith(f"adk_workflows.run:{outer['id']}:")
    # Nothing of it went to Forge workflows' queue.
    assert runs.queue.runs == []
    [submitted] = runs.queue.adk_runs
    member = runs.organization.member_id
    assert submitted["key"] == answer["key"]
    assert submitted["tenant_id"] == runs.organization.id
    assert submitted["labels"] == {
        "adk_workflow": outer["id"],
        "adk_session": answer["session_id"],
    }
    assert submitted["requested_by"]["id"] == member
    payload = submitted["payload"]
    assert payload == {
        "tenant_id": runs.organization.id,
        "agent_id": outer["id"],
        "revision": 1,
        "name": "Outer",
        "document": outer["document"],
        # The saved ADK workflow it runs, as saved now.
        "saved": {inner["id"]: inner["document"]},
        "input": {},
        "session_id": answer["session_id"],
        "run_as": member,
        # The test's people aren't users: named by their ID.
        "run_as_name": member,
        "trigger": {"type": "manual", "by": member},
    }


def test_a_runs_input_must_fit_its_start_and_its_document_must_build(
    runs: Runs,
) -> None:
    made = runs.create(shipping())
    run = f"{runs.agents}/{made['id']}/runs"
    refused = runs.member.post(run, json={"input": {"key": 7}})
    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "The input doesn't fit the start: key: 7 is not of type 'string'"
    )
    broken = runs.create(using("ag_nosuchthing"))
    unbuilt = runs.member.post(f"{runs.agents}/{broken['id']}/runs", json={})
    assert unbuilt.status_code == 422
    assert (
        "Node 'Inner': the organization has no agent ag_nosuchthing"
        in (unbuilt.json()["detail"])
    )
    assert runs.queue.adk_runs == []


def test_only_those_who_may_run_adk_workflows_run_them(runs: Runs) -> None:
    made = runs.create(shipping())
    run = f"{runs.agents}/{made['id']}/runs"
    body = {"input": {"key": "k-1"}}
    viewed = runs.viewer.post(run, json=body)
    assert viewed.status_code == 403
    assert viewed.json()["detail"] == "Requires agents:run"
    assert runs.outsider.post(run, json=body).status_code == 403
    missing = runs.member.post(f"{runs.agents}/ag_nosuchthing/runs", json=body)
    assert missing.status_code == 404
    runs.queue.refuse = True
    down = runs.admin.post(run, json=body)
    assert down.status_code == 503
    assert runs.queue.adk_runs == []


# ------------------------------------------------------------------- listing


def test_an_adk_workflows_runs_are_its_background_tasks(runs: Runs) -> None:
    made = runs.create(shipping())
    org = runs.organization.id
    runs.tasks.add(
        task(
            "adk-1",
            org,
            "RUNNING",
            task_type="adk_workflows",
            labels={"tenant": org, "adk_workflow": made["id"], "adk_session": "s-1"},
        ),
        # A Forge workflow's run, and another organization's.
        task(
            "wf-1", org, "RUNNING", labels={"tenant": org, "adk_workflow": made["id"]}
        ),
        task(
            "theirs",
            "another-organization",
            task_type="adk_workflows",
            labels={"tenant": "another-organization", "adk_workflow": made["id"]},
        ),
    )
    listed = runs.viewer.get(f"{runs.agents}/{made['id']}/runs?limit=5&offset=0")
    assert listed.status_code == 200
    assert [t["id"] for t in listed.json()["items"]] == ["adk-1"]
    assert listed.json()["total"] == 1
    query = runs.tasks.listed[-1]
    assert (query["tenant"], query["task_types"], query["labels"]) == (
        org,
        ["adk_workflows"],
        {"adk_workflow": made["id"]},
    )
    assert (query["limit"], query["offset"]) == (5, 0)
    assert runs.outsider.get(f"{runs.agents}/{made['id']}/runs").status_code == 403


# --------------------------------------------------------------------- steps


async def run_to_the_approval(
    sessions: BaseSessionService, made: dict[str, Any], member: str, session_id: str
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


def test_a_runs_steps_are_read_from_its_adk_session(runs: Runs) -> None:
    made = runs.create(shipping())
    member, org = runs.organization.member_id, runs.organization.id
    session_id = str(uuid4())
    asyncio.run(run_to_the_approval(runs.sessions, made, member, session_id))
    runs.tasks.add(
        task(
            "adk-1",
            org,
            "AWAITING_VALIDATION",
            task_type="adk_workflows",
            labels={
                "tenant": org,
                "adk_workflow": made["id"],
                "adk_session": session_id,
            },
            payload={"document": made["document"], "run_as": member, "input": {}},
        )
    )
    read = runs.viewer.get(f"{runs.adk_runs}/adk-1/steps")
    assert read.status_code == 200, read.json()
    answer = read.json()
    assert answer["session_id"] == session_id
    steps = {step["id"]: step for step in answer["steps"]}
    assert [step["id"] for step in answer["steps"]] == [
        "start",
        "shape",
        "review",
        "yes",
        "no",
    ]
    assert {step_id: step["status"] for step_id, step in steps.items()} == {
        "start": "done",
        "shape": "done",
        "review": "waiting",
        "yes": "not_reached",
        "no": "not_reached",
    }
    assert steps["shape"]["output"] == {"loud": "K-1"}
    assert set(steps["shape"]) == {
        "id",
        "name",
        "kind",
        "status",
        "output",
        "error",
        "started_at",
        "finished_at",
    }
    assert (steps["review"]["name"], steps["review"]["kind"]) == ("Review", "approval")

    # Before the worker made its session, nothing is reached yet.
    runs.tasks.add(
        task(
            "adk-2",
            org,
            "PENDING",
            task_type="adk_workflows",
            labels={"tenant": org, "adk_workflow": made["id"], "adk_session": "s-new"},
            payload={"document": made["document"], "run_as": member},
        )
    )
    pending = runs.member.get(f"{runs.adk_runs}/adk-2/steps").json()
    assert {step["status"] for step in pending["steps"]} == {"not_reached"}


def test_a_runs_steps_are_read_from_the_admin_database(runs: Runs) -> None:
    # The worker keeps the session in the admin MySQL; the API reads it there.
    made = runs.create(shipping())
    member, org = runs.organization.member_id, runs.organization.id
    session_id = str(uuid4())

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
        runs.tasks.add(
            task(
                "adk-db",
                org,
                "AWAITING_VALIDATION",
                task_type="adk_workflows",
                labels={"tenant": org, "adk_session": session_id},
                payload={"document": made["document"], "run_as": member},
            )
        )
        client = runs.with_database_sessions(member)
        read = client.get(f"{runs.adk_runs}/adk-db/steps")
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


def test_only_the_organizations_adk_workflow_runs_are_found(runs: Runs) -> None:
    org = runs.organization.id
    runs.tasks.add(
        task(
            "theirs",
            "another-organization",
            task_type="adk_workflows",
            labels={"tenant": "another-organization", "adk_session": "s"},
            payload={"document": {}, "run_as": "x"},
        ),
        # A Forge workflow's run.
        task("wf-1", org),
    )
    for task_id in ("theirs", "wf-1", "nothing"):
        missing = runs.member.get(f"{runs.adk_runs}/{task_id}/steps")
        assert missing.status_code == 404
        assert missing.json()["detail"] == NO_SUCH_RUN
    runs.waiting("mine", kind="approval", approvers="org:member")
    assert runs.outsider.get(f"{runs.adk_runs}/mine/steps").status_code == 403
    for route, body in (
        ("decisions", {"request_id": "req-theirs", "approved": True}),
        ("answers", {"request_id": "req-theirs", "answer": {}}),
    ):
        assert (
            runs.member.post(f"{runs.adk_runs}/theirs/{route}", json=body).status_code
            == 404
        )


# ----------------------------------------------------------------- decisions


def test_approvals_are_decided_by_whom_the_step_names(runs: Runs) -> None:
    runs.waiting("admins", kind="approval", approvers="org:admin")
    decision = {"request_id": "req-admins", "approved": True, "comment": " ship "}
    by_member = runs.member.post(f"{runs.adk_runs}/admins/decisions", json=decision)
    assert by_member.status_code == 403
    assert by_member.json()["detail"] == "Requires agents:approve"
    by_admin = runs.admin.post(f"{runs.adk_runs}/admins/decisions", json=decision)
    assert by_admin.status_code == 202, by_admin.json()
    assert by_admin.json() == {"queue": "workflows", "key": "decide:admins:req-admins"}
    [(action, task_id, actor)] = runs.tasks.actions
    assert (action, task_id, actor["id"], actor["comment"]) == (
        "approve",
        "admins",
        runs.organization.admin_id,
        "ship",
    )

    # One any member may decide; a stale one is refused before anything.
    runs.waiting("members", kind="approval", approvers="org:member")
    rejected = runs.member.post(
        f"{runs.adk_runs}/members/decisions",
        json={"request_id": "req-members", "approved": False},
    )
    assert rejected.status_code == 202
    assert runs.tasks.actions[-1][:2] == ("reject", "members")
    stale = runs.member.post(
        f"{runs.adk_runs}/members/decisions",
        json={"request_id": "req-old", "approved": True},
    )
    assert stale.status_code == 409
    viewed = runs.viewer.post(
        f"{runs.adk_runs}/members/decisions",
        json={"request_id": "req-members", "approved": True},
    )
    assert viewed.status_code == 403
    assert viewed.json()["detail"] == "Requires agents:run"


def test_an_adk_runs_gates_are_only_its_own_routes(runs: Runs) -> None:
    runs.waiting("asks", kind="human_input", response_schema=QUESTION)
    decided = runs.admin.post(
        f"{runs.adk_runs}/asks/decisions",
        json={"request_id": "req-asks", "approved": True, "comment": "anything"},
    )
    assert decided.status_code == 409
    assert "waits for an answer" in decided.json()["detail"]
    # No other route decides them.
    runs.waiting("approval", kind="approval", approvers="org:member")
    generic = runs.admin.post(
        f"{API}/organizations/{runs.organization.id}/background-tasks/approval/decisions",
        json={"request_id": "req-approval", "approved": True},
    )
    assert generic.status_code == 404
    answered = runs.member.post(
        f"{runs.adk_runs}/approval/answers",
        json={"request_id": "req-approval", "answer": {"send": True}},
    )
    assert answered.status_code == 409
    assert "waits for a decision" in answered.json()["detail"]
    assert runs.tasks.actions == []


# ------------------------------------------------------------------- answers


def test_questions_are_answered_with_what_they_ask_for(runs: Runs) -> None:
    runs.waiting("asks", kind="human_input", response_schema=QUESTION, message="Send?")
    answers = f"{runs.adk_runs}/asks/answers"
    bad = runs.member.post(
        answers,
        json={"request_id": "req-asks", "answer": {"send": "yes", "changes": 1}},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"] == (
        "The answer doesn't fit what the question asks: changes: 1 is not of type "
        "'string'; send: 'yes' is not of type 'boolean'"
    )
    viewed = runs.viewer.post(
        answers, json={"request_id": "req-asks", "answer": {"send": True}}
    )
    assert viewed.status_code == 403
    assert runs.tasks.actions == []

    good = runs.member.post(
        answers,
        json={"request_id": "req-asks", "answer": {"send": True, "changes": "Shorter"}},
    )
    assert good.status_code == 202, good.json()
    [(action, task_id, actor)] = runs.tasks.actions
    assert (action, task_id, actor["id"]) == (
        "approve",
        "asks",
        runs.organization.member_id,
    )
    assert json.loads(actor["comment"]) == {"send": True, "changes": "Shorter"}
    stale = runs.member.post(
        answers, json={"request_id": "req-old", "answer": {"send": True}}
    )
    assert stale.status_code == 409
