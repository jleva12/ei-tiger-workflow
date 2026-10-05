"""OpenAI (or Azure OpenAI) embedder.

``text-embedding-3-*`` vectors are unit length, so Atlas ``dotProduct`` is
exact cosine. ``dimensions`` shortens them (Matryoshka) with little quality
loss: 3-large at 1024 dims is a good default for Atlas.

Unlike Voyage there is no query/document ``input_type`` and no shared-space
model family, so queries and documents must use the same model.

API limits handled here: 8192 tokens per input (longer inputs are truncated),
2048 inputs and 300k tokens per request, no empty strings.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from forge_embeddings.errors import EmbeddingError
from forge_tasks.errors import TaskError

if TYPE_CHECKING:
    from forge_embeddings.protocols import Tokenizer

log = logging.getLogger(__name__)

MAX_INPUTS_PER_REQUEST = 2048
MAX_TOKENS_PER_REQUEST = 300_000
MAX_TOKENS_PER_INPUT = 8192


def _permanent(msg: str) -> TaskError:
    err = TaskError(msg)
    err.permanent = True
    return err


def _wrap(exc: Exception) -> Exception:
    """Map SDK errors onto the retry contract."""
    try:
        import openai
    except ImportError:  # pragma: no cover
        return EmbeddingError(str(exc))
    if isinstance(exc, openai.RateLimitError):
        if getattr(exc, "code", None) == "insufficient_quota":  # billing, not throttling: retrying won't help
            return _permanent(f"openai quota exhausted: {exc}")
        return EmbeddingError(f"openai rate limited: {exc}")
    if isinstance(exc, (openai.APIConnectionError, openai.InternalServerError, openai.ConflictError)):
        return EmbeddingError(f"openai call failed: {exc}")  # includes APITimeoutError
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return EmbeddingError(f"openai {exc.status_code}: {exc}")
        return _permanent(f"openai rejected the request ({exc.status_code}): {exc}")
    return EmbeddingError(f"openai call failed: {exc}")


_UNSET: Any = object()
_encoding: Any = _UNSET


def _cl100k() -> Any:
    """tiktoken's cl100k_base (the v3 models' encoding), loaded once per process.
    tiktoken downloads the BPE file on first use: bake it into the image
    (``TIKTOKEN_CACHE_DIR``) so workers don't need that egress."""
    global _encoding
    if _encoding is _UNSET:
        try:
            import tiktoken

            _encoding = tiktoken.get_encoding("cl100k_base")
        except Exception as exc:
            log.warning("tiktoken cl100k_base unavailable (%s); using a conservative byte estimate", exc)
            _encoding = None
    return _encoding


class _TokenCounter:
    """Exact counts with tiktoken; otherwise utf-8 bytes / 2, which over-counts
    English (~4 chars/token) and stays safe for CJK (~1-2 tokens/char)."""

    def __init__(self) -> None:
        self._enc = _cl100k()

    def count(self, text: str) -> int:
        if self._enc is not None:
            return len(self._enc.encode(text, disallowed_special=()))
        return (len(text.encode("utf-8")) + 1) // 2

    def truncate(self, text: str, max_tokens: int) -> str:
        if self._enc is None:
            return text.encode("utf-8")[: max_tokens * 2].decode("utf-8", errors="ignore")
        ids = self._enc.encode(text, disallowed_special=())
        return text if len(ids) <= max_tokens else self._enc.decode(ids[:max_tokens])


class OpenAIEmbedder:
    def __init__(
        self,
        *,
        api_key: str | None = None,  # None -> OPENAI_API_KEY (or AZURE_OPENAI_API_KEY)
        model: str = "text-embedding-3-large",
        dimensions: int = 1024,
        batch_size: int = 256,
        max_batch_tokens: int = 250_000,
        max_input_tokens: int = MAX_TOKENS_PER_INPUT - 192,  # headroom for count drift
        concurrency: int = 4,
        max_retries: int = 4,
        timeout: float = 60.0,
        base_url: str | None = None,  # OpenAI-compatible gateway/proxy
        azure_endpoint: str | None = None,  # set -> Azure OpenAI; ``model`` is then the deployment name
        api_version: str | None = None,
        organization: str | None = None,
        tokenizer: Tokenizer | None = None,  # accepted for interface parity; counting uses cl100k
        client: Any = None,
    ) -> None:
        # The SDK client is built on first use: the OpenAI SDK refuses to build
        # one without a key, and a worker without one should still start (its
        # embedding jobs then fail with the reason).
        self._client = client
        self._client_options: dict[str, Any] = {"max_retries": max_retries, "timeout": timeout, "api_key": api_key}
        if azure_endpoint:
            self._client_options.update(azure_endpoint=azure_endpoint, api_version=api_version or "2024-10-21")
        else:
            self._client_options.update(base_url=base_url, organization=organization)
        self.model = model
        # ada-002 is fixed at 1536 and rejects ``dimensions``
        self._send_dimensions = not model.startswith("text-embedding-ada")
        if not self._send_dimensions and dimensions != 1536:
            raise ValueError(f"{model} only produces 1536-dim vectors")
        self.dimensions = dimensions
        self.model_id = f"{model}@{dimensions}"
        self.batch_size = min(batch_size, MAX_INPUTS_PER_REQUEST)
        self.max_batch_tokens = min(max_batch_tokens, MAX_TOKENS_PER_REQUEST)
        self.max_input_tokens = min(max_input_tokens, MAX_TOKENS_PER_INPUT)
        self.concurrency = concurrency
        self._slots = asyncio.Semaphore(concurrency)  # requests at once across this process's jobs
        self._tok = _TokenCounter()

    def _prepare(self, text: str) -> tuple[str, int]:
        text = text if text.strip() else "(empty)"
        n = self._tok.count(text)
        if n > self.max_input_tokens:
            text = self._tok.truncate(text, self.max_input_tokens)
            n = self.max_input_tokens
        return text, n

    def _batches(self, texts: Sequence[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        cur: list[str] = []
        cur_tokens = 0
        for raw in texts:
            t, n = self._prepare(raw)
            if cur and (len(cur) >= self.batch_size or cur_tokens + n > self.max_batch_tokens):
                batches.append(cur)
                cur, cur_tokens = [], 0
            cur.append(t)
            cur_tokens += n
        if cur:
            batches.append(cur)
        return batches

    def client(self) -> Any:
        """The SDK client, built on first use (see __init__).

        :raises TaskError: Permanent: no API key, or a bad endpoint setting.
        """
        if self._client is None:
            import openai

            azure = "azure_endpoint" in self._client_options
            try:
                self._client = (openai.AsyncAzureOpenAI if azure else openai.AsyncOpenAI)(**self._client_options)
            except openai.OpenAIError as exc:
                raise _permanent(
                    f"openai client: {exc} Set HYBRID_EMBEDDING__API_KEY (or OPENAI_API_KEY"
                    f"{' / AZURE_OPENAI_API_KEY' if azure else ''} in the environment)."
                ) from exc
        return self._client

    async def _embed(self, batch: list[str]) -> list[list[float]]:
        kwargs: dict[str, Any] = {"input": batch, "model": self.model, "encoding_format": "float"}
        if self._send_dimensions:
            kwargs["dimensions"] = self.dimensions
        client = self.client()
        try:
            res = await client.embeddings.create(**kwargs)
        except Exception as exc:
            raise _wrap(exc) from exc
        rows = sorted(res.data, key=lambda d: d.index)
        if len(rows) != len(batch):
            raise EmbeddingError(f"openai returned {len(rows)} vectors for {len(batch)} inputs")
        return [_unit(r.embedding) for r in rows]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        sem = self._slots

        async def run(batch: list[str]) -> list[list[float]]:
            async with sem:
                return await self._embed(batch)

        results = await asyncio.gather(*(run(b) for b in self._batches(texts)))
        return [v for batch in results for v in batch]

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([self._prepare(text)[0]]))[0]


def _unit(vec: Sequence[float]) -> list[float]:
    """Re-normalize (cheap insurance for dotProduct if a gateway/model returns
    non-unit vectors)."""
    v = [float(x) for x in vec]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm > 0 else v
