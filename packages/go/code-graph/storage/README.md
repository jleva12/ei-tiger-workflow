# Spanner store

Module `ei-aitiger-codegraph/storage`, package `spanner` (imported as
`spannerstore`). It is how the ingestion worker writes and reads its
durable state: repositories, runs and leases, the versioned graph with its
generations, per-file identity maps, retained source, search documents and
embeddings, and the worker registry (`CGWorkers`), where live workers record
their analysis configuration. Its reads are the ones ingestion needs (the
baseline generation, the documents to embed); everything else that reads
these tables, search and cross-repository links (`CGCrossLinks`) included,
is the Python package `forge_codegraph_query`, which reads Spanner directly.
The schema still declares every table and index those readers use. The queue of
ingestion jobs is not here: the Forge admin API keeps it in its MySQL, where
the worker claims from it. The schema under `spanner/schema/` is
embedded and applied by `ProvisionEmulator` for local development; managed
Spanner is provisioned from the same statements.

The embedding column and its vector index have a fixed `vector_length`, so
`Config.VectorLength` is required and `Ping` verifies the database was created
with it. Only `packages/go/code-graph/domain` contracts are imported here; backend internals
never are.

```sh
GOTOOLCHAIN=auto go test ./packages/go/code-graph/storage/...
SPANNER_EMULATOR_HOST=127.0.0.1:19030 GOTOOLCHAIN=auto go -C packages/go/code-graph/storage test -tags=integration ./spanner
```

The root `make worker-test-spanner` starts the emulator and runs the
integration tests here and the ingestion pipeline tests in
`apps/forge-codegraph-worker`.
