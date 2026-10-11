"""How code search ranks, on the emulator: the worker's Go tests of its search
(packages/go/code-graph/storage/spanner, TestLexicalSearchAndRerank,
TestNaturalLanguageSearch, TestFindNodesAndBackfill,
TestSearchDocumentsEmbeddingsHybrid) and of a generation's changes, ported
when the search moved here, so every reader keeps ranking as the worker did.

Run with ``make codegraph-query-test-spanner``; skipped otherwise.
"""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from spanner_fixture import VECTOR_LENGTH, Graph, create_database, drop_database, emulator

from forge_codegraph_query import InvalidRequest, NotFound
from forge_codegraph_query.spanner import SpannerGraphStore
from forge_codegraph_query.store import (
    CHANGE_RETIRED,
    CHANGE_UPDATED,
    SEARCH_LEXICAL,
    SEARCH_SEMANTIC,
    SearchHit,
    SearchRequest,
)

pytestmark = [
    pytest.mark.spanner,
    pytest.mark.skipif(
        emulator() is None, reason="needs the Spanner emulator (SPANNER_EMULATOR_HOST)"
    ),
]

AGENT = "core/src/main/java/agents/LlmAgent.java"
FLOW = "core/src/main/java/flows/LlmFlow.java"
AGENT_TEST = "core/src/test/java/agents/LlmAgentTest.java"
SHOP = "core/src/main/java/shop/OrderService.java"


def code(text: str) -> dict[str, str]:
    """A declaration's text, as the Go tests' fixtures carry it."""
    return {"source_text": text, "signature": text.split("{", 1)[0]}


def unit(i: int) -> list[float]:
    return [1.0 if j == i else 0.0 for j in range(VECTOR_LENGTH)]


def agents(url: str, *, staged: bool = False) -> Graph:
    """An agent framework; ``staged``, with an unpublished generation that
    rewrote run's code."""
    g = Graph(url)
    g.node("class", "LlmAgent", "com.acme.agents.LlmAgent", AGENT,
           props=code("public class LlmAgent extends BaseAgent { }"))  # fmt: skip
    impl = g.node(
        "method", "runAsyncImpl", "runAsyncImpl(com.acme.agents.InvocationContext)", AGENT,
        props=code("protected Flowable<Event> runAsyncImpl(InvocationContext ctx) { return llmFlow.run(ctx); }"),
    )  # fmt: skip
    run = g.node(
        "method", "run", "run(com.acme.agents.InvocationContext)", FLOW,
        props=code("public Flowable<Event> run(InvocationContext ctx) { return steps(ctx); }"),
        gen_to=2 if staged else None,
    )  # fmt: skip
    helper = g.node("method", "runHelper", "runHelper()", AGENT_TEST,
                    props=code("void runHelper() { run(); run(); run(); run(); run(); }"))  # fmt: skip
    g.edge("calls", impl, run)
    g.edge("calls", helper, run)
    if staged:
        # Loading generation 2 rewrote run's search document; live is still 1.
        g.node(
            "method", "run", "run(com.acme.agents.InvocationContext)", FLOW,
            props={"source_text": "public Flowable<Event> run(InvocationContext ctx) { return fastPath(ctx); }"},
            gen_from=2, node_id=run["id"],
        )  # fmt: skip
    return g


def finds() -> Graph:
    g = Graph("https://github.com/acme/find")
    g.node("class", "Foo", "com.acme.Foo", "src/Foo.java")
    g.node("method", "bar", "bar()", "src/Foo.java", props={"source_text": "void bar() { }"})
    g.node(
        "method", "bar", "bar(int)", "src/Foo.java", props={"source_text": "void bar(int n) { }"}
    )
    for i in range(14):
        g.node("method", "run", f"com.acme.Runner{i:02d}.run", f"src/Runner{i:02d}.java")
    return g


def orders() -> tuple[Graph, list[dict[str, Any]]]:
    """Three documents, embedded in id order as e0, e1, e2, and a symbol
    without one."""
    g = Graph("https://github.com/acme/orders")
    documented = [
        g.node("class", "OrderService", "OrderService", "src/orders/OrderService.java",
               props={"source_text": "class OrderService { void placeOrder() {} }"}),
        g.node("method", "placeOrder", "OrderService.placeOrder", "src/orders/OrderService.java",
               props={"source_text": "void placeOrder() { inventory.reserve(); }"}),
        g.node("method", "reserve", "InventoryService.reserve", "src/inventory/InventoryService.java",
               props={"source_text": "void reserve() { stock--; }"}),
    ]  # fmt: skip
    g.node("external_symbol", "List", "java.util.List", "")
    documented.sort(key=lambda node: node["id"])
    for i, node in enumerate(documented):
        g.embed(node, unit(i))
    return g, documented


def shop() -> Graph:
    g = Graph("https://github.com/acme/natural")
    service = g.node("class", "OrderService", "shop.OrderService", SHOP,
                     props=code("public class OrderService { /* places orders and reserves stock */ }"))  # fmt: skip
    place = g.node(
        "method", "placeOrder", "placeOrder(shop.Cart)", SHOP,
        props=code("public Order placeOrder(Cart cart) { inventory.reserve(cart); return orders.save(cart); }"),
    )  # fmt: skip
    g.node("field", "order", "order", SHOP, props=code("private Order order;"))
    g.node("class", "Order", "shop.Order", "core/src/main/java/shop/Order.java",
           props=code("public class Order { long id; }"))  # fmt: skip
    reserve = g.node(
        "method", "reserve", "reserve(shop.Cart)", "core/src/main/java/inventory/InventoryService.java",
        props=code("public void reserve(Cart cart) { stock.hold(cart.items()); }"),
    )  # fmt: skip
    checkout = g.node(
        "method", "checkout", "checkout(shop.Cart)", "web/src/main/java/shop/CheckoutController.java",
        props=code("public Response checkout(Cart cart) { return orderService.placeOrder(cart); }"),
    )  # fmt: skip
    g.edge("calls", checkout, place)
    g.edge("calls", place, reserve)
    del service
    return g


def changed() -> Graph:
    """Generation 2 updated bar and retired foo; generation 1 opened four."""
    g = Graph("https://github.com/acme/changes", live=2)
    foo = g.node("method", "foo", "foo()", "src/A.java", gen_to=2, retired=True)
    bar = g.node("method", "bar", "bar()", "src/A.java", gen_to=2)
    g.node("method", "bar", "bar()", "src/A.java", props={"source_text": "void bar() { work(); }"},
           gen_from=2, node_id=bar["id"])  # fmt: skip
    g.node("method", "baz", "baz()", "src/A.java")
    g.node("method", "qux", "qux()", "src/A.java")
    del foo
    return g


class World:
    def __init__(self) -> None:
        self.database = create_database()
        self.agents = agents("https://github.com/acme/agents")
        self.staged = agents("https://github.com/acme/staged", staged=True)
        self.finds = finds()
        self.orders, self.documented = orders()
        self.shop = shop()
        self.changes = changed()
        for graph in (self.agents, self.staged, self.finds, self.orders, self.shop, self.changes):
            graph.write(self.database)


@pytest.fixture(scope="module")
def world() -> Iterator[World]:
    made = World()
    yield made
    drop_database(made.database)


@pytest.fixture
async def store(world: World) -> AsyncIterator[SpannerGraphStore]:
    opened = SpannerGraphStore(
        world.database, scope="test", cursor_signing_key=b"k" * 32, vector_length=VECTOR_LENGTH
    )
    yield opened
    await opened.close()


def name(hit: SearchHit) -> str:
    return str((hit.node.node or {}).get("name")) if hit.node else ""


def first(hits: list[SearchHit]) -> str:
    return name(hits[0]) if hits else ""


async def test_lexical_search_and_rerank(store: SpannerGraphStore, world: World) -> None:
    async def search(text: str, **options: Any) -> list[SearchHit]:
        return await store.hybrid_search(
            SearchRequest(repository_ids=[world.agents.repository], text=text, **options)
        )

    # An identifier fragment reaches the camelCase name through the identifier tokens.
    hits = await search("async")
    assert first(hits) == "runAsyncImpl"
    top = hits[0]
    assert top.lexical > 0 and top.vector == 0 and not top.exact_match
    assert top.matched and top.snippet and top.signature
    # A partial identifier reaches it through the substring tokens.
    assert first(await search("AsyncImp")) == "runAsyncImpl"
    # The exact name wins over a document that repeats the word.
    hits = await search("run")
    assert first(hits) == "run" and hits[0].exact_match and hits[0].callers == 2
    # An exact qualified name is recognised too.
    hits = await search("com.acme.agents.LlmAgent")
    assert first(hits) == "LlmAgent" and hits[0].exact_match
    # Level two: near the caller's file and better connected ranks first.
    hits = await search("run ctx", near_path="core/src/main/java/flows/Steps.java")
    assert first(hits) == "run"
    degrees = {name(h): (h.callers, h.callees) for h in hits}
    assert degrees["run"] == (2, 0)
    assert degrees["runAsyncImpl"] == (0, 1)
    # Filters apply to the lexical branch.
    hits = await search("run", kinds=["method"], path_prefix="core/src/test/")
    assert [name(h) for h in hits] == ["runHelper"]
    assert await search("run", mode=SEARCH_LEXICAL)
    with pytest.raises(InvalidRequest):
        await search("run", mode=SEARCH_SEMANTIC)
    with pytest.raises(InvalidRequest):
        await search("run", mode="fuzzy")


async def test_a_document_an_unpublished_generation_rewrote_doesnt_match_live(
    store: SpannerGraphStore, world: World
) -> None:
    async def search(text: str) -> list[SearchHit]:
        return await store.hybrid_search(
            SearchRequest(repository_ids=[world.staged.repository], text=text)
        )

    assert "run" not in [name(h) for h in await search("run")]
    # The live document speaks for the live node (the Go test's sweep).
    assert (
        first(
            await store.hybrid_search(
                SearchRequest(repository_ids=[world.agents.repository], text="steps")
            )
        )
        == "run"
    )


async def test_find_nodes(store: SpannerGraphStore, world: World) -> None:
    repo = world.finds.repository
    assert len(await store.find_nodes(repo, "bar", "", None, 0, 10)) == 2
    found = await store.find_nodes(repo, "", "bar(int)", None, 0, 10)
    assert [v.node["qualified_name"] for v in found if v.node] == ["bar(int)"]
    found = await store.find_nodes(repo, "", "com.acme.Foo", ["class"], 0, 10)
    assert [v.node["name"] for v in found if v.node] == ["Foo"]
    assert await store.find_nodes(repo, "BAR", "", ["class"], 0, 10) == []
    with pytest.raises(InvalidRequest):
        await store.find_nodes(repo, "bar", "bar()", None, 0, 10)
    # The qualified name narrows in the query, not after the name's window.
    last = await store.find_nodes(repo, "", "com.acme.Runner13.run", None, 0, 1)
    assert [v.node["qualified_name"] for v in last if v.node] == ["com.acme.Runner13.run"]
    hits = await store.hybrid_search(SearchRequest(repository_ids=[repo], text="Foo"))
    assert first(hits) == "Foo" and hits[0].exact_match


async def test_documents_embeddings_and_hybrid_search(
    store: SpannerGraphStore, world: World
) -> None:
    repo = world.orders.repository
    documented = world.documented

    def request(**options: Any) -> SearchRequest:
        return SearchRequest(
            repository_ids=[repo], text=options.pop("text", ""), model="fake", **options
        )

    with pytest.raises(InvalidRequest):
        await store.hybrid_search(request(vector=[0.0] * 8))
    hits = await store.hybrid_search(request(vector=unit(1), limit=5))
    assert len(hits) == 3 and hits[0].node is not None and hits[0].node.id == documented[1]["id"]
    assert abs(hits[0].vector - 1) < 1e-6 and hits[0].lexical == 0
    hits = await store.hybrid_search(
        SearchRequest(repository_ids=[repo], text="inventory reserve stock")
    )
    assert first(hits) == "reserve" and hits[0].lexical > 0 and hits[0].vector == 0
    service = next(i for i, node in enumerate(documented) if node["kind"] == "class")
    hits = await store.hybrid_search(request(text="placeOrder", vector=unit(service), limit=2))
    assert len(hits) == 2 and hits[0].score > hits[1].score
    assert all(h.repository_id == repo and h.node and h.node.gen_from == 1 for h in hits)
    methods = [i for i, node in enumerate(documented) if node["kind"] == "method"]
    hits = await store.hybrid_search(request(vector=unit(methods[0]), kinds=["method"]))
    assert len(hits) == 2 and {h.node.node["kind"] for h in hits if h.node and h.node.node} == {
        "method"
    }
    hits = await store.hybrid_search(request(vector=unit(0), path_prefix="src/inventory/"))
    assert [name(h) for h in hits] == ["reserve"]
    with pytest.raises(NotFound):
        await store.hybrid_search(
            SearchRequest(repository_ids=[repo, "search-unknown"], text="reserve")
        )
    with pytest.raises(InvalidRequest):
        await store.hybrid_search(SearchRequest(repository_ids=[repo], text=""))


async def test_natural_language_search(store: SpannerGraphStore, world: World) -> None:
    async def search(text: str, **options: Any) -> tuple[str, dict[str, SearchHit]]:
        """The first hit's name, and every hit by name."""
        hits = await store.hybrid_search(
            SearchRequest(repository_ids=[world.shop.repository], text=text, **options)
        )
        return first(hits), {name(h): h for h in hits}

    top, hits = await search("who calls placeOrder from the checkout flow?")
    assert top == "placeOrder"
    assert hits["placeOrder"].exact_match and not hits["placeOrder"].name_match
    assert hits["checkout"].name_match and not hits["checkout"].exact_match
    assert not {"who", "calls", "from", "the"} & {t for h in hits.values() for t in h.matched}
    _, hits = await search("explain OrderService.placeOrder() to me")
    assert hits["placeOrder"].exact_match and hits["OrderService"].exact_match
    _, hits = await search("how is an order placed and reserved")
    assert hits["Order"].name_match and not hits["Order"].exact_match
    assert "order" not in hits or not hits["order"].name_match
    assert not any(h.exact_match for h in hits.values())
    top, _ = await search("where does reserve hold stock under core/src/main/java/inventory")
    assert top == "reserve"
    top, hits = await search("placeOrder")
    assert top == "placeOrder" and hits["placeOrder"].exact_match
    await search("what is the class")
    _, fields = await search("how is an order placed", kinds=["field"])
    assert all(h.node and h.node.node and h.node.node["kind"] == "field" for h in fields.values())
    assert not any(h.name_match for h in fields.values())


async def test_a_generations_changes(store: SpannerGraphStore, world: World) -> None:
    repo = world.changes.repository
    page = await store.changes(repo, 0, "node", 0, "")
    assert (page.generation, page.commit) == (2, "c2")
    assert (page.added, page.updated, page.retired) == (0, 1, 1)
    by_op = {change.op: change for change in page.changes}
    assert set(by_op) == {CHANGE_UPDATED, CHANGE_RETIRED}
    updated = by_op[CHANGE_UPDATED]
    assert updated.before is not None and updated.before.gen_to == 2
    assert updated.version is not None and updated.version.gen_from == 2
    first_page = await store.changes(repo, 1, "node", 3, "")
    assert first_page.added == 4 and len(first_page.changes) == 3 and first_page.next_cursor
    rest = await store.changes(repo, 1, "node", 3, first_page.next_cursor)
    assert len(rest.changes) == 1 and not rest.next_cursor
    with pytest.raises(InvalidRequest):
        await store.changes(repo, 3, "node", 0, "")
