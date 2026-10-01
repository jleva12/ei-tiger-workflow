"""An organization's background tasks, against MySQL.

A site administrator builds an organization with an admin and a member. The
async worker's background tasks API is a fake
(background_tasks_fake.py) holding tasks of this organization and another's.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from background_tasks_fake import FakeBackgroundTasks, task
from fastapi.testclient import TestClient

from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.config import Settings

pytestmark = pytest.mark.mysql

API = "/api/v1"
NO_SUCH_TASK = "The organization has no such background task"


@dataclass
class Organization:
    id: str
    admin_id: str
    member_id: str
    admin: TestClient
    member: TestClient
    worker: FakeBackgroundTasks
    acting_as: Callable[..., TestClient]

    @property
    def tasks(self) -> str:
        return f"{API}/organizations/{self.id}/background-tasks"


@pytest.fixture
def organization(
    site_admin: str, client_as: Callable[[str], TestClient], mysql_settings: Settings
) -> Iterator[Organization]:
    admin = client_as(site_admin)
    tag = uuid4().hex[:8]
    organization_id = admin.post(
        f"{API}/organizations", json={"name": f"Org {tag}"}
    ).json()["id"]
    org_admin, member = f"admin-{tag}", f"member-{tag}"
    for user, role in ((org_admin, "org:admin"), (member, "org:member")):
        assigned = admin.put(
            f"{API}/scopes/org:{organization_id}/members/{user}/roles/{role}"
        )
        assert assigned.status_code == 200

    worker = FakeBackgroundTasks()
    worker.add(
        task("mine-failed", organization_id),
        task("mine-done", organization_id, status="COMPLETED"),
        task("theirs", "another-organization"),
    )
    clients: list[TestClient] = []

    def acting_as(user: str, set_up: bool = True) -> TestClient:
        server = ApiServer(
            mysql_settings.model_copy(
                update={
                    "local_user_id": user,
                    "async_worker_url": None,
                    "async_worker_token": None,
                }
            ),
            routers=ROUTERS,
        ).create_app()
        server.state.background_tasks = worker if set_up else None
        client = TestClient(server)
        client.__enter__()
        clients.append(client)
        return client

    yield Organization(
        organization_id,
        org_admin,
        member,
        acting_as(org_admin),
        acting_as(member),
        worker,
        acting_as,
    )

    for client in clients:
        client.__exit__(None, None, None)
    admin.delete(f"{API}/organizations/{organization_id}")


def test_members_see_their_organizations_tasks_only(organization: Organization) -> None:
    listed = organization.member.get(
        organization.tasks,
        params={"task_type": "workflows", "status": "FAILED", "limit": 20},
    )
    assert listed.status_code == 200
    assert {t["id"] for t in listed.json()["items"]} == {"mine-failed", "mine-done"}
    assert organization.worker.listed[-1] == {
        "tenant": organization.id,
        "task_types": ["workflows"],
        "exclude_task_types": [],
        "statuses": ["FAILED"],
        "limit": 20,
        "offset": 0,
    }
    detail = organization.member.get(f"{organization.tasks}/mine-failed")
    assert (
        detail.status_code == 200
        and detail.json()["failure"]["category"] == "permanent"
    )
    for other in ("theirs", "nowhere"):
        refused = organization.member.get(f"{organization.tasks}/{other}")
        assert (refused.status_code, refused.json()["detail"]) == (404, NO_SUCH_TASK)

    outsider = organization.acting_as(f"outsider-{uuid4().hex[:8]}")
    denied = outsider.get(organization.tasks)
    assert (denied.status_code, denied.json()["detail"]) == (
        403,
        "Requires organizations:read",
    )
    assert (
        organization.member.get(
            organization.tasks, params={"task_type": "Bad Type"}
        ).status_code
        == 422
    )
    assert (
        organization.member.get(
            organization.tasks, params={"exclude_task_type": "Bad Type"}
        ).status_code
        == 422
    )


def test_the_list_can_leave_workflow_runs_out(organization: Organization) -> None:
    # Another kind of task the worker runs for the organization.
    organization.worker.add(
        task("mine-other", organization.id, task_type="other", job_name="other.job")
    )
    everything = organization.member.get(organization.tasks).json()
    assert {t["id"] for t in everything["items"]} == {
        "mine-failed",
        "mine-done",
        "mine-other",
    }

    rest = organization.member.get(
        organization.tasks, params={"exclude_task_type": "workflows"}
    )
    assert rest.status_code == 200
    assert {t["id"] for t in rest.json()["items"]} == {"mine-other"}
    assert rest.json()["total"] == 1
    assert organization.worker.listed[-1]["exclude_task_types"] == ["workflows"]


def test_admins_resubmit_restart_and_abandon(organization: Organization) -> None:
    for action in ("resubmit", "restart", "abandon"):
        refused = organization.member.post(f"{organization.tasks}/mine-failed/{action}")
        assert (refused.status_code, refused.json()["detail"]) == (
            403,
            "Requires background_tasks:manage",
        )

    assert (
        organization.admin.post(f"{organization.tasks}/mine-failed/restart").status_code
        == 202
    )
    assert (
        organization.admin.post(f"{organization.tasks}/mine-failed/resubmit").json()[
            "queue"
        ]
        == "workflows"
    )
    abandoned = organization.admin.post(f"{organization.tasks}/mine-failed/abandon")
    assert (abandoned.status_code, abandoned.json()["status"]) == (200, "ABANDONED")
    assert [(a, t, who["id"]) for a, t, who in organization.worker.actions] == [
        ("restart", "mine-failed", organization.admin_id),
        ("resubmit", "mine-failed", organization.admin_id),
        ("abandon", "mine-failed", organization.admin_id),
    ]
    # What the task's state doesn't allow, the worker refuses; another
    # organization's is none of theirs.
    refused = organization.admin.post(f"{organization.tasks}/mine-done/restart")
    assert refused.status_code == 409 and "completed" in refused.json()["detail"]
    assert (
        organization.admin.post(f"{organization.tasks}/theirs/resubmit").status_code
        == 404
    )


def test_unset_or_unavailable_worker(organization: Organization) -> None:
    off = organization.acting_as(organization.member_id, set_up=False)
    answer = off.get(organization.tasks)
    assert (
        answer.status_code == 503
        and "FORGE_ADMIN_ASYNC_WORKER_URL" in answer.json()["detail"]
    )
    organization.worker.refuse = True
    down = organization.member.get(organization.tasks)
    assert (down.status_code, down.json()["detail"]) == (
        503,
        "The async worker is unavailable; try again",
    )
