"""What a run records and how it's scored.

Per answerable question, over the passages the search answered (best first):

- ``hit@k``: every evidence item is in the top k passages, so an agent
  reading k passages has all it needs.
- ``recall@k``: the share of its evidence items in the top k.
- ``mrr``: the mean, over its evidence items, of 1 / the rank of the first
  passage holding it (0 when none of the passages does).
- ``doc@k``: every document its evidence comes from has a passage in the
  top k: told apart from ``hit@k``, a miss is either the wrong document or
  the right document's wrong (or too fragmentary) passage.
- ``empty``: the search answered nothing (the relevance floor cut all).

Per unanswerable question: ``abstained``, the search answered nothing.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from forge_task_documents.evaluation.suite import Case

DEFAULT_KS = (1, 3, 5, 10)


class Retrieved(BaseModel):
    """One passage the search answered, as the report keeps it."""

    rank: int
    doc: str
    section: str
    score: float
    text: str  # the start of it


class AnswerGrade(BaseModel):
    answer: str
    verdict: str  # correct | partial | incorrect
    reason: str = ""


class CaseResult(BaseModel):
    id: str
    kind: str
    formats: list[str]
    answerable: bool
    question: str
    # Per evidence item, the rank of the first passage holding it (None: no passage does).
    evidence_ranks: list[int | None] = Field(default_factory=list)
    # Per evidence item's documents, the rank of their first passage.
    doc_ranks: list[int | None] = Field(default_factory=list)
    retrieved: list[Retrieved] = Field(default_factory=list)
    latency_ms: float
    answer: AnswerGrade | None = None

    @property
    def abstained(self) -> bool:
        return not self.retrieved

    def hit(self, k: int) -> bool:
        return bool(self.evidence_ranks) and all(r is not None and r <= k for r in self.evidence_ranks)

    def recall(self, k: int) -> float:
        if not self.evidence_ranks:
            return 0.0
        return sum(1 for r in self.evidence_ranks if r is not None and r <= k) / len(self.evidence_ranks)

    def doc_hit(self, k: int) -> bool:
        return bool(self.doc_ranks) and all(r is not None and r <= k for r in self.doc_ranks)

    @property
    def rr(self) -> float:
        if not self.evidence_ranks:
            return 0.0
        return sum(1 / r for r in self.evidence_ranks if r is not None) / len(self.evidence_ranks)

    @property
    def first_rank(self) -> int | None:
        """The rank by which all its evidence is in: the passages an agent must read."""
        if not self.evidence_ranks or any(r is None for r in self.evidence_ranks):
            return None
        return max(r for r in self.evidence_ranks if r is not None)


class Metrics(BaseModel):
    n: int = 0
    hit: dict[str, float] = Field(default_factory=dict)
    recall: dict[str, float] = Field(default_factory=dict)
    doc: dict[str, float] = Field(default_factory=dict)
    mrr: float = 0.0
    empty: float = 0.0
    # Unanswerable questions.
    n_unanswerable: int = 0
    abstained: float | None = None
    # Answer grading (--answers): correct = 1, partial = 0.5.
    answer_score: float | None = None
    answers_correct: float | None = None


class Ingested(BaseModel):
    doc: str
    file: str
    chunks: int
    ms: float


class RunInfo(BaseModel):
    name: str
    created_at: str
    backend: str
    embedding: dict[str, Any]
    rerank: str
    tokenizer: str
    chunker_version: str
    settings: dict[str, Any]  # the documents settings that shape ingest and search
    overrides: dict[str, Any]
    git: dict[str, Any]
    ks: list[int]
    depth: int
    answers_model: str | None = None


class Report(BaseModel):
    suite: str
    suite_fingerprint: str
    run: RunInfo
    ingested: list[Ingested]
    overall: Metrics
    by_kind: dict[str, Metrics]
    by_format: dict[str, Metrics]
    latency_ms: dict[str, float]
    cases: list[CaseResult]

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n")
        return path

    @classmethod
    def load(cls, path: str | Path) -> Report:
        return cls.model_validate_json(Path(path).read_text())


def score(case: Case, doc_ids: list[str], texts: list[str]) -> tuple[list[int | None], list[int | None]]:
    """Where each evidence item, and its documents, first appear in the answered passages."""
    evidence_ranks: list[int | None] = []
    doc_ranks: list[int | None] = []
    for item in case.evidence:
        evidence_ranks.append(
            next((i for i, (d, t) in enumerate(zip(doc_ids, texts, strict=True), 1) if item.satisfied_by(d, t)), None)
        )
        doc_ranks.append(next((i for i, d in enumerate(doc_ids, 1) if d in item.docs), None))
    return evidence_ranks, doc_ranks


def metrics(results: Iterable[CaseResult], ks: Iterable[int]) -> Metrics:
    results = list(results)
    ks = list(ks)
    answerable = [r for r in results if r.answerable]
    unanswerable = [r for r in results if not r.answerable]
    out = Metrics(n=len(answerable), n_unanswerable=len(unanswerable))
    if answerable:
        n = len(answerable)
        out.hit = {str(k): round(sum(r.hit(k) for r in answerable) / n, 4) for k in ks}
        out.recall = {str(k): round(sum(r.recall(k) for r in answerable) / n, 4) for k in ks}
        out.doc = {str(k): round(sum(r.doc_hit(k) for r in answerable) / n, 4) for k in ks}
        out.mrr = round(sum(r.rr for r in answerable) / n, 4)
        out.empty = round(sum(r.abstained for r in answerable) / n, 4)
    if unanswerable:
        out.abstained = round(sum(r.abstained for r in unanswerable) / len(unanswerable), 4)
    graded = [r.answer for r in results if r.answer is not None]
    if graded:
        points = {"correct": 1.0, "partial": 0.5}
        out.answer_score = round(sum(points.get(a.verdict, 0.0) for a in graded) / len(graded), 4)
        out.answers_correct = round(sum(a.verdict == "correct" for a in graded) / len(graded), 4)
    return out


def grouped(results: list[CaseResult], key: str, ks: Iterable[int]) -> dict[str, Metrics]:
    """Metrics per question kind (``key="kind"``) or per document format (``"formats"``; a question
    across two formats counts in both)."""
    groups: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        values = (r.formats or ["(none)"]) if key == "formats" else [r.kind]
        for value in values:
            groups[value].append(r)
    return {name: metrics(group, ks) for name, group in sorted(groups.items())}


def latency(results: list[CaseResult]) -> dict[str, float]:
    times = sorted(r.latency_ms for r in results)
    if not times:
        return {}
    p95 = times[min(len(times) - 1, round(0.95 * (len(times) - 1)))]
    return {"mean": round(statistics.fmean(times), 1), "p50": round(statistics.median(times), 1), "p95": round(p95, 1)}
