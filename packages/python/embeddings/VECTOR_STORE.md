# Select the document and commit vector store

Set the same values on the async documents worker, commits worker, workflows worker (knowledge search), async API,
and code graph MCP server:

```dotenv
FORGE_VECTOR_STORE=spanner
FORGE_VECTOR_SPANNER_DATABASE=projects/PROJECT/instances/INSTANCE/databases/DATABASE
```

`FORGE_VECTOR_STORE=mongo` is the default and preserves the existing Mongo
adapters, collections, indexes and configuration. An explicit selector overrides
`HYBRID_DOCUMENTS__STORAGE_BACKEND` and `HYBRID_COMMITS__STORAGE_BACKEND`; when the
selector is absent, those settings still support registered/test backends.
Invalid selectors fail startup. `.env` and `.env.common` references work as with
other Forge settings; process environment takes precedence.

The Python `StorageBackend` / `CommitStorage` protocols cover persistence and
search. Both Mongo and Spanner implement them. Go MCP's `documents.Searcher`
and `commits.Searcher` interfaces provide the corresponding read abstraction.
Ingestion, CLI queries, task APIs and MCP must use the same selection.

## Spanner setup

Use an existing **GoogleSQL** database. The vector database can be the code graph
database, but is explicitly selected rather than inferred from graph settings.
The worker creates `ForgeVectorRecords`, `ForgeVectors`, their lookup indexes,
and `ForgeVectorsText`; it does not modify the graph's `CG*` tables.
[Managed full-text search requires Enterprise or Enterprise Plus](https://docs.cloud.google.com/spanner/docs/full-text-search/search-indexes).

For hosted runtimes, set `FORGE_GOOGLE_CREDENTIALS_JSON` to a service-account
JSON key as a service-scoped secret. Python and Go both support it; never use
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
HYBRID_ENABLED_TASKS='["documents","commits"]' uv run forge-async-worker ensure-schema
```

Schema initialization requires Spanner DDL permissions. Runtime writers need
read/write access, and MCP needs only read access to these tables. After
initialization, workers may run without `--ensure-schema` under a credential
without DDL privileges. Existing `--ensure-schema` startup remains idempotent.
The schema SQL is packaged in `forge_embeddings/vector_store/schema.sql` for
infrastructure-managed initialization as well.

Compose forwards both settings. Local Spanner mode uses `worker-spanner:9010`;
start the graph worker first so its emulator database exists. With the cloud
Compose overlay, the async documents/commits/workflows workers and API receive the same
credential-file mount as the graph services; set `FORGE_VECTOR_SPANNER_DATABASE`
to the managed database explicitly.

## Behavior and differences

Spanner stores the full document/commit metadata, chunks, vectors, repository
configuration and indexing cursors. It implements tenant-scoped embedding reuse,
ordered chunk/section reads, document run fencing and tombstones, commit
reachability, stale detection and aggregate statistics. Chunk writes check the
current document run in the same transaction, including between bounded write
batches. Repository ownership is checked transactionally.

Search uses native `SEARCH` / `SCORE`, exact identifier matching, exact KNN
(`DOT_PRODUCT`, `COSINE_DISTANCE` or `EUCLIDEAN_DISTANCE` in Python), and weighted
reciprocal rank fusion with the existing `1 / (60 + rank)` rule. Document text
fields retain their boosts and exact phrases receive a boost. Commit search has
text, vector and symbol legs. Filters apply before ranking; model and dimension
guards prevent comparisons between incompatible vectors. MCP uses dot product,
matching the default worker settings and normalized embeddings.

Spanner's tokenizer/scoring is different from Atlas/Lucene: lexical rankings and
phrase matching are not byte-for-byte identical. Spanner uses exact phrase
boosts rather than Lucene's two-token phrase slop. Exact KNN has no approximate
candidate count or quantization; `num_candidates`, Mongo index names, binary
vector encoding and server/client Mongo fusion settings do not affect Spanner.
There is no ANN index yet: exact search scans the filtered vectors, so benchmark
larger corpora before rollout. Full-text indexes are partitioned by dataset;
all reads retain tenant predicates, including MCP searches across teams.

This selection does **not** move ETF job tracking, workflows, coding-task state,
or other application data out of Mongo. With Spanner selected, that Mongo
service no longer needs Atlas Search for these embedding tasks.

## Switching existing data

The selector switches storage; it does not copy or delete data. An empty Spanner
database initially returns no indexed documents or commit histories. Keep the
Mongo database for rollback. Quiesce indexing, initialize the destination,
re-register repositories and reindex commits, and resubmit source documents for
ingestion with their existing tenant/document IDs. Then switch every reader and
writer together and validate retrieval. Re-embedding can incur model-provider
charges. Switching back to `mongo` reads the preserved Mongo data; changes made
only in Spanner are not replicated back.

## Verification

The opt-in contract tests require an existing disposable database. They create
schema if needed, use unique tenant/repository IDs, and remove their own rows:

```sh
export SPANNER_EMULATOR_HOST=localhost:9010
export FORGE_TEST_SPANNER_DATABASE=projects/test-project/instances/test-instance/databases/test-vectors
uv run --project apps/forge-async-worker pytest apps/forge-async-worker/tests/test_spanner_storage.py
GOTOOLCHAIN=auto go test ./apps/forge-codegraph-mcp/...
```

Python tests cover fences, tombstones, tenant isolation, full-text/vector/hybrid
search, model/dimension isolation, filters, stats and reachability. Go integration
tests exercise MCP search/read behavior against the same schema. Without the
opt-in database variable, only database-dependent tests skip. Managed-Spanner
IAM, latency and capacity must still be verified in the deployment environment.
