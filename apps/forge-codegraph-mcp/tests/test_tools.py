"""The tools through an MCP client, over the in-memory store."""

import json
from collections.abc import Iterable
from typing import Any

import pytest
from conftest import REPO, FakeEmbedder, Shop, make_settings
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from memstore import MemStore
from test_query import services

from forge_codegraph_mcp.access import RepositoryScope
from forge_codegraph_mcp.core.mcp import McpServerFactory
from forge_codegraph_mcp.tools import CodeGraphTools

TOOLS = {
    "list_repositories",
    "explore_code",
    "search_code",
    "find_symbol",
    "get_node",
    "neighbors",
    "callers",
    "callees",
    "cross_repository_links",
    "impact",
    "changes",
    "change_impact",
    "path",
    "hubs",
    "history",
    "read_source",
    "read_file",
    "repository_state",
}


def server_over(
    store: MemStore,
    embedder: FakeEmbedder | None = None,
    readable: Iterable[str] | None = None,
) -> FastMCP:
    """The tools over a store, for a caller who reads ``readable``: by
    default every repository in it."""
    settings = make_settings()
    ids = frozenset(readable if readable is not None else (store.repo, *store.others))
    scope = RepositoryScope("apikey:k1", "3f6c0000-0000-4000-8000-000000000001", ids)
    return McpServerFactory(settings).create(
        [CodeGraphTools(settings, store, embedder, scope=lambda: scope)]
    )


async def call(server: FastMCP, tool: str, **arguments: Any) -> dict[str, Any]:
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    [content] = result.content
    answer = json.loads(content.text)  # type: ignore[union-attr]
    assert result.structured_content == answer
    return answer


async def test_tools_are_the_go_servers_and_read_only(shop: Shop) -> None:
    async with Client(server_over(shop.store)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert set(tools) == TOOLS
    for tool in tools.values():
        assert tool.annotations is not None and tool.annotations.read_only_hint
        assert tool.annotations.idempotent_hint and tool.annotations.title
        assert tool.description
    # The Go server's argument names, Python keywords and all.
    path = tools["path"].input_schema
    assert set(path["required"]) == {"repository", "from", "to"}
    assert set(tools["search_code"].input_schema["required"]) == {"query", "repositories"}
    assert tools["get_node"].input_schema["properties"]["generation"]["minimum"] == 0
    assert "Use this first" in (tools["explore_code"].description or "")


async def test_explore_code(shop: Shop, embedder: FakeEmbedder) -> None:
    server = server_over(shop.store, embedder)
    compact = await call(
        server, "explore_code", question="who calls placeOrder?", repositories=[REPO], expand=1
    )
    assert compact["semantic"] is True
    assert compact["seeds"][0]["id"] == shop.place_order["id"]
    assert compact["seeds"][0]["expanded"] is True
    assert {"seed_id", "via", "direction"} <= set(compact["related"][0])
    full = await call(
        server,
        "explore_code",
        question="who calls placeOrder?",
        repositories=[REPO],
        expand=1,
        full=True,
    )
    assert full["seeds"][0]["content_sha256"] == shop.sha
    assert full["query"]["symbols"] == ["placeOrder"]


async def test_search_code(shop: Shop) -> None:
    answer = await call(
        server_over(shop.store), "search_code", query="placeOrder", repositories=[REPO]
    )
    assert [hit["id"] for hit in answer["hits"]] == [
        shop.place_order["id"],
        shop.order_service["id"],
    ]
    assert answer["hits"][0]["exact_match"] is True and answer["semantic"] is False
    assert answer["mode"] == "hybrid" and answer["query"]["text"] == "placeOrder"


async def test_find_get_and_state(shop: Shop) -> None:
    server = server_over(shop.store)
    found = await call(server, "find_symbol", repository=REPO, name="placeOrder")
    assert [v["fact"]["node"]["id"] for v in found["nodes"]] == [shop.place_order["id"]]
    node = await call(server, "get_node", repository=REPO, id=shop.place_order["id"])
    assert node["gen_from"] == 1 and node["fact"]["node"]["kind"] == "method"
    assert "gen_to" not in node
    state = await call(server, "repository_state", repository=REPO)
    assert state == {"repository_id": REPO, "live_generation": 1, "live_commit": "c1"}
    repositories = await call(server, "list_repositories")
    assert [r["id"] for r in repositories["repositories"]] == [REPO]


async def test_callers_callees_and_neighbors(shop: Shop) -> None:
    server = server_over(shop.store)
    callers = await call(server, "callers", repository=REPO, id=shop.place_order["id"])
    assert [(n["id"], n["direction"], n["via"]) for n in callers["neighbors"]] == [
        (shop.checkout["id"], "in", "calls")
    ]
    callees = await call(server, "callees", repository=REPO, id=shop.place_order["id"])
    assert {n["id"] for n in callees["neighbors"]} == {
        shop.reserve["id"],
        shop.pay["id"],
        shop.order_service["id"],
        shop.place["id"],
    }
    lines = {n["id"]: n.get("at_line") for n in callees["neighbors"]}
    assert lines[shop.reserve["id"]] == 5
    contains = await call(
        server, "neighbors", repository=REPO, id=shop.place_order["id"], kinds=["contains"]
    )
    assert [n["id"] for n in contains["neighbors"]] == [shop.order_service["id"]]
    assert "across_repositories" not in contains
    paged = await call(server, "neighbors", repository=REPO, id=shop.place_order["id"], limit=2)
    assert len(paged["neighbors"]) == 2 and paged["next_cursor"] == "2"
    full = await call(server, "callers", repository=REPO, id=shop.place_order["id"], full=True)
    assert full["neighbors"][0]["edge"]["fact"]["edge"]["kind"] == "calls"


async def test_links_across_repositories() -> None:
    store, repos, nodes = services()
    server = server_over(store)
    # The handler in orders is called from checkout's client.
    callers = await call(server, "callers", repository=repos["orders"], id=nodes["handler"]["id"])
    [hop] = callers["across_repositories"]
    assert (hop["id"], hop["repository_id"], hop["via"], hop["direction"]) == (
        nodes["client"]["id"],
        repos["checkout"],
        "calls_api",
        "in",
    )
    links = await call(
        server, "cross_repository_links", repository=repos["checkout"], id=nodes["client"]["id"]
    )
    assert [link["id"] for link in links["links"]] == [nodes["handler"]["id"]]
    # A kind filter without cross-repository kinds leaves them out.
    neighbors = await call(
        server,
        "neighbors",
        repository=repos["orders"],
        id=nodes["handler"]["id"],
        kinds=["calls"],
    )
    assert "across_repositories" not in neighbors
    # impact follows them into the other repositories.
    impact = await call(
        server,
        "impact",
        repository=repos["billing"],
        id=nodes["listener"]["id"],
        change="body",
        depth=6,
    )
    assert [crossing["repository_id"] for crossing in impact["across"]] == [
        repos["orders"],
        repos["checkout"],
    ]


async def test_a_caller_reads_only_its_organizations_repositories() -> None:
    store, repos, nodes = services()
    server = server_over(store, readable=[repos["billing"], repos["orders"]])
    listed = await call(server, "list_repositories")
    assert [repository["id"] for repository in listed["repositories"]] == sorted(
        [repos["billing"], repos["orders"]]
    )
    # Checkout is another organization's: as if it weren't there.
    unread = f"not found: repository {repos['checkout']}"
    with pytest.raises(ToolError, match=unread):
        await call(server, "repository_state", repository=repos["checkout"])
    with pytest.raises(ToolError, match=unread):
        await call(server, "callers", repository=repos["checkout"], id=nodes["client"]["id"])
    with pytest.raises(ToolError, match=unread):
        await call(
            server, "search_code", query="client", repositories=[repos["orders"], repos["checkout"]]
        )
    with pytest.raises(ToolError, match=unread):
        await call(server, "explore_code", question="client", repositories=[repos["checkout"]])
    # Links into it aren't followed.
    callers = await call(server, "callers", repository=repos["orders"], id=nodes["handler"]["id"])
    assert "across_repositories" not in callers
    impact = await call(
        server,
        "impact",
        repository=repos["billing"],
        id=nodes["listener"]["id"],
        change="body",
        depth=6,
    )
    assert [crossing["repository_id"] for crossing in impact["across"]] == [repos["orders"]]
    # A caller whose organization has no repositories reads none.
    nothing = server_over(store, readable=[])
    assert (await call(nothing, "list_repositories"))["repositories"] == []


async def test_impact_and_changes(shop: Shop) -> None:
    server = server_over(shop.store)
    impact = await call(server, "impact", repository=REPO, id=shop.place_order["id"])
    assert impact["root"] == shop.place_order["id"] and impact["change"] == "any"
    assert {node["id"] for node in impact["nodes"]} == {
        shop.checkout["id"],
        shop.legacy["id"],
        shop.cron["id"],
    }
    assert impact["by_kind"] == {"method": 3} and "assessment" in impact
    full = await call(server, "impact", repository=REPO, id=shop.place_order["id"], full=True)
    assert full["hits"][0]["node"]["fact"]["node"]["id"]
    changes = await call(server, "changes", repository=REPO)
    assert changes["generation"] == 1 and changes["added"] == 8 and len(changes["nodes"]) == 8
    review = await call(server, "change_impact", repository=REPO)
    assert review["generation"] == 1 and len(review["changed"]) == 8


async def test_path_hubs_history(shop: Shop) -> None:
    server = server_over(shop.store)
    found = await call(
        server, "path", repository=REPO, **{"from": shop.cron["id"], "to": shop.place["id"]}
    )
    assert found["found"] is True and found["hops"] == 2 and found["from"] == shop.cron["id"]
    hubs = await call(server, "hubs", repository=REPO, limit=1)
    assert hubs["hubs"][0]["id"] == shop.place["id"] and hubs["hubs"][0]["in_degree"] == 2
    assert hubs["edge_kinds"][0] == "calls"
    history = await call(server, "history", repository=REPO, id=shop.pay["id"])
    assert [v["gen_from"] for v in history["versions"]] == [1]


async def test_read_source_and_file(shop: Shop) -> None:
    server = server_over(shop.store)
    source = await call(server, "read_source", repository=REPO, id=shop.place_order["id"])
    assert source["text"].startswith("public Order placeOrder") and source["text_start_line"] == 4
    whole = await call(server, "read_file", repository=REPO, sha256=shop.sha)
    assert whole["text"] == shop.source and whole["size"] == len(shop.source)
    assert whole["truncated"] is False
    part = await call(server, "read_file", repository=REPO, sha256=shop.sha, start=0, end=7)
    assert part["text"] == "package"


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("get_node", {"repository": REPO, "id": "method:missing"}, "not found: node"),
        (
            "find_symbol",
            {"repository": REPO, "name": "a", "qualified_name": "b"},
            "invalid request",
        ),
        ("find_symbol", {"repository": REPO, "name": "a", "limit": 500}, "limit 1-200"),
        ("history", {"repository": REPO, "id": "method:x", "kind": "file"}, "node or edge"),
        ("impact", {"repository": REPO, "id": "method:x", "depth": 9}, "impact depth"),
        (
            "read_file",
            {"repository": REPO, "sha256": "f" * 64},
            "not found: source",
        ),
        ("explore_code", {"question": "x", "repositories": []}, "explore needs repositories"),
        (
            "cross_repository_links",
            {"repository": REPO, "id": "method:x", "direction": "sideways"},
            "direction in, out or both",
        ),
    ],
)
async def test_failures_reach_the_model(
    shop: Shop, tool: str, arguments: dict[str, Any], message: str
) -> None:
    async with Client(server_over(shop.store)) as client:
        with pytest.raises(ToolError, match=message):
            await client.call_tool(tool, arguments)


async def test_unexpected_failures_are_masked(shop: Shop) -> None:
    async def broken(_repo: str) -> Any:
        raise RuntimeError("spanner said something internal")

    shop.store.state = broken  # type: ignore[method-assign,assignment]
    async with Client(server_over(shop.store)) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool("repository_state", {"repository": REPO})
    assert "internal" not in str(raised.value)
