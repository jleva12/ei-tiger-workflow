"""Offline embedder/reranker for tests and local dev (no API keys, no network).

Feature-hashed unigrams + bigrams, L2-normalized: similar wording -> similar
vectors. Not semantic, but deterministic and good enough to exercise the full
pipeline and the vector leg of hybrid search.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

_WORD = re.compile(r"[a-z0-9]+")


class HashingEmbedder:
    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions
        self.model_id = f"hashing-v1@{dimensions}"
        self.calls = 0  # handy in tests to assert embedding reuse

    def _vector(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
        vec = [0.0] * self.dimensions
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            vec[h % self.dimensions] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += len(texts)
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class OverlapReranker:
    """Scores by query-term overlap. Stand-in for a cross-encoder in tests."""

    model_id = "overlap-v1"

    async def rerank(self, query: str, documents: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        q = set(_WORD.findall(query.lower()))
        scored = []
        for i, d in enumerate(documents):
            words = set(_WORD.findall(d.lower()))
            scored.append((i, len(q & words) / (len(q) or 1)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
