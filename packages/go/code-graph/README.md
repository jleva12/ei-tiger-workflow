# Code graph shared Go modules

Go libraries behind the code graph, copied from `forge-aidlc-parent` (and
before that from `ei-aitiger-codegraph`). Each directory is an independent Go
module linked by the root `go.work`. The ingestion worker in
[apps/forge-codegraph-worker](../../../apps/forge-codegraph-worker/README.md)
writes the graph with them. `agentquery` holds the queries a reader of the
graph asks (search, explore, neighbors, impact, source…) over `storage`; in
`forge-aidlc-parent` an MCP server serves them to agents, and nothing here
reads the graph through it yet.

| Directory | Module | Purpose |
|---|---|---|
| [domain](domain/README.md) | `ei-aitiger-codegraph/pkg` | Shared domain and parser contracts: IR, graph, deployments, ingestion tasks |
| [storage](storage/README.md) | `ei-aitiger-codegraph/storage` | Spanner persistence (repositories, runs, leases, the graph), schema and search; the job queue is the admin API's MySQL |
| [agentquery](agentquery/README.md) | `ei-aitiger-codegraph/agentquery` | The graph queries agents ask (explore, search, neighbors, impact, paths, history, source, the impact of several changed declarations, a file's declarations) over `storage` |
| [serviceconfig](serviceconfig/README.md) | `ei-aitiger-codegraph/serviceconfig` | Shared service configuration: Spanner, embeddings, search, the name of the queue a worker claims from |
| `authorization` | `ei-aitiger-codegraph/authorization` | Tenant authorization model; `storage` depends on it |
| [tree-sitter-java](tree-sitter-java/README.md) | `github.com/tree-sitter/tree-sitter-java` | Audited Java grammar and Go binding, pinned by the worker's `replace` |

Go import paths remain those declared in each `go.mod`. Application `replace`
directives resolve these modules when the workspace is disabled, including in
Docker builds. From the repository root, `make worker-check` vets and tests
the worker and every module here. To test one module on its own:

```sh
GOWORK=off GOTOOLCHAIN=auto go -C packages/go/code-graph/domain test ./...
```
