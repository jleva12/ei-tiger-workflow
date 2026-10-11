"""Runs a suite through the real ingest pipeline and the knowledge bases'
search, with one set of settings, and scores it.

Each run ingests the suite's documents afresh into a knowledge base of its
own (``eval-<suite>-<run>``): parsed, chunked, enriched and embedded as the
worker would with these settings, then asks every question through
:class:`~forge_task_documents.retrieval.KnowledgeBaseSearch`, the search
agents use. The ``memory`` backend keeps it all in this process (any
embedding model, nothing left behind; its BM25 approximates Atlas Search's);
``configured`` uses the worker's storage (``HYBRID_DOCUMENTS__STORAGE_BACKEND``,
Atlas Search and Vector Search as deployed: its vector index's dimensions)
and deletes the run's documents afterwards unless asked to keep them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from forge_embeddings.clients import build_models
from forge_embeddings.config import ModelSettings
from forge_task_documents.chunking import ChunkEngine
from forge_task_documents.config import DocumentsSettings
from forge_task_documents.evaluation.report import (
    DEFAULT_KS,
    CaseResult,
    Ingested,
    Report,
    Retrieved,
    RunInfo,
    grouped,
    latency,
    metrics,
    score,
)
from forge_task_documents.evaluation.suite import Case, Suite
from forge_task_documents.models import HybridSearchRequest, IngestStatus, SearchFilters, SourceFile
from forge_task_documents.retrieval import KnowledgeBaseSearch
from forge_task_documents.storage.memory import InMemoryStorage
from forge_task_documents.task import DocumentsTaskFactory, build_pipeline

if TYPE_CHECKING:
    from forge_embeddings.clients import ModelClients
    from forge_task_documents.protocols import StorageBackend
    from forge_task_documents.retrieval import Passage

log = logging.getLogger(__name__)

Backend = Literal["memory", "configured"]
MODEL_SECTIONS = ("embedding", "embedding_profiles", "rerank", "tokenizer", "llm")
SNIPPET_CHARS = 240
ANSWER_CONCURRENCY = 8


def passage_text(section: str, text: str) -> str:
    """What a label is matched against: the passage's section and text, as an agent reads them."""
    return f"{section}\n{text}" if section else text


# ----------------------------------------------------------------------------- settings


def parse_overrides(pairs: list[str]) -> dict[str, Any]:
    """``key=value`` pairs (``search.vector_weight=0.5``); a value is JSON when it parses as JSON."""
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"expected key=value, got {pair!r}")
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        out[key.strip()] = value
    return out


def _set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    node = data
    *parents, leaf = dotted.split(".")
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[leaf] = value


def apply_overrides(
    documents: DocumentsSettings,
    models: ModelSettings,
    overrides: Mapping[str, Any],
    *,
    profile: str | None = None,
) -> tuple[DocumentsSettings, ModelSettings]:
    """The settings with ``overrides`` applied: ``embedding.*``, ``rerank.*``,
    ``tokenizer.*`` and ``llm.*`` keys change the model settings, every other
    key the documents settings (``chunking.max_tokens``, ``search.min_similarity``).
    ``profile`` embeds with that named embedding profile (``HYBRID_EMBEDDING_PROFILES``)."""
    doc_data = documents.model_dump()
    model_data = models.model_dump()
    if profile:
        if profile not in models.embedding_profiles:
            raise ValueError(f"no embedding profile {profile!r}; configured: {sorted(models.embedding_profiles)}")
        model_data["embedding"] = models.embedding_profiles[profile].model_dump()
    for key, value in overrides.items():
        target = model_data if key.split(".", 1)[0] in MODEL_SECTIONS else doc_data
        _set_path(target, key, value)
    return DocumentsSettings.model_validate(doc_data), ModelSettings.model_validate(model_data)


def describe_embedding(models: ModelSettings, embedder_id: str) -> dict[str, Any]:
    e = models.embedding
    return {
        "provider": e.provider,
        "model": e.model,
        "model_id": embedder_id,
        "dimensions": e.dimensions,
        "base_url": e.base_url,
        "azure_endpoint": e.azure_endpoint,
    }


def git_state(cwd: Any) -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    return {"commit": run("rev-parse", "--short", "HEAD") or None, "dirty": bool(run("status", "--porcelain"))}


# ----------------------------------------------------------------------------- labels


def check_labels(suite: Suite, documents: DocumentsSettings, models: ModelClients) -> list[str]:
    """The labels no chunk can satisfy with these settings: a label that's
    wrong, or chunking that splits what one passage should hold. Parses and
    chunks only (no embedding)."""
    registry = DocumentsTaskFactory._registry(documents)
    chunker = ChunkEngine(models.tokenizer, documents.chunking)
    passages: dict[str, list[str]] = {}
    for doc in suite.documents:
        source = SourceFile(
            tenant_id="labels", doc_id=doc.id, filename=suite.path(doc).name, data=suite.path(doc).read_bytes()
        )
        parsed = registry.resolve(source).parse(source)
        passages[doc.id] = [passage_text(c.section_text, c.text) for c in chunker.chunk(parsed)]
    problems: list[str] = []
    for case in suite.cases:
        for item in case.evidence:
            if not any(item.satisfied_by(doc, text) for doc in item.docs for text in passages.get(doc, [])):
                wanted = " | ".join(f"{o.doc}: {o.contains}" for o in item.options)
                problems.append(f"{case.id}: no passage holds {wanted}")
    return problems


# ----------------------------------------------------------------------------- run


async def _wait_searchable(
    storage: StorageBackend,
    tenant: str,
    expected: Mapping[str, tuple[int, str]],
    embed: Callable[[str], Any],
    *,
    timeout_s: float = 90.0,
) -> None:
    """Wait for a search index that updates after the write (Atlas) to hold
    every chunk of the run, on both legs: each document's chunks, found by its title."""
    deadline = time.monotonic() + timeout_s
    pending = dict(expected)
    while pending and time.monotonic() < deadline:
        for doc_id, (count, title) in list(pending.items()):
            vector = await embed(title)
            request = HybridSearchRequest(
                tenant_ids=[tenant],
                text=title,
                vector=vector,
                filters=SearchFilters(doc_ids=[doc_id]),
                limit=count + 5,
                per_leg_limit=count + 5,
                num_candidates=max(100, 10 * (count + 5)),
            )
            text_hits, vector_hits = await asyncio.gather(
                storage.search.text_search(request), storage.search.vector_search(request)
            )
            if len(text_hits) >= count and len(vector_hits) >= count:
                pending.pop(doc_id)
        if pending:
            await asyncio.sleep(1.0)
    if pending:
        log.warning("search index still missing chunks of %s after %.0fs", sorted(pending), timeout_s)


async def _grade_answers(
    models: ModelClients, suite: Suite, results: list[CaseResult], passages: list[list[Passage]]
) -> None:
    """Each question answered from its passages and graded, ``ANSWER_CONCURRENCY`` at a time."""
    from forge_task_documents.evaluation.answers import grade_answer

    llm = models.require_llm()
    slots = asyncio.Semaphore(ANSWER_CONCURRENCY)

    async def one(result: CaseResult, case: Case, found: list[Passage]) -> None:
        async with slots:
            result.answer = await grade_answer(llm, case, found)

    await asyncio.gather(*(one(r, c, p) for r, c, p in zip(results, suite.cases, passages, strict=True)))


async def run_suite(
    suite: Suite,
    *,
    name: str,
    documents: DocumentsSettings,
    model_settings: ModelSettings,
    overrides: Mapping[str, Any] | None = None,
    models: ModelClients | None = None,
    backend: Backend = "memory",
    storage: StorageBackend | None = None,
    ks: tuple[int, ...] = DEFAULT_KS,
    depth: int | None = None,
    answers: bool = False,
    answer_k: int = 5,
    keep: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Report:
    """
    Ingest the suite and ask its questions, with these settings.

    :param models: Prebuilt model clients (a test's); otherwise built from ``model_settings``.
    :param storage: The ``configured`` backend's storage (the worker's); ``memory`` makes its own.
    :param depth: Passages asked for per question (the most any k needs, by default).
    :param answers: Also have the LLM answer from the top ``answer_k`` passages, and grade it.
    :param keep: Leave the run's documents in ``configured`` storage.
    """
    say = progress or (lambda _msg: None)
    models = models or build_models(model_settings)
    embedder = models.embedder("default")
    depth = depth or max(ks)
    if backend == "memory" or storage is None:
        storage, backend = InMemoryStorage(), "memory"
    tenant = f"eval-{suite.name}-{uuid.uuid4().hex[:8]}"
    pipeline = build_pipeline(documents, models, storage, embedder=embedder)
    search = KnowledgeBaseSearch(storage, embedder=embedder, reranker=models.reranker, config=documents.search)

    ingested: list[Ingested] = []
    expected: dict[str, tuple[int, str]] = {}
    try:
        for doc in suite.documents:
            path = suite.path(doc)
            source = SourceFile(tenant_id=tenant, doc_id=doc.id, filename=path.name, data=path.read_bytes())
            started = time.perf_counter()
            result = await pipeline.ingest(source, force=True)
            if result.status is not IngestStatus.READY:
                raise RuntimeError(f"ingesting {doc.file} failed: {result.error or result.reason}")
            ingested.append(
                Ingested(
                    doc=doc.id,
                    file=doc.file,
                    chunks=result.chunk_count,
                    ms=round((time.perf_counter() - started) * 1000, 1),
                )
            )
            title = pipeline.registry.resolve(source).parse(source).title
            expected[doc.id] = (result.chunk_count, title)
            say(f"ingested {doc.file}: {result.chunk_count} chunks")
        if backend != "memory":
            say("waiting for the search index")
            await _wait_searchable(storage, tenant, expected, embedder.embed_query)

        results: list[CaseResult] = []
        answered: list[list[Passage]] = []
        for case in suite.cases:
            started = time.perf_counter()
            passages = await search.search([tenant], case.question, limit=depth)
            elapsed = (time.perf_counter() - started) * 1000
            doc_ids = [p.document_id for p in passages]
            texts = [passage_text(" > ".join(p.section_path), p.text) for p in passages]
            evidence_ranks, doc_ranks = score(case, doc_ids, texts)
            results.append(
                CaseResult(
                    id=case.id,
                    kind=case.kind,
                    formats=suite.formats(case),
                    answerable=case.answerable,
                    question=case.question,
                    evidence_ranks=evidence_ranks,
                    doc_ranks=doc_ranks,
                    retrieved=[
                        Retrieved(
                            rank=i,
                            doc=p.document_id,
                            section=" > ".join(p.section_path),
                            score=round(p.score, 4),
                            text=p.text[:SNIPPET_CHARS],
                        )
                        for i, p in enumerate(passages, 1)
                    ],
                    latency_ms=round(elapsed, 1),
                )
            )
            answered.append(passages[:answer_k])
        say(f"asked {len(results)} questions")
        if answers:
            say("grading answers")
            await _grade_answers(models, suite, results, answered)
    finally:
        if backend != "memory" and not keep:
            for doc in suite.documents:
                await pipeline.delete(tenant, doc.id)

    return Report(
        suite=suite.name,
        suite_fingerprint=suite.fingerprint,
        run=RunInfo(
            name=name,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            backend=backend,
            embedding=describe_embedding(model_settings, embedder.model_id),
            rerank=model_settings.rerank.provider,
            tokenizer=model_settings.tokenizer.kind,
            chunker_version=pipeline.chunker.version,
            settings=documents.model_dump(mode="json"),
            overrides=dict(overrides or {}),
            git=git_state(suite.root),
            ks=list(ks),
            depth=depth,
            answers_model=models.llm_model_id if answers else None,
        ),
        ingested=ingested,
        overall=metrics(results, ks),
        by_kind=grouped(results, "kind", ks),
        by_format=grouped(results, "formats", ks),
        latency_ms=latency(results),
        cases=results,
    )
