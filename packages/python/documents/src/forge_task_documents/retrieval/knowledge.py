"""Searching knowledge bases: the passages of their documents that best match
a question, for agents and for the people who build them.

A knowledge base is a tenant of the documents task: every chunk of its
documents carries its ID. An agent or a workflow is given some of its
organization's knowledge bases ("Member knowledge", say, or that and "Claims
knowledge"); a search names exactly those and runs once over all of them, so
their passages are ranked against each other and nothing else is searched.
(Searching each on its own and merging the results by score doesn't rank
them: fused scores come from ranks alone, so every knowledge base's first
passage would tie.)

Everything that searches knowledge bases goes through
:class:`KnowledgeBaseSearch` (the admin API's chat agents and search route,
ADK workflows' agents), over the store the worker writes, so each finds the
same passages.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from forge_task_documents.models import SearchFilters, SearchHit, SearchQuery, SourceLocation
from forge_task_documents.retrieval.service import HybridSearchService

if TYPE_CHECKING:
    from forge_embeddings.protocols import Embedder, Reranker
    from forge_task_documents.protocols import StorageBackend
    from forge_task_documents.retrieval.service import SearchConfig

#: The most passages one search answers.
MAX_PASSAGES = 20


def citation_ref(chunk_id: str) -> str:
    """
    The ref an answer cites a passage by, in brackets: three letters and
    four digits ("[KQM4821]"), the citations chat UIs read
    (``@forge-ui`` ``source-refs``). Made from the chunk, so the same passage
    has the same ref in every search and every turn of a conversation, and
    parallel searches never hand out the same one; chat UIs number the
    citations they show (1, 2, 3), so nobody reads it but the model.
    """
    n = int(hashlib.sha256(chunk_id.encode()).hexdigest()[:15], 16)
    letters = ""
    for _ in range(3):
        n, letter = divmod(n, 26)
        letters += chr(ord("A") + letter)
    return f"{letters}{n % 10_000:04d}"


def describe_location(source_type: str, location: SourceLocation) -> str:
    """:return: Where in its document a passage is, in words: "page 4",
    "slide 3: Pricing", "sheet Prices, A1:D20", "lines 12–30"; empty when unknown."""
    if location.sheet:
        return f"sheet {location.sheet}" + (f", {location.cell_range}" if location.cell_range else "")
    if location.page is not None:
        unit = "slide" if source_type == "pptx" else "page"
        return f"{unit} {location.page}" + (f": {location.page_name}" if location.page_name else "")
    if location.page_name:
        return location.page_name
    if location.line_start is not None:
        if location.line_end is None or location.line_end == location.line_start:
            return f"line {location.line_start}"
        return f"lines {location.line_start}–{location.line_end}"
    return ""


@dataclass(frozen=True)
class Passage:
    """
    One passage a search found.

    :ivar chunk_id: The chunk.
    :ivar document_id: The document it's from.
    :ivar title: The document's title, as the worker read it.
    :ivar section_path: The headings it's under, outermost first.
    :ivar text: The passage.
    :ivar score: How relevant it is: the reranker's score when there is
        one, else the similarity of its meaning to the question's (the
        cosine of their vectors, -1 to 1), else its fused rank score.
        Passages are in their ranked order, which also weighs their words.
    :ivar knowledge_base_id: The knowledge base it's in.
    :ivar filename: The document's name as uploaded, from the worker's
        record of it; empty when it has none.
    :ivar location: Where in the document it is, in words (:func:`describe_location`).
    """

    chunk_id: str
    document_id: str
    title: str
    section_path: list[str] = field(default_factory=list)
    text: str = ""
    score: float = 0.0
    knowledge_base_id: str = ""
    filename: str = ""
    location: str = ""

    @property
    def document(self) -> str:
        """The document's name: as uploaded, else its title."""
        return self.filename or self.title

    @property
    def ref(self) -> str:
        """What an answer cites it by (:func:`citation_ref`)."""
        return citation_ref(self.chunk_id)

    def for_agent(self, *, knowledge_base: str, document: str | None = None) -> dict[str, Any]:
        """
        :param knowledge_base: The name of the knowledge base it's in, so an
            agent searching several can tell their passages apart.
        :param document: The document's name, when the caller knows a
            better one than :attr:`document`.
        :return: The passage as a knowledge base tool answers it, for the
            model and for the chat UI that shows its citations:
            ``{"ref", "knowledge_base", "knowledge_base_id", "document",
            "document_id", "section", "location", "text", "score"}``.
        """
        return {
            "ref": self.ref,
            "knowledge_base": knowledge_base,
            "knowledge_base_id": self.knowledge_base_id,
            "document": document or self.document,
            "document_id": self.document_id,
            "section": " > ".join(self.section_path),
            "location": self.location,
            "text": self.text,
            "score": round(self.score, 4),
        }


def _relevance(hit: SearchHit) -> float:
    """The reranker's score, else the similarity of its meaning, else its fused rank score."""
    for score in (hit.rerank_score, hit.similarity):
        if score is not None:
            return score
    return hit.score


class KnowledgeBaseSearch:
    """
    Searches knowledge bases: the documents' hybrid search (BM25 and vector,
    fused, then reranked when there's a reranker) over a storage backend.

    :param storage: The worker's store of documents and chunks.
    :param embedder: Embeds the question: the model the worker embedded the
        chunks with.
    :param reranker: Reorders the fused candidates; None keeps their order.
    :param config: The search's candidate counts and leg weights.
    :param owns_storage: Close ``storage`` with :meth:`aclose` (a service
        that opened it for this search), rather than leave it to its owner.
    """

    def __init__(
        self,
        storage: StorageBackend,
        *,
        embedder: Embedder,
        reranker: Reranker | None = None,
        config: SearchConfig | None = None,
        owns_storage: bool = False,
    ) -> None:
        self.storage = storage
        #: What questions are embedded with; only chunks embedded with it are compared by meaning.
        self.model_id: str = embedder.model_id
        self.service = HybridSearchService(
            search=storage.search, chunks=storage.chunks, embedder=embedder, reranker=reranker, config=config
        )
        self._owns_storage = owns_storage

    async def search(
        self,
        knowledge_base_ids: Sequence[str],
        query: str,
        *,
        limit: int = 8,
        document_ids: Sequence[str] | None = None,
    ) -> list[Passage]:
        """
        Search some knowledge bases, together.

        :param knowledge_base_ids: The knowledge bases searched, and only
            these: one, or several ranked as one.
        :param query: What to look for, in plain words.
        :param limit: The most passages to answer, up to ``MAX_PASSAGES``.
        :param document_ids: Only these documents' passages.
        :return: The passages, best first. A passage of a document the
            worker has since deleted is left out.
        :raises ValueError: No knowledge base is named.
        """
        tenant_ids = list(dict.fromkeys(knowledge_base_ids))
        if not tenant_ids:
            raise ValueError("Name the knowledge bases to search")
        limit = max(1, min(limit, MAX_PASSAGES))
        found = await self.service.search(
            SearchQuery(
                tenant_ids=tenant_ids,
                text=query,
                top_k=limit,
                filters=SearchFilters(doc_ids=list(document_ids) if document_ids else None),
            )
        )
        filenames = await self._filenames({(hit.chunk.tenant_id, hit.chunk.doc_id) for hit in found.hits})
        return [
            Passage(
                chunk_id=hit.chunk.id,
                document_id=hit.chunk.doc_id,
                title=hit.chunk.title,
                section_path=list(hit.chunk.section_path),
                text=hit.chunk.text,
                score=_relevance(hit),
                knowledge_base_id=hit.chunk.tenant_id,
                filename=filenames[(hit.chunk.tenant_id, hit.chunk.doc_id)],
                location=describe_location(hit.chunk.source_type, hit.chunk.location),
            )
            for hit in found.hits
            if (hit.chunk.tenant_id, hit.chunk.doc_id) in filenames
        ]

    async def _filenames(self, documents: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """:return: Each document's name as uploaded, by (knowledge base, document); one the worker deleted is missing."""
        keys = sorted(documents)
        records = await asyncio.gather(*(self.storage.documents.get(*key) for key in keys))
        return {key: record.filename for key, record in zip(keys, records, strict=True) if record is not None}

    async def aclose(self) -> None:
        if self._owns_storage:
            await self.storage.close()
