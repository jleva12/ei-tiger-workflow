# forge-codegraph-query

Every read of the code graph the [code graph worker](../../../apps/forge-codegraph-worker/README.md)
writes to Spanner, and the one write beside it: the cross-repository links
people draw on system maps. The worker only ingests; its users read the graph
through this package, so a search ranks, cuts and answers the same wherever
it's asked:

| User | Reads |
|---|---|
| Admin API | the code graph explorer, the ingestion audit, system design knowledge base search (the search route, hosted chat agents), code links (nodes, cross-link writes) |
| ADK workflows (async worker) | system design knowledge base search for LLM nodes |
| Code graph MCP server | every tool |

| Module | Owns |
|---|---|
| `service.py` | `CodeGraph`: one per process, `CodeGraph.open(settings)`; checks every argument the same way for every caller. `search`, `view`, `node`, `neighbors`, `symbols`, `source`, `stats`, `runs`, `replace_cross_links` |
| `settings.py` | `CodeGraphSettings` (`spanner`, `embedding`, `search`), nested under each app's prefix |
| `spanner.py` | `SpannerGraphStore`: the worker's tables, indexes and generation rules; hybrid search |
| `query/` | the operations the MCP tools serve (a port of the Go `agentquery`): `search_code`, `explore`, `impact`, `path`, `graph_view`… and their compact answers |
| `codesearch.py`, `embedding.py` | query parsing, and the search documents and question embeddings exactly as the worker builds them |
| `testing.py` | `MemStore` (the graph in memory), `FakeEmbedder`, `build_shop()`: for this package's tests and its users' |

The caller decides who may read which repository: `CodeGraph` reads any
repository it is given. Its failures are `GraphError`s whose messages are
safe to show; `status_of(error)` is the HTTP status to answer with.

```python
from forge_codegraph_query import CodeGraph, CodeGraphSettings, to_json

graph = CodeGraph.open(settings.codegraph)  # FORGE_ADMIN_CODEGRAPH__*, ...
await graph.wait(settings.codegraph.spanner.startup_timeout)
found = await graph.search(["repo:1Fb-ic-0MMj7iztdpYxEnQ"], "how are tokens signed", source=True)
# {"hits": [{"id", "kind", "qualified_name", "file", "line", "score", ..., "text"}], ...}
answer = to_json(found)
```

## Settings

Each app nests `CodeGraphSettings` under its prefix and takes the values
from `.env.common`:

| Setting | `.env.common` |
|---|---|
| `spanner.database` | `FORGE_CODEGRAPH_SPANNER_DATABASE` (unset: the code graph is off) |
| `spanner.cursor_signing_key` | `FORGE_CODEGRAPH_CURSOR_SIGNING_KEY` |
| `search.enhance_query` | `FORGE_CODEGRAPH_SEARCH_ENHANCE_QUERY` |
| `embedding.model`, `.dimensions`, `.base_url` | `FORGE_EMBEDDING_*`, the worker's |
| `embedding.api_key` | `OPENAI_API_KEY` |

The Spanner client finds the emulator in the process environment
(`SPANNER_EMULATOR_HOST`); the Make targets and Compose set it.

## The worker's contract

The worker writes; this package reads what it wrote, so the two agree on:

- the schema (`packages/go/code-graph/storage/spanner/schema`, the worker's);
- each node's search document and its hash (`codesearch.document_of`, the
  worker's `codesearch.FromNode`): a hit whose stored hash differs from the
  one computed here is dropped as stale;
- graph ids (`model.graph_id`) and the question embedding transform
  (`embedding.windows`, `combine`).

`tests/test_contract.py` checks all three against the Go tests' golden file,
`packages/go/code-graph/domain/codesearch/testdata/reader_contract.json`:
change one side and both test suites fail until the other follows.

```bash
make codegraph-query-check          # ruff, mypy, unit tests
make codegraph-query-test-spanner   # the store against the Spanner emulator
```
