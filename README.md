# ei-tiger-agent-workflow-builder

Forge builds and runs **Google ADK workflows** and designs **Google ADK
agents**: a web console with visual builders for both, the API that keeps
organizations' ADK workflows and decides who may do what, and an async worker
that runs them on [Google ADK](https://google.github.io/adk-docs/)'s graph
engine. People draw an ADK workflow on a canvas: LLM agents and teams of
them, other saved ADK workflows, human approvals and questions, HTTP calls,
data transforms, delays, branches and loops. They run it from the console
and watch each run step by step. An agent is one Google ADK chat agent with
its tools and the sub-agents it hands off to, published in numbered versions
and served by the admin API over ADK's run API and Google's A2A protocol, or
generated as a project of its own. An AI assistant on every page helps with
access and administration.

Deployable applications live under `apps/`, shared libraries under
`packages/`. The web console owns its `package.json` and `node_modules`. The
Python apps and packages are one [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/):
each keeps its own `pyproject.toml`, and they share one `uv.lock` and one
`.venv` at the root. The root orchestrates them all with one Compose stack and
one Makefile.

## Where things are

Folders are named as the console names things: **ADK workflows** (the
graph workflows, `agents` in URLs and the API from before they were called
that), **Agents** (Google ADK chat agents), **runs** (of ADK workflows) and
the **assistant** (the AI helper on every page).

| To change… | Look in |
|---|---|
| A page or its URL | `apps/forge-web/src/routes/`: `organizations/$organizationId.tsx` is an organization's workspace, `$organizationId_.agents.$agentId.tsx` the ADK workflow builder, `$organizationId_.chat-agents.$chatAgentId.tsx` the agent builder, `admin/` site administration |
| The app's frame: layout, sub nav, scope switcher, settings | `apps/forge-web/src/app/` |
| The ADK workflow builder's nodes and fields, and the ADK workflow format | `apps/forge-web/src/features/adk-workflows/` |
| The agent (chat agent) builder and its format | `apps/forge-web/src/features/agents/` |
| A run's page, lists of runs, starting a run, approvals and answers | `apps/forge-web/src/features/runs/` |
| The canvas, library, step dialog, layout and lines both builders share | `apps/forge-web/src/features/builder/` |
| Forge's steps (Approval, HTTP request, Transform, If / else…) and JSONata expressions | `apps/forge-web/src/features/steps/` |
| The assistant's panel and window | `apps/forge-web/src/features/assistant/` |
| Site administration screens | `apps/forge-web/src/features/admin/` |
| Buttons, dialogs, the shell and other design-system pieces | `packages/forge-ui/` (installed into `apps/forge-web/src/components/`; change them there) |
| An API endpoint | `apps/forge-admin-api/src/forge_admin/api/routes/`: `adk_workflows.py`, `adk_workflow_runs.py`, `assistant.py`, `organizations.py`, `members.py`, `users.py`, `roles.py`, `permissions.py` |
| Saving and checking ADK workflows, building a run's document, queuing a run | `apps/forge-admin-api/src/forge_admin/adk_workflows/` |
| The assistant's agent and its tools | `apps/forge-admin-api/src/forge_admin/assistant/` |
| Roles, permissions and tokens | `apps/forge-admin-api/src/forge_admin/auth/` |
| MySQL tables and migrations | `apps/forge-admin-api/src/forge_admin/models/`, `db/migrations/` |
| What the worker does with a run (start, pause, resume, retry) | `apps/forge-async-worker/src/forge_async_worker/` (`jobs.py`, `control.py`) |
| How an ADK workflow becomes a Google ADK graph, and each step's code | `packages/python/adk-workflows/src/forge_task_adk_workflows/` (`graph/`, `steps.py`, `support/`) |
| The run store (runs, their status and activity) | `packages/python/adk-workflows/src/forge_task_adk_workflows/run_store.py` |
| An organization's code repositories and their ingestions: the page, the routes, the queue table the worker claims from | `apps/forge-web/src/features/code-repositories/`, `apps/forge-admin-api/src/forge_admin/api/routes/code_repositories.py`, `models/code_repositories.py`, `apps/forge-codegraph-worker/internal/jobqueue/` |
| How a GitHub repository is ingested into the code graph (fetch, parse, resolve, load, embed) | `apps/forge-codegraph-worker/` (`internal/ingestion/`, `internal/languages/`, `internal/resolve/`) |
| The code graph's MCP tools, and the queries behind them | `apps/forge-codegraph-mcp/src/forge_codegraph_mcp/` (`tools/graph.py`, `graph/query/`, `graph/spanner.py`) |
| The code graph's storage, schema, search and the queries agents ask of it | `packages/go/code-graph/` (`storage/`, `agentquery/`) |
| Settings, ports and the Compose stack | `.env.common`, each app's `.env`, `compose.yaml`, `Makefile` |

## What it does

- **Organizations.** The hierarchy is site → organizations. ADK workflows,
  their runs, agents and members each belong to one organization. Site
  administrators manage organizations, users, roles and permissions.
- **Access control.** Casbin roles and permissions (`resource:action`), assigned
  to users in a scope (the site or an organization) and enforced by the API on
  every request. People and other systems call the API with bearer tokens.
- **ADK workflow builder.** Nodes: Start; LLM agents, sequential, parallel
  and loop agents, and saved ADK workflows; human input and approvals; HTTP,
  Transform and Delay; If / Switch / Match, Loop, Merge and End. An LLM agent
  is either set up in its node, with the Agents builder's tools (the
  organization's MCP servers, knowledge bases, HTTP tools, OpenAPI specs,
  agents and workflows), its sub-agents too; or it is an agent from the
  Agents page, used whole at the version picked, sent a message and its input
  fields. A tool a person confirms pauses the run until they allow it on the
  run's page, where each step lists the tools its agents called. Every
  expression is [JSONata](packages/python/jsonata/README.md). ADK workflows are
  saved as `forge.agent/v1` JSON documents in MongoDB, one per ADK workflow,
  with revision checks so concurrent edits never silently overwrite each
  other.
- **Runs.** The admin API checks a run's input and builds the document, takes
  along what its LLM agents use (the agents from the Agents page at their
  version, the knowledge bases' names), then
  starts the run in the run store (two tables in the admin MySQL) and queues
  it on the async worker's `adk_workflows` queue on Redis. The worker's ADK
  workflows task runs it on Google ADK's graph engine, its state an ADK
  session in the admin MySQL, from which the run page reads its steps. The
  run store keeps each run's status, attempts, failures and activity; the
  worker retries a hiccup with backoff, carries on a run cut off by a crash,
  and lets a run that waits (an approval, a person's answer, a long delay)
  give up its worker until it's answered.
- **Agents.** A builder for one Google ADK chat agent (`forge.chat_agent/v1`):
  its instructions and model, its tools and the agents it hands off to. Drafts
  are published as versions that never change (MongoDB). The hosted runtime
  (`/api/v1/runtime`, [forge-agent-runtime](packages/python/agent-runtime/README.md))
  runs `ca_x`, `ca_x@3` or `ca_x@draft` over ADK's run API (`run_sse`, for chat
  UIs) and Google's A2A protocol (`/runtime/a2a/{agent}`: JSON-RPC, A2A 1.0
  and 0.3, and each agent's card, for other agents), both public unless
  `FORGE_ADMIN_AGENT_RUNTIME_PUBLIC=false`. **Generate standalone agent**
  downloads the agent as a project of its own that speaks both too.
- **Outside apps.** Workflows are published as versions too, and outside apps
  run them by reference (`ag_x`, `ag_x@3`, `ag_x@draft`) over REST
  (`/api/v1/runtime/workflows/{ref}/runs`: start, wait, answer what a run
  waits at) or A2A (a task is a run; its pauses are `input-required`). Each
  organization makes **API keys** (Settings → API keys), each holding one of
  its roles (API caller by default), sent as `Authorization: Bearer fk_…`;
  when the runtime isn't public, calls need one (or a sign-in) with
  `agents:run` where the agent or workflow is.
- **Code ingestion.** The code graph worker
  ([forge-codegraph-worker](apps/forge-codegraph-worker/README.md)) turns a
  GitHub repository's branch into a graph of its code: declarations,
  references, calls, types and files, with each declaration's source and its
  embedding, published one generation per commit and updated incrementally.
  It analyses Java (javac, with Maven or Gradle), TypeScript/JavaScript (the
  TypeScript compiler) and Python (Pyright). The graph is in Spanner (the
  emulator locally) and is the engine for making codebases a knowledge
  base. An organization adds its GitHub repositories on the **Code
  repositories** tab of its Knowledge bases page (or
  `/api/v1/organizations/{org}/code-repositories`),
  each on one branch; the admin API queues each ingestion as a row of
  `code_ingestion_jobs` in its MySQL, and the worker claims it there, builds
  the graph and writes how it went back. A repository's graph is one per
  GitHub URL, shared by the organizations that have it. A **system design
  knowledge base** (Knowledge bases → Knowledge base → System design
  knowledge base) holds a system's applications, some of the organization's
  repositories, and how they connect: its page adds them (one of the
  organization's, or a GitHub URL), ingests them, draws them on a system map
  where people connect them (calls, sends events to, depends on, shares data
  with) and say where in the code each connection happens (code links, which
  the code graph follows across repositories), explores each one's code
  graph and audits its ingestions; chat agents and workflow LLM nodes search
  it with their knowledge base tool, as they search a RAG one's documents,
  through the worker's search API, are told its applications and
  connections, and cite the declarations they answer from. The code graph MCP server
  ([forge-codegraph-mcp](apps/forge-codegraph-mcp/README.md)) serves the
  published graphs to agents: read-only tools to explore a question, search,
  walk callers and callees, assess a change's impact and read exact source.
- **AI assistant.** A Google ADK agent served by the admin API (`/api/v1/agents`),
  on Gemini by default or the models of the shared model-provider YAML. It
  acts as the signed-in person, within their permissions: it explains their
  access and why something was refused, finds people and gives or removes
  roles, and reads and edits organizations, roles, permissions and users. It
  asks the person to confirm every change first.

## Services

| Service | Module | Port | Role |
|---|---|---|---|
| `web` | `apps/forge-web` | 5190 (dev), 18190 (Compose) | React web console on the Forge UI design system: the ADK workflow builder and its runs, the agent builder, administration of organizations, users, roles and permissions, and the assistant panel |
| `admin` | `apps/forge-admin-api` | 8101 (native), 18201 (Compose) | FastAPI API (`/api/v1`, docs at `/docs`): organizations, users, Casbin roles and permissions, ADK workflows (MongoDB), ADK workflow runs (starting, listing, deciding, retrying, from the run store), runs' steps from their ADK sessions, and the assistant |
| `admin-mysql` | `apps/forge-admin-api` | 13326 | MySQL 8.4 for the admin API (Alembic migrations), ADK workflow runs (the run store) and their sessions, with a persistent volume |
| `worker` | `apps/forge-codegraph-worker` | 8090 (native), 18090 (Compose) | Go code graph ingestion worker: claims ingestion jobs from the admin MySQL's `code_ingestion_jobs` and loads one graph generation per job into Spanner; health probes and its graph API on the same port |
| `codegraph-mcp` | `apps/forge-codegraph-mcp` | 8103 (native), 18203 (Compose) | Python MCP server (FastMCP, streamable HTTP at `/mcp`) over the worker's code graph: explore, search, callers and callees, impact, changes, path, hubs and source; reads the worker's Spanner database |
| `worker-spanner` | `apps/forge-codegraph-worker` | 19030 (gRPC), 19040 (REST) | Cloud Spanner emulator the worker writes the code graph to; in memory, so `make down` loses it (`SPANNER=cloud` uses managed Spanner instead) |
| `async-worker-adk-workflows` | `apps/forge-async-worker` | none (SAQ worker) | Async worker (`forge-async-worker`) serving the `adk_workflows` queue: runs organizations' ADK workflows on Google ADK, the runs and their sessions in the admin MySQL, pausing for approvals, questions and waits |
| `mongo` | `compose.infrastructure.yaml` | 27037 | Shared MongoDB (Atlas Local 8.3.9, a single-node replica set): ADK workflows (`forge_admin`); persistent volumes |
| `redis` | `compose.infrastructure.yaml` | 16389 | Shared Redis: the async worker's SAQ queue in database 0; persistent AOF, no eviction |

```text
 web console
      ▼
  admin API ──────► MySQL    organizations, users, roles, permissions; ADK workflow runs and sessions
    │     └───────► MongoDB  ADK workflows (forge_admin)
    ▼ queue a run (run_adk)
  Redis (SAQ, db 0)
    ▼
  async-worker-adk-workflows ──► MySQL  each run (the run store) and its ADK session

  admin API ──► MySQL (code_ingestion_jobs)  an organization's repository's ingestion, queued
                    ▲ claim (FOR UPDATE SKIP LOCKED), lease, outcome
  worker (code graph) ──► GitHub   clone and fetch the repository
                     └──► Spanner  each repository's code graph, its source and embeddings
```

MongoDB and Redis are defined once in [compose.infrastructure.yaml](compose.infrastructure.yaml),
and every application's `compose.yaml` is a fragment of the root deployment
([compose.yaml](compose.yaml)). Connection settings and published ports live in
`.env.common` (`FORGE_MONGO_*`, `FORGE_REDIS_PORT`, `FORGE_REDIS_URL`); app
`.env` files reference them. Redis database 15 is reserved for the SAQ
integration tests. `make infrastructure-check` validates the rendered stack:
its services, shared connections, queues, persistent storage and
loopback-only ports.

## Run locally

Requires Node.js 24+, [uv](https://docs.astral.sh/uv/) (it installs Python
3.13 itself), Go 1.25+ with a C compiler for the code graph worker (its
parsers and SQLite use cgo) and, for the container stack, Docker Compose
2.20.3+.

Put your names, email and MS ID in the `FORGE_ADMIN_SITE_ADMIN_*` settings of
`apps/forge-admin-api/.env` first (`make env` creates it): the seed makes you
the site administrator, and the web console signs in as you with a bearer
token. For the assistant and LLM nodes, set `FORGE_GOOGLE_API_KEY` in
`.env.common`. For the code graph worker to ingest private repositories, set
`FORGE_GITHUB_TOKEN` there too (public ones need none), and
`OPENAI_API_KEY` for it to embed them.

```sh
make infrastructure    # admin-mysql, migrations, and you as the site administrator
make web-token         # sign the web console in as you (VITE_API_TOKEN)
make start             # build and start web, admin, async-worker-*, worker, codegraph-mcp and their databases
make logs-admin
make down              # MySQL, MongoDB and Redis data persist in volumes; the Spanner emulator's don't
```

For native development, install once and run each app with reload:

```sh
make install           # npm ci in apps/forge-web and packages/forge-ui; uv sync --all-packages into the root .venv;
                       # the code graph worker's analyzers and Go modules
make web               # Vite dev server on http://localhost:5190
make admin-deps        # admin-mysql, and the shared mongo and redis
make admin             # admin API on http://localhost:8101; docs at /docs
make async-worker      # redis and admin-mysql, then the async worker on the adk_workflows queue
make worker            # the Spanner emulator, then the code graph worker on http://localhost:8090
make codegraph-mcp     # the code graph MCP server on http://localhost:8103/mcp, reading the worker's graph
```

Or run them all at once in one terminal, each output line prefixed with its
app. It starts the databases, then every app. An app that exits says so
without stopping the others; Ctrl-C stops them all, and the databases keep
running until `make down`. Use it instead of `make up`, not beside it, and
stop any app you already run natively first, since they share ports.

```sh
make up-all-local
```

### Settings

Values several apps share live once in `.env.common` at the root: the
admin's MySQL connection, the shared MongoDB and Redis, the model API
keys (`FORGE_GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`), and the
one embedding model and vector length everything embeds with
(`FORGE_EMBEDDING_MODEL`, `FORGE_EMBEDDING_DIMENSIONS` and
`FORGE_EMBEDDING_BASE_URL`): the knowledge bases' chunks and searches, and the
code graph and its MCP server's questions, so none can disagree. Changing
them means embedding everything again (see `.env.common.example`). `make env`
points every app's embedding settings at them, and `make
infrastructure-check` fails when an app's template or Compose service doesn't. Everything else lives with its app:
`apps/forge-admin-api/.env`, `apps/forge-async-worker/.env`,
`apps/forge-codegraph-worker/.env` and `apps/forge-codegraph-mcp/.env`, plus
`apps/forge-web/.env.local` (see `apps/forge-web/.env.example`). `make env`
creates each from its `.env.example` (`.env.common` from
`.env.common.example`, `.env.compose` from `.env.compose.example`) and
generates the admin API's JWT secret, once.

An app's `.env` names each shared value it uses with a `${NAME}` reference,
for example `HYBRID_REDIS_URL=${FORGE_REDIS_URL}`;
`.env.common` never reaches an app by itself, so an app only gets the shared
values it references. A reference resolves to an earlier line of the same
file, otherwise to `.env.common`, and a name defined in neither stops the app
at startup. The process environment still overrides any app setting. Native
runs, including IDE run configurations started in `apps/<name>`, read
`../../.env.common`; `FORGE_ENV_COMMON_FILE` points elsewhere, or disables it
when empty. Compose resolves the references itself from `.env.common`, which
the Makefile passes with `--env-file`. The web console's `VITE_*` values are
build-time only and share nothing.

LLM nodes and the assistant run on Gemini with `FORGE_GOOGLE_API_KEY`
unless an app's `.env` names the shared model-provider YAML
(`packages/python/common/src/forge_common/model_provider`), which can offer
OpenAI's and Anthropic's models with the keys it references.

Host ports and build arguments for the stack go in `.env.compose`, created
from `.env.compose.example`. `VITE_API_URL` is inlined into the web bundle at
build time, so change it there and run `make restart` to rebuild the image.

## Layout

```text
apps/
  forge-web/              React web console (Vite, TanStack Router and Query, Forge UI)
    src/                  routes/ (pages and URLs), app/ (the frame: layout, sub nav, settings), features/ (adk-workflows,
                          agents, runs, builder, steps, json, assistant, organizations, admin), components/ and lib/
                          (the Forge UI design system and app-wide helpers)
    tests/                Builder steps, ADK workflow and agent tests, timestamp helpers (node --test)
    scripts/              Generates the builder's copy of the agent JSON Schema, and checks it
    Dockerfile            Static build served by unprivileged nginx, built from the repository root
    compose.yaml          The web service
    nginx.conf            SPA fallback and cache headers
    .env.example          Dev-server settings template
    package.json          The app's scripts and dependencies
    package-lock.json     The app's lockfile; node_modules is installed beside it
  forge-admin-api/        FastAPI admin API over MySQL and MongoDB
    src/forge_admin/      api/ (app, routes), auth/ (Casbin, tokens), assistant/ (the in-app assistant), cli/, db/ (sessions,
                          migrations), models/, adk_workflows/ (ADK workflow documents and agent.schema.json, building, runs,
                          the worker's queue)
    tests/                Unit and MySQL integration tests
    Dockerfile            uv-built image running as a non-root user, built from the repository root
    compose.yaml          The admin service, its admin-mysql database and the admin-seed tool
    alembic.ini           Alembic CLI settings for creating and applying revisions
    .env.example          Service settings template
    pyproject.toml        The app's dependencies and tool settings (a workspace member)
  forge-async-worker/     forge-async-worker: runs ADK workflow runs off the adk_workflows SAQ queue on Redis, kept in the run store
    src/forge_async_worker/  saq_worker.py (run_adk, expire_pause, the maintain cron, graceful stop, health check), jobs.py
                          (what each job does to a run), control.py (a run's JobControl on the run store), queue.py, cli.py
    tests/                The jobs on a SQLite run store, with a stand-in task and the real ADK one; the worker on a real Redis
    Dockerfile            uv-built worker image with the ADK workflows task, built from the repository root
    compose.yaml          async-worker-adk-workflows
    .env.example          Worker and task settings template
    pyproject.toml        The app's dependencies (the ADK workflows task among them) and tool settings (a workspace member)
  forge-codegraph-worker/ Go code ingestion engine: GitHub repositories into a code graph in Spanner (see its README)
    cmd/                  codegraph-worker (the process) and codegraph-schema (the Spanner DDL)
    internal/             workerapp/ (config, job loop, health, admission and graph API), ingestion/ (the pipeline),
                          languages/, parser/, resolve/ (Java, TypeScript, Python), buildcontext/, discovery/,
                          graphanalysis/, localindex/, repository/github/
    python-analyzer/, typescript-analyzer/  The Pyright and TypeScript compiler bridges it runs under Node
    docs/                 Design notes: architecture, language routing, build context, IR, parser contract
    Dockerfile            Go build with the JDK, Maven, Node and uv it analyses projects with, built from the repository root
    compose.yaml          worker and worker-spanner (the Spanner emulator)
    .env.example          Worker settings template
    go.mod, go.sum        The module and its dependencies; replaces point at packages/go/code-graph
  forge-codegraph-mcp/    Python MCP server over the code graph, on the template-mcp design (see its README)
    src/forge_codegraph_mcp/  server.py (ServerBuilder), core/ (settings, auth, FastMCP factory), routes/, tools/,
                          graph/ (the Spanner reader and the graph queries, ported from packages/go/code-graph)
    tests/                Pytest suite on an in-memory graph; no Spanner needed
    Dockerfile            uv workspace build, from the repository root
    compose.yaml          codegraph-mcp, reading the worker's database
    .env.example          Server settings template
packages/                 Shared Python and Go libraries and the Forge UI design system (see packages/README.md)
  forge-ui/               Forge UI: shadcn primitives, Forge composites, theme and libraries, a demo app, and the
                          shadcn registry apps install them from (registry.json, public/r)
  python/common/          forge-common: ADK toolset helpers and the shared model-provider YAML and loader
  python/jsonata/         forge-jsonata: the JSONata engine ADK workflows' expressions run on
  python/task-sdk/        forge-tasks: the contract between the async worker and a task package
  python/adk-workflows/   forge-task-adk-workflows: the ADK workflows task, which builds and runs ADK workflows,
                          and the run store their runs are kept in
  go/code-graph/          The Go modules the code graph worker links: domain, storage (Spanner), agentquery,
                          serviceconfig, authorization and the audited tree-sitter-java grammar
tests/infrastructure/     Validates the rendered Compose stack (make infrastructure-check)
.claude/                  Agent skills for the Forge UI, data and state conventions; dev-server launch config
.github/workflows/        forge-ui-registry.yml: Forge UI's typecheck, tests and committed-registry check
registry.json             Includes packages/forge-ui/registry.json, for shadcn's owner/repo/item addresses
compose.yaml              Includes compose.infrastructure.yaml and each app's compose.yaml
compose.infrastructure.yaml  The shared MongoDB and Redis
compose.cloud.yaml        Overlay that puts the code graph worker and MCP server on managed Cloud Spanner (make up SPANNER=cloud)
go.work, go.work.sum      The Go workspace: the code graph worker and its modules
.env.compose.example      Root Compose settings template
.env.common.example       Settings several apps share, which each app's .env references
Makefile                  Checks and local development commands for every app
pyproject.toml            The uv workspace: its members (every Python app and package) and the tools their checks run
uv.lock, .python-version  The workspace's one lockfile and Python version; .venv is installed beside them
PRODUCT.md, DESIGN.md     Product context and the design system, for people and design agents
```

Each application is self-contained: its own dependency manifest,
`Dockerfile`, `compose.yaml`, `.env.example` and README. The web console has
its own lockfile; the Python apps share the workspace's `uv.lock`, and each
image installs only its own app's dependencies from it (`uv sync --package`).
There is no root `package.json` or `node_modules`. Dockerfiles use the
repository root as their build context so they can also copy the workspace
and the shared packages. Dependencies flow from `apps/` to `packages/`; packages never import
application code, and no application imports another.

To add an application, create `apps/<name>/` with the same files, include its
`compose.yaml` in the root `compose.yaml`, and add it to `SERVICES` and the
install and check targets in the `Makefile`; a Python app also joins the
workspace's `members` in the root `pyproject.toml`.

Run `make` from the repository root. Run `npm` or `uv` inside the app it
belongs to, for example `cd apps/forge-web && npm install <pkg>` or
`cd apps/forge-admin-api && uv add <pkg>`, and `go` inside the Go module it
belongs to, for example `cd apps/forge-codegraph-worker && go get <module>`.

## Validation

```sh
make check             # Compose stack; web typecheck, lint, tests and build; admin lint, format and unit tests;
                       # async worker and task packages lint, format, types and unit tests; code graph worker vet and
                       # unit tests; code graph MCP server lint, format, types and unit tests; forge-common and JSONata
                       # checks; Forge UI typecheck, tests and committed registry
make admin-test-mysql  # admin migrations and readiness against real MySQL
make async-worker-test-redis  # the async worker's SAQ queue against redis
make worker-test-spanner      # the code graph's storage and ingestion pipeline against the Spanner emulator
make docker-build      # build every service image
```
