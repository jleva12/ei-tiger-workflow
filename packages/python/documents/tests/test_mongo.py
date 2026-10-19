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
    model = {"$match": {"live": {"$ne": False}, "embedding_model": "text-embedding-3-large@1024"}}
    fused = P.rank_fusion_pipeline(r, text_index="ti", vector_index="vi")[0]["$rankFusion"]["input"]["pipelines"]
    assert list(fused["vector"][0]) == ["$vectorSearch"] and fused["vector"][1] == model
    assert fused["text"][1:] == [
        {"$match": {"live": {"$ne": False}}},
        {"$limit": 40},
    ]  # words are words, whatever the model
    alone = P.single_leg_pipeline(r, leg="vector", text_index="ti", vector_index="vi")
    assert alone[1] == model
    # No model named (a request built by hand): only unpublished chunks are left out.
    assert P.vector_leg(req(), index="vi")[1] == {"$match": {"live": {"$ne": False}}}


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
    assert {f["path"] for f in v["fields"][1:]} == {
        "tenant_id",
        "doc_id",
        "source_type",
        "kind",
        "metadata.tags",
        "run_seq",
        "live",
    }
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


@pytest.mark.parametrize("write_errors", [[], [{"code": 11000, "errmsg": "duplicate"}]])
async def test_bulk_write_concern_failures_are_not_suppressed(monkeypatch, write_errors):
    from pymongo.errors import BulkWriteError

    from forge_task_documents.storage.mongo.schema import doc_to_chunk
    from forge_tasks.errors import TaskError

    storage = MongoStorage(AsyncMongoMockClient(), database="db")

    async def uncertain(*args, **kwargs):
        raise BulkWriteError(
            {
                "writeErrors": write_errors,
                "writeConcernErrors": [{"code": 64, "errmsg": "replication acknowledgement timed out"}],
            }
        )

    monkeypatch.setattr(storage.chunks._c, "bulk_write", uncertain)
    with pytest.raises(TaskError, match="replication acknowledgement"):
        await storage.chunks.replace_document_chunks("t1", "d", 1, [doc_to_chunk(_doc(1))])


async def test_legacy_rows_survive_reingestion_but_never_reappear_after_deletion():
    from forge_task_documents.models import DocumentRecord
    from forge_task_documents.storage.mongo.schema import doc_to_chunk

    storage = MongoStorage(AsyncMongoMockClient(), database="db")
    key = {"tenant_id": "t1", "doc_id": "d"}
    record = DocumentRecord(**key, filename="d.md", sha256="old", size_bytes=3)
    legacy = record.model_dump(mode="python", exclude={"published_run_seq"})
    legacy.update(status="ready", run_seq=1)
    await storage.documents._c.update_one(key, {"$set": legacy}, upsert=True)
    await storage.chunks._c.update_one({"_id": "c1"}, {"$set": {**_doc(1), "run_seq": 1}}, upsert=True)
    assert (await storage.documents.get("t1", "d")).published_run_seq is None
    assert (await storage.chunks.get_chunks("t1", ["c1"]))[0].text == "chunk 1"

    seq = await storage.documents.begin_run(record, expected_run_seq=1)
    replacement = doc_to_chunk({**_doc(1), "text": "new content"})
    await storage.chunks.replace_document_chunks("t1", "d", seq, [replacement])
    assert (await storage.chunks.get_chunks("t1", ["c1"]))[0].text == "chunk 1"
    assert await storage.documents.complete_run("t1", "d", seq, fields={})
    assert (await storage.chunks.get_chunks("t1", ["c1"]))[0].text == "new content"

    # Intentionally retain every physical generation, including pre-upgrade
    # rows: neither a tombstone nor explicit recreation can make them visible.
    await storage.documents.delete("t1", "d")
    await storage.documents.register(record)
    assert (await storage.documents.get("t1", "d")).published_run_seq == 0
    assert await storage.chunks.get_chunks("t1", ["c1"]) == []
    await storage.documents.register(record.model_copy(update={"doc_id": "new"}))
    assert (await storage.documents.get("t1", "new")).published_run_seq == 0


async def test_hybrid_search_uses_one_generation_snapshot_before_candidate_limits():
    from forge_task_documents.models import DocumentRecord
    from forge_task_documents.storage.mongo.schema import doc_to_chunk

    storage = MongoStorage(AsyncMongoMockClient(), database="db", fusion_mode="client")
    record = DocumentRecord(tenant_id="t1", doc_id="d", filename="d.md", sha256="a", size_bytes=1)
    first = await storage.documents.begin_run(record)
    await storage.chunks.replace_document_chunks("t1", "d", first, [doc_to_chunk(_doc(1))])
    await storage.documents.complete_run("t1", "d", first, fields={})
    second = await storage.documents.begin_run(record)
    await storage.chunks.replace_document_chunks("t1", "d", second, [doc_to_chunk(_doc(i)) for i in range(2, 10)])
    calls = 0

    def aggregate(pipe):
        nonlocal calls
        calls += 1
        filters = [stage["$match"] for stage in pipe if "$match" in stage]
        if "$vectorSearch" in pipe[0]:
            filters.append(pipe[0]["$vectorSearch"]["filter"])
        # Execute generated Mongo predicates against all retained generations.
        # Unpublished candidates cannot consume this one-result limit.
        selected = list(storage.chunks._c._c.find({"$and": filters}))[:1]
        if calls == 1:
            # Simulate publication between the concurrently issued legs.
            storage.documents._c._c.update_one(
                {"tenant_id": "t1", "doc_id": "d"}, {"$set": {"published_run_seq": second}}
            )
        return [{**row, "_score": 1.0} for row in selected]

    storage.search._c.aggregate_results = aggregate
    hits = await storage.search.hybrid_search(req(limit=1, per_leg_limit=1))
    assert calls == 2
    assert len(hits) == 1 and hits[0].chunk.id == "c1" and hits[0].generation == first
    assert hits[0].ranks == {"vector": 1, "text": 1}


async def test_registering_a_legacy_record_without_run_seq_advances_its_default_fence():
    from forge_task_documents.errors import StaleRunError
    from forge_task_documents.models import DocumentRecord

    storage = MongoStorage(AsyncMongoMockClient(), database="db")
    record = DocumentRecord(tenant_id="t1", doc_id="d", filename="d.md", sha256="a", size_bytes=1)
    legacy = record.model_dump(mode="python", exclude={"run_seq", "published_run_seq"})
    await storage.documents._c.update_one({"tenant_id": "t1", "doc_id": "d"}, {"$set": legacy}, upsert=True)
    before = await storage.documents.get("t1", "d")
    assert before.run_seq == 0
    await storage.documents.register(record)
    with pytest.raises(StaleRunError):
        await storage.documents.begin_run(record, expected_run_seq=before.run_seq)
