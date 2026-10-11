"""The Spanner store against the emulator: the worker's schema, rows as its
loader writes them, and the reader's SQL over the real indexes.

Run with ``make codegraph-query-test-spanner`` (it starts the emulator and
sets SPANNER_EMULATOR_HOST); skipped otherwise.
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from spanner_fixture import MODEL, VECTOR_LENGTH, Graph, create_database, drop_database, emulator

from forge_codegraph_query import CodeGraph, CrossLink, InvalidRequest, NotFound, to_json
from forge_codegraph_query.model import BOTH, IN, OUT, CrossLinkQuery, NeighborQuery
from forge_codegraph_query.spanner import SpannerGraphStore, VectorLengthMismatch
from forge_codegraph_query.store import SEARCH_LEXICAL, SEARCH_SEMANTIC, SearchRequest
from forge_codegraph_query.testing import FakeEmbedder

pytestmark = [
    pytest.mark.spanner,
    pytest.mark.skipif(
        emulator() is None, reason="needs the Spanner emulator (SPANNER_EMULATOR_HOST)"
    ),
]

KEY = b"k" * 32

ORDER_SERVICE = """public class OrderService {
    public Order placeOrder(Cart cart) {
        inventory.reserve(cart);
        return payments.pay(cart);
    }
}
"""


@dataclass
class Shop:
    database: str
    shop: Graph
    billing: Graph
    nodes: dict[str, dict[str, Any]]


@pytest.fixture(scope="module")
def world() -> Iterator[Shop]:
    """Two repositories: a shop whose order service calls inventory and
    payments (with one retired and one stale document), and billing."""
    database = create_database()
    shop = Graph("https://github.com/acme/shop", live=2)
    service = shop.node(
        "class",
        "OrderService",
        "shop.OrderService",
        "src/shop/OrderService.java",
        props={"signature": "public class OrderService", "docstring": "Places customer orders."},
        source=ORDER_SERVICE,
        vector=[1.0, 0.0, 0.0, 0.0],
    )
    place = shop.node(
        "method",
        "placeOrder",
        "shop.OrderService.placeOrder(shop.Cart)",
        "src/shop/OrderService.java",
        props={"signature": "public Order placeOrder(Cart cart)"},
        source="    public Order placeOrder(Cart cart) {\n        return payments.pay(cart);\n    }\n",
        start_line=2,
        vector=[0.9, 0.1, 0.0, 0.0],
    )
    reserve = shop.node(
        "method",
        "reserve",
        "shop.InventoryService.reserve(shop.Cart)",
        "src/shop/InventoryService.java",
        props={"signature": "void reserve(Cart cart)", "docstring": "Holds stock for a cart."},
        vector=[0.0, 1.0, 0.0, 0.0],
    )
    pay = shop.node(
        "method",
        "pay",
        "shop.PaymentService.pay(shop.Cart)",
        "src/shop/PaymentService.java",
        props={"signature": "Order pay(Cart cart)", "docstring": "Charges the card."},
        vector=[0.0, 0.0, 1.0, 0.0],
    )
    checkout = shop.node(
        "method",
        "checkout",
        "shop.CheckoutController.checkout(shop.Cart)",
        "web/CheckoutController.java",
        props={"signature": "Response checkout(Cart cart)"},
    )
    # Retired at generation 2: invisible at live, visible at 1.
    legacy = shop.node(
        "method",
        "legacyPlaceOrder",
        "shop.Legacy.legacyPlaceOrder()",
        "src/legacy/Legacy.java",
        gen_to=2,
    )
    shop.edge("contains", service, place)
    shop.edge("calls", place, reserve)
    shop.edge("calls", place, pay)
    shop.edge("calls", checkout, place)
    shop.run("run-1", 1, generation=1)
    shop.run("run-2", 2, generation=2)
    shop.run("run-3", 3, phase="FAILED", generation=0)
    billing = Graph("https://github.com/acme/billing")
    invoice = billing.node(
        "method",
        "invoice",
        "billing.Invoices.invoice(billing.Order)",
        "src/billing/Invoices.java",
        props={"signature": "void invoice(Order order)", "docstring": "Bills a placed order."},
        vector=[0.0, 0.0, 0.0, 1.0],
    )
    shop.write(database)
    billing.write(database)
    nodes = {
        "service": service,
        "place": place,
        "reserve": reserve,
        "pay": pay,
        "checkout": checkout,
        "legacy": legacy,
        "invoice": invoice,
    }
    yield Shop(database, shop, billing, nodes)
    drop_database(database)


@pytest.fixture
async def store(world: Shop) -> AsyncIterator[SpannerGraphStore]:
    opened = SpannerGraphStore(
        world.database, scope="test", cursor_signing_key=KEY, vector_length=VECTOR_LENGTH
    )
    yield opened
    await opened.close()


async def test_the_store_checks_the_embedding_column(world: Shop) -> None:
    await SpannerGraphStore(
        world.database, scope="test", cursor_signing_key=KEY, vector_length=VECTOR_LENGTH
    ).ping()
    wrong = SpannerGraphStore(world.database, scope="test", cursor_signing_key=KEY, vector_length=8)
    with pytest.raises(VectorLengthMismatch):
        await wrong.ping()


async def test_state_and_repositories(store: SpannerGraphStore, world: Shop) -> None:
    state = await store.state(world.shop.repository)
    assert (state.branch, state.live_generation, state.live_commit) == ("main", 2, "c" * 40)
    listed, more = await store.repositories(10)
    assert {r.full_name for r in listed} == {"acme/shop", "acme/billing"} and not more
    with pytest.raises(NotFound):
        await store.state("repo:nowhere")


async def test_generations_show_what_was_open(store: SpannerGraphStore, world: Shop) -> None:
    legacy = world.nodes["legacy"]["id"]
    with pytest.raises(NotFound):
        await store.get_node(world.shop.repository, legacy, 0)
    assert (await store.get_node(world.shop.repository, legacy, 1)).id == legacy
    with pytest.raises(InvalidRequest):
        await store.get_node(world.shop.repository, legacy, 3)


async def test_list_nodes_pages_by_id_and_kind(store: SpannerGraphStore, world: Shop) -> None:
    repo = world.shop.repository
    first = await store.list_nodes(repo, 0, "", 3, "")
    assert first.generation == 2 and len(first.nodes) == 3 and first.next_cursor
    rest = await store.list_nodes(repo, 0, "", 3, first.next_cursor)
    ids = [v.id for v in first.nodes + rest.nodes]
    assert ids == sorted(ids) and len(ids) == 5 and not rest.next_cursor
    classes = await store.list_nodes(repo, 0, "class", 10, "")
    assert [v.id for v in classes.nodes] == [world.nodes["service"]["id"]]
    # A cursor is bound to its query.
    with pytest.raises(InvalidRequest):
        await store.list_nodes(repo, 0, "class", 3, first.next_cursor)


async def test_neighbors_and_the_view(store: SpannerGraphStore, world: Shop) -> None:
    repo, place = world.shop.repository, world.nodes["place"]["id"]
    page = await store.neighbors(NeighborQuery(repository_id=repo, node_id=place, direction=OUT))
    assert {n.node.id for n in page.neighbors if n.node} == {
        world.nodes["reserve"]["id"],
        world.nodes["pay"]["id"],
    }
    callers = await store.neighbors(NeighborQuery(repository_id=repo, node_id=place, direction=IN))
    assert {n.node.id for n in callers.neighbors if n.node} == {
        world.nodes["checkout"]["id"],
        world.nodes["service"]["id"],
    }
    view = to_json(await CodeGraph(store).view(repo))
    assert view["generation"] == 2 and view["branch"] == "main"
    assert len(view["nodes"]) == 5 and len(view["edges"]) == 4 and not view["truncated"]
    # An earlier generation names the commit its run built.
    earlier = await CodeGraph(store).view(repo, generation=1)
    assert earlier.commit_sha == f"{1:040d}"
    assert any(v.id == world.nodes["legacy"]["id"] for v in earlier.nodes)


async def test_find_nodes_by_name_and_qualified_name(store: SpannerGraphStore, world: Shop) -> None:
    repo = world.shop.repository
    found = await store.find_nodes(repo, "PLACEORDER", "", None, 0, 10)
    assert [v.id for v in found] == [world.nodes["place"]["id"]]
    exact = await store.find_nodes(repo, "", "shop.PaymentService.pay(shop.Cart)", None, 0, 10)
    assert [v.id for v in exact] == [world.nodes["pay"]["id"]]
    assert await store.find_nodes(repo, "placeOrder", "", ["class"], 0, 10) == []


async def test_source_is_verified_and_cut(store: SpannerGraphStore, world: Shop) -> None:
    graph = CodeGraph(store)
    source = await graph.source(world.shop.repository, world.nodes["service"]["id"], context=0)
    assert source.text == ORDER_SERVICE
    with pytest.raises(NotFound):
        await graph.source(world.shop.repository, world.nodes["pay"]["id"])


async def test_lexical_search_ranks_exact_names_first(
    store: SpannerGraphStore, world: Shop
) -> None:
    hits = await store.hybrid_search(
        SearchRequest(
            repository_ids=[world.shop.repository], text="placeOrder", mode=SEARCH_LEXICAL
        )
    )
    assert hits[0].node is not None and hits[0].node.id == world.nodes["place"]["id"]
    assert hits[0].exact_match
    # The retired node's document doesn't match live.
    assert world.nodes["legacy"]["id"] not in {h.node.id for h in hits if h.node}
    # Callers rank higher among the rest: placeOrder has one.
    assert hits[0].callers == 1 and hits[0].callees == 2


async def test_semantic_search_follows_the_vectors(store: SpannerGraphStore, world: Shop) -> None:
    def semantic(repository: str, vector: list[float]) -> SearchRequest:
        return SearchRequest(
            repository_ids=[repository], text="", mode=SEARCH_SEMANTIC, model=MODEL, vector=vector
        )

    hits = await store.hybrid_search(semantic(world.shop.repository, [0.0, 0.0, 1.0, 0.0]))
    assert hits[0].node is not None and hits[0].node.id == world.nodes["pay"]["id"]
    assert hits[0].vector > 0.99 and hits[0].lexical == 0
    billed = await store.hybrid_search(semantic(world.billing.repository, [0.0, 0.0, 0.0, 1.0]))
    assert [h.node.id for h in billed if h.node] == [world.nodes["invoice"]["id"]]


async def test_repositories_are_ranked_by_rank_not_similarity(
    store: SpannerGraphStore, world: Shop
) -> None:
    # Each repository's branches are fused by rank, then the repositories'
    # hits merged by score: a near match in one repository ranks with the
    # other's first hits however far they are, and connectivity breaks ties.
    hits = await store.hybrid_search(
        SearchRequest(
            repository_ids=[world.shop.repository, world.billing.repository],
            text="",
            mode=SEARCH_SEMANTIC,
            model=MODEL,
            vector=[0.0, 0.0, 0.0, 1.0],
            limit=10,
        )
    )
    invoice = next(h for h in hits if h.repository_id == world.billing.repository)
    assert invoice.vector > 0.99
    assert hits[0].repository_id == world.shop.repository and hits[0].vector < 0.5


async def test_code_search_answers_alike_for_every_caller(
    world: Shop, store: SpannerGraphStore
) -> None:
    graph = CodeGraph(store, FakeEmbedder())
    found = await graph.search(
        [world.shop.repository, world.billing.repository], "where do we place an order", source=True
    )
    answer = to_json(found)
    assert answer["semantic"] is True
    first = answer["hits"][0]
    assert first["id"] in {world.nodes["service"]["id"], world.nodes["place"]["id"]}
    assert first["file"] == "src/shop/OrderService.java"
    assert "placeOrder" in first["text"]


async def test_stats_count_the_live_graph(store: SpannerGraphStore, world: Shop) -> None:
    stats = await store.stats(world.shop.repository, MODEL, VECTOR_LENGTH)
    assert (stats.generation, stats.nodes, stats.edges) == (2, 5, 4)
    assert stats.node_kinds == {"class": 1, "method": 4}
    assert stats.edge_kinds == {"contains": 1, "calls": 3}
    assert stats.searchable == 5
    assert (stats.embeddings.current, stats.embeddings.stored) == (4, 4)
    off = await store.stats(world.shop.repository, "", 0)
    assert off.embeddings.model == "" and off.searchable == 5


async def test_runs_page_newest_first(store: SpannerGraphStore, world: Shop) -> None:
    first = await store.runs(world.shop.repository, 2, "")
    assert [r["key"]["run_id"] for r in first.runs] == ["run-3", "run-2"]
    rest = await store.runs(world.shop.repository, 2, first.next_cursor)
    assert [r["key"]["run_id"] for r in rest.runs] == ["run-1"] and not rest.next_cursor
    with pytest.raises(NotFound):
        await store.runs("repo:nowhere", 2, "")


async def test_cross_links_are_replaced_per_owner(store: SpannerGraphStore, world: Shop) -> None:
    shop, billing = world.shop.repository, world.billing.repository
    place, invoice = world.nodes["place"]["id"], world.nodes["invoice"]["id"]

    def link(link_id: str, owner: str) -> CrossLink:
        return CrossLink.from_json(
            {
                "id": link_id,
                "owner": owner,
                "kind": "sends_event",
                "source": {"repository_id": shop, "node_id": place},
                "target": {"repository_id": billing, "node_id": invoice},
                "provenance": "manual",
            }
        )

    await store.replace_cross_links("kb:a", [link("1", "kb:a"), link("2", "kb:a")])
    await store.replace_cross_links("kb:b", [link("3", "kb:b")])
    await store.replace_cross_links("kb:a", [link("2", "kb:a")])

    found = await store.cross_links(CrossLinkQuery(repository_id=shop, direction=BOTH))
    assert sorted((x.owner, x.id) for x in found) == [("kb:a", "2"), ("kb:b", "3")]
    into = await store.cross_links(
        CrossLinkQuery(repository_id=billing, direction=IN, node_ids=(invoice,))
    )
    assert len(into) == 2
    await store.replace_cross_links("kb:a", [])
    await store.replace_cross_links("kb:b", [])
    assert await store.cross_links(CrossLinkQuery(repository_id=shop, direction=BOTH)) == []
