# forge-task-documents

Mongo and Spanner storage are selectable with `FORGE_VECTOR_STORE=mongo|spanner`.
See [vector store setup and migration](../embeddings/VECTOR_STORE.md).

The `documents` task (`import forge_task_documents`), on the `documents` queue:
load a file (local path or `s3://`), parse it (PDF, Word, PowerPoint, Excel, CSV,
Markdown, HTML, text, Visio, Mermaid; docling optional), chunk, enrich, embed and store it in MongoDB Atlas or Spanner for
hybrid (BM25 + vector) search. It registers with the
[async worker](../../../apps/forge-async-worker/README.md) through the
`forge_async_worker.tasks` entry point; its model clients come from
[forge-embeddings](../embeddings/README.md).

Parsers feed a common IR; `ChunkEngine` applies type-aware strategies:

| Type | Parser | Chunking |
|---|---|---|
| .pdf | pdfplumber, OCR for scanned pages (or Docling → IR) | headings from bookmarks or font sizes; two-column pages read in order; running headers/footers dropped; ruled and borderless tables separate |
| .docx | python-docx (or Docling → IR) | prose packed per section; tables/code separate |
| .pptx / .ppsx | python-pptx | a section per slide (title, bullets, notes); tables and chart data as tables |
| .ppt, .doc, .xls, .rtf, .odp, .odt, .ods | LibreOffice → the OOXML parser above | as the format converted to |
| .xlsx / .csv | openpyxl streaming / csv sniffer | row groups with repeated headers + a summary chunk |
| .md / .txt | markdown-it-py / encoding detection | heading-aware prose, nested lists (see below), atomic code |
| .html / .htm | BeautifulSoup (lxml) | the page's content only (no nav, banner, footer, scripts, hidden elements); heading-aware prose, nested lists (see below), tables with header rows and spans, atomic code |
| .vsdx | vsdx | edges `[A] --label--> [B]` in flow order + summary |

**Lists keep their hierarchy.** A list item is rendered indented by depth under
its marker (`-`, or its number in an ordered list), and a later paragraph of the
same item is indented under it without one. An item and everything nested under
it stay in one chunk when they fit; a chunk that starts inside a list repeats the
items above it (cut to fit a quarter of the budget), so a nested bullet is never
embedded or returned without its parents.

Fallbacks that need system packages turn on when those are installed (the
worker image has Tesseract; the deployment installs LibreOffice):

- **Scanned PDFs.** A page with no text layer (or text turned into outlines,
  or a full-page image with a line of text stamped on it) is rendered at
  `HYBRID_DOCUMENTS__OCR__DPI` (300) and read by Tesseract
  (`HYBRID_DOCUMENTS__OCR__LANGUAGES`, `eng`; up to
  `HYBRID_DOCUMENTS__OCR__MAX_PAGES` per document). Its words go through the same
  layout as a text layer's. The document records `ocr_pages`. Without Tesseract,
  or when OCR finds nothing, it fails with "no text in pdf".
- **Borderless tables.** Three or more lines whose words sit in the same
  columns, with clear space between, become a table; a lone line under a cell
  continues it.
- **Legacy and OpenDocument office files** are converted by `soffice --headless`
  (`HYBRID_DOCUMENTS__OFFICE__COMMAND`) to .pptx/.docx/.xlsx and read by the
  registry's parser for that; without LibreOffice they fail saying so.

Consistency: a per-document fencing token (`run_seq`) guards source loading and ingestion. Deletes retain tombstones, including for IDs not registered yet; only an explicit registration recreates them. Chunks use immutable storage generations while retaining their public IDs. `DocumentStore.complete_run` atomically marks READY and selects `published_run_seq`; reads select only that generation, and `ChunkStore.publish_run` performs optional cleanup. Failed staging and cleanup cannot alter visible published contents. Adding a format = one `Parser` class (entry point group `forge_task_documents.parsers`). Swapping the DB = implement `DocumentStore`, `ChunkStore`, `SearchBackend` and call `register_document_storage(...)`.

Jobs: `documents.ingest {tenant_id, doc_id, uri}`, `documents.delete {tenant_id, doc_id}`.

**Knowledge bases.** In Forge a knowledge base is a tenant. A search names
its tenants (`SearchQuery.tenant_ids`, never empty) and runs once over all of
them, so an agent given "Member knowledge" and "Claims knowledge" gets one
ranking of those two and nothing else. `retrieval.KnowledgeBaseSearch` is the
one search the admin API (chat agents, the search route) and ADK workflows'
agents both use: the worker builds it with `build_knowledge_search(ctx)`, and
a service outside the worker opens the same store with
`storage.open_storage(...)` (it follows `FORGE_VECTOR_STORE`).

**Agent search.** Chat agents and ADK workflows use this Python package's shared `KnowledgeBaseSearch` service. Direct database consumers must follow the same publication-pointer and tenant filters; filtering chunks only by `live` is insufficient for the generation format.

**Evaluation.** [`eval/`](eval/README.md) holds a labelled suite: ten documents, one
per format, and 80 questions labelled with their answers and evidence. It runs
through this pipeline and search with any settings, embedding model or
reranker, scores what the search finds, and compares two runs
(`make docs-eval NAME=…`, `make docs-eval-compare A=… B=…`). Run it before and
after changing the chunking, parsers, embeddings or search.

## MongoDB schema

`forge-async-worker ensure-schema` creates them: `documents` (one per file) and `chunks` (both search legs), with search indexes `chunks_text` and `chunks_vector`. Field docs are in `storage/mongo/schema.py`.

### Upgrading document generations

Coordinate the upgrade of document readers and ingestion workers. Do not run
old writers alongside generation-aware readers: old workers do not advance the
publication pointer. Old readers that only check `live` cannot read the new
format, whose chunks retain `live: false` and are selected by the document's
pointer. Update any external or direct database consumers before enabling new
ingestion. The Go ingestion project remains outside this remediation's scope.

For MongoDB Atlas, add `run_seq` and `live` as `filter` fields in the existing
vector index **before deploying the new search code**, and wait until it is
queryable. These filters select the active generation before candidate limits.
The ordinary `forge-async-worker ensure-schema` command deliberately only logs
index drift; it does not rebuild existing indexes. Run this one-off migration
from the configured async-worker directory using the new package:

```sh
uv run python - <<'PY'
import asyncio
from forge_async_worker.config import WorkerSettings
from forge_async_worker.runtime import build_runtime
from forge_task_documents.storage.mongo import MongoStorage

async def main():
    runtime = build_runtime(WorkerSettings(enabled_tasks=["documents"]))
    try:
        task = runtime.documents
        if not isinstance(task.storage, MongoStorage):
            raise RuntimeError("This index migration requires FORGE_VECTOR_STORE=mongo")
        await task.storage.ensure_schema(
            embedding_dimensions=task.embedder.dimensions,
            update_existing=True,
            wait=True,
        )
    finally:
        await runtime.close()

asyncio.run(main())
PY
```

Existing Mongo documents without `published_run_seq` remain readable through
the legacy `live != false` rule while being re-ingested. Their next successful
run writes a positive pointer and permits cleanup of old rows; public chunk
IDs remain unchanged. A pointer of `0` explicitly means no published generation,
so deleting and registering an ID cannot revive retained legacy chunks. Do not
bulk-fill legacy pointers with the latest `run_seq`: that run may have failed or
never published. Re-ingest from the source to establish a verified generation.
No automatic rollback to old workers/readers is supported once new generations
have been written; use a coordinated forward upgrade or restore a consistent
pre-upgrade backup.

Each Mongo search reads document-generation metadata for the requested tenants
and document filters, then shares that snapshot between its search legs.
Production-scale query latency and generated filter size have not been
benchmarked; validate these with representative tenant document counts before
rollout.

Spanner needs no DDL migration: its existing JSON payload stores the publication
pointer and its existing vector rows store generation-qualified IDs. Legacy
documents remain readable until re-ingestion; the same coordinated reader and
writer rollout applies.

## Configuration

`HYBRID_DOCUMENTS__*` (`DocumentsSettings` in `config.py`), for example
`HYBRID_DOCUMENTS__DOCX_PARSER=docling` or `HYBRID_DOCUMENTS__PDF_PARSER=docling`
(the `docling` extra; for PDFs it also OCRs scanned pages, which the default
pdfplumber parser rejects as having no text),
`HYBRID_DOCUMENTS__CHUNKING__MAX_TOKENS=400`,
`HYBRID_DOCUMENTS__CONTEXT__ENABLED=true` (LLM chunk context). Source files at
`s3://` URIs are read with `HYBRID_S3__*` (`S3Settings`): locally
`embedding-s3` (RustFS), published on 127.0.0.1:19000, addressed path-style.

```sh
forge-async-worker docs ingest --tenant t1 docs/*
forge-async-worker docs search --tenant t1 "refund approval limit"
forge-async-worker docs search --tenant t1 --tenant t2 "refund approval limit"   # both, ranked as one
forge-async-worker docs chunks path/to/file.xlsx   # preview chunking, no DB
```

`examples/documents_api.py` is an example FastAPI app over the task (upload →
S3 → queue ingest, document status, search) that submits its jobs to the
worker by name; `tests/test_example_api.py` runs it with the jobs held and
then run as a worker would.

Tests run in the worker's environment: `make async-worker-check`, or from here
`uv run --project ../../../../apps/forge-async-worker pytest`.
