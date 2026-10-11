# Code graph shared Go modules

Go libraries behind the code graph, copied from `forge-aidlc-parent` (and
before that from `ei-aitiger-codegraph`). Each directory is an independent Go
module linked by the root `go.work`. The ingestion worker in
[apps/forge-codegraph-worker](../../../apps/forge-codegraph-worker/README.md)
writes the graph with them, and nothing here reads it back for anyone else:
the queries a reader of the graph asks (search, explore, neighbors, impact,
source…) live in the Python package
[`packages/python/codegraph-query`](../../python/codegraph-query)
(`forge_codegraph_query`), which reads the same Spanner tables and which the
admin API, the workflows worker and the code graph MCP server use.
`domain/codesearch/testdata/reader_contract.json` pins what both sides must
agree on: node payloads, document text and hashes, graph IDs and the
embedding transform.

| Directory | Module | Purpose |
|---|---|---|
| [domain](domain/README.md) | `ei-aitiger-codegraph/pkg` | Shared domain and parser contracts: IR, graph, deployments, ingestion tasks |
| [storage](storage/README.md) | `ei-aitiger-codegraph/storage` | Spanner persistence the worker writes through (repositories, runs, leases, the graph, search documents and embeddings) and the schema; the job queue is the admin API's MySQL |
| [serviceconfig](serviceconfig/README.md) | `ei-aitiger-codegraph/serviceconfig` | The worker's shared service configuration: Spanner, embeddings, logging, the name of the queue it claims from |
| `authorization` | `ei-aitiger-codegraph/authorization` | Tenant authorization model; `storage` depends on it |
| [tree-sitter-java](tree-sitter-java/README.md) | `github.com/tree-sitter/tree-sitter-java` | Audited Java grammar and Go binding, pinned by the worker's `replace` |

Go import paths remain those declared in each `go.mod`. Application `replace`
directives resolve these modules when the workspace is disabled, including in
Docker builds. From the repository root, `make worker-check` vets and tests
the worker and every module here. To test one module on its own:

```sh
GOWORK=off GOTOOLCHAIN=auto go -C packages/go/code-graph/domain test ./...
```
