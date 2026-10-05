"""The shared model clients (tokenizer, embedders, reranker, LLM), built from
:class:`~forge_embeddings.config.ModelSettings` once per process.

A task asks for them through its context: ``model_clients(ctx)`` returns the
process-wide clients, built on first use (or the ones a test pre-seeded).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from forge_embeddings.config import EmbeddingSettings, ModelSettings
from forge_embeddings.tokenizers import HeuristicTokenizer

if TYPE_CHECKING:
    from forge_embeddings.protocols import Embedder, LLMClient, Reranker, Tokenizer
    from forge_tasks.tasks import TaskContext

MODELS_RESOURCE = "forge_embeddings.models"


@dataclass
class ModelClients:
    """Shared model clients. Tasks ask for what they need; the LLM client is
    built on first use so tasks that don't need one don't need an API key."""

    tokenizer: Tokenizer
    embedders: dict[str, Embedder] = field(default_factory=dict)  # keyed by profile name
    reranker: Reranker | None = None
    llm_factory: Callable[[], LLMClient] | None = None
    llm_model_id: str | None = None
    _llm: LLMClient | None = None

    def embedder(self, profile: str = "default") -> Embedder:
        if profile in self.embedders:
            return self.embedders[profile]
        return self.embedders["default"]

    def require_llm(self) -> LLMClient:
        """Shared LLM client; constructed lazily on its first call."""
        if self._llm is None:
            if self.llm_factory is None:
                raise RuntimeError("no LLM configured (settings.llm)")
            from forge_embeddings.llm import LazyLLMClient

            self._llm = LazyLLMClient(self.llm_factory, model_id=self.llm_model_id or "unknown")
        return self._llm


def model_clients(ctx: TaskContext) -> ModelClients:
    """The process's shared model clients, built from the environment on first use."""
    return ctx.shared(MODELS_RESOURCE, lambda: build_models(ModelSettings()))


def prepare() -> None:
    """Download what the model clients would otherwise fetch on first use:
    tiktoken's cl100k_base (the OpenAI embedder's token counts), into
    ``TIKTOKEN_CACHE_DIR``. For image builds, so workers need no such egress."""
    import tiktoken

    tiktoken.get_encoding("cl100k_base")


# ----------------------------------------------------------------------------- builders


def build_tokenizer(settings: ModelSettings) -> Tokenizer:
    t = settings.tokenizer
    if t.kind == "tiktoken":
        from forge_embeddings.tokenizers import TiktokenTokenizer

        return TiktokenTokenizer(t.name or "cl100k_base")
    if t.kind == "huggingface":
        from forge_embeddings.tokenizers import HuggingFaceTokenizer

        return HuggingFaceTokenizer(t.name or "voyageai/voyage-4")
    return HeuristicTokenizer()


def _require_voyage(setting: str) -> None:
    """voyageai is optional: say how to get it rather than failing on the import."""
    try:
        import voyageai  # noqa: F401
    except ImportError:
        raise RuntimeError(
            f"{setting}=voyage needs the voyage extra (uv sync --extra voyage); the default is openai"
        ) from None


def build_embedder(e: EmbeddingSettings, tokenizer: Tokenizer | None = None) -> Embedder:
    key = e.api_key.get_secret_value() if e.api_key else None
    if e.provider == "hashing":
        from forge_embeddings.embedding import HashingEmbedder

        return HashingEmbedder(dimensions=e.dimensions)
    if e.provider == "openai":
        from forge_embeddings.embedding import OpenAIEmbedder

        if e.query_model and e.query_model != e.model:
            raise ValueError("openai embeddings must use the same model for queries and documents")
        return OpenAIEmbedder(
            api_key=key,
            model=e.model,
            dimensions=e.dimensions,
            batch_size=e.batch_size,
            concurrency=e.concurrency,
            base_url=e.base_url,
            azure_endpoint=e.azure_endpoint,
            api_version=e.api_version,
            organization=e.organization,
        )
    from forge_embeddings.embedding import VoyageEmbedder

    _require_voyage("HYBRID_EMBEDDING__PROVIDER")
    return VoyageEmbedder(
        api_key=key,
        document_model=e.model,
        query_model=e.query_model,
        dimensions=e.dimensions,
        batch_size=e.batch_size,
        concurrency=e.concurrency,
        tokenizer=tokenizer,
    )


def build_reranker(settings: ModelSettings) -> Reranker | None:
    r = settings.rerank
    if r.provider == "none":
        return None
    if r.provider == "overlap":
        from forge_embeddings.embedding import OverlapReranker

        return OverlapReranker()
    from forge_embeddings.embedding import VoyageReranker

    _require_voyage("HYBRID_RERANK__PROVIDER")
    key = r.api_key or (settings.embedding.api_key if settings.embedding.provider == "voyage" else None)
    return VoyageReranker(api_key=key.get_secret_value() if key else None, model=r.model)


def build_models(settings: ModelSettings) -> ModelClients:
    tokenizer = build_tokenizer(settings)
    embedders = {"default": build_embedder(settings.embedding, tokenizer)}
    for name, profile in settings.embedding_profiles.items():
        embedders[name] = build_embedder(profile, tokenizer)

    llm_factory = None
    if settings.llm.provider == "anthropic":

        def llm_factory() -> LLMClient:
            from forge_embeddings.llm import AnthropicLLMClient

            key = settings.llm.api_key
            return AnthropicLLMClient(model=settings.llm.model, api_key=key.get_secret_value() if key else None)

    elif settings.llm.provider == "openai":

        def llm_factory() -> LLMClient:
            from forge_embeddings.llm import OpenAILLMClient

            key = settings.llm.api_key
            return OpenAILLMClient(
                model=settings.llm.model,
                api_key=key.get_secret_value() if key else None,
                reasoning_effort=settings.llm.reasoning_effort,
            )

    return ModelClients(
        tokenizer=tokenizer,
        embedders=embedders,
        reranker=build_reranker(settings),
        llm_factory=llm_factory,
        llm_model_id=settings.llm.model,
    )
