"""Running an organization's workflows, against MySQL and MongoDB: a member runs
one as themselves; the async worker's workflows task calls the service routes
as that member (other workflows), with its token; and approvals are decided
by whom the step names.

A site administrator builds an organization with an admin and a member; runs
go to a fake of the worker's queues, and background tasks to a fake of its
API. Workflows are kept in a MongoDB database of the test's own, dropped
afterwards.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from background_tasks_fake import FakeBackgroundTasks, task
from embedding_fake import FakeEmbedding
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo import MongoClient

from forge_admin.api.app import PUBLIC_ROUTERS, ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.config import Settings

pytestmark = pytest.mark.mysql

API = "/api/v1"
SERVICE = "/internal/workflows"
TOKEN = "w" * 40
EXAMPLE = Path(__file__).parents[1] / "fixtures" / "example.workflow.json"
INPUT = {
    "request": {"id": "PR-1042", "item": "Laptops", "amount": 4200, "vendor": "v-7"}
}


@dataclass
class Organization:
    id: str
    admin_id: str
    member_id: str
    site_admin: TestClient


@pytest.fixture
def organization(
    site_admin: str, client_as: Callable[[str], TestClient]
) -> Iterator[Organization]:
    admin = client_as(site_admin)
    tag = uuid4().hex[:8]
    org = admin.post(f"{API}/organizations", json={"name": f"Org {tag}"}).json()["id"]
    lead, member = f"admin-{tag}", f"member-{tag}"
    for user, role in ((lead, "org:admin"), (member, "org:member")):
        assigned = admin.put(f"{API}/scopes/org:{org}/members/{user}/roles/{role}")
        assert assigned.status_code == 200

    yield Organization(org, lead, member, admin)

    admin.delete(f"{API}/organizations/{org}")


@dataclass
class Runs:
    organization: Organization
    runs: FakeEmbedding
    tasks: FakeBackgroundTasks
    worker: TestClient
    admin: TestClient
    member: TestClient
    outsider: TestClient

    @property
    def workflows(self) -> str:
        return f"{API}/organizations/{self.organization.id}/workflows"

    def create(self) -> dict[str, Any]:
        document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        made = self.member.post(self.workflows, json={"document": document})
        assert made.status_code == 201, made.json()
        return dict(made.json())

    def call(self, path: str, body: dict[str, Any], **kwargs: Any) -> Any:
        """Call a service route as the worker, acting as the organization's member."""
        acting = {
            "organization_id": self.organization.id,
            "user_id": self.organization.member_id,
        }
        return self.worker.post(
            f"{SERVICE}{path}",
            json={**acting, **body},
            headers={"Authorization": f"Bearer {TOKEN}"},
            **kwargs,
        )


@pytest.fixture
def runs(organization: Organization, mysql_settings: Settings) -> Iterator[Runs]:
    if mysql_settings.mongo_uri is None:
        pytest.skip("needs MongoDB: set FORGE_ADMIN_MONGO_URI (make env)")
    database = f"forge_admin_test_{uuid4().hex[:8]}"
    queue, tasks = FakeEmbedding(), FakeBackgroundTasks()
    clients: list[TestClient] = []

    def client_as(user: str | None) -> TestClient:
        settings = mysql_settings.model_copy(
            update={
                "local_user_id": user,
                "mongo_database": database,
                "workflows_token": SecretStr(TOKEN),
                # Whatever the local .env says: the fakes stand in.
                "embedding_redis_url": None,
            }
        )
        app = ApiServer(
            settings, routers=ROUTERS, public_routers=PUBLIC_ROUTERS
        ).create_app()
        app.state.embedding = queue
        app.state.background_tasks = tasks
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield Runs(
        organization,
        queue,
        tasks,
        client_as(None),
        client_as(organization.admin_id),
        client_as(organization.member_id),
        client_as(f"outsider-{uuid4().hex[:8]}"),
    )

    for client in clients:
        client.__exit__(None, None, None)
    mongo: MongoClient = MongoClient(
        mysql_settings.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=5000
    )
    mongo.drop_database(database)
    mongo.close()


def test_a_member_runs_the_workflow_as_it_is_saved_as_themselves(runs: Runs) -> None:
    made = runs.create()
    run = f"{runs.workflows}/{made['id']}/runs"

    started = runs.member.post(run, json={"input": INPUT})
    assert started.status_code == 202, started.json()
    answer = started.json()
    assert answer["queue"] == "workflows" and answer["revision"] == 1
    assert answer["key"].startswith(f"workflows.run:{made['id']}:")
    [submitted] = runs.runs.runs
    assert submitted["tenant_id"] == runs.organization.id
    assert submitted["labels"] == {"workflow": made["id"]}
    # The run records who started it (by ID here: the test's people aren't users).
    assert submitted["requested_by"]["id"] == runs.organization.member_id
    payload = submitted["payload"]
    assert payload["tenant_id"] == runs.organization.id
    assert payload["run_as"] == runs.organization.member_id
    assert payload["input"] == INPUT and payload["revision"] == 1
    assert payload["name"] == "Review purchase requests"
    assert payload["document"]["nodes"] == made["document"]["nodes"]
    assert payload["trigger"] == {"type": "manual", "by": runs.organization.member_id}

    # A run is of the revision saved when it starts.
    saved = runs.member.put(
        f"{runs.workflows}/{made['id']}",
        json={"document": {**made["document"], "name": "Review v2"}, "revision": 1},
    )
    assert saved.status_code == 200
    runs.admin.post(run, json={"input": INPUT})
    later = runs.runs.runs[-1]["payload"]
    assert (later["revision"], later["name"], later["run_as"]) == (
        2,
        "Review v2",
        runs.organization.admin_id,
    )


def test_a_runs_input_must_fit_its_start_step(runs: Runs) -> None:
    made = runs.create()
    run = f"{runs.workflows}/{made['id']}/runs"
    refused = runs.member.post(run, json={"input": {"request": {}}})
    assert refused.status_code == 422
    assert "doesn't fit the start step" in refused.json()["detail"]
    assert "request: 'id' is a required property" in refused.json()["detail"]
    assert runs.runs.runs == []


def test_only_the_organizations_members_run_its_workflows(runs: Runs) -> None:
    made = runs.create()
    refused = runs.outsider.post(
        f"{runs.workflows}/{made['id']}/runs", json={"input": INPUT}
    )
    assert refused.status_code == 403
    missing = runs.member.post(f"{runs.workflows}/wf_nosuchthing/runs", json={})
    assert missing.status_code == 404


def test_a_workflows_runs_are_the_organizations_background_tasks_labelled_with_it(
    runs: Runs,
) -> None:
    made = runs.create()
    labels = {"tenant": runs.organization.id, "workflow": made["id"]}
    runs.tasks.add(
        task(
            "run-1",
            runs.organization.id,
            "RUNNING",
            task_type="workflows",
            labels=labels,
        ),
        task("other", runs.organization.id, "COMPLETED"),
    )
    listed = runs.member.get(f"{runs.workflows}/{made['id']}/runs")
    assert listed.status_code == 200
    assert [t["id"] for t in listed.json()["items"]] == ["run-1"]
    assert runs.tasks.listed[-1]["labels"] == {"workflow": made["id"]}
    assert runs.tasks.listed[-1]["task_types"] == ["workflows"]


def test_approvals_are_decided_by_whom_the_step_names(runs: Runs) -> None:
    def waiting(task_id: str, approvers: str) -> None:
        runs.tasks.add(
            task(
                task_id,
                runs.organization.id,
                "AWAITING_VALIDATION",
                approval={
                    "id": f"req-{task_id}",
                    "reason": "Order PR-1042?",
                    "details": {"approvers": approvers, "key": "review#1"},
                },
            )
        )

    tasks = f"{API}/organizations/{runs.organization.id}/background-tasks"
    waiting("admins", "org:admin")
    decision = {"request_id": "req-admins", "approved": True, "comment": " go "}
    by_member = runs.member.post(f"{tasks}/admins/decisions", json=decision)
    assert by_member.status_code == 403
    assert by_member.json()["detail"] == "Requires workflows:approve"
    by_admin = runs.admin.post(f"{tasks}/admins/decisions", json=decision)
    assert by_admin.status_code == 202, by_admin.json()
    [(action, task_id, actor)] = runs.tasks.actions
    assert (action, task_id, actor["id"], actor["comment"]) == (
        "approve",
        "admins",
        runs.organization.admin_id,
        "go",
    )

    # One any member may decide; a stale one is refused before anything.
    waiting("members", "org:member")
    rejected = runs.member.post(
        f"{tasks}/members/decisions",
        json={"request_id": "req-members", "approved": False},
    )
    assert rejected.status_code == 202
    assert runs.tasks.actions[-1][:2] == ("reject", "members")
    stale = runs.member.post(
        f"{tasks}/members/decisions", json={"request_id": "req-old", "approved": True}
    )
    assert stale.status_code == 409
    theirs = runs.outsider.post(
        f"{tasks}/members/decisions",
        json={"request_id": "req-members", "approved": True},
    )
    assert theirs.status_code == 403


def test_the_service_routes_answer_only_to_the_workers_token(runs: Runs) -> None:
    made = runs.create()
    path = f"{SERVICE}/workflows/{made['id']}/runs"
    body = {
        "organization_id": runs.organization.id,
        "user_id": runs.organization.member_id,
        "input": INPUT,
        "parent": "run-1/child#1",
        "depth": 1,
    }
    assert runs.worker.post(path, json=body).status_code == 401
    wrong = runs.worker.post(
        path, json=body, headers={"Authorization": "Bearer " + "x" * 40}
    )
    assert wrong.status_code == 401
    # A signed-in person is no worker.
    assert runs.member.post(path, json=body).status_code == 401
    assert runs.runs.runs == []
    # The member the run acts as must still be allowed to run workflows.
    outsider = runs.worker.post(
        path,
        json={**body, "user_id": "someone-else"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert outsider.status_code == 403
    assert "can't run workflows" in outsider.json()["detail"]


def test_a_run_starts_another_workflow_once_as_the_same_member(runs: Runs) -> None:
    made = runs.create()
    start = {"input": INPUT, "parent": "run-1/child#1", "depth": 1}
    first = runs.call(f"/workflows/{made['id']}/runs", start)
    assert first.status_code == 202, first.json()
    assert first.json()["workflow_name"] == "Review purchase requests"
    again = runs.call(f"/workflows/{made['id']}/runs", start)
    assert again.json()["key"] == first.json()["key"]
    [submitted] = runs.runs.runs
    assert submitted["labels"] == {"workflow": made["id"], "parent": "run-1/child#1"}
    assert submitted["payload"]["depth"] == 1
    assert submitted["payload"]["run_as"] == runs.organization.member_id
    refused = runs.call(
        f"/workflows/{made['id']}/runs", {**start, "parent": "run-2/x#1", "input": {}}
    )
    assert refused.status_code == 422
