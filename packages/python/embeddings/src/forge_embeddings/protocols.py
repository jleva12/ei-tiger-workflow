"""The model clients shared by the embedding tasks: Tokenizer, Embedder,
Reranker and LLMClient."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------- model clients


@runtime_checkable
class Tokenizer(Protocol):
    name: str

    def count(self, text: str) -> int: ...

    def split(self, text: str, max_tokens: int, overlap: int = 0) -> list[str]:
        """Hard split into windows of at most ``max_tokens`` (last resort)."""
        ...


@runtime_checkable
class Embedder(Protocol):
    model_id: str  # document-side model (+ dims); part of every content hash
    dimensions: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class Reranker(Protocol):
    model_id: str

    async def rerank(self, query: str, documents: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        """Return (index into documents, relevance score), best first."""
        ...


@runtime_checkable
class LLMClient(Protocol):
    model_id: str

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 200,
        cache_system: bool = True,
        temperature: float | None = None,
    ) -> str: ...
