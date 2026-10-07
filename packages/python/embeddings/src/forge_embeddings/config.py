"""Settings of the shared model clients: ``HYBRID_EMBEDDING__*`` (the
"default" profile), ``HYBRID_EMBEDDING_PROFILES``, ``HYBRID_RERANK__*``,
``HYBRID_TOKENIZER__*`` and ``HYBRID_LLM__*``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from forge_tasks.settings import EnvSettings


class EmbeddingSettings(BaseModel):
    provider: Literal["openai", "voyage", "hashing"] = "openai"
    api_key: SecretStr | None = None  # falls back to OPENAI_API_KEY / AZURE_OPENAI_API_KEY / VOYAGE_API_KEY
    # None -> provider default: text-embedding-3-large (openai) / voyage-4 (voyage).
    # For Azure OpenAI this is the *deployment* name.
    document_model: str | None = None
    query_model: str | None = None  # voyage only (shared-space models); openai must query with the same model
    dimensions: int = 1024  # text-embedding-3-* accept 256..3072 (large) / 1536 (small)
    batch_size: int = 128
    concurrency: int = 4
    # openai-only
    base_url: str | None = None  # OpenAI-compatible gateway/proxy
    azure_endpoint: str | None = None  # set -> Azure OpenAI
    api_version: str | None = None  # Azure API version
    organization: str | None = None

    @field_validator(
        "document_model", "query_model", "base_url", "azure_endpoint", "api_version", "organization", mode="before"
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        # The apps reference the shared FORGE_EMBEDDING_* (e.g. an empty
        # FORGE_EMBEDDING_BASE_URL for OpenAI itself), so empty means unset.
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def model(self) -> str:
        if self.document_model:
            return self.document_model
        return {"openai": "text-embedding-3-large", "voyage": "voyage-4"}.get(self.provider, "hashing-v1")


class RerankSettings(BaseModel):
    # "none" = fused order as-is. "voyage" needs a Voyage key; any other reranker
    # (Cohere, a local cross-encoder, an LLM) plugs in behind the Reranker protocol.
    provider: Literal["none", "voyage", "overlap"] = "none"
    model: str = "rerank-2.5"
    api_key: SecretStr | None = None  # voyage: falls back to VOYAGE_API_KEY


class TokenizerSettings(BaseModel):
    kind: Literal["heuristic", "tiktoken", "huggingface"] = "heuristic"
    name: str | None = None  # e.g. "cl100k_base" or "voyageai/voyage-4"


class LLMSettings(BaseModel):
    provider: Literal["anthropic", "openai", "none"] = "anthropic"
    model: str = "claude-haiku-4-5-20251001"
    api_key: SecretStr | None = None  # falls back to ANTHROPIC_API_KEY or OPENAI_API_KEY
    reasoning_effort: str | None = None  # openai reasoning models, e.g. "low"


class ModelSettings(EnvSettings):
    """Every model client setting, read from the environment and ``.env``."""

    embedding: EmbeddingSettings = EmbeddingSettings()  # the "default" profile
    embedding_profiles: dict[str, EmbeddingSettings] = Field(default_factory=dict)  # extra named profiles
    rerank: RerankSettings = RerankSettings()
    tokenizer: TokenizerSettings = TokenizerSettings()
    llm: LLMSettings = LLMSettings()
