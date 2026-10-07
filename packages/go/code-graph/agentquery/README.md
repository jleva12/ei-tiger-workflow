# Agent query operations

Module `ei-aitiger-codegraph/agentquery`. It composes the store's reads into
the operations an agent-facing surface exposes, so `codegraph-api` (HTTP) and
`codegraph-mcp` (MCP tools) answer identically:

- `Explore` turns a natural-language question into a context pack: search
  seeds with spans, signatures and snippets, one semantic hop around the
  strongest seeds, edges among seeds, and the files involved.
- `Impact` walks incoming dependency edges from a declaration and from what it
  overrides or implements.
- `NodeSource` cuts a node's exact bytes from the retained file, with optional
  lines of context.
- `GraphView` composes a bounded sample of nodes and their relationships for
  the visual console, pinning all reads to one published generation.

`Store` is the read interface these need; `*spannerstore.Store` satisfies it
and `memstore` is the in-memory implementation the tests use. This module
imports `packages/go/code-graph/storage` and `packages/go/code-graph/domain` only.

```sh
GOTOOLCHAIN=auto go test ./packages/go/code-graph/agentquery/...
```

## Compact answers, paths and hubs

`Brief` is the compact form of a node (id, kind, qualified name, file, line);
`CompactNeighbors`, `CompactHits`, `CompactExplore` and `CompactImpact`
project the full results onto briefs so an agent pays about a hundred bytes
per declaration instead of a kilobyte. `Impact` walks one depth level per
query over the edge indexes (`Store.DependencyEdges`, no payloads) and counts
the edges that reach each node; `Path` is a bidirectional breadth-first
search over the same reads; `Hubs` ranks nodes by incoming edges through an
index-only aggregation. `Search` is the one search entry point of the API and
the MCP server: hybrid search plus optional query expansion, which adds the
identifier fragments most shared by the top hits when none matched the
question exactly and fuses the two rankings by reciprocal rank.

## Change kinds, assessment and change sets

`Impact` takes a change kind that selects the dependants that matter: `body`
follows calls and references, including callers of what the declaration
overrides or implements; `signature` every direct user without dispatch;
`remove` everything that touches the declaration; `contract` the
implementors and overriders and their callers. Every result carries an
`Assessment`: hits grouped by module and source root (from the file node's
source set and the Maven layout of its path), counts by depth, the impacted
declarations nothing else depends on, and the test classes that contain
impacted code with one Maven selection per module. `Changes` pages through a
generation's added, updated and retired declarations over
`Store.Changes`, and `ChangeImpact` walks all of them as roots, the retired
ones at the generation before, into one assessment. `RootsImpact` does the
same for any declarations, each with its own change kind and its dispatch
roots as `Impact` adds them (the declarations a pull request touches, in the
MCP server's `pr_impact`), and `FileDeclarations` lists a file's
declarations with their spans at a generation, over `Store.LineagesByPath`
and `Store.RecordsByLineage`.
