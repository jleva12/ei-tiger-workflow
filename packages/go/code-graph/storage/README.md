# Spanner store

Module `ei-aitiger-codegraph/storage`, package `spanner` (imported as
`spannerstore`). It is the durable backend every process shares: repositories,
runs and leases, the versioned graph with its generations, per-file
identity maps, retained source, search documents and embeddings, the
two-level hybrid search over them, and the worker registry (`CGWorkers`),
where live workers record their analysis configuration. The queue of
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

The root `make test-spanner-local` starts the emulator and runs the
integration tests here, the admin API test in `apps/forge-admin-api` and the ingestion
pipeline test in `apps/forge-codegraph-worker`.
