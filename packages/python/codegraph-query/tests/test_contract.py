"""The worker's contract with every reader of its graph, from the Go tests'
golden file: what the worker writes (search documents and their hashes,
graph ids, embedding windows) must be what this package computes, or a
search drops every hit as stale, names repositories that don't exist, or
embeds questions unlike the code.

The Go side (packages/go/code-graph/domain/codesearch, TestReaderContract)
checks the same file against the worker's code: change either side and both
suites fail until the other follows (go test ./domain/codesearch -run
TestReaderContract -update rewrites it from Go).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from forge_codegraph_query import codesearch
from forge_codegraph_query.embedding import combine, windows
from forge_codegraph_query.model import graph_id

GOLDEN = (
    Path(__file__).resolve().parents[4]
    / "packages/go/code-graph/domain/codesearch/testdata/reader_contract.json"
)


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return dict(json.loads(GOLDEN.read_text()))


def test_the_document_version_is_the_workers(contract: dict[str, Any]) -> None:
    assert contract["document_version"] == codesearch.DOCUMENT_VERSION


def test_search_documents_hash_as_the_worker_wrote_them(contract: dict[str, Any]) -> None:
    assert contract["documents"], "the golden file has no documents"
    for case in contract["documents"]:
        found = codesearch.document_of(case["node"])
        expected = case["document"]
        if expected is None:
            assert found is None, case["node"]
            continue
        assert found is not None, case["node"]
        assert (found.node_id, found.text, found.hash) == (
            expected["node_id"],
            expected["text"],
            expected["hash"],
        ), case["node"]


def test_graph_ids_are_the_workers(contract: dict[str, Any]) -> None:
    assert contract["graph_ids"]
    for case in contract["graph_ids"]:
        assert graph_id(case["kind"], *case["parts"]) == case["id"], case


def test_questions_are_embedded_as_the_worker_embeds_code(contract: dict[str, Any]) -> None:
    case = contract["embedding"]
    pieces = windows(case["text"], case["max_input_bytes"])
    assert pieces == case["windows"]
    vector = combine(pieces, case["window_vectors"], case["dimensions"])
    assert vector == pytest.approx(case["vector"], abs=1e-6)
