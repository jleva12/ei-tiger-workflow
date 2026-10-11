"""CodeGraph, the object every app reads the graph through, over the shop."""

from typing import Any

import pytest
from conftest import REPO, FakeEmbedder, Shop

from forge_codegraph_query import (
    CodeGraph,
    CrossLink,
    InvalidRequest,
    NotFound,
    Unavailable,
    VectorLengthMismatch,
    status_of,
    to_json,
)
from forge_codegraph_query.query.search import HIT_SOURCE_BYTES, bound_source
from forge_codegraph_query.service import MAX_SEARCH_REPOSITORIES
from forge_codegraph_query.testing import MemStore

OTHER = "repo:other"


def graph_of(shop: Shop, embedder: FakeEmbedder | None = None, **options: Any) -> CodeGraph:
    return CodeGraph(shop.store, embedder, **options)


async def test_search_answers_hits_in_brief_with_their_signature(
    shop: Shop, embedder: FakeEmbedder
) -> None:
    found = await graph_of(shop, embedder, enhance_query=True).search([REPO], "placeOrder")

    answer = to_json(found)
    assert answer["semantic"] is True
    assert [hit["id"] for hit in answer["hits"]] == [
        shop.place_order["id"],
        shop.order_service["id"],
    ]
    first = answer["hits"][0]
    assert first["repository_id"] == REPO
    assert first["qualified_name"] == "placeOrder(shop.Cart)"
    assert first["file"] == "src/shop/OrderService.java"
    assert first["exact_match"] is True
    assert first["signature"] == "public Order placeOrder(Cart cart)"
    assert "text" not in first
    # Every search runs with the configured query enhancement.
    assert shop.store.searches[0].enhance_query is True


async def test_search_with_source_carries_each_hits_code(shop: Shop) -> None:
    found = await graph_of(shop).search([REPO], "placeOrder", source=True)

    by_id = {hit.hit.brief.id: hit for hit in found.hits}
    place = by_id[shop.place_order["id"]]
    assert place.text.startswith("public Order placeOrder(Cart cart) {")
    assert (place.text_start_line, place.text_end_line) == (4, 7)
    assert not place.truncated
    # The class's span is the whole file.
    assert by_id[shop.order_service["id"]].text == shop.source


async def test_a_hit_whose_source_cant_be_read_keeps_its_snippet(shop: Shop) -> None:
    shop.store.sources.clear()

    found = await graph_of(shop).search([REPO], "placeOrder", source=True)

    assert [hit.text for hit in found.hits] == ["", ""]


async def test_search_reads_each_repository_once_and_none_finds_nothing(shop: Shop) -> None:
    graph = graph_of(shop)

    await graph.search([REPO, REPO], "placeOrder")
    assert shop.store.searches[0].repository_ids == [REPO]

    assert (await graph.search([], "placeOrder")).hits == []
    assert len(shop.store.searches) == 1


@pytest.mark.parametrize(
    ("repositories", "query", "limit"),
    [
        ([REPO], "", 0),
        ([REPO], "q" * 8001, 0),
        ([f"repo:{i}" for i in range(MAX_SEARCH_REPOSITORIES + 1)], "x", 0),
        ([REPO], "x", 51),
        ([REPO], "x", -1),
    ],
)
async def test_search_bounds(shop: Shop, repositories: list[str], query: str, limit: int) -> None:
    with pytest.raises(InvalidRequest) as refused:
        await graph_of(shop).search(repositories, query, limit=limit)
    assert status_of(refused.value) == 400


async def test_a_question_that_cant_be_embedded_is_unavailable(shop: Shop) -> None:
    class Down(FakeEmbedder):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            raise ConnectionError("provider down")

    with pytest.raises(Unavailable) as refused:
        await graph_of(shop, Down()).search([REPO], "where are orders placed")
    assert status_of(refused.value) == 503


def test_bound_source_cuts_at_a_line_end_never_inside_a_character() -> None:
    assert bound_source("short", 10) == ("short", False)
    assert bound_source("one\ntwo\nthree", 10) == ("one\ntwo", True)
    # No line end before the cut: cut at the last whole character.
    assert bound_source("ééééé", 5) == ("éé", True)
    long = ("x" * 99 + "\n") * 100
    text, cut = bound_source(long, HIT_SOURCE_BYTES)
    assert cut and len(text.encode()) <= HIT_SOURCE_BYTES and long.startswith(text)


async def test_view_samples_seeds_and_the_edges_among_them(shop: Shop) -> None:
    view = await graph_of(shop).view(REPO)

    answer = to_json(view)
    assert answer["repository_id"] == REPO
    assert answer["generation"] == 1
    assert answer["commit_sha"] == "c1"
    assert len(answer["nodes"]) == 8
    assert {e["fact"]["edge"]["kind"] for e in answer["edges"]} == {
        "contains",
        "calls",
        "uses_type",
        "overrides",
    }
    assert answer["truncated"] is False
    assert [n["fact"]["node"]["id"] for n in answer["nodes"]] == sorted(
        n["fact"]["node"]["id"] for n in answer["nodes"]
    )


async def test_view_of_one_kind_pages_its_seeds(shop: Shop) -> None:
    graph = graph_of(shop)

    classes = await graph.view(REPO, kind="class")
    assert [v.id for v in classes.nodes if (v.node or {}).get("kind") == "class"] == [
        shop.order_service["id"]
    ]

    with pytest.raises(InvalidRequest):
        await graph.view(REPO, generation=2)


async def test_node_neighbors_symbols_and_source(shop: Shop) -> None:
    graph = graph_of(shop)
    place = shop.place_order["id"]

    assert (await graph.node(REPO, place)).id == place

    around = await graph.neighbors(REPO, place, direction="out", limit=2)
    assert len(around.neighbors) == 2 and around.next_cursor

    found = await graph.symbols(REPO, name="PLACEORDER")
    assert [v.id for v in found] == [place]

    source = await graph.source(REPO, place, context=1)
    assert source.text_start_line == 3 and source.text.startswith("public class OrderService")

    with pytest.raises(NotFound) as missing:
        await graph.node(REPO, "method:missing")
    assert status_of(missing.value) == 404


@pytest.mark.parametrize(
    "call",
    [
        lambda g: g.node(REPO, ""),
        lambda g: g.node(REPO, "method:x", generation=-1),
        lambda g: g.neighbors(REPO, "method:x", direction="sideways"),
        lambda g: g.neighbors(REPO, "method:x", limit=201),
        lambda g: g.neighbors(REPO, "method:x", limit=0),
        lambda g: g.symbols(REPO),
        lambda g: g.symbols(REPO, name="a", qualified_name="b"),
        lambda g: g.symbols(REPO, name="a", limit=201),
        lambda g: g.source(REPO, "method:x", context=201),
        lambda g: g.runs(REPO, limit=51),
        lambda g: g.runs(REPO, cursor="c" * 8193),
    ],
)
async def test_explorer_bounds(shop: Shop, call: Any) -> None:
    with pytest.raises(InvalidRequest):
        await call(graph_of(shop))


async def test_stats_count_the_configured_models_embeddings(shop: Shop) -> None:
    shop.store.searchable, shop.store.embedded = 6, 5

    stats = await graph_of(shop, embedding_model="m", embedding_dimensions=1024).stats(REPO)

    assert stats.nodes == 8 and stats.node_kinds == {"class": 1, "method": 7}
    assert stats.edges == 8 and stats.edge_kinds["calls"] == 5
    assert stats.searchable == 6
    assert to_json(stats.embeddings) == {
        "model": "m",
        "dimensions": 1024,
        "current": 5,
        "stored": 5,
    }
    off = await graph_of(shop).stats(REPO)
    assert off.embeddings.model == ""


async def test_runs_page_newest_first(shop: Shop) -> None:
    shop.store.run_records = [{"key": {"run_id": f"r{i}"}} for i in range(3)]
    graph = graph_of(shop)

    first = await graph.runs(REPO, limit=2)
    assert [r["key"]["run_id"] for r in first.runs] == ["r0", "r1"]
    rest = await graph.runs(REPO, limit=2, cursor=first.next_cursor)
    assert [r["key"]["run_id"] for r in rest.runs] == ["r2"] and not rest.next_cursor


def link(link_id: str, owner: str = "kb:1", **changes: Any) -> dict[str, Any]:
    return {
        "id": link_id,
        "owner": owner,
        "kind": "calls_api",
        "source": {"repository_id": REPO, "node_id": "method:a", "qualified_name": "a()"},
        "target": {"repository_id": OTHER, "node_id": "method:b"},
        "provenance": "manual",
        **changes,
    }


async def test_replace_cross_links_makes_the_owners_whole_set(shop: Shop) -> None:
    graph = graph_of(shop)
    shop.store.link("old", "calls_api", REPO, "method:a", OTHER, "method:b")

    assert await graph.replace_cross_links("kb:1", [link("l1"), link("l2")]) == 2
    assert await graph.replace_cross_links("kb:1", [link("l2")]) == 1

    owners = sorted((x.owner, x.id) for x in shop.store.links)
    assert owners == [("kb:1", "l2"), ("team:test", "old")]


@pytest.mark.parametrize(
    "bad",
    [
        link("l1", kind="calls_maybe"),
        link("l1", owner="kb:2"),
        link("l1", provenance="inferred"),
        link("l1", target={"repository_id": REPO, "node_id": "method:b"}),
        link("l1", source={"repository_id": REPO, "node_id": "not an id"}),
        link("has space"),
        link("l1", label="x" * 501),
    ],
)
async def test_replace_cross_links_refuses_a_bad_link(shop: Shop, bad: dict[str, Any]) -> None:
    with pytest.raises(InvalidRequest):
        await graph_of(shop).replace_cross_links("kb:1", [bad])


async def test_cross_link_round_trips_through_json() -> None:
    parsed = CrossLink.from_json(link("l1", label="POST /orders", created_by="u1"))
    parsed.validate()
    assert to_json(parsed) == link("l1", label="POST /orders", created_by="u1")


async def test_a_store_without_writes_refuses_cross_links() -> None:
    class ReadOnly:
        pass

    with pytest.raises(InvalidRequest):
        await CodeGraph(ReadOnly()).replace_cross_links("kb:1", [])  # type: ignore[arg-type]


async def test_close_runs_the_openers_cleanup() -> None:
    closed: list[bool] = []

    async def on_close() -> None:
        closed.append(True)

    await CodeGraph(MemStore(REPO), on_close=on_close).close()
    assert closed == [True]


async def test_wait_for_the_workers_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from forge_codegraph_query import service

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(service.asyncio, "sleep", no_sleep)

    class Store(MemStore):
        def __init__(self, failures: list[Exception]) -> None:
            super().__init__(REPO)
            self.failures = failures
            self.pings = 0

        async def ping(self) -> None:
            self.pings += 1
            if self.failures:
                raise self.failures.pop(0)

    # Not there yet, then there: startup goes on.
    store = Store([ConnectionError("no database"), ConnectionError("no schema")])
    await CodeGraph(store).wait(60)
    assert store.pings == 3
    # A database made for other embeddings never will be.
    store = Store([VectorLengthMismatch("3072, configured 1024")])
    with pytest.raises(VectorLengthMismatch):
        await CodeGraph(store).wait(60)
    assert store.pings == 1
    # Out of patience, the last failure stops startup.
    store = Store([ConnectionError("down")] * 3)
    with pytest.raises(ConnectionError):
        await CodeGraph(store).wait(0)
