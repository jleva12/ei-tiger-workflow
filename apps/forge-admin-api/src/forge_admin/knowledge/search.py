"""Searching knowledge bases: the documents task's knowledge base search
(``forge_task_documents.retrieval.KnowledgeBaseSearch``), in-process, over the
chunks the async worker embedded. ADK workflows' agents search with the same
one on the worker, so both find the same passages.

The worker writes the chunks (``HYBRID_MONGO__*``, ``HYBRID_EMBEDDING__*``,
``HYBRID_RERANK__*``); this reads them, so both must read the same store
(``FORGE_VECTOR_STORE``; for Mongo, ``FORGE_ADMIN_MONGO_URI`` and
``_KNOWLEDGE_DATABASE``) and embed and rerank with the same models
(``_KNOWLEDGE_EMBEDDING__*``, ``_KNOWLEDGE_RERANK__*``). A knowledge base is
the worker's tenant: a search names the knowledge bases an agent was given,
and finds only their chunks, ranked together.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Self

from forge_task_documents.retrieval.knowledge import MAX_PASSAGES, Passage

if TYPE_CHECKING:
    from forge_task_documents.models import DocumentRecord
    from forge_task_documents.retrieval import KnowledgeBaseSearch

    from forge_admin.config import Settings

logger = logging.getLogger(__name__)

# The most passages one search answers.
MAX_RESULTS = MAX_PASSAGES

__all__ = ["MAX_RESULTS", "KnowledgeSearch", "Passage", "SearchError"]


class SearchError(Exception):
    """The knowledge bases' chunks could not be searched (their store or the
    embedding model is unavailable, or refused)."""


class KnowledgeSearch:
    """
    Searches knowledge bases' chunks.

    :param search: The documents task's knowledge base search, over the
        worker's chunks.
    """

    def __init__(self, search: KnowledgeBaseSearch) -> None:
        self._search = search

    @property
    def model_id(self) -> str:
        """What questions are embedded with (e.g. ``text-embedding-3-large@1024``):
        a document embedded with another model needs re-indexing."""
        return self._search.model_id

    @classmethod
    def from_settings(cls, settings: Settings) -> Self | None:
        """
        :param settings: Settings with ``mongo_uri``, ``knowledge_database``,
            ``knowledge_embedding``, ``knowledge_rerank`` and
            ``knowledge_search``.
        :return: A search over the store ``FORGE_VECTOR_STORE`` selects, as
            the worker's is; None when that's Mongo and there's no
            ``mongo_uri``. Nothing connects until the first search.
        """
        from forge_embeddings.clients import build_embedder, build_reranker
        from forge_task_documents.config import DocumentsSettings
        from forge_task_documents.retrieval import KnowledgeBaseSearch
        from forge_task_documents.storage import open_storage

        documents = DocumentsSettings()
        storage = open_storage(
            documents,
            mongo_uri=settings.mongo_uri.get_secret_value()
            if settings.mongo_uri
            else None,
            mongo_database=settings.knowledge_database,
        )
        if storage is None:
            return None
        return cls(
            KnowledgeBaseSearch(
                storage,
                embedder=build_embedder(settings.knowledge_embedding),
                reranker=build_reranker(
                    settings.knowledge_rerank, settings.knowledge_embedding
                ),
                config=settings.knowledge_search,
                owns_storage=True,
            )
        )

    async def search(
        self,
        knowledge_base_ids: Sequence[str],
        query: str,
        *,
        limit: int = 8,
        document_ids: list[str] | None = None,
    ) -> list[Passage]:
        """
        Search one or more knowledge bases, together.

        :param knowledge_base_ids: The knowledge bases, and only these: their
            passages are ranked as one.
        :param query: What to look for, in plain words.
        :param limit: The most passages to answer, up to ``MAX_RESULTS``.
        :param document_ids: Only these documents' passages.
        :return: The passages, best first.
        :raises SearchError: The store or the embedding model failed.
        """
        try:
            return await self._search.search(
                knowledge_base_ids, query, limit=limit, document_ids=document_ids
            )
        except (
            Exception
        ) as error:  # the store's, the embedder's: any is a failed search
            logger.warning(
                "Searching knowledge bases %s failed: %s",
                ", ".join(knowledge_base_ids),
                error,
            )
            raise SearchError(str(error) or type(error).__name__) from error

    async def records(
        self, documents: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], DocumentRecord | None]:
        """
        The worker's own records of documents: how each one's last ingestion
        went, whatever its queue still has.

        :param documents: (knowledge base, document) pairs.
        :return: Each one's record, by the pair; None for one it has none of,
            or has deleted.
        :raises SearchError: The store failed.
        """
        keys = list(dict.fromkeys(documents))
        store = self._search.storage.documents
        try:
            found = await asyncio.gather(*(store.get(*key) for key in keys))
        except Exception as error:  # the store's: any is a failed read
            raise SearchError(str(error) or type(error).__name__) from error
        return dict(zip(keys, found, strict=True))

    async def forget(self, knowledge_base_id: str, document_id: str) -> bool:
        """
        Tombstone the worker's record of a document being removed: from now
        on no search finds it (this API's, chat agents', workflows'), though
        its delete job hasn't removed its chunks yet, and an ingest still
        running for it can't finish. Best effort: when the store fails, the
        delete job tombstones it as it runs.

        :return: Whether it's tombstoned now.
        """
        try:
            await self._search.storage.documents.delete(knowledge_base_id, document_id)
        except Exception as error:  # the store's; the delete job does it later
            logger.warning(
                "Couldn't hide document %s of knowledge base %s from search before "
                "its delete job: %s",
                document_id,
                knowledge_base_id,
                error,
            )
            return False
        return True

    async def aclose(self) -> None:
        await self._search.aclose()
