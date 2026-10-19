# Select the documents vector store

Set the same values everywhere the knowledge bases' chunks are written or
read: the async worker (its documents task writes them, and the ADK workflows
task's LLM agents search them with the documents task's search) and the admin
API (its knowledge base search, `forge_admin/knowledge/search.py`, which chat
agents' knowledge base tools use):

```dotenv
FORGE_VECTOR_STORE=spanner
FORGE_VECTOR_SPANNER_DATABASE=projects/PROJECT/instances/INSTANCE/databases/DATABASE
```

`FORGE_VECTOR_STORE=mongo` is the default and preserves the existing Mongo
adapters, collections, indexes and configuration. An explicit selector overrides
`HYBRID_DOCUMENTS__STORAGE_BACKEND`; when the selector is absent, that setting
still supports registered/test backends.
Invalid selectors fail startup. `.env` and `.env.common` references work as with
other Forge settings; process environment takes precedence.

The Python `StorageBackend` protocol (`forge_task_documents.protocols`) covers
persistence and search. Both Mongo and Spanner implement it. Ingestion, the
`docs` CLI's queries and the admin API's search must use the same selection.

## Spanner setup

Use an existing **GoogleSQL** database. The vector database can be the code graph
database, but is explicitly selected rather than inferred from graph settings.
The worker creates `ForgeVectorRecords`, `ForgeVectors`, their lookup indexes,
and `ForgeVectorsText`; it does not modify the graph's `CG*` tables.
[Managed full-text search requires Enterprise or Enterprise Plus](https://docs.cloud.google.com/spanner/docs/full-text-search/search-indexes).

For hosted runtimes, set `FORGE_GOOGLE_CREDENTIALS_JSON` to a service-account
JSON key as a service-scoped secret. The Python adapter reads it; never use
personal refresh-token credentials. Alternatively, use Application Default
Credentials, or mount a runtime service-account or
workload-identity credential configuration and export
`GOOGLE_APPLICATION_CREDENTIALS` in the process environment. For local tests,
export `SPANNER_EMULATOR_HOST=localhost:9010` instead. Do not set the emulator
variable in a managed deployment. Google SDK settings are process variables,
not Forge `.env` settings. The Python adapter defaults the SDK's multiplexed
session settings to `false` to avoid the 3.70/3.71 SDK's ten-minute blocking
shutdown; an explicitly exported SDK setting overrides that default.

Initialize once before serving queries, from `apps/forge-async-worker`:

```sh
HYBRID_ENABLED_TASKS='["documents"]' uv run forge-async-worker ensure-schema
```

Schema initialization requires Spanner DDL permissions. Runtime writers need
read/write access, and the admin API needs only read access to these tables. After
initialization, workers may run without `--ensure-schema` under a credential
without DDL privileges. Existing `--ensure-schema` startup remains idempotent.
The schema SQL is packaged in `forge_embeddings/vector_store/schema.sql` for
infrastructure-managed initialization as well.

Compose doesn't set either value itself: each reaches a container through its
app's `.env`, which the service reads as an `env_file`. Local Spanner mode uses
`worker-spanner:9010`; start the graph worker first so its emulator database
exists. Only the admin API's and the ADK workflows worker's containers set
`SPANNER_EMULATOR_HOST` (for the code graph), and only they get the cloud
Compose overlay's credential-file mount; the documents worker's container has
neither. Set `FORGE_VECTOR_SPANNER_DATABASE` to the managed database
explicitly.

## Behavior and differences

Spanner stores the documents' metadata, chunks and vectors. It implements
tenant-scoped embedding reuse, ordered chunk/section reads, and document run
fencing, generations and tombstones. Chunk writes check the
current document run in the same transaction, including between bounded write
batches.

Search uses native `SEARCH` / `SCORE`, exact identifier matching, exact KNN
(`DOT_PRODUCT`, `COSINE_DISTANCE` or `EUCLIDEAN_DISTANCE` in Python), and weighted
reciprocal rank fusion with the existing `1 / (60 + rank)` rule. Document text
fields retain their boosts and exact phrases receive a boost. Filters apply
before ranking; model and dimension guards prevent comparisons between
incompatible vectors.

Spanner's tokenizer/scoring is different from Atlas/Lucene: lexical rankings and
phrase matching are not byte-for-byte identical. Spanner uses exact phrase
boosts rather than Lucene's two-token phrase slop. Exact KNN has no approximate
candidate count or quantization; `num_candidates`, Mongo index names, binary
vector encoding and server/client Mongo fusion settings do not affect Spanner.
There is no ANN index yet: exact search scans the filtered vectors, so benchmark
larger corpora before rollout. Full-text indexes are partitioned by dataset;
all reads retain tenant predicates, including searches across several
knowledge bases.

This selection does **not** move ADK workflows, chat agents or other
application data out of Mongo. With Spanner selected, the knowledge database
no longer needs Atlas Search for the documents task.

## Switching existing data

The selector switches storage; it does not copy or delete data. An empty Spanner
database initially returns no indexed documents. Keep the
Mongo database for rollback. Quiesce indexing, initialize the destination,
and resubmit source documents for
ingestion with their existing tenant/document IDs. Then switch every reader and
writer together and validate retrieval. Re-embedding can incur model-provider
charges. Switching back to `mongo` reads the preserved Mongo data; changes made
only in Spanner are not replicated back.

## Verification

No test runs against a real Spanner database. These need no service:

```sh
cd packages/python/embeddings && uv run pytest tests/test_vector_store.py
cd packages/python/documents && uv run pytest tests/test_spanner_generations.py
```

`test_vector_store.py` covers the selector (`.env`, `.env.common` and process
precedence), the database setting, error classification and credentials.
`test_spanner_generations.py` runs the Spanner store's transactions and
visibility SQL on SQLite: run fencing, generations, tombstones and the hybrid
search legs' shared snapshot. Native `SEARCH` / `SCORE` and KNN, managed-Spanner
IAM, latency and capacity must still be verified in the deployment environment.
