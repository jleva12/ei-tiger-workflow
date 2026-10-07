# Code graph MCP server

`forge-codegraph-mcp` is the Model Context Protocol server over the code graph
the [code graph worker](../forge-codegraph-worker/README.md) writes to Spanner.
It serves read-only tools to agents over streamable HTTP:
`list_repositories` for the repositories with a published graph,
`explore_code` for a question in plain language, `search_code`,
`find_symbol`, `get_node`, `neighbors`, `callers`, `callees`, `impact`,
`path`, `hubs`, `changes`, `change_impact`, `history`, `read_source`,
`read_file`, `repository_state` and `cross_repository_links`.

It is a Python rewrite of `forge-aidlc-parent`'s Go server
(`apps/forge-codegraph-mcp` there), on the design of the `template-mcp`
service template: a `ServerBuilder`
that assembles FastAPI and a FastMCP server from settings, toolsets as
classes of `@mcp_tool` methods, route classes, and pluggable auth. The graph
operations are a port of the Go modules under
[packages/go/code-graph](../../packages/go/code-graph/README.md) the worker
links (`agentquery`, and `storage`'s reads), so on the same graph a tool
answers as the Go server did: same queries, same ranking, same JSON.

| Path | Owns |
|---|---|
| `main.py` | The process: settings, logging, the store and embedder, the server assembled |
| `server.py` | `ServerBuilder` (the template's): FastAPI, routes, the MCP endpoint mounted at the root, middleware, Uvicorn |
| `core/` | Settings (`settings.py`) with `.env` references to `.env.common` (`env_files.py`), auth providers (`auth.py`), the FastMCP server factory and its request logging and rate limiting (`mcp.py`) |
| `routes/` | `Routes` and `Route` (the template's), and the health probes |
| `tools/` | `Toolset` and `Tool` (the template's), and `CodeGraphTools`, the tools |
| `graph/` | The graph: records and results (`model.py`), query parsing and retrieval documents (`codesearch.py`), signed cursors (`cursor.py`), question embedding (`embedding.py`), the store interface (`store.py`) and its Spanner reader (`spanner.py`) |
| `graph/query/` | The operations the tools serve: search, explore, impact with its assessment, changes, path, hubs, source, cross-repository hops |

Not ported: the Go server's commit search, document search, repository wikis,
`code_owners`, `pr_impact` and `repository_connections`, whose data (the
commits and wiki tasks, the admin API's owners, pull request and project-map
endpoints) this repository doesn't have; and its Prometheus `/metrics`.

## Tools

Answers are compact by default: every node is a brief (id, kind, qualified
name, file and line), every neighbor adds the edge kind, its direction and
the line of the occurrence that proves it, and `impact` lists each impacted
node once with the number of dependency edges that reach it and a by-kind
breakdown; local variables, parameters and pattern variables are folded away
(`skipped_locals` counts them) unless `include_locals: true`. `impact` takes
a `change` kind: `body` walks callers, including callers of what the
declaration overrides or implements; `signature` every direct user; `remove`
everything that touches it; `contract` the implementors and overriders and
their callers; `any` (the default) everything. Every impact carries an
assessment: counts per module and source root, the entry points nothing else
depends on, the test classes with impacted code and one Maven command per
module to run them. `changes` lists what a generation added, updated and
retired, and `change_impact` walks every changed declaration of a generation
as a root (retired ones at the generation before) into one merged
assessment, for reviewing a commit after the fact.

`full: true` on `explore_code`, `search_code`, `neighbors`, `callers`,
`callees`, `impact` and `change_impact` returns the complete records with
spans, content hashes and properties; `get_node` does the same for one node.
`path` is a bounded bidirectional search over proven edges, by default a
dependency path from one declaration to another that may descend from a type
into its members, and `hubs` ranks a repository's own declarations by
incoming semantic edges (`include_external: true` also ranks library
symbols). `explore_code` and `search_code` expand a question with the
repository's own vocabulary when no exact match exists (reported as
`expanded_terms`; `no_expansion: true` turns it off).

Search is two-level: the lexical branch queries the graph's search index
(Spanner `SEARCH` over each declaration's document and identifiers) and, with
an embedding model and key, the vector branch ranks the worker's embeddings
(`APPROX_COSINE_DISTANCE`); reciprocal rank fusion combines them, then exact
names, connectivity and path proximity rerank.

**Across repositories.** People record where code in one repository reaches
another over the network (`CGCrossLinks`, beside the graphs): `callers`,
`callees` and `neighbors` list a node's links under `across_repositories` on
their first page, `impact` continues into the linked repositories under
`across`, and `cross_repository_links` lists one node's links. A link finds
its far node by id, or by qualified name after a re-ingestion renumbered it;
otherwise it's `stale`.

Failures the model can act on (an argument out of range, a node that isn't
there at the generation, a stale cursor) come back as tool errors with their
reason; anything else is logged and masked.

## Access

Every caller reads one organization's repositories, and only those
(`access.py`). It sends a Forge credential as its bearer token: an
organization's API key (`fk_…`, made on the organization's API keys page in
the web console), or a Forge token minted for an organization
(`forge-admin-token --organization`, whose `org_id` claim names it). The
server passes the credential on, as it came, to the admin API's
`GET /code-graph/access`, which answers whose it is and the organization's
code repositories, and needs `repositories:read` there (every organization
role has it, API caller included). The answer is kept for the same
credential for `CODEGRAPH_MCP__AUTH__CACHE_SECONDS` (60), so a deleted key, a
removed member or a newly added repository shows within a minute.

The graph has one repository per GitHub URL, whichever organizations ingest
it, its ID following from the URL; a caller reads the graphs of its
organization's repositories' URLs. `list_repositories` lists only those;
every tool refuses any other repository as if it weren't there; and links
into other repositories (`across_repositories`, `impact`'s `across`) are
followed only into those the caller reads.

- No credential, or one the admin API refuses: 401.
- A credential that names no organization (a sign-in token not minted for
  one), or without `repositories:read` there: 403 (`insufficient_scope`).
- The admin API can't be reached or fails: 503, and nothing is kept.

The probes stay open.

## Configuration

Settings are `CODEGRAPH_` plus the setting's path, nested groups joined with
`__` (`CODEGRAPH_SPANNER__DATABASE` is `spanner.database`), read from
defaults, then `.env` in the working directory, then the process
environment. `make env` creates `.env` from [.env.example](.env.example);
values shared with the worker come from the repository's `.env.common` by
`${NAME}` reference.

| Setting | Default | Meaning |
|---|---|---|
| `CODEGRAPH_SERVER__HOST`, `__PORT` | `127.0.0.1`, `8103` | The HTTP listener; Compose listens on `0.0.0.0:8103` |
| `CODEGRAPH_MCP__PATH` | `/mcp` | The endpoint path |
| `CODEGRAPH_MCP__STATELESS_HTTP`, `__JSON_RESPONSE` | `true`, `true` | No server-side sessions, so any replica serves any request |
| `CODEGRAPH_MCP__AUTH__ADMIN_URL` | `http://localhost:8101/api/v1` | The admin API that checks callers' credentials (above), with its prefix; Compose uses `http://admin:8091/api/v1` |
| `CODEGRAPH_MCP__AUTH__ADMIN_API_KEY` | none | The admin API's deployment key (its `FORGE_ADMIN_API_KEY`), sent with each check when it has one; `make env` copies it |
| `CODEGRAPH_MCP__AUTH__CACHE_SECONDS`, `__CACHE_SIZE`, `__TIMEOUT` | `60`, `1024`, `10` | How long and for how many credentials the admin API's answers are kept; zero asks every time. How long it has to answer |
| `CODEGRAPH_MCP__RATE_LIMIT__ENABLED` | `false` | Requests per second per client |
| `CODEGRAPH_SPANNER__DATABASE` | `projects/codegraph-local/instances/codegraph/databases/codegraph` | The worker's database |
| `CODEGRAPH_SPANNER__SCOPE` | `codegraph` | The worker's scope |
| `CODEGRAPH_SPANNER__CURSOR_SIGNING_KEY` | required | The worker's cursor key (`FORGE_CODEGRAPH_CURSOR_SIGNING_KEY`), at least 32 characters |
| `CODEGRAPH_SPANNER__CREDENTIALS_JSON` | none | A service account's key, for hosts without key files; otherwise Application Default Credentials, or the emulator at `SPANNER_EMULATOR_HOST` |
| `CODEGRAPH_SPANNER__TIMEOUT`, `__STARTUP_TIMEOUT` | `30`, `60` | Seconds per query; how long startup waits for the database |
| `CODEGRAPH_EMBEDDING__MODEL`, `__DIMENSIONS`, `__BASE_URL`, `__API_KEY` | none, 3072 | The embedding model, vector length and gateway every app shares, `FORGE_EMBEDDING_*` in `.env.common` (the worker embeds the graph with them); with a model and key, search adds the semantic branch. Startup checks the database's vector length against the dimensions |
| `CODEGRAPH_SEARCH__ENHANCE_QUERY` | `false` | Spanner's query enhancement, on managed Spanner |
| `CODEGRAPH_LOGGING__LEVEL`, `__FORMAT` | `INFO`, by terminal | Structured logs (`forge_common.logging`) |

## Endpoints

| Path | Purpose |
|---|---|
| `/mcp` | The MCP endpoint (streamable HTTP, stateless, JSON responses) |
| `/health/live` | Liveness: the process is up |
| `/health/ready` | Readiness: Spanner answers within three seconds, else 503 |
| `/docs` | The HTTP routes' OpenAPI docs, outside production |

Every HTTP request is logged with its request id, status and duration, and
every MCP request with its method, tool and duration.

## Run

From the repository root:

```sh
make env              # creates apps/forge-codegraph-mcp/.env
make admin            # the admin API, which checks the server's callers
make worker           # the code graph worker and its emulator; it creates the database and ingests
make codegraph-mcp    # the server on http://localhost:8103/mcp, against the emulator
make codegraph-mcp SPANNER=cloud   # the same against managed Spanner (WORKER_CLOUD_SPANNER_DATABASE)
```

Startup waits up to `CODEGRAPH_SPANNER__STARTUP_TIMEOUT` for the worker to
create the database, and fails when it was made for other embedding
dimensions. `make up` starts the server with the rest of the stack as the
Compose service `codegraph-mcp` on `http://localhost:18203/mcp`
(`CODEGRAPH_MCP_PORT` in `.env.compose`), reading the worker's database and
scope once the worker is healthy; `make up SPANNER=cloud` points both at
managed Spanner (`compose.cloud.yaml`). `make up-all-local` runs it natively
with the other apps.

## Connect an agent

Forge's own agents and workflows use it as one of an organization's MCP
servers: on the web console's MCP servers page, **+ MCP server** offers it
ready-made as **Code explorer** (the admin API's `default_servers.yaml`),
with its URL and auth filled in; paste one of the organization's API keys and
add it.

Give any MCP client that speaks streamable HTTP an organization's API key,
or a token minted for the organization, e.g. Claude Code:

```sh
TOKEN=$(cd apps/forge-admin-api && uv run forge-admin-token --organization Acme)
claude mcp add --transport http codegraph http://localhost:8103/mcp --header "Authorization: Bearer $TOKEN"
```

Use port 18203 for `make up`. The agent starts with `list_repositories` for
the ids it may use: the organization's code repositories the worker has
ingested.

## Tests

```sh
make codegraph-mcp-check   # ruff, format check, mypy and pytest
```

The tests run the operations on the Go tests' fixtures with an in-memory
store, every tool through an MCP client (and what a caller of one
organization may read), the HTTP surface with its probes and the admin API's
checks (an `httpx` mock of it), and settings; the cursor and
id encodings are checked against values the Go code computes. No Spanner is
needed.
