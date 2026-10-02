"""Organizations' agents, against MySQL and MongoDB: an organization's members
make and save agents the whole organization shares; a save from an older
revision is refused; its viewers read them but don't change them, and nobody
outside the organization reads or changes them.

A site administrator builds two organizations. Each test keeps its agents in
a MongoDB database of its own, dropped afterwards. make admin-test-mysql
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
# The example ADK workflow, kept with the package that runs it.
EXAMPLE = (
    Path(__file__).parents[4]
    / "packages/python/adk-workflows/tests/fixtures/example.agent.json"
)


def example(name: str = "Support desk") -> dict[str, Any]:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return {**document, "name": name}


@dataclass
class Organizations:
    id: str
    other_id: str
    org_admin: TestClient
    member: TestClient
    viewer: TestClient
    # A member of the other organization only.
    neighbour: TestClient
    outsider: TestClient
    admin: TestClient
    mongo: MongoClient
    database: str

    @property
    def agents(self) -> str:
        return f"{API}/organizations/{self.id}/agents"

    def stored(self, agent_id: str) -> dict[str, Any] | None:
        return self.mongo[self.database]["agents"].find_one({"_id": agent_id})


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
        for name in ("Support", "Sales")
    )
    org_admin, member, viewer, neighbour = (
        f"admin-{tag}",
        f"member-{tag}",
        f"viewer-{tag}",
        f"neighbour-{tag}",
    )
    for scope, user, role in (
        (organization_id, org_admin, "org:admin"),
        (organization_id, member, "org:member"),
        (organization_id, viewer, "org:viewer"),
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
        client_as(viewer),
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
    made = organizations.member.post(organizations.agents, json={"document": example()})
    assert made.status_code == 201, made.json()
    return made.json()


def test_a_member_makes_an_agent_the_whole_organization_reads(
    organizations: Organizations,
) -> None:
    made = create(organizations)
    assert made["id"].startswith("ag_")
    assert made["revision"] == 1
    assert made["organization_id"] == organizations.id
    assert made["created_by"].startswith("member-")
    # The member has no user record; they're named by their ID.
    assert made["updated_by_name"] == made["created_by"]
    # The document carries the ID and organization the API gave it.
    assert made["document"]["id"] == made["id"]
    assert made["document"]["organization_id"] == organizations.id
    assert made["document"]["nodes"] == example()["nodes"]

    for reader in (organizations.org_admin, organizations.viewer):
        listed = reader.get(organizations.agents)
        assert listed.status_code == 200
        assert [a["id"] for a in listed.json()] == [made["id"]]
        read = reader.get(f"{organizations.agents}/{made['id']}")
        assert read.json() == made

    stored = organizations.stored(made["id"])
    assert stored is not None
    assert stored["organization_id"] == organizations.id
    assert stored["deleted_at"] is None


def test_saves_go_revision_by_revision_and_a_stale_one_is_refused(
    organizations: Organizations,
) -> None:
    made = create(organizations)
    url = f"{organizations.agents}/{made['id']}"
    renamed = {**made["document"], "name": "Support, faster"}

    saved = organizations.org_admin.put(url, json={"document": renamed, "revision": 1})
    assert saved.status_code == 200, saved.json()
    assert saved.json()["revision"] == 2
    assert saved.json()["document"]["name"] == "Support, faster"
    assert saved.json()["created_at"] == made["created_at"]
    assert saved.json()["updated_by"].startswith("admin-")

    # The member's save was made from revision 1: someone saved since.
    stale = organizations.member.put(
        url, json={"document": made["document"], "revision": 1}
    )
    assert stale.status_code == 409
    assert "Someone else saved this agent" in stale.json()["detail"]
    assert organizations.member.get(url).json()["document"]["name"] == "Support, faster"


def test_key_order_is_kept(organizations: Organizations) -> None:
    document = example()
    start = next(node for node in document["nodes"] if node["kind"] == "start")
    start["config"]["input_schema"] = {
        "type": "object",
        "properties": {"zeta": {"type": "string"}, "alpha": {"type": "number"}},
        "required": ["zeta"],
    }
    made = organizations.member.post(
        organizations.agents, json={"document": document}
    ).json()
    read = organizations.member.get(f"{organizations.agents}/{made['id']}").json()
    schema = next(n for n in read["document"]["nodes"] if n["kind"] == "start")[
        "config"
    ]["input_schema"]
    assert list(schema["properties"]) == ["zeta", "alpha"]


def test_a_document_that_isnt_an_agent_is_refused(
    organizations: Organizations,
) -> None:
    broken = {**example(), "nodes": [{"id": "start", "kind": "teleport"}]}
    refused = organizations.member.post(organizations.agents, json={"document": broken})
    assert refused.status_code == 422
    assert "isn't a forge.agent/v1 document" in refused.json()["detail"]

    made = create(organizations)
    url = f"{organizations.agents}/{made['id']}"
    saved = organizations.member.put(url, json={"document": broken, "revision": 1})
    assert saved.status_code == 422
    assert organizations.member.get(url).json()["revision"] == 1


def test_a_draft_that_cant_run_yet_is_saved(organizations: Organizations) -> None:
    draft = {**example(), "edges": []}
    made = organizations.member.post(organizations.agents, json={"document": draft})
    assert made.status_code == 201, made.json()


def test_the_api_makes_every_id(organizations: Organizations) -> None:
    chosen = organizations.member.post(
        organizations.agents, json={"document": example(), "id": "ag_mychoice"}
    )
    assert chosen.status_code == 422
    # The document's own ID is replaced too.
    made = create(organizations)
    assert made["id"] != example()["id"]
    assert made["document"]["id"] == made["id"]


def test_a_deleted_agent_is_gone_for_the_organization(
    organizations: Organizations,
) -> None:
    made = create(organizations)
    url = f"{organizations.agents}/{made['id']}"
    assert organizations.member.delete(url).status_code == 204
    gone = organizations.org_admin.get(url)
    assert gone.status_code == 404
    assert gone.json()["detail"] == "The organization has no such agent"
    assert organizations.org_admin.get(organizations.agents).json() == []
    body = {"document": made["document"], "revision": 1}
    assert organizations.org_admin.put(url, json=body).status_code == 404
    assert organizations.org_admin.delete(url).status_code == 404
    # Kept out of sight, marked with who deleted it.
    assert organizations.stored(made["id"])["deleted_by"].startswith("member-")  # type: ignore[index]


def test_a_viewer_reads_agents_but_doesnt_change_them(
    organizations: Organizations,
) -> None:
    made = create(organizations)
    url = f"{organizations.agents}/{made['id']}"
    viewer = organizations.viewer
    assert viewer.get(url).status_code == 200
    for refused in (
        viewer.post(organizations.agents, json={"document": example()}),
        viewer.put(url, json={"document": made["document"], "revision": 1}),
        viewer.delete(url),
    ):
        assert refused.status_code == 403
        assert refused.json()["detail"] == "Requires agents:manage"
    assert organizations.stored(made["id"])["revision"] == 1  # type: ignore[index]


def test_nobody_outside_the_organization_reads_or_changes_its_agents(
    organizations: Organizations,
) -> None:
    made = create(organizations)
    url = f"{organizations.agents}/{made['id']}"
    body = {"document": made["document"], "revision": 1}

    for refused in (
        organizations.outsider.get(organizations.agents),
        organizations.outsider.get(url),
        organizations.neighbour.get(organizations.agents),
    ):
        assert refused.status_code == 403
    assert (
        organizations.outsider.post(
            organizations.agents, json={"document": example()}
        ).json()["detail"]
        == "Requires agents:manage"
    )
    assert organizations.neighbour.put(url, json=body).status_code == 403
    assert organizations.neighbour.delete(url).status_code == 403

    # Through their own organization, the agent isn't there at all.
    theirs = f"{API}/organizations/{organizations.other_id}/agents/{made['id']}"
    assert organizations.neighbour.get(theirs).status_code == 404
    assert organizations.neighbour.put(theirs, json=body).status_code == 404
    assert (
        organizations.neighbour.get(
            f"{API}/organizations/{organizations.other_id}/agents"
        ).json()
        == []
    )


def test_deleting_the_organization_removes_its_agents(
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
