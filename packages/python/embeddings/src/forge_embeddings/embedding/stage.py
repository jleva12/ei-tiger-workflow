"""Shared embedding step: content-hash reuse + batched embedding.

Every task type embeds "units" (document chunks, commit docs, ...). A unit's
``content_hash`` = hash(embed_text, model) is the reuse key, so re-running a
job, re-summarizing to identical text, or re-ingesting an edited file only
pays for text that actually changed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from forge_tasks.errors import TaskError

if TYPE_CHECKING:
    from forge_embeddings.protocols import Embedder

VectorLookup = Callable[[Collection[str]], Awaitable[dict[str, list[float]]]]


def content_hash(embed_text: str, model_id: str) -> str:
    return hashlib.sha256(f"{model_id}\x1f{embed_text}".encode()).hexdigest()


class Embeddable(Protocol):
    embed_text: str
    content_hash: str
    embedding: list[float] | None
    embedding_model: str | None


class EmbedStats(BaseModel):
    embedded: int = 0  # distinct texts sent to the model
    reused: int = 0  # units that got a stored vector


class EmbeddingStage:
    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder

    @property
    def model_id(self) -> str:
        return self.embedder.model_id

    async def embed(self, units: Sequence[Embeddable], lookup: VectorLookup | None = None) -> EmbedStats:
        model = self.embedder.model_id
        for u in units:
            u.content_hash = content_hash(u.embed_text, model)
            u.embedding_model = model
        known = await lookup({u.content_hash for u in units}) if lookup and units else {}
        todo: dict[str, str] = {}
        for u in units:
            if u.content_hash not in known:
                todo.setdefault(u.content_hash, u.embed_text)
        if todo:
            vectors = await self.embedder.embed_documents(list(todo.values()))
            if len(vectors) != len(todo):
                raise TaskError(f"embedder returned {len(vectors)} vectors for {len(todo)} inputs")
            known.update(zip(todo.keys(), vectors, strict=True))
        for u in units:
            u.embedding = known[u.content_hash]
        return EmbedStats(embedded=len(todo), reused=sum(1 for u in units if u.content_hash not in todo))
