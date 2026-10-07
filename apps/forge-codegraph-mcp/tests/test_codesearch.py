"""The query parser and tokenizers must agree with the worker's Go versions
(packages/go/code-graph/domain/codesearch), whose test cases these are."""

import pytest

from forge_codegraph_mcp.graph import codesearch
from forge_codegraph_mcp.graph.model import graph_id


@pytest.mark.parametrize(
    ("text", "sentence", "terms", "symbols", "paths"),
    [
        # An identifier keeps its single term and is itself the symbol.
        ("runAsyncImpl", False, ["runasyncimpl"], ["runAsyncImpl"], []),
        # A partial identifier is a plain term; the substring index handles it.
        ("AsyncImp", False, ["asyncimp"], ["AsyncImp"], []),
        ("run", False, ["run"], [], []),
        # Two words are not a sentence: every term stays, nothing is a symbol.
        ("run ctx", False, ["run", "ctx"], [], []),
        # A question drops its function and code words, keeps the symbols it names.
        (
            "who calls runAsyncImpl in LlmAgent?",
            True,
            ["runasyncimpl", "llmagent"],
            ["runAsyncImpl", "LlmAgent"],
            [],
        ),
        # Quotes, backticks and call parentheses are stripped from symbols.
        (
            "Where is `OrderService.placeOrder()` defined",
            True,
            ["orderservice", "placeorder"],
            ["OrderService.placeOrder()"],
            [],
        ),
        # Sentence openers are not symbols; a capitalised type name is.
        (
            "Explain how Order validation works",
            True,
            ["order", "validation", "works"],
            ["Order"],
            [],
        ),
        # Paths are recognised by separator or extension and never become symbols.
        (
            "what does core/src/main/java/flows/LlmFlow.java export",
            True,
            ["core", "src", "main", "java", "flows", "llmflow", "export"],
            [],
            ["core/src/main/java/flows/LlmFlow.java"],
        ),
        (
            "retry policy in LlmFlow.java",
            True,
            ["retry", "policy", "llmflow", "java"],
            [],
            ["LlmFlow.java"],
        ),
        # A URL is not a path and a version number is not a symbol.
        (
            "see https://example.com/docs for version 2.0",
            True,
            ["see", "https", "example", "com", "docs", "version"],
            [],
            [],
        ),
        # When every word is a stop word the original terms survive.
        ("what is the class", True, ["what", "is", "the", "class"], [], []),
        ("   ", False, [], [], []),
    ],
)
def test_parse_query(
    text: str, sentence: bool, terms: list[str], symbols: list[str], paths: list[str]
) -> None:
    query = codesearch.parse_query(text)
    assert (query.sentence, query.terms, query.symbols, query.paths) == (
        sentence,
        terms,
        symbols,
        paths,
    )


def test_parse_query_bounds() -> None:
    text = "".join(
        f" symbolNumber{chr(ord('A') + i)} dir{chr(ord('a') + i)}/file.java" for i in range(20)
    )
    query = codesearch.parse_query(text)
    # "file" is a stop word; the cap applies after filtering.
    assert len(query.symbols) == codesearch.MAX_QUERY_SYMBOLS
    assert len(query.paths) == codesearch.MAX_QUERY_PATHS
    assert len(query.terms) == codesearch.MAX_TERMS


@pytest.mark.parametrize(
    ("symbol", "name", "owner"),
    [
        ("runAsyncImpl", "runAsyncImpl", ""),
        ("com.acme.Foo", "Foo", ""),
        ("OrderService.placeOrder()", "placeOrder", "OrderService"),
        ("bar(com.acme.Foo)", "bar", ""),
        ("a.b", "b", ""),
        ("Outer.Inner.run", "run", "Inner"),
    ],
)
def test_simple_name_and_owner(symbol: str, name: str, owner: str) -> None:
    assert codesearch.simple_name(symbol) == name
    assert codesearch.owner(symbol) == owner


@pytest.mark.parametrize(
    ("identifier", "fragments"),
    [
        ("runAsyncImpl", ["run", "Async", "Impl"]),
        ("HTTPServer2", ["HTTP", "Server", "2"]),
        ("max_heap_mib", ["max", "heap", "mib"]),
        ("LlmAgent", ["Llm", "Agent"]),
        ("x", ["x"]),
        ("ALLCAPS", ["ALLCAPS"]),
        ("getX509Cert", ["get", "X", "509", "Cert"]),
        ("", []),
    ],
)
def test_split_identifier(identifier: str, fragments: list[str]) -> None:
    assert codesearch.split_identifier(identifier) == fragments


def test_identifier_terms_and_terms() -> None:
    terms = codesearch.identifier_terms(
        "runAsyncImpl", "runAsyncImpl(com.google.adk.agents.InvocationContext)"
    )
    for want in ["runAsyncImpl", "run", "Async", "Impl", "com", "google", "adk", "agents"]:
        assert f" {want} " in f" {terms} "
    assert " InvocationContext " in f" {terms} " and " Invocation " in f" {terms} "
    assert f" {terms} ".count(" run ") == 1
    assert codesearch.terms("LlmAgent.runAsync(ctx) OR flow") == [
        "llmagent",
        "runasync",
        "ctx",
        "or",
        "flow",
    ]


def test_snippet() -> None:
    text = (
        "line one\nprotected Flowable<Event> runAsyncImpl(InvocationContext ctx) {\n"
        "    return llmFlow.run(ctx);\n}\n"
    )
    found = codesearch.snippet(text, ["llmflow"], 40)
    assert "llmFlow.run" in found and len(found.encode()) <= 40
    assert codesearch.snippet(text, ["absent"], 20).startswith("line one")
    assert codesearch.snippet("", ["x"], 10) == ""
    assert codesearch.snippet("abc", [], 0) == ""


def test_document_is_the_workers() -> None:
    node = {
        "id": "method:x",
        "kind": "method",
        "name": "placeOrder",
        "qualified_name": "placeOrder(shop.Cart)",
        "properties": {
            "file_path": {"string": "src/shop/OrderService.java"},
            "signature": {"string": "public Order placeOrder(Cart cart)"},
            "line_count": {"int64": 4},
        },
    }
    document = codesearch.document_of(node)
    assert document is not None
    assert document.text == (
        "method placeOrder\nQualified name: placeOrder(shop.Cart)"
        "\nFile:\nsrc/shop/OrderService.java\nSignature:\npublic Order placeOrder(Cart cart)"
    )
    assert codesearch.document_of({**node, "kind": "parameter"}) is None


def test_graph_id_is_the_workers() -> None:
    # Computed with the worker's graph.ID.
    assert graph_id("method", "shop.OrderService.placeOrder(shop.Cart)") == (
        "method:rH06sSN2RIDlqYZOV8dNnA"
    )
    assert graph_id("file", "repo:x", "m", "main", "src/a<b>.java") == (
        "file:iPNxGZWfUwhTQzeE285faQ"
    )
