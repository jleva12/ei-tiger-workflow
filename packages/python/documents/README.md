# forge-task-documents

Mongo and Spanner storage are selectable with `FORGE_VECTOR_STORE=mongo|spanner`.
See [vector store setup and migration](../embeddings/VECTOR_STORE.md).

The `documents` task (`import forge_task_documents`), on the `documents` queue:
load a file (local path or `s3://`), parse it (PDF, Word, PowerPoint, Excel, CSV,
Markdown, text, Visio, Mermaid; docling optional), chunk, enrich, embed and store it in MongoDB Atlas or Spanner for
hybrid (BM25 + vector) search. It registers with the
[async worker](../../../../apps/forge-async-worker/README.md) through the
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
| .md / .txt | markdown-it-py / encoding detection | heading-aware prose, atomic code |
| .vsdx | vsdx | edges `[A] --label--> [B]` in flow order + summary |

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

Consistency: per-document fencing token (`run_seq`), tombstoned deletes, stale-chunk cleanup. Adding a format = one `Parser` class (entry point group `forge_task_documents.parsers`). Swapping the DB = implement `DocumentStore`, `ChunkStore`, `SearchBackend` and call `register_document_storage(...)`.

Jobs: `documents.ingest {tenant_id, doc_id, uri}`, `documents.delete {tenant_id, doc_id}`.

**Knowledge bases.** In Forge a knowledge base is a tenant. A search names
its tenants (`SearchQuery.tenant_ids`, never empty) and runs once over all of
them, so an agent given "Member knowledge" and "Claims knowledge" gets one
ranking of those two and nothing else. `retrieval.KnowledgeBaseSearch` is the
one search the admin API (chat agents, the search route) and ADK workflows'
agents both use: the worker builds it with `build_knowledge_search(ctx)`, and
a service outside the worker opens the same store with
`storage.open_storage(...)` (it follows `FORGE_VECTOR_STORE`).

**Agent search.** In Forge, agents search the documents through the code graph MCP server ([apps/forge-codegraph-mcp](../../../../apps/forge-codegraph-mcp/README.md#document-search)), which ports `HybridSearchService`'s search to Go over this MongoDB, as the same read-only user as commit search, for the teams (tenants) the admin API says each caller may read, all at once. A change to the search here (`storage/mongo/pipelines.py`, `forge_embeddings/identifiers.py`, the chunk fields or the `chunks_text` / `chunks_vector` indexes) belongs there too.

## MongoDB schema

`forge-async-worker ensure-schema` creates them: `documents` (one per file) and `chunks` (both search legs), with search indexes `chunks_text` and `chunks_vector`. Field docs are in `storage/mongo/schema.py`.

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
