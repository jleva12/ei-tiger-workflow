from __future__ import annotations

from forge_embeddings.identifiers import IdentifierExtractor
from forge_task_documents.enrichment import (
    BreadcrumbEnricher,
    CompositeEnricher,
    IdentifierEnricher,
    LLMContextEnricher,
)
from forge_task_documents.models import Element, ElementKind, ParsedDocument


def test_identifier_extraction():
    text = (
        "See BILL-1042 and INC-000042. Part AB12-XY34, SKU12345, error E4012, 0x1F3A, "
        "released in v2.3.1; call issue_refund() or getUserById on app.services.search. The year 2024 is not an id."
    )
    ids = IdentifierExtractor().extract(text)
    for expected in [
        "BILL-1042",
        "INC-000042",
        "AB12-XY34",
        "SKU12345",
        "E4012",
        "0x1F3A",
        "v2.3.1",
        "issue_refund",
        "getUserById",
        "app.services.search",
    ]:
        assert expected in ids, expected
    assert "2024" not in ids and "The" not in ids


def _doc(chunks_text: list[str]):
    doc = ParsedDocument(
        tenant_id="t",
        doc_id="d",
        source_type="md",
        title="Billing runbook",
        elements=[
            Element(kind=ElementKind.PARAGRAPH, text=t, section_path=["Refunds", f"Part {i}"])
            for i, t in enumerate(chunks_text)
        ],
        parser_name="x",
        parser_version="1",
    )
    from forge_embeddings.tokenizers import HeuristicTokenizer
    from forge_task_documents.chunking import ChunkEngine, ChunkingConfig

    chunks = ChunkEngine(HeuristicTokenizer(), ChunkingConfig(max_tokens=40, min_tokens=0, merge_peers=False)).chunk(
        doc
    )
    return doc, chunks


class FakeLLM:
    model_id = "fake"

    def __init__(self, fail_on: str | None = None):
        self.calls = []
        self.fail_on = fail_on

    async def complete(self, *, system, user, max_tokens=200, cache_system=True, temperature=None):
        self.calls.append((system, user, cache_system))
        if self.fail_on and self.fail_on in user:
            raise RuntimeError("boom")
        return "This chunk is about refund approvals in the billing runbook."


async def test_composite_enrichment_builds_embed_text():
    doc, chunks = _doc(["Refunds above 500 need approval via BILL-1042.", "Second paragraph that will fail."])
    llm = FakeLLM(fail_on="fail")
    enricher = CompositeEnricher([LLMContextEnricher(llm), BreadcrumbEnricher(), IdentifierEnricher()])
    out = await enricher.enrich(doc, chunks)
    first, second = out
    assert first.context.startswith("This chunk is about")
    assert first.embed_text.startswith("Document: Billing runbook\nSection: Refunds > Part 0\nContext: This chunk")
    assert first.embed_text.endswith(first.text)
    assert first.text == "Refunds above 500 need approval via BILL-1042."  # raw text untouched
    assert "BILL-1042" in first.identifiers
    assert second.context is None  # failure logged, not raised
    assert all(cache for _, _, cache in llm.calls)  # document prompt is cached
    assert "<document>" in llm.calls[0][0]
