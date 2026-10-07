"""Knowledge bases searched for agents (``retrieval.knowledge``): only the ones
named, several ranked as one, each passage naming its document."""

from __future__ import annotations

import re

import pytest

from forge_embeddings.embedding import OverlapReranker
from forge_task_documents.config import DocumentsSettings
from forge_task_documents.models import SourceLocation
from forge_task_documents.retrieval import (
    MAX_PASSAGES,
    KnowledgeBaseSearch,
    Passage,
    citation_ref,
    describe_location,
)
from forge_task_documents.storage import open_storage

from .conftest import source

MEMBERS = {
    "Member benefits.md": b"""# Member benefits

## Dental
Members get two dental cleanings a year at no cost, and orthodontics until 19.

## Gym
Members save 20 percent at partner gyms. Bring your member card to claim it.
""",
}
CLAIMS = {
    "Appeals guide.md": b"""# Appeals

## Appealing a denied claim
To appeal a denied claim, file the appeal within 180 days of the denial.
The appeals team reviews every denied claim appeal and answers in 30 days.
""",
    "Claim denials.md": b"""# Denials

## Why a claim is denied
A claim is denied when the service isn't covered. You can appeal a denied claim.
""",
    "Filing claims.md": b"""# Filing

## Filing a claim
File a claim within 90 days. If the claim is denied, appeal the denied claim.
""",
}


@pytest.fixture
async def knowledge(pipeline, storage, embedder) -> KnowledgeBaseSearch:
    for knowledge_base, files in (("members", MEMBERS), ("claims", CLAIMS)):
        for name, data in files.items():
            await pipeline.ingest(source(name, data, tenant=knowledge_base, doc_id=f"{knowledge_base}:{name}"))
    return KnowledgeBaseSearch(storage, embedder=embedder)


async def test_only_the_named_knowledge_bases_are_searched(knowledge: KnowledgeBaseSearch) -> None:
    members = await knowledge.search(["members"], "claim", limit=10)
    assert members and {p.knowledge_base_id for p in members} == {"members"}
    claims = await knowledge.search(["claims"], "claim", limit=10)
    assert claims and {p.knowledge_base_id for p in claims} == {"claims"}
    both = await knowledge.search(["members", "claims"], "claim", limit=10)
    assert {p.knowledge_base_id for p in both} == {"members", "claims"}
    assert await knowledge.search(["another"], "claim") == []


async def test_several_knowledge_bases_are_ranked_as_one(knowledge: KnowledgeBaseSearch) -> None:
    passages = await knowledge.search(["members", "claims"], "appeal a denied claim", limit=10)
    # The claims documents all answer it, so they come first. Searched one at
    # a time and merged by score, the members' best passage (first in its own
    # knowledge base) would have tied with the claims' best.
    assert [p.knowledge_base_id for p in passages[:3]] == ["claims"] * 3


async def test_passages_name_their_documents_and_leave_out_deleted_ones(
    knowledge: KnowledgeBaseSearch, storage
) -> None:
    passages = await knowledge.search(["claims"], "appeal a denied claim", limit=10)
    assert {p.document for p in passages} == set(CLAIMS)
    first = passages[0]
    assert first.document_id.startswith("claims:") and first.chunk_id and first.text
    # Markdown is cited by its lines.
    assert first.location.startswith("line")
    assert first.for_agent(knowledge_base="Claims knowledge") == {
        "ref": first.ref,
        "knowledge_base": "Claims knowledge",
        "knowledge_base_id": "claims",
        "document": first.filename,
        "document_id": first.document_id,
        "section": " > ".join(first.section_path),
        "location": first.location,
        "text": first.text,
        "score": round(first.score, 4),
    }
    # The worker deleted it, but its chunks haven't gone yet.
    await storage.documents.delete("claims", "claims:Appeals guide.md")
    later = await knowledge.search(["claims"], "appeal a denied claim", limit=10)
    assert later and "Appeals guide.md" not in {p.document for p in later}
    # It was the best match; the next ones take its place, so a search still
    # answers as many as it was asked for.
    assert len(await knowledge.search(["claims"], "appeal a denied claim", limit=2)) == 2


async def test_limits_and_document_filters(knowledge: KnowledgeBaseSearch) -> None:
    assert len(await knowledge.search(["members", "claims"], "claim", limit=1)) == 1
    assert len(await knowledge.search(["members", "claims"], "claim", limit=500)) <= MAX_PASSAGES
    only = await knowledge.search(["claims"], "claim", document_ids=["claims:Filing claims.md"])
    assert only and {p.document for p in only} == {"Filing claims.md"}
    with pytest.raises(ValueError, match="Name the knowledge bases"):
        await knowledge.search([], "claim")


async def test_a_reranker_scores_the_passages(storage, embedder, knowledge: KnowledgeBaseSearch) -> None:
    reranked = KnowledgeBaseSearch(storage, embedder=embedder, reranker=OverlapReranker())
    passages = await reranked.search(["members", "claims"], "appeal a denied claim", limit=3)
    assert len(passages) == 3 and all(p.knowledge_base_id == "claims" for p in passages)


async def test_a_question_they_dont_answer_finds_nothing(knowledge: KnowledgeBaseSearch) -> None:
    # The vector leg always has nearest neighbours; none is near enough, and
    # no passage shares a word with the question.
    assert await knowledge.search(["members", "claims"], "weather forecast for Paris tomorrow") == []
    assert await knowledge.search(["claims"], "member card") == []


async def test_passages_score_their_relevance(knowledge: KnowledgeBaseSearch) -> None:
    passages = await knowledge.search(["claims"], "appeal a denied claim", limit=3)
    # Not the fused rank score (~0.03 for every first passage): how close its
    # meaning is to the question's, -1 to 1.
    assert all(0.2 < p.score <= 1 for p in passages)


async def test_each_passage_has_its_own_ref_in_every_search(knowledge: KnowledgeBaseSearch) -> None:
    first = await knowledge.search(["members", "claims"], "appeal a denied claim", limit=10)
    again = await knowledge.search(["claims"], "denied claim appeal", limit=10)
    refs = {p.chunk_id: p.ref for p in first}
    assert len(set(refs.values())) == len(refs)  # one each
    # The same passage, found again by another search: the same ref.
    assert all(refs[p.chunk_id] == p.ref for p in again if p.chunk_id in refs)
    # As chat UIs read citations: [KQM4821], or several as [KQM4821, BTR0042].
    assert all(re.fullmatch(r"[A-Z]{1,3}\d{1,4}", ref) for ref in refs.values())


def test_refs_are_stable_and_rarely_collide() -> None:
    assert citation_ref("chunk-1") == citation_ref("chunk-1")
    refs = {citation_ref(f"chunk-{i}") for i in range(2000)}
    assert len(refs) >= 1999  # 26^3 x 10^4 of them


@pytest.mark.parametrize(
    ("source_type", "location", "described"),
    [
        ("pdf", SourceLocation(page=4), "page 4"),
        ("pptx", SourceLocation(page=3, page_name="Pricing"), "slide 3: Pricing"),
        ("visio", SourceLocation(page=2, page_name="Order flow"), "page 2: Order flow"),
        ("xlsx", SourceLocation(sheet="Prices", cell_range="A1:D20"), "sheet Prices, A1:D20"),
        ("markdown", SourceLocation(line_start=12, line_end=30), "lines 12–30"),
        ("text", SourceLocation(line_start=7, line_end=7), "line 7"),
        ("docx", SourceLocation(), ""),
    ],
)
def test_where_a_passage_is_in_words(source_type: str, location: SourceLocation, described: str) -> None:
    assert describe_location(source_type, location) == described


def test_a_passage_without_its_record_goes_by_its_title() -> None:
    passage = Passage(chunk_id="c", document_id="d", title="Appeals", section_path=["Appeals", "Denied"])
    assert passage.document == "Appeals"
    assert passage.for_agent(knowledge_base="Claims", document="appeals.pdf")["document"] == "appeals.pdf"
    assert passage.for_agent(knowledge_base="Claims")["section"] == "Appeals > Denied"


async def test_open_storage_follows_the_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    from forge_task_documents.storage.mongo import MongoStorage

    monkeypatch.delenv("FORGE_VECTOR_STORE", raising=False)
    settings = DocumentsSettings()
    assert open_storage(settings, mongo_uri=None, mongo_database="kb") is None
    mongo = open_storage(settings, mongo_uri="mongodb://localhost:1", mongo_database="kb")
    assert isinstance(mongo, MongoStorage)
    await mongo.close()
    with pytest.raises(ValueError, match="outside the worker"):
        open_storage(DocumentsSettings(storage_backend="memory"), mongo_uri=None, mongo_database="kb")
    # The selection wins over the settings, as in the worker.
    monkeypatch.setenv("FORGE_VECTOR_STORE", "mongo")
    assert isinstance(
        mongo := open_storage(
            DocumentsSettings(storage_backend="memory"), mongo_uri="mongodb://localhost:1", mongo_database="kb"
        ),
        MongoStorage,
    )
    await mongo.close()
