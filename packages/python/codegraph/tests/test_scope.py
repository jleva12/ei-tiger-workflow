"""What a caller of the code tools reads, from the admin API's access answer."""

import pytest
from forge_codegraph_query import NotFound

from forge_codegraph import RepositoryScope, repository_id

SHOP = "https://github.com/acme/shop"
BILLING = "https://github.com/acme/billing"
ANSWER = {
    "subject": "apikey:k1",
    "organization_id": "org-1",
    "repositories": [{"url": SHOP}, {"url": BILLING}],
    "connections": [
        {
            "id": "c1",
            "knowledge_base_id": "kb1",
            "knowledge_base": "Checkout",
            "kind": "calls",
            "description": "POST /orders",
            "source": {"url": SHOP, "owner": "acme", "name": "shop"},
            "target": {"url": BILLING, "owner": "acme", "name": "billing"},
            "code_links": [],
        }
    ],
    "link_owners": ["kb:kb1"],
    "systems": [
        {
            "id": "kb1",
            "name": "Checkout",
            "description": "",
            "groups": [],
            "applications": [{"name": "acme/shop", "url": SHOP, "group": ""}],
            "components": [],
            "connections": [],
        }
    ],
}


def test_the_answer_names_the_graphs_repositories() -> None:
    scope = RepositoryScope.from_access(ANSWER)
    assert scope.repository_ids == {repository_id(SHOP), repository_id(BILLING)}
    assert scope.readable(repository_id(SHOP))
    assert not scope.readable(repository_id("https://github.com/x/y"))
    [connection] = scope.connections
    assert connection["source"] == {
        "repository_id": repository_id(SHOP),
        "name": "acme/shop",
    }
    assert scope.systems[0]["applications"][0]["repository_id"] == repository_id(SHOP)
    assert scope.owns("kb:kb1") and not scope.owns("kb:other")
    with pytest.raises(NotFound, match="repository repo:theirs"):
        scope.check(repository_id(SHOP), "repo:theirs")


def test_claims_carry_the_scope_whole() -> None:
    scope = RepositoryScope.from_access(ANSWER)
    carried = RepositoryScope.from_claims(scope.claims(), scope.subject)
    assert carried == scope
    assert (carried.link_owners, carried.connections, carried.systems) == (
        scope.link_owners,
        scope.connections,
        scope.systems,
    )


def test_an_admin_api_from_before_system_maps_follows_every_link() -> None:
    old = {key: value for key, value in ANSWER.items() if key != "link_owners"}
    scope = RepositoryScope.from_access(old)
    assert scope.link_owners is None and scope.owns("kb:anyone")
    assert "link_owners" not in scope.claims()
    assert (
        RepositoryScope.from_access({**ANSWER, "link_owners": None}).link_owners
        == frozenset()
    )
