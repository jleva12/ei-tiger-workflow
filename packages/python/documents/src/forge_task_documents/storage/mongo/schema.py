"""MongoDB Atlas schema: collections, B-tree indexes, Atlas Search and Vector
Search index definitions, and document <-> model mapping.

documents collection (one per source file)
-----------------------------------------
{
  _id: ObjectId,
  tenant_id: str, doc_id: str,              # unique together
  filename, media_type, source_uri, sha256, size_bytes,
  title, source_type,
  status: "pending"|"processing"|"ready"|"failed", error,
  chunk_count, parser_name, parser_version, chunker_version, embedding_model,
  run_seq: int,                             # fencing token, only ever increases
  status: ... | "deleted",                  # deletes are tombstones (keep run_seq)
  metadata: {...},                          # your app's fields (tags, acl, ...)
  created_at, updated_at, ingested_at
}

chunks collection (one per chunk; both BM25 and vector search run here)
----------------------------------------------------------------------
{
  _id: str,                                 # deterministic chunk id
  tenant_id, doc_id, source_type,
  kind: "prose"|"code"|"table"|"table_summary"|"graph"|"graph_summary",
  ordinal: int,                             # order within the document
  title, section_path: [str], section_text: "A > B", section_key,
  text,                                     # BM25 + display
  embed_text,                               # exactly what was embedded
  context,                                  # optional LLM context
  identifiers: [str],                       # exact-match field
  token_count, content_hash,                # hash(embed_text, model) -> reuse
  location: {page, page_name, sheet, cell_range, line_start, line_end, shape_ids},
  metadata: {tags: [...], ...},
  chunker_version, embedding_model,
  embedding: BinData(vector float32) | [double],
  run_seq,                                  # fencing token of the run that wrote it
  updated_at
}
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pymongo import ASCENDING, DESCENDING, IndexModel

from forge_embeddings.vectors import decode_vector, encode_vector
from forge_task_documents.models import Chunk, DocumentRecord, SourceLocation, utcnow

DOCUMENT_INDEXES: list[IndexModel] = [
    IndexModel([("tenant_id", ASCENDING), ("doc_id", ASCENDING)], unique=True, name="tenant_doc_unique"),
    IndexModel(
        [("tenant_id", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING)],
        name="tenant_status_updated",
    ),
    IndexModel([("tenant_id", ASCENDING), ("sha256", ASCENDING)], name="tenant_sha256"),
]

CHUNK_INDEXES: list[IndexModel] = [
    IndexModel([("tenant_id", ASCENDING), ("doc_id", ASCENDING), ("ordinal", ASCENDING)], name="tenant_doc_ordinal"),
    IndexModel(
        [("tenant_id", ASCENDING), ("doc_id", ASCENDING), ("section_key", ASCENDING), ("ordinal", ASCENDING)],
        name="tenant_doc_section",
    ),
    IndexModel([("tenant_id", ASCENDING), ("content_hash", ASCENDING)], name="tenant_content_hash"),
    IndexModel([("tenant_id", ASCENDING), ("doc_id", ASCENDING), ("run_seq", ASCENDING)], name="tenant_doc_run"),
]

# Fields that exist on every chunk and can be filtered in both search legs.
FILTER_FIELDS = ("tenant_id", "doc_id", "source_type", "kind", "metadata.tags")


def search_index_definition(*, language_analyzer: str = "lucene.english") -> dict[str, Any]:
    """Atlas Search (BM25) index on the chunks collection."""
    return {
        "analyzer": language_analyzer,
        "searchAnalyzer": language_analyzer,
        "mappings": {
            "dynamic": False,
            "fields": {
                "text": {
                    "type": "string",
                    "analyzer": language_analyzer,
                    # standard analyzer (no stemming) for phrase / exact wording
                    "multi": {"exact": {"type": "string", "analyzer": "lucene.standard"}},
                },
                "section_text": {"type": "string", "analyzer": language_analyzer},
                "title": {"type": "string", "analyzer": language_analyzer},
                "context": {"type": "string", "analyzer": language_analyzer},
                "identifiers": {"type": "string", "analyzer": "keyword_lowercase"},
                "tenant_id": {"type": "token"},
                "doc_id": {"type": "token"},
                "source_type": {"type": "token"},
                "kind": {"type": "token"},
                "metadata": {"type": "document", "dynamic": False, "fields": {"tags": {"type": "token"}}},
            },
        },
        "analyzers": [
            {
                "name": "keyword_lowercase",
                "tokenizer": {"type": "keyword"},
                "tokenFilters": [{"type": "lowercase"}],
            }
        ],
    }


def vector_index_definition(
    *, dimensions: int, similarity: str = "dotProduct", quantization: str = "scalar"
) -> dict[str, Any]:
    """Atlas Vector Search index. ``dotProduct`` assumes unit-length vectors
    (true for OpenAI text-embedding-3 and Voyage); use ``cosine`` if your model doesn't normalize."""
    fields: list[dict[str, Any]] = [
        {
            "type": "vector",
            "path": "embedding",
            "numDimensions": dimensions,
            "similarity": similarity,
            "quantization": quantization,
        }
    ]
    fields += [{"type": "filter", "path": f} for f in FILTER_FIELDS]
    return {"fields": fields}


# ----------------------------------------------------------------------------- mapping


def chunk_to_doc(chunk: Chunk, run_seq: int, *, binary_vectors: bool) -> dict[str, Any]:
    doc = chunk.model_dump(mode="python", exclude={"id", "embedding", "location"})
    doc["_id"] = chunk.id
    doc["kind"] = chunk.kind.value
    doc["section_text"] = chunk.section_text
    doc["location"] = chunk.location.model_dump(mode="python", exclude_none=True, exclude_defaults=True)
    if chunk.embedding is not None:
        doc["embedding"] = encode_vector(chunk.embedding, binary=binary_vectors)
    doc["run_seq"] = run_seq
    doc["updated_at"] = utcnow()
    return doc


def doc_to_chunk(doc: Mapping[str, Any]) -> Chunk:
    data = dict(doc)
    data["id"] = data.pop("_id")
    data["embedding"] = decode_vector(data.get("embedding"))
    loc = data.get("location") or {}
    if "shape_ids" in loc:
        loc = {**loc, "shape_ids": tuple(loc["shape_ids"])}
    data["location"] = SourceLocation(**loc)
    for extra in ("section_text", "run_seq", "updated_at", "_score", "_score_details"):
        data.pop(extra, None)
    return Chunk.model_validate(data)


def record_to_doc(record: DocumentRecord) -> dict[str, Any]:
    doc = record.model_dump(mode="python")
    doc["status"] = record.status.value
    return doc


def doc_to_record(doc: Mapping[str, Any]) -> DocumentRecord:
    data = {k: v for k, v in doc.items() if k != "_id"}
    return DocumentRecord.model_validate(data)
