"""Storage backends. Each one implements ``protocols.StorageBackend``.

To add a database (e.g. Postgres + pgvector + tsvector):
  1. implement DocumentStore, ChunkStore and SearchBackend (fuse with
     ``retrieval.fusion.rrf_fuse`` if the DB has no native fusion),
  2. bundle them in a class with ``ensure_schema`` / ``close``,
  3. register a factory: ``register_storage_backend("postgres", factory)``,
  4. run tests/test_storage_contract.py against it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forge_task_documents.config import DocumentsSettings
    from forge_task_documents.protocols import StorageBackend


def open_storage(settings: DocumentsSettings, *, mongo_uri: str | None, mongo_database: str) -> StorageBackend | None:
    """
    The documents' store for a service outside the worker that reads what it
    wrote (the admin API's knowledge base search): the backend
    ``FORGE_VECTOR_STORE`` selects, as the worker's own selection does, on a
    connection of its own to close.

    :param settings: The documents task's settings (collection and index
        names, vector similarity): the worker's.
    :param mongo_uri: The worker's MongoDB, when Mongo is selected.
    :param mongo_database: Its database.
    :return: The store; None when Mongo is selected and there's no URI.
    :raises ValueError: The selection names another backend, or Spanner's
        database setting is wrong.
    """
    from forge_embeddings.vector_store import selected_backend

    backend = selected_backend(settings.storage_backend)
    if backend == "spanner":
        from forge_task_documents.storage.spanner import SpannerStorage

        return SpannerStorage(vector_similarity=settings.vector_similarity)
    if backend != "mongo":
        raise ValueError(f"documents storage {backend!r} can't be opened outside the worker")
    if mongo_uri is None:
        return None
    from forge_task_documents.storage.mongo import MongoStorage

    return MongoStorage.from_uri(
        mongo_uri,
        database=mongo_database,
        documents_collection=settings.documents_collection,
        chunks_collection=settings.chunks_collection,
        text_index=settings.text_index,
        vector_index=settings.vector_index,
        vector_similarity=settings.vector_similarity,
        vector_quantization=settings.vector_quantization,
        language_analyzer=settings.language_analyzer,
        binary_vectors=settings.binary_vectors,
        fusion_mode=settings.fusion_mode,
    )
