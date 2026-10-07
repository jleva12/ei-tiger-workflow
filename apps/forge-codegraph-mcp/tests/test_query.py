"""The graph operations, on the Go tests' fixtures (packages/go/code-graph/agentquery)."""

import dataclasses
import hashlib
from typing import Any

import pytest
from conftest import REPO, FakeEmbedder, Shop
from memstore import MemStore

from forge_codegraph_mcp.graph import query
from forge_codegraph_mcp.graph.errors import IntegrityFailure, InvalidRequest, NotFound
from forge_codegraph_mcp.graph.model import (
    BOTH,
    EDGE_CALLS,
    EDGE_USES_TYPE,
    IN,
    OUT,
    Version,
    graph_id,
    to_json,
)
from forge_codegraph_mcp.graph.query.impact import ImpactHit, locate
from forge_codegraph_mcp.graph.query.impact import TestClass as ImpactTestClass
from forge_codegraph_mcp.graph.query.impact import test_class_of as class_of_test
from forge_codegraph_mcp.graph.query.impact import test_commands as commands_for_tests


def hit_ids(hits: list[ImpactHit]) -> dict[str, ImpactHit]:
    return {hit.node.id: hit for hit in hits if hit.node is not None}


def with_build(shop: Shop) -> dict[str, Any]:
    """The build context the assessment reads: a source_file node per
    lineage with its Maven source set, and a test method calling placeOrder."""
    main = graph_id("file", REPO, "m", "main", "src/shop/OrderService.java")
    test = graph_id("file", REPO, "m", "test", "src/test/java/shop/OrderServiceTest.java")
    for lineage, path, source_set in (
        (main, "src/shop/OrderService.java", "m:main"),
        (test, "src/test/java/shop/OrderServiceTest.java", "m:test"),
    ):
        shop.store.add_node(
            {
                "id": lineage,
                "kind": "source_file",
                "name": path.rsplit("/", 1)[1],
                "properties": {
                    "file_path": {"string": path},
                    "language": {"string": "java"},
                    "module_id": {"string": "m"},
                    "source_set_id": {"string": source_set},
                },
            }
        )
    method = shop.store.add_node(
        {
            "id": graph_id("method", "testPlaceOrder()"),
            "kind": "method",
            "name": "testPlaceOrder",
            "qualified_name": "testPlaceOrder()",
            "source": {
                "lineage": test,
                "content_sha256": "",
                "span": {"start": {"line": 12}, "end": {}},
            },
            "properties": {
                "file_path": {"string": "src/test/java/shop/OrderServiceTest.java"},
                "language": {"string": "java"},
            },
        }
    )
    shop.store.add_edge(EDGE_CALLS, method["id"], shop.place_order["id"])
    return method


async def test_explore(shop: Shop, embedder: FakeEmbedder) -> None:
    result = await query.explore(
        shop.store,
        embedder,
        repository_ids=[REPO],
        question="who calls placeOrder in OrderService?",
        expand=1,
        near_path="src/shop/Cart.java",
    )
    search = shop.store.searches[0]
    assert result.semantic and embedder.calls == 1 and len(shop.store.searches) == 1
    assert search.model == "fake" and search.vector is not None
    assert search.near_path == "src/shop/Cart.java" and search.limit == 8
    assert result.query.sentence and len(result.query.symbols) == 2
    first, second = result.seeds
    assert first.summary.id == shop.place_order["id"] and first.expanded and not second.expanded
    assert first.summary.signature == "public Order placeOrder(Cart cart)"
    assert first.summary.start_line == 4 and first.summary.content_sha256
    assert second.documentation == "Places orders."
    # One hop from placeOrder: callees out, the caller in, the seed link to
    # OrderService kept as a link, and the containment edge ignored.
    want = {
        shop.reserve["id"]: OUT,
        shop.pay["id"]: OUT,
        shop.checkout["id"]: IN,
        shop.place["id"]: OUT,
    }
    assert {r.summary.id: r.direction for r in result.related} == want
    assert all(r.seed_id == shop.place_order["id"] and r.via for r in result.related)
    assert [(link.via, link.source_id, link.target_id) for link in result.links] == [
        (EDGE_USES_TYPE, shop.place_order["id"], shop.order_service["id"])
    ]
    assert len(result.files) == 5
    assert (result.files[0].file_path, result.files[0].seeds) == ("src/shop/OrderService.java", 2)
    assert result.files[0].language == "java" and result.files[1].related == 1
    assert not result.truncated

    # Lexical mode never embeds; a page cap reports truncation.
    shop.store.neighbor_page = 2
    result = await query.explore(
        shop.store,
        embedder,
        repository_ids=[REPO],
        question="placeOrder",
        mode="lexical",
        expand=1,
        neighbors=10,
    )
    assert not result.semantic and embedder.calls == 1 and result.truncated
    assert len(result.related) + len(result.links) == 2

    # Zero limits take their defaults; without an embedder search is lexical.
    shop.store.neighbor_page = 0
    result = await query.explore(
        shop.store, None, repository_ids=[REPO], question="placeOrder", limit=1
    )
    assert not result.semantic and len(result.seeds) == 1 and result.seeds[0].expanded
    assert len(result.related) == 5 and not result.links
    assert shop.store.searches[-1].vector is None

    compact = to_json(query.compact_explore(result))
    assert set(compact) == {"question", "semantic", "seeds", "related", "files", "truncated"}
    assert compact["seeds"][0]["qualified_name"] == "placeOrder(shop.Cart)"
    assert "name" not in compact["seeds"][0]  # the qualified name starts with it


@pytest.mark.parametrize(
    "bad",
    [
        {"repository_ids": [], "question": "x"},
        {"repository_ids": [REPO], "question": ""},
        {"repository_ids": [REPO], "question": "q" * 8001},
        {"repository_ids": [REPO], "question": "x", "limit": 26},
        {"repository_ids": [REPO], "question": "x", "expand": 11},
        {"repository_ids": [REPO], "question": "x", "neighbors": 101},
    ],
)
async def test_explore_bounds(shop: Shop, bad: dict[str, Any]) -> None:
    with pytest.raises(InvalidRequest):
        await query.explore(shop.store, None, **bad)


async def test_search_expands_with_the_repositorys_vocabulary(shop: Shop) -> None:
    store = shop.store
    inventory = [
        store.add_node(
            {
                "id": graph_id("method", name),
                "kind": "method",
                "name": name,
                "qualified_name": f"{name}()",
                "properties": {"owner_key": {"string": "shop.InventoryLedger"}},
            }
        )
        for name in ("reserveStock", "releaseStock")
    ]
    store.hits = [store.hit(node["id"], 0.03, False) for node in inventory]
    result = await query.search(
        store, None, repository_ids=[REPO], text="how are items held back", expand=True
    )
    # Both hits share their owner's fragments and "stock"; "reserve" and
    # "release" appear once each.
    assert result.expanded_terms == ["stock", "inventoryledger", "inventory", "ledger"]
    assert store.searches[-1].mode == "lexical"
    assert store.searches[-1].text == (
        "how are items held back stock inventoryledger inventory ledger"
    )
    # An exact match means nothing is expanded.
    store.hits = [store.hit(shop.place_order["id"], 1.2, True)]
    result = await query.search(
        store, None, repository_ids=[REPO], text="where do we place orders", expand=True
    )
    assert result.expanded_terms == []


async def test_impact(shop: Shop) -> None:
    result = await query.impact(shop.store, repository_id=REPO, node_id=shop.place_order["id"])
    # The override's callers and the base method's callers are both impacted;
    # the second hop reaches the legacy caller's caller. Callees are not.
    assert result.root == shop.place_order["id"]
    assert result.roots == [shop.place_order["id"], shop.place["id"]] and not result.truncated
    got = hit_ids(result.hits)
    assert set(got) == {shop.checkout["id"], shop.legacy["id"], shop.cron["id"]}
    assert (got[shop.checkout["id"]].depth, got[shop.checkout["id"]].from_) == (
        1,
        shop.place_order["id"],
    )
    assert (got[shop.legacy["id"]].depth, got[shop.legacy["id"]].from_) == (1, shop.place["id"])
    assert (got[shop.cron["id"]].depth, got[shop.cron["id"]].from_) == (2, shop.legacy["id"])
    assert got[shop.checkout["id"]].via == EDGE_CALLS

    result = await query.impact(
        shop.store, repository_id=REPO, node_id=shop.place_order["id"], depth=1
    )
    assert len(result.hits) == 2 and not result.truncated
    result = await query.impact(
        shop.store, repository_id=REPO, node_id=shop.place_order["id"], limit=1
    )
    assert len(result.hits) == 1 and result.truncated
    with pytest.raises(NotFound):
        await query.impact(shop.store, repository_id=REPO, node_id="method:missing")
    with pytest.raises(InvalidRequest):
        await query.impact(shop.store, repository_id=REPO, node_id=shop.place_order["id"], depth=7)


async def test_impact_change_kinds(shop: Shop) -> None:
    test = with_build(shop)
    place_order = shop.place_order["id"]
    # body: callers, and callers of what placeOrder overrides, transitively.
    result = await query.impact(shop.store, repository_id=REPO, node_id=place_order, change="body")
    got = hit_ids(result.hits)
    assert result.change == "body" and len(result.roots) == 2 and len(got) == 4
    assert got[shop.checkout["id"]].depth == 1 and got[shop.legacy["id"]].from_ == shop.place["id"]
    assert got[shop.cron["id"]].depth == 2 and got[test["id"]].root == "test"
    # signature: every direct user, no dispatch roots.
    result = await query.impact(
        shop.store, repository_id=REPO, node_id=place_order, change="signature"
    )
    assert len(result.roots) == 1 and len(result.hits) == 2
    # remove: a type use counts too.
    result = await query.impact(
        shop.store, repository_id=REPO, node_id=shop.order_service["id"], change="remove", depth=1
    )
    assert [(h.node.id, h.via) for h in result.hits if h.node] == [(place_order, EDGE_USES_TYPE)]
    # contract on the interface method: its overrider is a root too.
    result = await query.impact(
        shop.store, repository_id=REPO, node_id=shop.place["id"], change="contract"
    )
    got = hit_ids(result.hits)
    assert result.roots == [shop.place["id"], place_order] and len(got) == 4
    assert got[shop.checkout["id"]].from_ == place_order
    assert got[shop.legacy["id"]].from_ == shop.place["id"]
    with pytest.raises(InvalidRequest):
        await query.impact(shop.store, repository_id=REPO, node_id=place_order, change="rename")


async def test_impact_assessment(shop: Shop) -> None:
    test = with_build(shop)
    result = await query.impact(shop.store, repository_id=REPO, node_id=shop.place_order["id"])
    assessment = result.assessment
    assert assessment is not None
    # Three main hits and one test hit, all in the root module.
    assert [(g.module, g.root, g.nodes) for g in assessment.groups] == [
        (".", "main", 3),
        (".", "test", 1),
    ]
    assert assessment.by_depth == {"1": 3, "2": 1}
    # checkout and nightly have no dependants of their own; legacyCheckout does.
    assert assessment.entry_points_complete
    assert {b.id for b in assessment.entry_points} == {shop.checkout["id"], shop.cron["id"]}
    assert [(t.test_class, t.module, t.nodes) for t in assessment.tests] == [
        ("shop.OrderServiceTest", ".", 1)
    ]
    assert assessment.commands == [
        "mvn -Dtest=shop.OrderServiceTest -Dsurefire.failIfNoSpecifiedTests=false test"
    ]
    hit = hit_ids(result.hits)[test["id"]]
    assert (hit.module, hit.root) == (".", "test")
    compact = to_json(query.compact_impact(result))
    assert compact["assessment"]["tests"][0]["class"] == "shop.OrderServiceTest"
    assert compact["nodes"][0]["module"] == "." and compact["by_kind"] == {"method": 4}
    assert "node" not in compact["nodes"][0] and "from" in compact["nodes"][0]


@pytest.mark.parametrize(
    ("path", "source_set", "module", "root", "test_class"),
    [
        (
            "core/src/main/java/com/google/adk/agents/BaseAgent.java",
            "maven-module:abc:main",
            "core",
            "main",
            "",
        ),
        (
            "core/src/test/java/com/google/adk/runner/RunnerTest.java",
            "maven-module:abc:test",
            "core",
            "test",
            "com.google.adk.runner.RunnerTest",
        ),
        (
            "contrib/planners/src/test/java/com/google/adk/planner/LoopPlannerTest.java",
            "",
            "contrib/planners",
            "test",
            "com.google.adk.planner.LoopPlannerTest",
        ),
        (
            "tokt/src/test/kotlin/com/google/adk/tokt/KtRunnerInteropTest.kt",
            "",
            "tokt",
            "test",
            "com.google.adk.tokt.KtRunnerInteropTest",
        ),
        ("src/main/java/App.java", "", ".", "main", ""),
        ("dev/browser/main.js", "", "dev", "main", ""),
    ],
)
def test_locate_and_test_class(
    path: str, source_set: str, module: str, root: str, test_class: str
) -> None:
    properties = {"file_path": {"string": path}, "language": {"string": "java"}}
    if source_set:
        properties["source_set_id"] = {"string": source_set}
    info = locate(properties)
    assert (info.module, info.root) == (module, root)
    if test_class:
        found = class_of_test(info)
        assert found is not None and found.test_class == test_class


def test_test_commands() -> None:
    commands = commands_for_tests(
        [
            ImpactTestClass(module="core", test_class="b.T2", language="java"),
            ImpactTestClass(module="core", test_class="a.T1", language="java"),
            ImpactTestClass(module="web", test_class="x", language="typescript"),
        ]
    )
    assert commands == [
        "mvn -pl core -am -Dtest=a.T1,b.T2 -Dsurefire.failIfNoSpecifiedTests=false test"
    ]


async def test_changes_and_change_impact(shop: Shop) -> None:
    test = with_build(shop)
    store = shop.store
    # Generation 2: refund is added, placeOrder is updated, pay is retired.
    refund = {
        "id": graph_id("method", "refund(shop.Order)"),
        "kind": "method",
        "name": "refund",
        "qualified_name": "refund(shop.Order)",
        "properties": {"file_path": {"string": "src/shop/PaymentService.java"}},
    }
    store.add_version(Version(fact={"node": refund}, gen_from=2, commit_from="c2"))
    old = store.nodes[shop.place_order["id"]]
    store.add_version(dataclasses.replace(old, gen_to=2, commit_to="c2"))
    store.add_version(dataclasses.replace(old, gen_from=2, commit_from="c2"))
    pay = store.nodes.pop(shop.pay["id"])
    store.add_version(dataclasses.replace(pay, gen_to=2, commit_to="c2", retired=True))

    found = await query.changes(store, repository_id=REPO, generation=2)
    ops = {node.brief.id: node.op for node in found.nodes}
    assert (found.generation, found.added, found.updated, found.retired) == (2, 1, 1, 1)
    assert ops == {
        refund["id"]: "added",
        shop.place_order["id"]: "updated",
        shop.pay["id"]: "retired",
    }
    # Generation 1 added every node; the totals count every record, the
    # listing only declarations (nine here, not the two file nodes).
    found = await query.changes(store, repository_id=REPO, generation=1, limit=3)
    assert len(found.nodes) == 9 and found.added == 11
    found = await query.changes(store, repository_id=REPO, generation=1, kinds=["source_file"])
    assert len(found.nodes) == 2

    result = await query.change_impact(store, repository_id=REPO, generation=2)
    # Roots: refund and placeOrder at generation 2, pay at generation 1.
    # placeOrder's callers are impacted; pay's only dependant is placeOrder,
    # itself a root, so it adds nothing.
    got = hit_ids(result.impact.hits)
    assert result.generation == 2 and len(result.changed) == 3 and not result.roots_truncated
    assert len(result.impact.roots) == 3 and len(got) == 2
    assert got[shop.checkout["id"]].depth == 1 and got[test["id"]].root == "test"
    assert result.impact.assessment is not None and len(result.impact.assessment.commands) == 1
    result = await query.change_impact(store, repository_id=REPO, generation=2, max_roots=1)
    assert result.roots_truncated and len(result.changed) == 1
    with pytest.raises(InvalidRequest):
        await query.change_impact(store, repository_id=REPO, generation=2, change="x")
    compact = query.compact_change_impact(result)
    assert compact.generation == 2 and len(compact.impact.nodes) == len(result.impact.hits)


async def test_node_source(shop: Shop) -> None:
    place_order = shop.place_order["id"]
    result = await query.node_source(shop.store, repository_id=REPO, node_id=place_order)
    assert result.text.startswith("public Order placeOrder(Cart cart) {")
    assert result.text.endswith("    }") and not result.truncated
    assert (result.text_start_line, result.text_end_line) == (4, 7)

    result = await query.node_source(
        shop.store, repository_id=REPO, node_id=place_order, context_lines=1
    )
    assert result.text.startswith(
        "public class OrderService implements OrderPort {\n    public Order placeOrder"
    )
    assert result.text.endswith("    }\n}\n")
    assert (result.text_start_line, result.text_end_line) == (3, 8)
    assert result.text_byte_start == shop.source.index("public class")

    result = await query.node_source(
        shop.store, repository_id=REPO, node_id=place_order, context_lines=50
    )
    assert result.text == shop.source
    assert (result.text_start_line, result.text_end_line) == (1, 8)
    with pytest.raises(NotFound, match="no source anchor"):
        await query.node_source(shop.store, repository_id=REPO, node_id=shop.reserve["id"])
    with pytest.raises(InvalidRequest):
        await query.node_source(
            shop.store, repository_id=REPO, node_id=place_order, context_lines=201
        )

    # A huge span is cut; a span past the retained bytes is an integrity failure.
    big = ("x" * (64 * 1024 + 10) + "\n").encode()
    sha = hashlib.sha256(big).hexdigest()
    shop.store.sources[sha] = big

    def anchored(node_id: str, end: int) -> None:
        shop.store.add_node(
            {
                "id": node_id,
                "kind": "method",
                "name": node_id,
                "source": {
                    "content_sha256": sha,
                    "span": {"start": {"line": 1}, "end": {"byte_offset": end, "line": 1}},
                },
            }
        )

    anchored("method:huge", len(big))
    result = await query.node_source(shop.store, repository_id=REPO, node_id="method:huge")
    assert result.truncated and len(result.text) == 64 * 1024
    anchored("method:broken", len(big) + 1)
    with pytest.raises(IntegrityFailure):
        await query.node_source(shop.store, repository_id=REPO, node_id="method:broken")


async def test_path(shop: Shop) -> None:
    # cron -> legacy -> place: a dependency path.
    result = await query.path(
        shop.store, repository_id=REPO, from_id=shop.cron["id"], to_id=shop.place["id"]
    )
    assert result.found and result.hops == 2
    assert [n.id for n in result.nodes] == [shop.cron["id"], shop.legacy["id"], shop.place["id"]]
    assert [e.kind for e in result.edges] == [EDGE_CALLS, EDGE_CALLS]
    # Nothing depends from place back to cron, unless direction is ignored.
    result = await query.path(
        shop.store, repository_id=REPO, from_id=shop.place["id"], to_id=shop.cron["id"]
    )
    assert not result.found
    result = await query.path(
        shop.store,
        repository_id=REPO,
        from_id=shop.place["id"],
        to_id=shop.cron["id"],
        direction=BOTH,
    )
    assert result.found and [n.id for n in result.nodes] == [
        shop.place["id"],
        shop.legacy["id"],
        shop.cron["id"],
    ]
    same = await query.path(
        shop.store, repository_id=REPO, from_id=shop.pay["id"], to_id=shop.pay["id"]
    )
    assert same.found and same.hops == 0 and len(same.nodes) == 1
    with pytest.raises(InvalidRequest):
        await query.path(
            shop.store,
            repository_id=REPO,
            from_id=shop.cron["id"],
            to_id=shop.place["id"],
            direction="in",
        )


async def test_hubs(shop: Shop) -> None:
    result = await query.hubs(shop.store, repository_id=REPO, limit=2)
    # place is overridden by placeOrder and called by the legacy checkout;
    # everything else has one dependant.
    assert len(result.hubs) == 2
    assert (result.hubs[0].brief.id, result.hubs[0].in_degree) == (shop.place["id"], 2)
    assert result.hubs[0].by_kind == {"calls": 1, "overrides": 1}
    only_classes = await query.hubs(shop.store, repository_id=REPO, node_kinds=["class"])
    assert [h.brief.id for h in only_classes.hubs] == [shop.order_service["id"]]


def services() -> tuple[MemStore, dict[str, str], dict[str, dict[str, Any]]]:
    """Three repositories linked the way services are: checkout's
    OrdersClient.create calls the API OrderController.create serves, and
    orders publishes an event (Events.created) billing's OrderListener.on
    consumes."""
    repos = {name: graph_id("repo", name) for name in ("checkout", "orders", "billing")}
    store = MemStore(repos["checkout"])
    orders, billing = store.add(repos["orders"]), store.add(repos["billing"])

    def method(held: MemStore, qualified: str) -> dict[str, Any]:
        return held.add_node(
            {
                "id": graph_id("entity", held.repo, qualified),
                "kind": "method",
                "name": qualified,
                "qualified_name": qualified,
                "properties": {"file_path": {"string": qualified + ".java"}},
            }
        )

    nodes = {
        "place_order": method(store, "shop.CheckoutService.placeOrder()"),
        "client": method(store, "shop.OrdersClient.create(shop.Order)"),
        "handler": method(orders, "orders.OrderController.create(orders.OrderRequest)"),
        "save": method(orders, "orders.OrderService.save(orders.Order)"),
        "created": method(orders, "orders.Events.created(orders.Order)"),
        "listener": method(billing, "billing.OrderListener.on(billing.OrderCreated)"),
    }
    store.add_edge(EDGE_CALLS, nodes["place_order"]["id"], nodes["client"]["id"])
    orders.add_edge(EDGE_CALLS, nodes["handler"]["id"], nodes["save"]["id"])
    orders.add_edge(EDGE_CALLS, nodes["save"]["id"], nodes["created"]["id"])
    store.link(
        "calls",
        "calls_api",
        repos["checkout"],
        nodes["client"]["id"],
        repos["orders"],
        nodes["handler"]["id"],
    )
    store.link(
        "events",
        "sends_event",
        repos["orders"],
        nodes["created"]["id"],
        repos["billing"],
        nodes["listener"]["id"],
    )
    return store, repos, nodes


def everyone(_repository: str) -> bool:
    return True


def hop_targets(hops: list[query.CrossHop]) -> list[str]:
    return [
        f"{hop.direction} {hop.node.node['qualified_name']}"
        if hop.node is not None and hop.node.node is not None
        else f"{hop.direction} stale"
        for hop in hops
    ]


async def test_cross_hops() -> None:
    store, repos, nodes = services()
    client, handler = nodes["client"], nodes["handler"]
    out = await query.cross_hops(store, repos["checkout"], client, OUT, everyone)
    assert hop_targets(out) == [f"out {handler['qualified_name']}"]
    assert out[0].repository_id == repos["orders"]
    into = await query.cross_hops(store, repos["orders"], handler, BOTH, everyone)
    assert hop_targets(into) == [f"in {client['qualified_name']}"]
    assert await query.cross_hops(store, repos["orders"], handler, OUT, everyone) == []
    # Repositories the caller can't read aren't there.
    hidden = await query.cross_hops(
        store, repos["orders"], handler, IN, lambda repo: repo != repos["checkout"]
    )
    assert hidden == []

    # Re-ingested without a baseline, the handler has a new id: the link finds
    # it by its qualified name, both ways.
    orders = store.others[repos["orders"]]
    del orders.nodes[handler["id"]]
    renumbered = orders.add_node({**handler, "id": graph_id("entity", "orders", "handler-2")})
    out = await query.cross_hops(store, repos["checkout"], client, OUT, everyone)
    assert len(out) == 1 and out[0].node is not None and out[0].node.id == renumbered["id"]
    into = await query.cross_hops(store, repos["orders"], renumbered, IN, everyone)
    assert hop_targets(into) == [f"in {client['qualified_name']}"]

    # Gone altogether, the far end is stale and keeps what the link recorded.
    del orders.nodes[renumbered["id"]]
    out = await query.cross_hops(store, repos["checkout"], client, OUT, everyone)
    compact = query.compact_cross_hops(out)
    assert len(out) == 1 and out[0].stale
    assert compact[0].brief.qualified_name == handler["qualified_name"]
    assert (compact[0].via, compact[0].repository_id) == ("calls_api", repos["orders"])


def across_names(result: Any) -> list[str]:
    out = []
    for crossing in result.across:
        out.append(crossing.node.node["qualified_name"])
        out.extend(hit.node.node["qualified_name"] for hit in crossing.hits)
    return out


async def test_impact_across_repositories() -> None:
    store, repos, nodes = services()
    listener = nodes["listener"]["id"]

    async def walk(**options: Any) -> Any:
        return await query.impact(
            store, repository_id=repos["billing"], node_id=listener, change="body", **options
        )

    # Changing billing's listener reaches orders through the event it
    # consumes, then checkout through the API the publisher's callers serve.
    result = await walk(depth=6, across=everyone)
    want = [
        nodes[name]["qualified_name"]
        for name in ("created", "save", "handler", "client", "place_order")
    ]
    assert across_names(result) == want
    into_orders, into_checkout = result.across
    assert into_orders.repository_id == repos["orders"]
    assert into_orders.from_repository_id == repos["billing"] and into_orders.from_ == listener
    assert into_orders.depth == 1 and into_orders.link.id == "events"
    assert [hit.depth for hit in into_orders.hits] == [2, 3]
    assert into_checkout.repository_id == repos["checkout"]
    assert into_checkout.from_ == nodes["handler"]["id"]
    assert into_checkout.depth == 4 and into_checkout.hits[0].depth == 5
    compact = query.compact_impact(result)
    assert len(compact.across) == 2
    assert compact.across[1].node.qualified_name == nodes["client"]["qualified_name"]
    assert compact.across[1].via == "calls_api" and len(compact.across[1].nodes) == 1

    # The depth bounds the walk across as within.
    assert across_names(await walk(depth=3, across=everyone)) == want[:3]
    # Only readable repositories, and only when asked.
    unreadable = await walk(depth=6, across=lambda repo: repo != repos["checkout"])
    assert across_names(unreadable) == want[:3]
    assert (await walk(depth=6)).across == []
    # The limit counts what's reached across.
    limited = await walk(depth=6, limit=2, across=everyone)
    assert len(across_names(limited)) == 2 and limited.truncated
