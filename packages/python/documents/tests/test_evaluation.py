from __future__ import annotations

from pathlib import Path

import pytest

from forge_embeddings.clients import build_models
from forge_embeddings.config import EmbeddingSettings, LLMSettings, ModelSettings, RerankSettings
from forge_task_documents.config import DocumentsSettings
from forge_task_documents.evaluation import (
    Case,
    CaseResult,
    EvidenceItem,
    Report,
    apply_overrides,
    check_labels,
    compare,
    load_suite,
    normalize,
    parse_overrides,
    run_suite,
    summary,
)
from forge_task_documents.evaluation.report import grouped, metrics

CORE = Path(__file__).parents[1] / "eval" / "suites" / "core"


def offline_models() -> ModelSettings:
    """Hashing embeddings, no reranker, no LLM: deterministic and keyless."""
    return ModelSettings(
        embedding=EmbeddingSettings(provider="hashing", dimensions=256),
        rerank=RerankSettings(provider="none"),
        llm=LLMSettings(provider="none"),
    )


def test_normalize_folds_case_space_quotes_and_dashes():
    assert normalize("90 Days’  written\n notice – NOW") == normalize("90 days' written notice - now")


def test_evidence_needs_one_passage_of_its_document_to_hold_every_string():
    item = EvidenceItem(doc="manual", contains=["EMEA", "up to 8,000 EUR"])
    assert item.satisfied_by("manual", "- EMEA\n  - Field Adjuster\n      - Approve without review: up to 8,000 EUR")
    assert not item.satisfied_by("manual", "- Approve without review: up to 8,000 EUR")  # whose limit?
    assert not item.satisfied_by("other", "EMEA up to 8,000 EUR")
    either = EvidenceItem.model_validate({"any": [{"doc": "a", "contains": ["x"]}, {"doc": "b", "contains": ["y"]}]})
    assert either.satisfied_by("b", "y") and either.docs == {"a", "b"}


def test_cases_are_validated():
    with pytest.raises(ValueError, match="needs evidence"):
        Case(id="q", question="?", answer="a")
    with pytest.raises(ValueError, match="has no evidence"):
        Case(id="q", question="?", answer="a", answerable=False, evidence=[{"doc": "d", "contains": ["x"]}])
    assert Case(id="q", question="?", answer=44.0, evidence=[{"doc": "d", "contains": [44]}]).answer == "44.0"


def test_suite_rejects_evidence_for_unknown_documents(tmp_path: Path):
    (tmp_path / "a.md").write_text("# A\n\nhello")
    (tmp_path / "suite.yaml").write_text(
        "name: t\ndocuments:\n  - {id: a, file: a.md}\nquestions:\n"
        "  - {id: q, question: 'hi?', answer: hello, evidence: [{doc: b, contains: [hello]}]}\n"
    )
    with pytest.raises(ValueError, match="unknown document 'b'"):
        load_suite(tmp_path)


def test_core_suite_loads_and_every_label_holds_in_some_chunk():
    suite = load_suite(CORE)
    assert len({Path(d.file).suffix for d in suite.documents}) == len(suite.documents) == 10  # one per format
    assert {c.kind for c in suite.cases} >= {"hierarchy", "table", "paraphrase", "multi_doc", "unanswerable"}
    # A failure here means a label is wrong, or a chunking change splits what one passage should hold.
    assert check_labels(suite, DocumentsSettings(), build_models(offline_models())) == []


def test_overrides_go_to_documents_or_model_settings():
    pairs = parse_overrides(
        ["chunking.max_tokens=300", "search.min_similarity=null", "embedding.dimensions=512", "rerank.provider=overlap"]
    )
    documents, models = apply_overrides(DocumentsSettings(), offline_models(), pairs)
    assert documents.chunking.max_tokens == 300 and documents.search.min_similarity is None
    assert models.embedding.dimensions == 512 and models.rerank.provider == "overlap"
    with pytest.raises(ValueError, match="key=value"):
        parse_overrides(["oops"])


async def test_run_scores_the_core_suite_offline(tmp_path: Path):
    suite = load_suite(CORE)
    report = await run_suite(suite, name="hashing", documents=DocumentsSettings(), model_settings=offline_models())
    assert report.overall.n + report.overall.n_unanswerable == len(suite.cases)
    assert sum(i.chunks for i in report.ingested) > len(suite.documents)
    # Hashing vectors aren't semantic, but BM25 + exact wording still find most labelled facts.
    assert report.overall.hit["10"] >= 0.6
    assert report.overall.hit["1"] <= report.overall.hit["5"] <= report.overall.hit["10"]
    assert set(report.by_format) >= {"md", "docx", "xlsx", "html", "pptx", "pdf", "csv", "txt", "mmd", "vsdx"}
    assert report.run.embedding["model_id"] == "hashing-v1@256" and report.run.backend == "memory"
    hit = next(c for c in report.cases if c.id == "rn-cc-4821")  # an identifier: found on its own words
    assert hit.first_rank == 1 and hit.retrieved[0].doc == "release-notes"

    again = Report.load(report.save(tmp_path / "r.json"))
    assert again == report
    assert "## Missed at hit@5" in summary(report) or report.overall.hit["5"] == 1.0
    same = compare(report, again)
    assert same.regressed == [] and same.improved == [] and not same.fails(0.0)


def _result(case_id: str, ranks: list[int | None], *, kind: str = "fact", answerable: bool = True) -> CaseResult:
    from forge_task_documents.evaluation.report import Retrieved

    retrieved = [] if ranks == [] else [Retrieved(rank=1, doc="d", section="", score=0.5, text="t")]
    return CaseResult(
        id=case_id,
        kind=kind,
        formats=["md"],
        answerable=answerable,
        question="?",
        evidence_ranks=ranks if answerable else [],
        doc_ranks=ranks if answerable else [],
        retrieved=retrieved,
        latency_ms=1.0,
    )


def _report(name: str, results: list[CaseResult]) -> Report:
    from forge_task_documents.evaluation.report import RunInfo, latency

    ks = (1, 5)
    return Report(
        suite="s",
        suite_fingerprint="f",
        run=RunInfo(
            name=name,
            created_at="",
            backend="memory",
            embedding={"model_id": name},
            rerank="none",
            tokenizer="heuristic",
            chunker_version="1",
            settings={},
            overrides={},
            git={},
            ks=list(ks),
            depth=10,
        ),
        ingested=[],
        overall=metrics(results, ks),
        by_kind=grouped(results, "kind", ks),
        by_format=grouped(results, "formats", ks),
        latency_ms=latency(results),
        cases=results,
    )


def test_metrics_need_every_evidence_item_within_k():
    two = _result("q", [1, 4])
    assert not two.hit(1) and two.hit(5) and two.recall(1) == 0.5 and two.rr == pytest.approx((1 + 1 / 4) / 2)
    assert _result("m", [2, None]).first_rank is None


def test_compare_reports_regressions_improvements_and_the_gate():
    baseline = _report(
        "a", [_result("kept", [1]), _result("lost", [2]), _result("won", [None]), _result("no", [], answerable=False)]
    )
    candidate = _report(
        "b", [_result("kept", [1]), _result("lost", [None]), _result("won", [3]), _result("no", [7], answerable=False)]
    )
    result = compare(baseline, candidate, k=5)
    assert result.regressed == ["lost", "no"]  # "no" stopped abstaining on a question the documents don't answer
    assert result.improved == ["won"]
    assert result.hit_delta == 0.0 and not result.fails(0.0)
    assert "embedding.model_id`: 'a' → 'b'" in result.text
    worse = compare(baseline, _report("c", [_result("kept", [None]), _result("lost", [None]), _result("won", [None])]))
    assert worse.fails(0.1)
