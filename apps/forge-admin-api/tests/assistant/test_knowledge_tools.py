"""The assistant's knowledge base tools, against a stand-in for the admin API.

As for the administration tools: what each tool sends (as whom, where, with
what), that each body is one its route's own request model takes, and what
the model gets back. None uploads or removes anything.
"""

import asyncio
from typing import Any

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, SecretStr
from test_admin_tools import Api, Handler, answer, context

from forge_admin.api.routes.knowledge_bases import (
    OrganizationSearchRequest,
    ReindexRequest,
)
from forge_admin.assistant import access_tools, admin_tools, knowledge_tools
from forge_admin.assistant.knowledge_tools import KnowledgeToolset
from forge_admin.config import Settings

ORG = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e01"
CLAIMS = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e02"
MEMBERS = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e03"
DOC = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e04"
PREFIX = "/api/v1"
BASES = f"/organizations/{ORG}/knowledge-bases"

# A search as the organization's search route answers it.
FOUND = {
    "searched": [
        {"id": MEMBERS, "name": "Member knowledge"},
        {"id": CLAIMS, "name": "Claims knowledge"},
    ],
    "hits": [
        {
            "chunk_id": "c1",
            "ref": "KQM4821",
            "knowledge_base_id": CLAIMS,
            "knowledge_base": "Claims knowledge",
            "document_id": DOC,
            "filename": "Appeals guide.pdf",
            "section_path": ["Appeals", "Denied claims"],
            "location": "page 4",
            "text": "To appeal a denied claim, file within 180 days.",
            "score": 0.61234,
        }
    ],
}
DOCUMENT = {
    "id": DOC,
    "knowledge_base_id": CLAIMS,
    "filename": "Appeals guide.pdf",
    "phase": "FAILED",
    "error": "no text in pdf",
    "chunk_count": 0,
    "embedding_model": "",
    "storage_uri": "s3://forge/org/kb/documents/x/appeals.pdf",
}


@pytest.fixture
def signing(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 32), "api_key": SecretStr("k" * 32)}
    )


def routes(request: httpx.Request) -> httpx.Response:
    """Lists answer lists, the search a search, anything else a document."""
    path = request.url.path
    if path.endswith("/search"):
        return httpx.Response(200, json=FOUND)
    if path.endswith("/reindex"):
        return httpx.Response(202, json={"submitted": 2})
    if request.method == "GET" and path.endswith(("knowledge-bases", "documents")):
        return httpx.Response(200, json=[])
    return httpx.Response(200, json=DOCUMENT)


def tools(signing: Settings, handler: Handler = routes) -> tuple[KnowledgeToolset, Api]:
    api = Api(signing, handler)
    return KnowledgeToolset(api.person_api, confirm_changes=False), api


def call(toolset: KnowledgeToolset, tool_name: str, /, **args: Any) -> Any:
    async def main() -> Any:
        found = {tool.name: tool for tool in await toolset.get_tools()}
        return await found[tool_name].run_async(args=args, tool_context=context("ada"))

    return asyncio.run(main())


# Every tool: its arguments, then the method, path (below the prefix), query
# and body it sends, and the route's request model that body must fit.
CASES: list[
    tuple[str, dict[str, Any], str, str, dict[str, Any], Any, type[BaseModel] | None]
] = [
    ("list_knowledge_bases", {"organization_id": ORG}, "GET", BASES, {}, None, None),
    (
        "get_knowledge_base",
        {"organization_id": ORG, "knowledge_base_id": CLAIMS},
        "GET",
        f"{BASES}/{CLAIMS}",
        {},
        None,
        None,
    ),
    (
        "list_knowledge_documents",
        {
            "organization_id": ORG,
            "knowledge_base_id": CLAIMS,
            "search": " appeals ",
            "phase": "failed",
        },
        "GET",
        f"{BASES}/{CLAIMS}/documents",
        {"limit": "25", "q": "appeals", "phase": "FAILED"},
        None,
        None,
    ),
    (
        "get_knowledge_document",
        {"organization_id": ORG, "knowledge_base_id": CLAIMS, "document_id": DOC},
        "GET",
        f"{BASES}/{CLAIMS}/documents/{DOC}",
        {},
        None,
        None,
    ),
    (
        "search_knowledge_bases",
        {
            "organization_id": ORG,
            "query": " appeal a denied claim ",
            "knowledge_base_ids": [MEMBERS, CLAIMS],
        },
        "POST",
        f"{BASES}/search",
        {},
        {
            "query": "appeal a denied claim",
            "knowledge_base_ids": [MEMBERS, CLAIMS],
            "limit": 8,
        },
        OrganizationSearchRequest,
    ),
    (
        "retry_knowledge_document",
        {"organization_id": ORG, "knowledge_base_id": CLAIMS, "document_id": DOC},
        "POST",
        f"{BASES}/{CLAIMS}/documents/{DOC}/retry",
        {},
        None,
        None,
    ),
    (
        "reindex_knowledge_base",
        {"organization_id": ORG, "knowledge_base_id": CLAIMS},
        "POST",
        f"{BASES}/{CLAIMS}/reindex",
        {},
        {"stale_only": True},
        ReindexRequest,
    ),
]


def test_every_tool_is_covered_read_or_change_and_none_removes() -> None:
    assert {case[0] for case in CASES} == knowledge_tools.TOOL_NAMES
    assert KnowledgeToolset.tool_names() == knowledge_tools.TOOL_NAMES
    reads, changes = set(knowledge_tools.READ_TOOLS), set(knowledge_tools.CHANGE_TOOLS)
    assert not reads & changes
    assert not knowledge_tools.TOOL_NAMES & (
        admin_tools.TOOL_NAMES | access_tools.TOOL_NAMES
    )
    assert not any(name.startswith(("delete", "remove", "upload")) for name in changes)


@pytest.mark.parametrize(
    ("name", "args", "method", "path", "query", "body", "model"),
    CASES,
    ids=[case[0] for case in CASES],
)
def test_each_tool_sends_what_its_route_takes(
    signing: Settings,
    name: str,
    args: dict[str, Any],
    method: str,
    path: str,
    query: dict[str, Any],
    body: Any,
    model: type[BaseModel] | None,
) -> None:
    toolset, api = tools(signing)
    got = call(toolset, name, **args)

    assert got["status"] == "success", got
    sent = api.calls[-1]
    assert (sent.method, sent.url.path, dict(sent.url.params)) == (
        method,
        f"{PREFIX}{path}",
        query,
    )
    assert api.body() == body
    if model is not None:
        model.model_validate(body)
        assert set(body) <= set(model.model_fields)


def test_changes_wait_for_confirmation_and_reads_dont(signing: Settings) -> None:
    toolset = KnowledgeToolset(Api(signing, routes).person_api)

    async def main() -> dict[str, bool]:
        return {
            tool.name: await tool.check_require_confirmation({}, context())
            for tool in await toolset.get_tools()
        }

    asks = asyncio.run(main())
    assert {name for name, asked in asks.items() if asked} == set(
        knowledge_tools.CHANGE_TOOLS
    )


def test_a_search_answers_as_a_knowledge_base_tool_with_refs_to_cite(
    signing: Settings,
) -> None:
    toolset, _ = tools(signing)
    got = call(toolset, "search_knowledge_bases", organization_id=ORG, query="appeals")

    assert got["status"] == "success"
    # The shape chat UIs read citations from (@forge-ui knowledgeBaseSources).
    assert got["payload"] == {
        "organization_id": ORG,
        "searched": ["Member knowledge", "Claims knowledge"],
        "passages": [
            {
                "ref": "KQM4821",
                "knowledge_base": "Claims knowledge",
                "knowledge_base_id": CLAIMS,
                "document": "Appeals guide.pdf",
                "document_id": DOC,
                "section": "Appeals > Denied claims",
                "location": "page 4",
                "text": "To appeal a denied claim, file within 180 days.",
                "score": 0.6123,
            }
        ],
    }


def test_a_search_finding_nothing_says_not_to_answer_anyway(signing: Settings) -> None:
    toolset, api = tools(
        signing, answer({"searched": [{"id": CLAIMS, "name": "Claims"}], "hits": []})
    )
    got = call(
        toolset, "search_knowledge_bases", organization_id=ORG, query="weather in Paris"
    )

    assert got["payload"]["passages"] == []
    assert "don't answer" in got["payload"]["note"]
    # Every one of the organization's, when none is named.
    assert "knowledge_base_ids" not in api.body()


def test_documents_only_show_what_the_model_needs(signing: Settings) -> None:
    toolset, _ = tools(signing)
    got = call(
        toolset,
        "get_knowledge_document",
        organization_id=ORG,
        knowledge_base_id=CLAIMS,
        document_id=DOC,
    )

    assert (
        got["payload"]["phase"] == "FAILED"
        and got["payload"]["error"] == "no text in pdf"
    )
    assert "storage_uri" not in got["payload"]


@pytest.mark.parametrize(
    ("name", "args", "argument"),
    [
        (
            "list_knowledge_bases",
            {"organization_id": f"{ORG}/../users"},
            "organization_id",
        ),
        (
            "get_knowledge_base",
            {"organization_id": ORG, "knowledge_base_id": "search"},
            "knowledge_base_id",
        ),
        (
            "get_knowledge_document",
            {
                "organization_id": ORG,
                "knowledge_base_id": CLAIMS,
                "document_id": "../..",
            },
            "document_id",
        ),
        (
            "search_knowledge_bases",
            {"organization_id": ORG, "query": "x", "knowledge_base_ids": ["all"]},
            "knowledge_base_ids",
        ),
        ("search_knowledge_bases", {"organization_id": ORG, "query": "  "}, "query"),
        (
            "list_knowledge_documents",
            {"organization_id": ORG, "knowledge_base_id": CLAIMS, "phase": "DONE"},
            "phase",
        ),
        (
            "reindex_knowledge_base",
            {"organization_id": ORG, "knowledge_base_id": f"{CLAIMS}\n"},
            "knowledge_base_id",
        ),
    ],
)
def test_no_argument_reaches_another_route(
    signing: Settings, name: str, args: dict[str, Any], argument: str
) -> None:
    toolset, api = tools(signing)
    got = call(toolset, name, **args)

    assert got["status"] == "failed"
    assert argument in got["reason"]
    assert api.calls == []


def test_refusals_say_how_to_go_on(signing: Settings) -> None:
    toolset, _ = tools(
        signing,
        answer(
            {"detail": "Only a document whose ingestion failed can be retried"}, 409
        ),
    )
    got = call(
        toolset,
        "retry_knowledge_document",
        organization_id=ORG,
        knowledge_base_id=CLAIMS,
        document_id=DOC,
    )

    assert got["status"] == "failed"
    assert "get_knowledge_document" in got["suggested_fixes"][0]


def test_without_a_jwt_secret_there_are_no_knowledge_tools(settings: Settings) -> None:
    app = FastAPI()
    app.state.settings = settings.model_copy(update={"jwt_secret": None})
    assert knowledge_tools.toolset(app) is None
    app.state.settings = settings.model_copy(update={"jwt_secret": SecretStr("s" * 32)})
    assert isinstance(knowledge_tools.toolset(app), KnowledgeToolset)
