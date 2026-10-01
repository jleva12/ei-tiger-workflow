"""Organizations' workflows, against MySQL and MongoDB: an organization's members
make and save workflows the whole organization shares; a save from an older
revision is refused; nobody outside the organization reads or changes them.

A site administrator builds two organizations. Each test keeps its workflows
in a MongoDB database of its own, dropped afterwards. make admin-test-mysql
starts both admin-mysql and MongoDB.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from forge_admin.api.app import PUBLIC_ROUTERS, ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.config import Settings

pytestmark = pytest.mark.mysql

API = "/api/v1"
EXAMPLE = Path(__file__).parents[1] / "fixtures" / "example.workflow.json"


def example(name: str = "Review purchase requests") -> dict[str, Any]:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return {**document, "name": name}


@dataclass
class Organizations:
    id: str
    other_id: str
    org_admin: TestClient
    member: TestClient
    # A member of the other organization only.
    neighbour: TestClient
    outsider: TestClient
    admin: TestClient
    mongo: MongoClient
    database: str

    @property
    def workflows(self) -> str:
        return f"{API}/organizations/{self.id}/workflows"

    def stored(self, workflow_id: str) -> dict[str, Any] | None:
        return self.mongo[self.database]["workflows"].find_one({"_id": workflow_id})


@pytest.fixture
def organizations(site_admin: str, mysql_settings: Settings) -> Iterator[Organizations]:
    if mysql_settings.mongo_uri is None:
        pytest.skip("needs MongoDB: set FORGE_ADMIN_MONGO_URI (make env)")
    database = f"forge_admin_test_{uuid4().hex[:8]}"
    clients: list[TestClient] = []

    def client_as(user: str) -> TestClient:
        settings = mysql_settings.model_copy(
            update={"local_user_id": user, "mongo_database": database}
        )
        server = ApiServer(settings, routers=ROUTERS, public_routers=PUBLIC_ROUTERS)
        client = TestClient(server.create_app())
        client.__enter__()
        clients.append(client)
        return client

    admin = client_as(site_admin)
    tag = uuid4().hex[:8]
    organization_id, other_id = (
        admin.post(f"{API}/organizations", json={"name": f"{name} {tag}"}).json()["id"]
        for name in ("Purchasing", "Billing")
    )
    org_admin, member, neighbour = f"admin-{tag}", f"member-{tag}", f"neighbour-{tag}"
    for scope, user, role in (
        (organization_id, org_admin, "org:admin"),
        (organization_id, member, "org:member"),
        (other_id, neighbour, "org:member"),
    ):
        assigned = admin.put(f"{API}/scopes/org:{scope}/members/{user}/roles/{role}")
        assert assigned.status_code == 200
    mongo: MongoClient = MongoClient(
        mysql_settings.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=5000
    )

    yield Organizations(
        organization_id,
        other_id,
        client_as(org_admin),
        client_as(member),
        client_as(neighbour),
        client_as(f"outsider-{tag}"),
        admin,
        mongo,
        database,
    )

    for doomed in (organization_id, other_id):
        admin.delete(f"{API}/organizations/{doomed}")
    for client in clients:
        client.__exit__(None, None, None)
    mongo.drop_database(database)
    mongo.close()


def create(organizations: Organizations) -> dict[str, Any]:
    made = organizations.member.post(
        organizations.workflows, json={"document": example()}
    )
    assert made.status_code == 201, made.json()
    return made.json()


def test_a_member_makes_a_workflow_the_whole_organization_reads(
    organizations: Organizations,
) -> None:
    made = create(organizations)
    assert made["id"].startswith("wf_")
    assert made["revision"] == 1
    assert made["organization_id"] == organizations.id
    # The document carries the ID and organization the API gave it.
    assert made["document"]["id"] == made["id"]
    assert made["document"]["organization_id"] == organizations.id

    listed = organizations.org_admin.get(organizations.workflows)
    assert listed.status_code == 200
    assert [w["id"] for w in listed.json()] == [made["id"]]
    read = organizations.org_admin.get(f"{organizations.workflows}/{made['id']}")
    assert read.json()["document"] == made["document"]

    stored = organizations.stored(made["id"])
    assert stored is not None
    assert stored["organization_id"] == organizations.id
    assert stored["deleted_at"] is None


def test_saves_go_revision_by_revision_and_a_stale_one_is_refused(
    organizations: Organizations,
) -> None:
    made = create(organizations)
    url = f"{organizations.workflows}/{made['id']}"
    renamed = {**made["document"], "name": "Review, faster"}

    saved = organizations.org_admin.put(url, json={"document": renamed, "revision": 1})
    assert saved.status_code == 200, saved.json()
    assert saved.json()["revision"] == 2
    assert saved.json()["document"]["name"] == "Review, faster"
    assert saved.json()["created_at"] == made["created_at"]
    assert saved.json()["updated_by"].startswith("admin-")

    # The member's save was made from revision 1: someone saved since.
    stale = organizations.member.put(
        url, json={"document": made["document"], "revision": 1}
    )
    assert stale.status_code == 409
    assert "Someone else saved this workflow" in stale.json()["detail"]
    assert organizations.member.get(url).json()["document"]["name"] == "Review, faster"


def test_key_order_is_kept(organizations: Organizations) -> None:
    document = example()
    start = next(node for node in document["nodes"] if node["kind"] == "entry")
    start["config"]["input_schema"] = {
        "type": "object",
        "properties": {"zeta": {"type": "string"}, "alpha": {"type": "number"}},
        "required": ["zeta"],
    }
    made = organizations.member.post(
        organizations.workflows, json={"document": document}
    ).json()
    read = organizations.member.get(f"{organizations.workflows}/{made['id']}").json()
    schema = next(n for n in read["document"]["nodes"] if n["kind"] == "entry")[
        "config"
    ]["input_schema"]
    assert list(schema["properties"]) == ["zeta", "alpha"]


def test_a_document_that_isnt_a_workflow_is_refused(
    organizations: Organizations,
) -> None:
    broken = {**example(), "nodes": [{"id": "start", "kind": "teleport"}]}
    refused = organizations.member.post(
        organizations.workflows, json={"document": broken}
    )
    assert refused.status_code == 422
    assert "isn't a forge.workflow/v1 document" in refused.json()["detail"]


def test_the_api_makes_every_id(organizations: Organizations) -> None:
    chosen = organizations.member.post(
        organizations.workflows, json={"document": example(), "id": "wf_mychoice"}
    )
    assert chosen.status_code == 422
    # The document's own ID is replaced too.
    made = create(organizations)
    assert made["id"] != example()["id"]
    assert made["document"]["id"] == made["id"]


def test_a_deleted_workflow_is_gone_for_the_organization(
    organizations: Organizations,
) -> None:
    made = create(organizations)
    url = f"{organizations.workflows}/{made['id']}"
    assert organizations.member.delete(url).status_code == 204
    assert organizations.org_admin.get(url).status_code == 404
    assert organizations.org_admin.get(organizations.workflows).json() == []
    body = {"document": made["document"], "revision": 1}
    assert organizations.org_admin.put(url, json=body).status_code == 404
    assert organizations.org_admin.delete(url).status_code == 404
    # Kept out of sight, marked with who deleted it.
    assert organizations.stored(made["id"])["deleted_by"].startswith("member-")  # type: ignore[index]


def test_nobody_outside_the_organization_reads_or_changes_its_workflows(
    organizations: Organizations,
) -> None:
    made = create(organizations)
    url = f"{organizations.workflows}/{made['id']}"
    body = {"document": made["document"], "revision": 1}

    for refused in (
        organizations.outsider.get(organizations.workflows),
        organizations.outsider.get(url),
        organizations.neighbour.get(organizations.workflows),
    ):
        assert refused.status_code == 403
    assert (
        organizations.outsider.post(
            organizations.workflows, json={"document": example()}
        ).json()["detail"]
        == "Requires workflows:manage"
    )
    assert organizations.neighbour.put(url, json=body).status_code == 403
    assert organizations.neighbour.delete(url).status_code == 403

    # Through their own organization, the workflow isn't there at all.
    theirs = f"{API}/organizations/{organizations.other_id}/workflows/{made['id']}"
    assert organizations.neighbour.get(theirs).status_code == 404
    assert organizations.neighbour.put(theirs, json=body).status_code == 404
    assert (
        organizations.neighbour.get(
            f"{API}/organizations/{organizations.other_id}/workflows"
        ).json()
        == []
    )


def test_deleting_the_organization_removes_its_workflows(
    organizations: Organizations,
) -> None:
    made = create(organizations)
    assert (
        organizations.admin.delete(
            f"{API}/organizations/{organizations.id}"
        ).status_code
        == 204
    )
    assert organizations.stored(made["id"]) is None
