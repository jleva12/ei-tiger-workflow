"""Voyage AI embedder + reranker (MongoDB's native models).

Voyage 4 models share one embedding space, so you can index with a bigger
model and query with a cheaper one (document_model="voyage-4-large",
query_model="voyage-4-lite") without re-indexing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from forge_embeddings.errors import EmbeddingError
from forge_tasks.errors import TaskError

if TYPE_CHECKING:
    from forge_embeddings.protocols import Tokenizer


def _wrap(exc: Exception) -> Exception:
    try:
        from voyageai import error as verr
    except ImportError:  # pragma: no cover
        return EmbeddingError(str(exc))
    if isinstance(exc, (verr.InvalidRequestError, verr.MalformedRequestError, verr.AuthenticationError)):
        err = TaskError(f"voyage rejected the request: {exc}")
        err.permanent = True
        return err
    return EmbeddingError(f"voyage call failed: {exc}")


class VoyageEmbedder:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        document_model: str = "voyage-4",
        query_model: str | None = None,
        dimensions: int = 1024,
        batch_size: int = 128,
        max_batch_tokens: int = 100_000,
        concurrency: int = 4,
        max_retries: int = 4,
        timeout: float = 60.0,
        tokenizer: Tokenizer | None = None,
        client: Any = None,
    ) -> None:
        if client is None:
            import voyageai

            client = voyageai.AsyncClient(api_key=api_key, max_retries=max_retries, timeout=timeout)
        self._client = client
        self.document_model = document_model
        self.query_model = query_model or document_model
        self.dimensions = dimensions
        self.model_id = f"{document_model}@{dimensions}"
        self.batch_size = batch_size
        self.max_batch_tokens = max_batch_tokens
        self.concurrency = concurrency
        self._slots = asyncio.Semaphore(concurrency)  # requests at once across this process's jobs
        self._tokenizer = tokenizer

    def _batches(self, texts: Sequence[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        cur: list[str] = []
        cur_tokens = 0
        for t in texts:
            n = self._tokenizer.count(t) if self._tokenizer else len(t) // 3
            if cur and (len(cur) >= self.batch_size or cur_tokens + n > self.max_batch_tokens):
                batches.append(cur)
                cur, cur_tokens = [], 0
            cur.append(t)
            cur_tokens += n
        if cur:
            batches.append(cur)
        return batches

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        sem = self._slots

        async def run(batch: list[str]) -> list[list[float]]:
            async with sem:
                try:
                    res = await self._client.embed(
                        batch,
                        model=self.document_model,
                        input_type="document",
                        output_dimension=self.dimensions,
                    )
                except Exception as exc:
                    raise _wrap(exc) from exc
                return [list(map(float, v)) for v in res.embeddings]

        results = await asyncio.gather(*(run(b) for b in self._batches(texts)))
        return [v for batch in results for v in batch]

    async def embed_query(self, text: str) -> list[float]:
        try:
            res = await self._client.embed(
                [text], model=self.query_model, input_type="query", output_dimension=self.dimensions
            )
        except Exception as exc:
            raise _wrap(exc) from exc
        return list(map(float, res.embeddings[0]))


class VoyageReranker:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "rerank-2.5",
        max_retries: int = 3,
        timeout: float = 30.0,
        client: Any = None,
    ) -> None:
        if client is None:
            import voyageai

            client = voyageai.AsyncClient(api_key=api_key, max_retries=max_retries, timeout=timeout)
        self._client = client
        self.model_id = model

    async def rerank(self, query: str, documents: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        if not documents:
            return []
        try:
            res = await self._client.rerank(query, list(documents), model=self.model_id, top_k=top_k)
        except Exception as exc:
            raise _wrap(exc) from exc
        return [(r.index, float(r.relevance_score)) for r in res.results]
