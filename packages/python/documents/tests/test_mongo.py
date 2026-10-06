"""Atlas-specific pieces that can be verified offline: index definitions,
aggregation pipelines ($rankFusion rules), hit mapping and client-side fusion."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge_task_documents.models import ChunkKind, HybridSearchRequest, SearchFilters
from forge_task_documents.storage.mongo import MongoStorage, search_index_definition, vector_index_definition
from forge_task_documents.storage.mongo import pipelines as P

from .mongo_fake import AsyncMongoMockClient

ALLOWED_IN_FUSION = {"$search", "$vectorSearch", "$match", "$sort", "$skip", "$limit", "$sample", "$geoNear"}


def req(**kw) -> HybridSearchRequest:
    base = dict(
        tenant_ids=["t1"],
        text="refund approval limit",
        vector=[0.1, 0.2, 0.3],
        limit=20,
        per_leg_limit=40,
        num_candidates=400,
    )
    base.update(kw)
    return HybridSearchRequest(**base)


def test_rank_fusion_pipeline_obeys_selection_pipeline_rules():
    pipe = P.rank_fusion_pipeline(req(), text_index="ti", vector_index="vi")
    fusion = pipe[0]["$rankFusion"]
    legs = fusion["input"]["pipelines"]
    assert set(legs) == {"vector", "text"}
    for stages in legs.values():
        for stage in stages:
            (name,) = stage
            assert name in ALLOWED_IN_FUSION  # nothing that modifies documents inside fusion
    assert legs["text"][0]["$search"]["index"] == "ti"
    assert legs["vector"][0]["$vectorSearch"]["index"] == "vi"
    assert fusion["combination"]["weights"] == {"vector": 1.0, "text": 1.0}
    # projection / meta happen after fusion
    stage_names = [next(iter(s)) for s in pipe]
    assert stage_names == ["$rankFusion", "$limit", "$addFields", "$unset"]
    assert pipe[2]["$addFields"]["_score"] == {"$meta": "score"}
    assert "embedding" in pipe[-1]["$unset"]


def test_tenant_filter_is_on_both_legs_always():
    r = req(filters=SearchFilters(source_types=["docx"], kinds=[ChunkKind.TABLE], tags=["kb"], doc_ids=["a"]))
    vs = P.vector_stage(r, index="vi")["$vectorSearch"]
    clauses = vs["filter"]["$and"]
    assert {"tenant_id": {"$eq": "t1"}} in clauses
    assert {"kind": {"$in": ["table"]}} in clauses and {"metadata.tags": {"$in": ["kb"]}} in clauses
    assert vs["numCandidates"] == 400 and vs["limit"] == 40
    ts = P.text_stage(r, index="ti")["$search"]["compound"]
    assert ts["filter"][0] == {"equals": {"path": "tenant_id", "value": "t1"}}
    assert {"in": {"path": "doc_id", "value": ["a"]}} in ts["filter"]
    only_tenant = P.vector_stage(req(), index="vi")["$vectorSearch"]["filter"]
    assert only_tenant == {"tenant_id": {"$eq": "t1"}}


def test_only_vectors_of_the_querys_model_are_compared():
    r = req(embedding_model="text-embedding-3-large@1024")
    model = {"$match": {"embedding_model": "text-embedding-3-large@1024"}}
    fused = P.rank_fusion_pipeline(r, text_index="ti", vector_index="vi")[0]["$rankFusion"]["input"]["pipelines"]
    assert list(fused["vector"][0]) == ["$vectorSearch"] and fused["vector"][1] == model
    assert fused["text"][1] == {"$limit": 40}  # words are words, whatever the model
    alone = P.single_leg_pipeline(r, leg="vector", text_index="ti", vector_index="vi")
    assert alone[1] == model
    # No model named (a request built by hand): nothing to guard on.
    assert len(P.vector_leg(req(), index="vi")) == 1


def test_several_tenants_are_searched_together_and_only_they():
    r = req(tenant_ids=["members", "claims"], filters=SearchFilters(doc_ids=["a"]))
    clauses = P.vector_stage(r, index="vi")["$vectorSearch"]["filter"]["$and"]
    assert {"tenant_id": {"$in": ["members", "claims"]}} in clauses and {"doc_id": {"$in": ["a"]}} in clauses
    ts = P.text_stage(r, index="ti")["$search"]["compound"]
    assert ts["filter"][0] == {"in": {"path": "tenant_id", "value": ["members", "claims"]}}
    # One pipeline: both tenants' candidates are fused into one ranking.
    legs = P.rank_fusion_pipeline(r, text_index="ti", vector_index="vi")[0]["$rankFusion"]["input"]["pipelines"]
    assert legs["vector"][0]["$vectorSearch"]["filter"]["$and"][0] == {"tenant_id": {"$in": ["members", "claims"]}}
    # A search is always scoped: no tenants is not "every tenant".
    with pytest.raises(ValidationError):
        req(tenant_ids=[])


def test_text_stage_clauses():
    single = P.text_stage(req(text="refunds"), index="ti")["$search"]["compound"]["should"]
    assert not any("phrase" in c for c in single)  # phrase only for multi-word queries
    assert not any(c.get("text", {}).get("path") == "identifiers" for c in single)
    multi = P.text_stage(req(identifiers=["BILL-1042"]), index="ti")["$search"]["compound"]
    paths = [c.get("text", c.get("phrase", {})).get("path") for c in multi["should"]]
    assert {"value": "text", "multi": "exact"} in paths and "identifiers" in paths
    assert multi["minimumShouldMatch"] == 1


def test_index_definitions():
    s = search_index_definition()
    fields = s["mappings"]["fields"]
    assert s["mappings"]["dynamic"] is False
    assert fields["identifiers"]["analyzer"] == "keyword_lowercase"
    assert any(a["name"] == "keyword_lowercase" for a in s["analyzers"])
    assert fields["tenant_id"]["type"] == "token"
    assert fields["text"]["multi"]["exact"]["analyzer"] == "lucene.standard"
    v = vector_index_definition(dimensions=1024)
    vec = v["fields"][0]
    assert vec == {
        "type": "vector",
        "path": "embedding",
        "numDimensions": 1024,
        "similarity": "dotProduct",
        "quantization": "scalar",
    }
    assert {f["path"] for f in v["fields"][1:]} == {"tenant_id", "doc_id", "source_type", "kind", "metadata.tags"}
    assert all(f["type"] == "filter" for f in v["fields"][1:])


def _doc(i: int) -> dict:
    return {
        "_id": f"c{i}",
        "tenant_id": "t1",
        "doc_id": "d",
        "source_type": "docx",
        "kind": "prose",
        "ordinal": i,
        "title": "T",
        "section_path": ["S"],
        "section_text": "S",
        "section_key": "k",
        "text": f"chunk {i}",
        "token_count": 2,
        "chunker_version": "v",
        "location": {"page": 1},
    }


async def test_server_fusion_maps_hits_and_score_details():
    storage = MongoStorage(AsyncMongoMockClient(), database="db", fusion_mode="server")
    coll = storage.search._c
    coll.aggregate_results = lambda pipe: [
        {
            **_doc(1),
            "_score": 0.032,
            "_score_details": {
                "details": [{"inputPipelineName": "text", "rank": 1}, {"inputPipelineName": "vector", "rank": 3}]
            },
        },
        {**_doc(2), "_score": 0.016},
    ]
    hits = await storage.search.hybrid_search(req(score_details=True))
    assert [h.chunk.id for h in hits] == ["c1", "c2"]
    assert hits[0].score == 0.032 and hits[0].ranks == {"text": 1, "vector": 3}
    assert hits[0].chunk.location.page == 1
    (pipe,) = coll.aggregate_calls
    assert "$rankFusion" in pipe[0]
    assert pipe[3] == {"$addFields": {"_score_details": {"$meta": "scoreDetails"}}}


async def test_client_fusion_matches_rrf():
    storage = MongoStorage(AsyncMongoMockClient(), database="db", fusion_mode="client")
    coll = storage.search._c

    def results(pipe):
        if "$search" in pipe[0]:
            return [{**_doc(1), "_score": 9.0}, {**_doc(2), "_score": 5.0}]
        return [{**_doc(2), "_score": 0.9}, {**_doc(3), "_score": 0.8}]

    coll.aggregate_results = results
    hits = await storage.search.hybrid_search(req(text_weight=1.0, vector_weight=1.0))
    assert hits[0].chunk.id == "c2"  # in both legs -> fused to the top
    assert hits[0].ranks == {"text": 2, "vector": 1}
    assert abs(hits[0].score - (1 / 62 + 1 / 61)) < 1e-12
    assert len(coll.aggregate_calls) == 2


async def test_ensure_schema_is_idempotent_and_never_rebuilds_by_surprise(caplog):
    import copy

    storage = MongoStorage(AsyncMongoMockClient(), database="db")
    coll = storage.search._c
    await storage.ensure_schema(embedding_dimensions=1024, wait=True)
    assert set(coll.search_indexes) == {"chunks_text", "chunks_vector"}
    assert coll.search_indexes["chunks_vector"]["type"] == "vectorSearch"

    # Atlas echoes definitions back with defaults added and fields reordered: not a change
    v = coll.search_indexes["chunks_vector"]["latestDefinition"] = copy.deepcopy(
        coll.search_indexes["chunks_vector"]["latestDefinition"]
    )
    v["fields"].reverse()
    v["fields"][0]["extra_default"] = True
    await storage.ensure_schema(embedding_dimensions=1024)
    assert coll.search_index_updates == []

    # a real change is only logged unless explicitly allowed
    await storage.ensure_schema(embedding_dimensions=512)
    assert coll.search_index_updates == [] and "differs" in caplog.text
    await storage.ensure_schema(embedding_dimensions=512, update_existing=True)
    assert coll.search_index_updates == ["chunks_vector"]


async def test_wait_until_ready_raises_on_failed_build():
    import pytest

    from forge_tasks.errors import TaskError

    storage = MongoStorage(AsyncMongoMockClient(), database="db")
    await storage.ensure_schema(embedding_dimensions=8)
    storage.search._c.search_indexes["chunks_text"]["status"] = "FAILED"
    with pytest.raises(TaskError) as info:
        await storage.wait_until_ready(timeout_s=1, poll_s=0.01)
    assert info.value.permanent  # a failed build needs a human, not retries


async def test_falls_back_to_client_fusion_when_rank_fusion_unsupported():
    from pymongo.errors import OperationFailure

    storage = MongoStorage(AsyncMongoMockClient(), database="db", fusion_mode="server")
    coll = storage.search._c

    def results(pipe):
        if "$rankFusion" in pipe[0]:
            raise OperationFailure("Unrecognized pipeline stage name: '$rankFusion'", code=40324)
        return [{**_doc(1), "_score": 1.0}]

    coll.aggregate_results = results
    hits = await storage.search.hybrid_search(req())
    assert [h.chunk.id for h in hits] == ["c1"]
    assert storage.search.fusion_mode == "client"
