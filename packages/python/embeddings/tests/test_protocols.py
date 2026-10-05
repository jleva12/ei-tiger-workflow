"""Every shipped model client structurally satisfies its protocol."""

from __future__ import annotations

import pytest

from forge_embeddings import protocols as P
from forge_embeddings.embedding import (
    HashingEmbedder,
    OpenAIEmbedder,
    OverlapReranker,
    VoyageEmbedder,
    VoyageReranker,
)
from forge_embeddings.llm import AnthropicLLMClient, LazyLLMClient
from forge_embeddings.tokenizers import HeuristicTokenizer


def test_model_clients():
    class Dummy:
        model_id = "d"

        async def complete(self, *, system, user, max_tokens=200, cache_system=True, temperature=None):
            return ""

    assert isinstance(HeuristicTokenizer(), P.Tokenizer)
    assert isinstance(Dummy(), P.LLMClient)
    assert isinstance(LazyLLMClient(Dummy, model_id="d"), P.LLMClient)
    assert isinstance(HashingEmbedder(), P.Embedder)
    assert isinstance(VoyageEmbedder(client=object()), P.Embedder)
    assert isinstance(OpenAIEmbedder(client=object()), P.Embedder)
    assert isinstance(OverlapReranker(), P.Reranker)
    assert isinstance(VoyageReranker(client=object()), P.Reranker)
    pytest.importorskip("anthropic")
    assert isinstance(AnthropicLLMClient(api_key="x"), P.LLMClient)


def test_model_clients_are_shared_by_the_process():
    from forge_embeddings.clients import MODELS_RESOURCE, build_models, model_clients
    from forge_embeddings.config import ModelSettings
    from forge_tasks.settings import CoreSettings
    from forge_tasks.tasks import TaskContext

    models = build_models(ModelSettings(_env_file=None, embedding={"provider": "hashing", "dimensions": 8}))  # type: ignore[call-arg]
    ctx = TaskContext(settings=CoreSettings(_env_file=None), resources={MODELS_RESOURCE: models})  # type: ignore[call-arg]
    assert model_clients(ctx) is models
