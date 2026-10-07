# Shared Go contracts

This module holds the reusable graph, semantic, deployment, ingestion request,
search, build context, syntax IR and parser contracts. Backend implementations
live in `apps/forge-codegraph-worker/internal`; shared packages must not import them.

The module path remains `ei-aitiger-codegraph/pkg`, preserving existing imports
such as `ei-aitiger-codegraph/pkg/graph` after the directory move.

From the repository root:

```sh
GOTOOLCHAIN=auto go test ./packages/go/code-graph/domain/...
```

Or independently of the workspace:

```sh
cd packages/go/code-graph/domain
GOWORK=off GOTOOLCHAIN=auto go test ./...
```

The root `make check` includes this module, every application module and the
Java grammar.
