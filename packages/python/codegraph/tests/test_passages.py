from typing import Any

from forge_codegraph_query import MAX_SEARCH_REPOSITORIES, CodeGraph
from forge_codegraph_query.testing import SHOP, build_shop

from forge_codegraph import (
    Repository,
    Searched,
    citation_ref,
    code_passages,
    interleave,
    search_passages,
)

REPOS = [
    Repository(id="r1", graph_id="repo:a", name="pallets/itsdangerous"),
    Repository(id="r2", graph_id="repo:b", name="pallets/markupsafe"),
]


def test_citation_ref_is_the_documents_algorithm() -> None:
    # Pinned against forge_task_documents.retrieval.knowledge.citation_ref,
    # which chat UIs and the documents' passages share.
    from forge_task_documents.retrieval.knowledge import citation_ref as documents_ref

    for key in ("repo:a:entity:1", "chunk-42", "é"):
        assert citation_ref(key) == documents_ref(key)
    assert len(citation_ref("x")) == 7


def test_hits_become_cited_passages() -> None:
    hits: list[dict[str, Any]] = [
        {
            "repository_id": "repo:a",
            "id": "entity:1",
            "kind": "class",
            "qualified_name": "itsdangerous.signer.Signer",
            "file": "src/itsdangerous/signer.py",
            "line": 80,
            "score": 0.9,
            "snippet": "class Signer:",
            "text": "class Signer:\n    ...\n",
            "text_start_line": 80,
            "text_end_line": 81,
        },
        {
            "repository_id": "repo:b",
            "id": "entity:2",
            "kind": "function",
            "name": "escape",
            "file": "src/markupsafe/__init__.py",
            "line": 12,
            "snippet": "def escape(s):",
        },
        # Not one of the knowledge base's repositories.
        {"repository_id": "repo:z", "id": "entity:3"},
    ]
    passages = code_passages(
        hits, knowledge_base_id="kb1", knowledge_base="Code", repositories=REPOS
    )
    assert [p["node_id"] for p in passages] == ["entity:1", "entity:2"]
    first, second = passages
    assert first == {
        "ref": citation_ref("repo:a:entity:1"),
        "knowledge_base": "Code",
        "knowledge_base_id": "kb1",
        "document": "pallets/itsdangerous: src/itsdangerous/signer.py",
        "document_id": "r1:src/itsdangerous/signer.py",
        "section": "class itsdangerous.signer.Signer",
        "location": "src/itsdangerous/signer.py, lines 80–81",
        "text": "class Signer:\n    ...\n",
        "score": 0.9,
        "repository_id": "r1",
        "node_id": "entity:1",
    }
    # Without source, the snippet; without a span, the line.
    assert second["text"] == "def escape(s):"
    assert second["location"] == "src/markupsafe/__init__.py, line 12"
    assert second["section"] == "function escape"
    assert second["score"] == 0.0


def test_interleave_merges_by_rank_without_repeats() -> None:
    documents = [{"ref": "D1"}, {"ref": "D2"}, {"ref": "D3"}]
    code = [{"ref": "C1"}, {"ref": "D2"}]
    assert [p["ref"] for p in interleave([documents, code], 10)] == [
        "D1",
        "C1",
        "D2",
        "D3",
    ]
    assert [p["ref"] for p in interleave([documents, code], 3)] == ["D1", "C1", "D2"]
    assert interleave([], 5) == []


async def test_knowledge_bases_code_is_searched_as_cited_passages() -> None:
    shop = build_shop()
    other = shop.store.add("repo:other")
    graph = CodeGraph(shop.store)
    orders = Repository(id="r1", graph_id=SHOP, name="acme/shop")
    first = Searched(id="kb1", name="Checkout", repositories=[orders])
    # The same repository in a second knowledge base is cited from the first.
    second = Searched(
        id="kb2",
        name="Everything",
        repositories=[
            orders,
            Repository(id="r2", graph_id=other.repo, name="acme/other"),
        ],
    )

    passages = await search_passages(graph, [first, second], "placeOrder", limit=5)

    assert [(p["knowledge_base"], p["node_id"]) for p in passages] == [
        ("Checkout", shop.place_order["id"]),
        ("Checkout", shop.order_service["id"]),
    ]
    assert passages[0]["text"].startswith("public Order placeOrder(Cart cart)")
    assert passages[0]["location"] == "src/shop/OrderService.java, lines 4–7"
    # One search over both graphs, the source attached.
    [asked] = shop.store.searches
    assert asked.repository_ids == [SHOP, "repo:other"] and asked.limit == 5
    # Nothing to search asks nothing.
    assert (
        await search_passages(graph, [Searched("kb3", "Empty", [])], "x", limit=5) == []
    )
    assert len(shop.store.searches) == 1


async def test_a_search_covers_at_most_the_code_graphs_repositories() -> None:
    shop = build_shop()
    many = [
        Repository(id=f"r{i}", graph_id=f"repo:{i}", name=f"o/{i}") for i in range(40)
    ]

    await search_passages(
        CodeGraph(shop.store), [Searched("kb", "Many", many)], "placeOrder", limit=5
    )

    assert len(shop.store.searches[0].repository_ids) == MAX_SEARCH_REPOSITORIES
