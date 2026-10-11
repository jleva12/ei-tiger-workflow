"""The assistant's code tools, against a stand-in for the admin API and the
shop's graph in memory: the code graph MCP server's tools (forge_codegraph's
``CodeTools``), each answering as they do, over what the person reads in the
organization the call names."""

import asyncio
import inspect
from typing import Any

import httpx2 as httpx
import pytest
from forge_codegraph import TOOLS, CodeTools, RepositoryScope
from forge_codegraph_query import CodeGraph
from forge_codegraph_query.testing import SHOP, SHOP_URL, FakeEmbedder, Shop, build_shop
from pydantic import SecretStr
from test_admin_tools import Api, context

from forge_admin.assistant import code_tools
from forge_admin.assistant.code_tools import CodeToolset
from forge_admin.config import Settings

ORG = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e01"
OTHER_ORG = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e09"
ACCESS = f"/api/v1/organizations/{ORG}/code-graph/access"
# What the admin API answers for a person who reads the shop's repository.
ANSWER = {
    "subject": "ada",
    "organization_id": ORG,
    "repositories": [
        {"url": SHOP_URL, "owner": "acme", "name": "shop", "branch": "main"}
    ],
    "connections": [],
    "link_owners": [],
    "systems": [],
}


@pytest.fixture
def signing(settings: Settings) -> Settings:
    return settings.model_copy(update={"jwt_secret": SecretStr("s" * 32)})


@pytest.fixture
def shop() -> Shop:
    return build_shop()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def access(request: httpx.Request) -> httpx.Response:
    if request.url.path == ACCESS:
        return httpx.Response(200, json=ANSWER)
    return httpx.Response(403, json={"detail": "Missing permission repositories:read"})


def toolset(
    signing: Settings, shop: Shop, handler: Any = access, clock: Clock | None = None
) -> tuple[CodeToolset, Api, CodeGraph]:
    api = Api(signing, handler)
    graph = CodeGraph(shop.store, FakeEmbedder())
    return CodeToolset(api.person_api, graph, clock=clock or Clock()), api, graph


def run(
    tools: CodeToolset, tool: str, /, as_user: str | None = "ada", **args: Any
) -> Any:
    """Run a tool as ``as_user``; positional, so a tool may take ``name``."""

    async def main() -> Any:
        found = {each.name: each for each in await tools.get_tools()}
        return await found[tool].run_async(args=args, tool_context=context(as_user))

    return asyncio.run(main())


def direct(graph: CodeGraph, tool: str, /, **args: Any) -> Any:
    """What the MCP server's tool answers for the same caller."""
    scope = RepositoryScope.from_access(ANSWER)
    return asyncio.run(getattr(CodeTools(graph, lambda: scope), tool)(**args))


def test_the_tools_are_the_mcp_servers(signing: Settings, shop: Shop) -> None:
    tools, _, _ = toolset(signing, shop)
    declared = {
        tool.name: tool._get_declaration()  # type: ignore[attr-defined]
        for tool in asyncio.run(tools.get_tools())
    }
    assert list(declared) == list(TOOLS)
    assert frozenset(TOOLS) == code_tools.TOOL_NAMES
    shared = CodeTools(
        CodeGraph(shop.store), lambda: RepositoryScope.from_access(ANSWER)
    )
    for name, _, method in shared.tools():
        declaration = declared[name]
        schema = declaration.parameters_json_schema
        with_org = {"organization_id", *inspect.signature(method).parameters}
        # The same arguments, but path's from, which Python can't name.
        assert set(schema["properties"]) == with_org, name
        assert "organization_id" in schema["required"]
        assert declaration.description == method.__doc__
        for argument, spec in schema["properties"].items():
            if argument != "organization_id":
                assert spec.get("description"), (name, argument)


def test_each_tool_answers_as_the_mcp_servers_does(
    signing: Settings, shop: Shop
) -> None:
    tools, _, graph = toolset(signing, shop)
    place = shop.place_order["id"]
    calls: list[tuple[str, dict[str, Any]]] = [
        ("list_repositories", {}),
        (
            "explore_code",
            {"question": "where are orders placed", "repositories": [SHOP]},
        ),
        ("search_code", {"query": "placeOrder", "repositories": [SHOP]}),
        ("find_symbol", {"repository": SHOP, "name": "placeOrder"}),
        ("get_node", {"repository": SHOP, "id": place}),
        ("callers", {"repository": SHOP, "id": place}),
        ("callees", {"repository": SHOP, "id": place}),
        ("impact", {"repository": SHOP, "id": place}),
        (
            "path",
            {"repository": SHOP, "from_id": shop.checkout["id"], "to": shop.pay["id"]},
        ),
        ("read_source", {"repository": SHOP, "id": place}),
        ("repository_state", {"repository": SHOP}),
        ("system_design", {}),
    ]
    for name, args in calls:
        answer = run(tools, name, organization_id=ORG, **args)
        assert answer["status"] == "success", (name, answer)
        assert answer["payload"] == direct(graph, name, **args), name


def test_what_the_person_reads_is_asked_as_them_and_kept_a_while(
    signing: Settings, shop: Shop
) -> None:
    clock = Clock()
    tools, api, _ = toolset(signing, shop, clock=clock)
    run(tools, "list_repositories", organization_id=ORG)
    run(tools, "repository_state", organization_id=ORG.upper(), repository=SHOP)
    [asked] = api.calls
    assert (asked.method, asked.url.path) == ("GET", ACCESS)
    assert asked.headers["Authorization"].startswith("Bearer ")
    clock.now += code_tools.SCOPE_SECONDS + 1
    run(tools, "list_repositories", organization_id=ORG)
    assert len(api.calls) == 2
    # Another person asks for themselves.
    run(tools, "list_repositories", as_user="grace", organization_id=ORG)
    assert len(api.calls) == 3


def test_only_the_organizations_repositories_are_read(
    signing: Settings, shop: Shop
) -> None:
    other = shop.store.add("repo:theirs")
    tools, _, _ = toolset(signing, shop)
    listed = run(tools, "list_repositories", organization_id=ORG)
    assert [r["id"] for r in listed["payload"]["repositories"]] == [SHOP]
    refused = run(
        tools, "search_code", organization_id=ORG, query="x", repositories=[other.repo]
    )
    assert refused["status"] == "failed"
    assert refused["reason"] == "not found: repository repo:theirs"


def test_a_person_who_cant_read_the_organizations_code_is_told(
    signing: Settings, shop: Shop
) -> None:
    tools, _, _ = toolset(signing, shop)
    refused = run(tools, "list_repositories", organization_id=OTHER_ORG)
    assert refused["status"] == "failed"
    assert "repositories:read" in refused["reason"]
    assert any("repositories:read" in fix for fix in refused["suggested_fixes"])
    for organization in ("not-an-id", "../users"):
        bad = run(tools, "list_repositories", organization_id=organization)
        assert bad["status"] == "failed" and "UUID" in bad["reason"]
    nobody = run(tools, "list_repositories", as_user=None, organization_id=ORG)
    assert nobody["status"] == "failed"


def test_the_graphs_failures_are_read_and_the_rest_masked(
    signing: Settings, shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, _, _ = toolset(signing, shop)
    missing = run(
        tools, "get_node", organization_id=ORG, repository=SHOP, id="method:gone"
    )
    assert (
        missing["status"] == "failed"
        and missing["reason"] == "not found: node method:gone"
    )

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("spanner said something internal")

    monkeypatch.setattr(shop.store, "state", broken)
    masked = run(tools, "repository_state", organization_id=ORG, repository=SHOP)
    assert masked["status"] == "failed"
    assert masked["reason"] == "The code graph couldn't answer; try again."


def test_without_the_code_graph_there_are_no_code_tools(signing: Settings) -> None:
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = signing
    app.state.codegraph = None
    assert code_tools.toolset(app) is None
