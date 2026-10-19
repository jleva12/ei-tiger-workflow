from types import SimpleNamespace
from typing import Any, cast

import pytest

from forge_task_documents.models import Chunk, ChunkKind, SearchHit, SearchQuery
from forge_task_documents.protocols import ChunkStore
from forge_task_documents.retrieval import HybridSearchService

from .conftest import source


def chunk(text: str) -> Chunk:
    return Chunk(
        id="stable-id",
        tenant_id="tenant",
        doc_id="doc",
        source_type="text",
        kind=ChunkKind.PROSE,
        ordinal=0,
        title="Title",
        section_path=[],
        section_key="section",
        text=text,
        token_count=1,
        chunker_version="test",
    )


@pytest.mark.parametrize("collected", [False, True])
async def test_section_expansion_does_not_mix_a_new_publication_with_older_hits(
    pipeline: Any,
    storage: Any,
    embedder: Any,
    monkeypatch: Any,
    collected: bool,
) -> None:
    before = b"# Policy\n\nA refund is available for thirty days after purchase."
    after = b"# Policy\n\nA refund is available for sixty days after purchase."
    await pipeline.ingest(source("policy.md", before))
    if not collected:
        # Retaining old generations must not let a lookup silently select the new one either.
        async def no_cleanup(*args: Any, **kwargs: Any) -> int:
            return 0

        monkeypatch.setattr(storage.chunks, "publish_run", no_cleanup)

    class PublishingReranker:
        async def rerank(self, query: str, documents: list[str], *, top_k: int) -> list[tuple[int, float]]:
            assert any("thirty" in document for document in documents)
            await pipeline.ingest(source("policy.md", after))
            return [(index, 1.0) for index in range(min(top_k, len(documents)))]

    service = HybridSearchService(
        search=storage.search,
        chunks=storage.chunks,
        embedder=embedder,
        reranker=PublishingReranker(),
    )
    response = await service.search(SearchQuery(tenant_ids=["t1"], text="refund", expand_sections=True))
    assert response.hits
    assert all(hit.generation is not None for hit in response.hits)
    assert all("thirty" in hit.chunk.text for hit in response.hits)
    assert all(hit.expanded_text is None or "sixty" not in hit.expanded_text for hit in response.hits)
    assert "generation" not in response.hits[0].model_dump()


async def test_expansion_cache_is_scoped_to_the_hits_generation() -> None:
    requests = []

    async def section(tenant: str, doc: str, key: str, *, generation: int | None = None) -> list[Chunk]:
        requests.append(generation)
        return [chunk(f"section from generation {generation}")]

    service = HybridSearchService(
        search=cast(Any, None),
        chunks=cast(ChunkStore, SimpleNamespace(get_section=section)),
        embedder=cast(Any, None),
    )
    hits = [SearchHit(chunk=chunk("same logical ID"), score=1, generation=seq) for seq in (1, 2, 1)]
    await service._expand(hits)
    assert requests == [1, 2]
    assert [hit.expanded_text for hit in hits] == [
        "section from generation 1",
        "section from generation 2",
        "section from generation 1",
    ]


async def test_a_legacy_or_custom_hit_without_generation_remains_unexpanded() -> None:
    async def section(*args: Any, **kwargs: Any) -> list[Chunk]:
        pytest.fail("Unknown generation must not query the latest section")

    service = HybridSearchService(
        search=cast(Any, None),
        chunks=cast(ChunkStore, SimpleNamespace(get_section=section)),
        embedder=cast(Any, None),
    )
    hit = SearchHit(chunk=chunk("original"), score=1)
    await service._expand([hit])
    assert hit.expanded_text is None and hit.chunk.text == "original"
