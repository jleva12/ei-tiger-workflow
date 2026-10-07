from __future__ import annotations

from types import SimpleNamespace

import pytest

from forge_embeddings.embedding import VoyageEmbedder, VoyageReranker
from forge_embeddings.errors import EmbeddingError
from forge_embeddings.tokenizers import HeuristicTokenizer
from forge_tasks.errors import TaskError


class FakeVoyage:
    def __init__(self, fail: Exception | None = None):
        self.calls = []
        self.fail = fail

    async def embed(self, texts, model, input_type, output_dimension):
        self.calls.append((list(texts), model, input_type, output_dimension))
        if self.fail:
            raise self.fail
        return SimpleNamespace(embeddings=[[float(len(t)), 1.0] for t in texts])

    async def rerank(self, query, documents, model, top_k):
        return SimpleNamespace(
            results=[SimpleNamespace(index=1, relevance_score=0.9), SimpleNamespace(index=0, relevance_score=0.2)][
                :top_k
            ]
        )


async def test_voyage_batches_preserve_order_and_use_input_types():
    client = FakeVoyage()
    emb = VoyageEmbedder(
        client=client,
        document_model="voyage-4-large",
        query_model="voyage-4-lite",
        dimensions=512,
        batch_size=3,
        tokenizer=HeuristicTokenizer(),
    )
    texts = [f"text number {i}" + "x" * i for i in range(8)]
    vectors = await emb.embed_documents(texts)
    assert [v[0] for v in vectors] == [float(len(t)) for t in texts]  # order kept across batches
    assert [len(c[0]) for c in client.calls] == [3, 3, 2]
    assert {(c[1], c[2], c[3]) for c in client.calls} == {("voyage-4-large", "document", 512)}
    await emb.embed_query("q")
    assert client.calls[-1][1:] == ("voyage-4-lite", "query", 512)
    assert emb.model_id == "voyage-4-large@512"  # dimension is part of the reuse key


async def test_voyage_errors_are_classified():
    from voyageai import error as verr

    transient = VoyageEmbedder(client=FakeVoyage(fail=verr.RateLimitError("slow down")))
    with pytest.raises(EmbeddingError):
        await transient.embed_documents(["a"])
    permanent = VoyageEmbedder(client=FakeVoyage(fail=verr.InvalidRequestError("bad")))
    with pytest.raises(TaskError) as info:
        await permanent.embed_documents(["a"])
    assert info.value.permanent


async def test_voyage_reranker():
    rr = VoyageReranker(client=FakeVoyage())
    assert await rr.rerank("q", ["a", "b"], top_k=2) == [(1, 0.9), (0, 0.2)]


# ----------------------------------------------------------------------------- OpenAI


class _Row:
    def __init__(self, index, embedding):
        self.index, self.embedding = index, embedding


class _Res:
    def __init__(self, data):
        self.data = data


class FakeOpenAI:
    """Mimics client.embeddings.create; returns rows out of order like the API may."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self.embeddings = self

    async def create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail
        dims = kw.get("dimensions", 1536)
        rows = [_Row(i, [float(len(t))] + [1.0] * (dims - 1)) for i, t in enumerate(kw["input"])]
        return _Res(list(reversed(rows)))


def _api_error(cls, status, code=None):
    import httpx

    req = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    resp = httpx.Response(status, request=req)
    return cls("boom", response=resp, body={"code": code} if code else None)


async def test_openai_batches_preserve_order_dimensions_and_unit_norm():
    from forge_embeddings.embedding import OpenAIEmbedder

    client = FakeOpenAI()
    emb = OpenAIEmbedder(client=client, model="text-embedding-3-large", dimensions=8, batch_size=2)
    texts = ["a", "bbbb", "cc", "", "ddddddd"]
    vecs = await emb.embed_documents(texts)
    assert len(client.calls) == 3 and all(
        c["dimensions"] == 8 and c["model"] == "text-embedding-3-large" for c in client.calls
    )
    assert all(abs(sum(x * x for x in v) - 1) < 1e-9 for v in vecs)  # dotProduct-safe
    # first component encodes the input length, so order survives the API's shuffled rows
    lens = [len(t) or len("(empty)") for t in texts]
    firsts = [v[0] / v[1] for v in vecs]
    assert firsts == lens  # "" was replaced (the API rejects empty strings)
    assert emb.model_id == "text-embedding-3-large@8"
    q = await emb.embed_query("hello")
    assert len(q) == 8 and client.calls[-1]["input"] == ["hello"]


async def test_openai_truncates_long_inputs_and_respects_token_budget():
    from forge_embeddings.embedding import OpenAIEmbedder

    client = FakeOpenAI()
    emb = OpenAIEmbedder(client=client, dimensions=4, max_input_tokens=100, max_batch_tokens=105)
    await emb.embed_documents(["x" * 10_000, "y" * 10, "z" * 10])
    sent = [t for c in client.calls for t in c["input"]]
    assert emb._tok.count(sent[0]) <= 100
    # truncated input (100) + "y" (5) fill the 105-token request; "z" goes in a second one
    assert [len(c["input"]) for c in client.calls] == [2, 1]


async def test_openai_ada_omits_dimensions():
    from forge_embeddings.embedding import OpenAIEmbedder

    client = FakeOpenAI()
    emb = OpenAIEmbedder(client=client, model="text-embedding-ada-002", dimensions=1536)
    await emb.embed_query("q")
    assert "dimensions" not in client.calls[0]
    with pytest.raises(ValueError):
        OpenAIEmbedder(client=client, model="text-embedding-ada-002", dimensions=1024)


async def test_openai_errors_are_classified():
    import openai

    from forge_embeddings.embedding import OpenAIEmbedder
    from forge_embeddings.errors import EmbeddingError
    from forge_tasks.errors import TaskError

    cases = [
        (_api_error(openai.RateLimitError, 429), False),
        (_api_error(openai.RateLimitError, 429, code="insufficient_quota"), True),
        (_api_error(openai.InternalServerError, 503), False),
        (_api_error(openai.BadRequestError, 400), True),
        (_api_error(openai.AuthenticationError, 401), True),
        (openai.APIConnectionError(request=__import__("httpx").Request("POST", "https://x")), False),
    ]
    for exc, permanent in cases:
        emb = OpenAIEmbedder(client=FakeOpenAI(fail=exc), dimensions=4)
        with pytest.raises(TaskError) as info:
            await emb.embed_query("q")
        assert info.value.permanent is permanent, exc
        assert isinstance(info.value, EmbeddingError) is (not permanent)


def test_factory_defaults_to_openai_and_azure(monkeypatch):
    from forge_embeddings.clients import build_embedder, build_reranker
    from forge_embeddings.config import EmbeddingSettings, ModelSettings
    from forge_embeddings.embedding import OpenAIEmbedder

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    emb = build_embedder(EmbeddingSettings())
    assert isinstance(emb, OpenAIEmbedder) and emb.model_id == "text-embedding-3-large@1024"
    assert build_reranker(ModelSettings(_env_file=None).rerank) is None  # no Voyage key needed by default

    azure = build_embedder(
        EmbeddingSettings(
            document_model="my-embed-deployment", azure_endpoint="https://acme.openai.azure.com", api_key="k"
        )
    )
    assert type(azure.client()).__name__ == "AsyncAzureOpenAI" and azure.model == "my-embed-deployment"

    with pytest.raises(ValueError):
        build_embedder(EmbeddingSettings(query_model="text-embedding-3-small"))
    assert build_embedder(EmbeddingSettings(provider="voyage")).model_id == "voyage-4@1024"


async def test_openai_without_a_key_starts_and_fails_its_jobs(monkeypatch):
    """The SDK refuses to build a client without a key; the worker must still
    start, and embedding jobs fail permanently with what to set."""
    from forge_embeddings.clients import build_embedder
    from forge_embeddings.config import EmbeddingSettings
    from forge_embeddings.embedding import OpenAIEmbedder
    from forge_tasks.errors import TaskError

    for name in ("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "OPENAI_ADMIN_KEY"):
        monkeypatch.delenv(name, raising=False)
    emb = build_embedder(EmbeddingSettings())  # builds: no client yet
    assert isinstance(emb, OpenAIEmbedder)
    with pytest.raises(TaskError, match="HYBRID_EMBEDDING__API_KEY") as info:
        await emb.embed_documents(["a"])
    assert info.value.permanent


def test_empty_shared_embedding_settings_mean_unset():
    # The apps reference the shared FORGE_EMBEDDING_*; an empty base URL is
    # OpenAI's own API, not a URL.
    from forge_embeddings.config import EmbeddingSettings

    settings = EmbeddingSettings(
        document_model="text-embedding-3-large", dimensions="1024", base_url="", azure_endpoint=" "
    )
    assert settings.base_url is None and settings.azure_endpoint is None
    assert settings.model == "text-embedding-3-large" and settings.dimensions == 1024
    assert EmbeddingSettings(document_model="").model == "text-embedding-3-large"
    assert EmbeddingSettings(base_url="https://gateway.example/v1").base_url == "https://gateway.example/v1"
