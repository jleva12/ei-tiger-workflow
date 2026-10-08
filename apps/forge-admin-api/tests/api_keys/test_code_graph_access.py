"""What a credential reads of the code graph (GET /code-graph/access): an
organization's API key its organization's repositories, a token minted for
an organization that one's, each with repositories:read there."""

from datetime import timedelta
from typing import Any

import pytest
from key_world import ADMIN, MEMBER, NEIGHBOUR, ORG, OTHER_ORG, VIEWER, World
from sqlalchemy import MetaData, delete, select

from forge_admin.api.routes.code_graph import NO_ORGANIZATION
from forge_admin.auth.tokens import mint_subject_token
from forge_admin.db.base import Base
from forge_admin.models import (
    CasbinRule,
    CodeRepository,
    KnowledgeBase,
    KnowledgeBaseCodeLink,
    KnowledgeBaseConnection,
    KnowledgeBaseRepository,
    Permission,
)

ACCESS = "/code-graph/access"
READERS = ("org:admin", "org:member", "org:viewer", "org:api")
SHARED = "https://github.com/acme/shop"


@pytest.fixture
def code(world: World) -> World:
    """The world with repositories:read granted as the migration grants it,
    two repositories in Acme and one in Globex, one of them Acme's too."""

    async def add(session: Any) -> None:
        tables = MetaData()
        made = (
            "code_repositories",
            "knowledge_bases",
            "knowledge_base_repositories",
            "knowledge_base_connections",
            "knowledge_base_code_links",
        )
        for name in ("organizations", *made):
            table = Base.metadata.tables[name].to_metadata(tables)
            for column in table.columns:
                if column.name != "kind" or name != "knowledge_bases":
                    column.server_default = None
        connection = await session.connection()
        for name in made:
            await connection.run_sync(tables.tables[name].create)
        session.add(Permission(resource="repositories", action="read"))
        session.add_all(
            CasbinRule(ptype="p", v0=role, v1="repositories", v2="read")
            for role in READERS
        )
        session.add_all(
            CodeRepository(
                organization_id=org,
                url=f"https://github.com/{owner}/{name}",
                owner=owner,
                name=name,
                branch="main",
            )
            for org, owner, name in (
                (ORG, "acme", "shop"),
                (ORG, "acme", "billing"),
                (OTHER_ORG, "acme", "shop"),
                (OTHER_ORG, "globex", "radar"),
            )
        )
        await session.commit()

    world.run(add)
    return world


def token_for(world: World, user: str, organization: str | None) -> str:
    settings = world.client.app.state.settings  # type: ignore[attr-defined]
    return mint_subject_token(
        settings, user, lifetime=timedelta(minutes=5), organization_id=organization
    )


def ask(world: World, token: str) -> Any:
    return world.call("GET", ACCESS, None, headers={"Authorization": f"Bearer {token}"})


def urls(body: dict[str, Any]) -> list[str]:
    return [repository["url"] for repository in body["repositories"]]


def test_a_key_reads_its_organizations_repositories(code: World) -> None:
    key = code.make_key(name="Code explorer")
    for header in ("bearer", "x-api-key"):
        answer = code.call("GET", ACCESS, key=key["secret"], header=header)
        assert answer.status_code == 200, answer.json()
        body = answer.json()
        assert body["subject"] == key["subject"]
        assert body["organization_id"] == ORG
        assert urls(body) == [
            "https://github.com/acme/billing",
            "https://github.com/acme/shop",
        ]
        assert body["repositories"][0] == {
            "url": "https://github.com/acme/billing",
            "owner": "acme",
            "name": "billing",
            "branch": "main",
        }


def test_a_token_reads_the_organization_it_was_minted_for(code: World) -> None:
    for user in (ADMIN, MEMBER, VIEWER):
        token = token_for(code, user, ORG)
        answer = ask(code, token)
        assert answer.status_code == 200, answer.json()
        assert answer.json()["subject"] == user
        assert urls(answer.json()) == [
            "https://github.com/acme/billing",
            "https://github.com/acme/shop",
        ]
    token = token_for(code, NEIGHBOUR, OTHER_ORG)
    answer = ask(code, token)
    assert urls(answer.json()) == [SHARED, "https://github.com/globex/radar"]


def test_a_token_for_an_organization_they_arent_in_reads_nothing(code: World) -> None:
    token = token_for(code, NEIGHBOUR, ORG)
    answer = ask(code, token)
    assert answer.status_code == 403
    assert answer.json()["detail"] == "Requires repositories:read"


def test_a_credential_without_an_organization_reads_nothing(code: World) -> None:
    answer = code.call("GET", ACCESS, MEMBER)
    assert (answer.status_code, answer.json()["detail"]) == (403, NO_ORGANIZATION)
    assert code.call("GET", ACCESS, None).status_code == 401


def test_without_repositories_read_nothing_is_read(code: World) -> None:
    key = code.make_key(name="Key keeper", role="org:keykeeper")
    answer = code.call("GET", ACCESS, key=key["secret"])
    assert answer.status_code == 403


def test_an_organization_without_repositories_reads_none(code: World) -> None:
    async def forget(session: Any) -> None:
        await session.execute(
            delete(CodeRepository).where(CodeRepository.organization_id == ORG)
        )
        await session.commit()

    code.run(forget)
    key = code.make_key(name="CI")
    answer = code.call("GET", ACCESS, key=key["secret"])
    assert answer.status_code == 200
    assert answer.json()["repositories"] == []


def test_the_answer_says_how_the_organizations_repositories_connect(
    code: World,
) -> None:
    """Connections on Acme's system maps come with Acme's answer, with their
    code links and the owner of its links in the graph; Globex's don't."""

    async def draw(session: Any) -> str:
        repos = {
            (r.organization_id, r.name): r.id
            for r in await session.scalars(select(CodeRepository))
        }
        shop, billing = repos[(ORG, "shop")], repos[(ORG, "billing")]
        kb = KnowledgeBase(organization_id=ORG, name="Checkout", kind="system")
        rag = KnowledgeBase(organization_id=ORG, name="Docs", kind="rag")
        session.add_all([kb, rag])
        await session.flush()
        session.add_all(
            KnowledgeBaseRepository(knowledge_base_id=kb.id, repository_id=r)
            for r in (shop, billing)
        )
        await session.flush()
        connection = KnowledgeBaseConnection(
            knowledge_base_id=kb.id,
            source_repository_id=shop,
            target_repository_id=billing,
            kind="calls",
            description="POST /charges",
        )
        session.add(connection)
        await session.flush()
        session.add(
            KnowledgeBaseCodeLink(
                connection_id=connection.id,
                source_node_id="entity:client",
                source_kind="method",
                source_name="charge",
                source_qualified_name="shop.Billing.charge",
                source_path="shop/billing.py",
                target_node_id="entity:handler",
                target_kind="function",
                target_name="create_charge",
                target_qualified_name="billing.api.create_charge",
                target_path="billing/api.py",
                label="POST /charges",
            )
        )
        await session.commit()
        return kb.id

    kb = code.run(draw)
    body = ask(code, token_for(code, VIEWER, ORG)).json()
    assert body["link_owners"] == [f"kb:{kb}"]
    [connection] = body["connections"]
    assert connection["knowledge_base"] == "Checkout"
    assert (connection["kind"], connection["description"]) == ("calls", "POST /charges")
    assert connection["source"] == {
        "url": SHARED,
        "owner": "acme",
        "name": "shop",
    }
    assert connection["target"]["url"] == "https://github.com/acme/billing"
    assert connection["code_links"] == [
        {
            "source": {
                "node_id": "entity:client",
                "kind": "method",
                "name": "charge",
                "qualified_name": "shop.Billing.charge",
                "path": "shop/billing.py",
            },
            "target": {
                "node_id": "entity:handler",
                "kind": "function",
                "name": "create_charge",
                "qualified_name": "billing.api.create_charge",
                "path": "billing/api.py",
            },
            "label": "POST /charges",
        }
    ]
    # Globex has the shop too, but not Acme's map.
    other = ask(code, token_for(code, NEIGHBOUR, OTHER_ORG)).json()
    assert (other["connections"], other["link_owners"]) == ([], [])
