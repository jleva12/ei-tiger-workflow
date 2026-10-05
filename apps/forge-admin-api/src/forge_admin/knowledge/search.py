"""Searching a knowledge base: the documents task's hybrid search (BM25 and
vector, fused), in-process, over the chunks the async worker embedded into
MongoDB Atlas.

The worker writes the chunks (``HYBRID_MONGO__*``, ``HYBRID_EMBEDDING__*``);
this reads them, so both must name the same database and embed with the same
model and dimensions (``FORGE_ADMIN_MONGO_URI``, ``_KNOWLEDGE_DATABASE``,
``_KNOWLEDGE_EMBEDDING__*``). A knowledge base is the worker's tenant: a
search names it and finds only its chunks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from forge_task_documents.retrieval import HybridSearchService

    from forge_admin.config import Settings

logger = logging.getLogger(__name__)

# The most passages one search answers.
MAX_RESULTS = 20


class SearchError(Exception):
    """The knowledge bases' chunks could not be searched (MongoDB or the
    embedding model is unavailable, or refused)."""


@dataclass(frozen=True)
class Passage:
    """
    One passage a search found.

    :ivar chunk_id: The chunk.
    :ivar document_id: The document it's from.
    :ivar title: The document's title, as the worker read it.
    :ivar section_path: The headings it's under, outermost first.
    :ivar text: The passage.
    :ivar score: How well it matched, fused from both legs; higher is better.
    """

    chunk_id: str
    document_id: str
    title: str
    section_path: list[str] = field(default_factory=list)
    text: str = ""
    score: float = 0.0


class KnowledgeSearch:
    """
    Searches knowledge bases' chunks.

    :param service: The documents task's hybrid search, over the worker's
        chunks.
    :param close: Closes what the service holds open (the Mongo client).
    """

    def __init__(self, service: HybridSearchService, close: Any = None) -> None:
        self._service = service
        self._close = close

    @classmethod
    def from_settings(cls, settings: Settings) -> Self | None:
        """
        :param settings: Settings with ``mongo_uri``, ``knowledge_database``
            and ``knowledge_embedding``.
        :return: A search, or None without MongoDB. Nothing connects until
            the first search.
        """
        if settings.mongo_uri is None:
            return None
        from forge_embeddings.clients import build_embedder
        from forge_embeddings.identifiers import IdentifierExtractor
        from forge_task_documents.config import DocumentsSettings
        from forge_task_documents.retrieval import HybridSearchService
        from forge_task_documents.storage.mongo import MongoStorage

        documents = DocumentsSettings()
        storage = MongoStorage.from_uri(
            settings.mongo_uri.get_secret_value(),
            database=settings.knowledge_database,
            documents_collection=documents.documents_collection,
            chunks_collection=documents.chunks_collection,
            text_index=documents.text_index,
            vector_index=documents.vector_index,
            vector_similarity=documents.vector_similarity,
            vector_quantization=documents.vector_quantization,
            language_analyzer=documents.language_analyzer,
            binary_vectors=documents.binary_vectors,
            fusion_mode=documents.fusion_mode,
        )
        service = HybridSearchService(
            search=storage.search,
            chunks=storage.chunks,
            embedder=build_embedder(settings.knowledge_embedding),
            identifier_extractor=IdentifierExtractor(),
            config=documents.search,
        )
        return cls(service, close=storage.close)

    async def search(
        self,
        knowledge_base_ids: list[str],
        query: str,
        *,
        limit: int = 8,
        document_ids: list[str] | None = None,
    ) -> list[Passage]:
        """
        Search one or more knowledge bases.

        :param knowledge_base_ids: The knowledge bases; each is searched, and
            their passages are merged by score.
        :param query: What to look for, in plain words.
        :param limit: The most passages to answer, up to ``MAX_RESULTS``.
        :param document_ids: Only these documents' passages.
        :return: The passages, best first.
        :raises SearchError: MongoDB or the embedding model failed.
        """
        from forge_task_documents.models import SearchFilters, SearchQuery

        limit = max(1, min(limit, MAX_RESULTS))
        passages: list[Passage] = []
        for knowledge_base_id in dict.fromkeys(knowledge_base_ids):
            try:
                found = await self._service.search(
                    SearchQuery(
                        tenant_id=knowledge_base_id,
                        text=query,
                        top_k=limit,
                        filters=SearchFilters(doc_ids=document_ids or None),
                    )
                )
            except (
                Exception
            ) as error:  # Mongo's, the embedder's: any is a failed search
                logger.warning(
                    "Searching knowledge base %s failed: %s", knowledge_base_id, error
                )
                raise SearchError(str(error) or type(error).__name__) from error
            passages.extend(
                Passage(
                    chunk_id=hit.chunk.id,
                    document_id=hit.chunk.doc_id,
                    title=hit.chunk.title,
                    section_path=list(hit.chunk.section_path),
                    text=hit.chunk.text,
                    score=float(
                        hit.rerank_score if hit.rerank_score is not None else hit.score
                    ),
                )
                for hit in found.hits
            )
        passages.sort(key=lambda passage: passage.score, reverse=True)
        return passages[:limit]

    async def aclose(self) -> None:
        if self._close is not None:
            await self._close()
