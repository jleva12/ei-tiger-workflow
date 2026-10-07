"""Code search hits as knowledge base passages: the shape the knowledge base
tool hands the model (``forge_agent_runtime.services.KnowledgeBases``), so a
graph knowledge base's code is cited like a document's passages."""

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Repository:
    """
    A repository a graph knowledge base includes.

    :ivar id: Forge's repository ID (``code_repositories.id``).
    :ivar graph_id: Its graph on the worker (``repo:…``).
    :ivar name: ``owner/name``.
    """

    id: str
    graph_id: str
    name: str


def citation_ref(key: str) -> str:
    """
    The ref an answer cites a passage by ("[KQM4821]"): three letters and four
    digits made from the passage, so it's the same in every search. The same
    algorithm as the documents' ``forge_task_documents.retrieval.knowledge.
    citation_ref``, which this package doesn't depend on.
    """
    n = int(hashlib.sha256(key.encode()).hexdigest()[:15], 16)
    letters = ""
    for _ in range(3):
        n, letter = divmod(n, 26)
        letters += chr(ord("A") + letter)
    return f"{letters}{n % 10_000:04d}"


def code_passages(
    hits: Iterable[Mapping[str, Any]],
    *,
    knowledge_base_id: str,
    knowledge_base: str,
    repositories: Sequence[Repository],
) -> list[dict[str, Any]]:
    """
    :param hits: The worker's search hits (``CodeGraph.search``), best first.
    :param knowledge_base_id: The graph knowledge base searched.
    :param knowledge_base: Its name.
    :param repositories: Its repositories; a hit in any other is dropped.
    :return: The passages, in order: each ``{"ref", "knowledge_base",
        "knowledge_base_id", "document", "document_id", "section",
        "location", "text", "score"}``, the documents' shape, where the
        document is the file (``owner/name: path``) and the section the
        declaration; plus ``repository_id`` (Forge's) and ``node_id``, which
        a citation links to.
    """
    by_graph = {r.graph_id: r for r in repositories}
    passages: list[dict[str, Any]] = []
    for hit in hits:
        repository = by_graph.get(str(hit.get("repository_id", "")))
        node = str(hit.get("id", ""))
        if repository is None or not node:
            continue
        file = str(hit.get("file") or "")
        line = hit.get("text_start_line") or hit.get("line")
        name = str(hit.get("qualified_name") or hit.get("name") or "")
        kind = str(hit.get("kind") or "")
        text = str(hit.get("text") or hit.get("snippet") or hit.get("signature") or "")
        passages.append(
            {
                "ref": citation_ref(f"{repository.graph_id}:{node}"),
                "knowledge_base": knowledge_base,
                "knowledge_base_id": knowledge_base_id,
                "document": f"{repository.name}: {file}" if file else repository.name,
                "document_id": f"{repository.id}:{file}",
                "section": " ".join(p for p in (kind, name) if p),
                "location": _location(file, line, hit.get("text_end_line")),
                "text": text,
                "score": float(hit.get("score") or 0.0),
                "repository_id": repository.id,
                "node_id": node,
            }
        )
    return passages


def interleave(
    lists: Sequence[Sequence[dict[str, Any]]], limit: int
) -> list[dict[str, Any]]:
    """
    Merge ranked lists by rank: each one's best, then each one's second, …
    Document and code scores aren't on one scale, so neither is sorted by it.

    :param lists: Passages, each list best first.
    :param limit: The most to keep.
    :return: At most ``limit`` passages, without repeating a ref.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank in range(max((len(x) for x in lists), default=0)):
        for passages in lists:
            if rank >= len(passages):
                continue
            passage = passages[rank]
            ref = str(passage.get("ref", ""))
            if ref in seen:
                continue
            seen.add(ref)
            merged.append(passage)
            if len(merged) >= limit:
                return merged
    return merged


def _location(file: str, start: Any, end: Any) -> str:
    if not isinstance(start, int) or start <= 0:
        return file
    if isinstance(end, int) and end > start:
        return f"{file}, lines {start}–{end}" if file else f"lines {start}–{end}"
    return f"{file}, line {start}" if file else f"line {start}"
