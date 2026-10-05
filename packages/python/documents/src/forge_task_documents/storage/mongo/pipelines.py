"""Pure functions that build aggregation pipelines (unit-testable without Atlas).

$rankFusion constraints (MongoDB docs): each input pipeline must be a
"selection" + "ranked" pipeline -> only $search/$vectorSearch/$match/$sort/
$skip/$limit, and it must not modify documents. Projections go AFTER fusion.
$rankFusion needs MongoDB 8.0+ (8.1+ without a support ticket).
"""

from __future__ import annotations

from typing import Any

from forge_task_documents.models import HybridSearchRequest

HIDDEN_FIELDS = ["embedding", "embed_text", "run_seq"]
MAX_NUM_CANDIDATES = 10_000  # Atlas limit


def vector_filter(req: HybridSearchRequest) -> dict[str, Any]:
    """MQL pre-filter for $vectorSearch; fields must be 'filter' type in the index."""
    clauses: list[dict[str, Any]] = [{"tenant_id": {"$eq": req.tenant_id}}]
    f = req.filters
    if f.doc_ids:
        clauses.append({"doc_id": {"$in": list(f.doc_ids)}})
    if f.source_types:
        clauses.append({"source_type": {"$in": list(f.source_types)}})
    if f.kinds:
        clauses.append({"kind": {"$in": [k.value for k in f.kinds]}})
    if f.tags:
        clauses.append({"metadata.tags": {"$in": list(f.tags)}})
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def search_filters(req: HybridSearchRequest) -> list[dict[str, Any]]:
    """Atlas Search compound.filter clauses (non-scoring)."""
    out: list[dict[str, Any]] = [{"equals": {"path": "tenant_id", "value": req.tenant_id}}]
    f = req.filters
    if f.doc_ids:
        out.append({"in": {"path": "doc_id", "value": list(f.doc_ids)}})
    if f.source_types:
        out.append({"in": {"path": "source_type", "value": list(f.source_types)}})
    if f.kinds:
        out.append({"in": {"path": "kind", "value": [k.value for k in f.kinds]}})
    if f.tags:
        out.append({"in": {"path": "metadata.tags", "value": list(f.tags)}})
    return out


def vector_stage(req: HybridSearchRequest, *, index: str, path: str = "embedding") -> dict[str, Any]:
    return {
        "$vectorSearch": {
            "index": index,
            "path": path,
            "queryVector": req.vector,
            "numCandidates": min(max(req.num_candidates, req.per_leg_limit), MAX_NUM_CANDIDATES),
            "limit": req.per_leg_limit,
            "filter": vector_filter(req),
        }
    }


def text_stage(
    req: HybridSearchRequest,
    *,
    index: str,
    section_boost: float = 2.0,
    title_boost: float = 1.5,
    context_boost: float = 0.5,
    phrase_boost: float = 3.0,
    identifier_boost: float = 5.0,
) -> dict[str, Any]:
    q = req.text
    should: list[dict[str, Any]] = [
        {"text": {"query": q, "path": "text"}},
        {"text": {"query": q, "path": "section_text", "score": {"boost": {"value": section_boost}}}},
        {"text": {"query": q, "path": "title", "score": {"boost": {"value": title_boost}}}},
        {"text": {"query": q, "path": "context", "score": {"boost": {"value": context_boost}}}},
    ]
    if len(q.split()) > 1:
        should.append(
            {
                "phrase": {
                    "query": q,
                    "path": {"value": "text", "multi": "exact"},
                    "slop": 2,
                    "score": {"boost": {"value": phrase_boost}},
                }
            }
        )
    if req.identifiers:
        should.append(
            {"text": {"query": req.identifiers, "path": "identifiers", "score": {"boost": {"value": identifier_boost}}}}
        )
    return {
        "$search": {
            "index": index,
            "compound": {"filter": search_filters(req), "should": should, "minimumShouldMatch": 1},
        }
    }


def rank_fusion_pipeline(req: HybridSearchRequest, *, text_index: str, vector_index: str) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {
            "$rankFusion": {
                "input": {
                    "pipelines": {
                        "vector": [vector_stage(req, index=vector_index)],
                        "text": [text_stage(req, index=text_index), {"$limit": req.per_leg_limit}],
                    }
                },
                "combination": {"weights": {"vector": req.vector_weight, "text": req.text_weight}},
                "scoreDetails": req.score_details,
            }
        },
        {"$limit": req.limit},
        {"$addFields": {"_score": {"$meta": "score"}}},
    ]
    if req.score_details:
        pipeline.append({"$addFields": {"_score_details": {"$meta": "scoreDetails"}}})
    pipeline.append({"$unset": HIDDEN_FIELDS})
    return pipeline


def single_leg_pipeline(
    req: HybridSearchRequest, *, leg: str, text_index: str, vector_index: str
) -> list[dict[str, Any]]:
    if leg == "vector":
        head: list[dict[str, Any]] = [vector_stage(req, index=vector_index)]
        meta = "vectorSearchScore"
    elif leg == "text":
        head = [text_stage(req, index=text_index), {"$limit": req.per_leg_limit}]
        meta = "searchScore"
    else:
        raise ValueError(f"unknown leg {leg!r}")
    return [*head, {"$addFields": {"_score": {"$meta": meta}}}, {"$unset": HIDDEN_FIELDS}]
