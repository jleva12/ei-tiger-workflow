# ei-tiger-agent-workflow-builder

Forge builds and runs **Google ADK workflows** and designs **Google ADK
agents**: a web console with visual builders for both, the API that keeps
organizations' ADK workflows and decides who may do what, and an async worker
that runs them on [Google ADK](https://google.github.io/adk-docs/)'s graph
engine. People draw an ADK workflow on a canvas: LLM agents and teams of
them, other saved ADK workflows, human approvals and questions, HTTP calls,
data transforms, delays, branches and loops. They run it from the console
and watch each run step by step. An agent is one Google ADK chat agent with
its tools and the sub-agents it hands off to; for now the console keeps
agents in the browser, and nothing runs them yet. An AI assistant on every
page helps with access and administration.

Deployable applications live under `apps/`, shared libraries under
`packages/`. Each application owns its dependencies (`package.json` and
`node_modules`, or `pyproject.toml`, `uv.lock` and `.venv`); the root only
orchestrates them with one Compose stack and one Makefile.

## What it does

- **Organizations.** The hierarchy is site → organizations. ADK workflows,
  their runs, agents and members each belong to one organization. Site
  administrators manage organizations, users, roles and permissions.
- **Access control.** Casbin roles and permissions (`resource:action`), assigned
  to users in a scope (the site or an organization) and enforced by the API on
  every request. People and other systems call the API with bearer tokens.
- **ADK workflow builder.** Nodes: Start; LLM agents, sequential, parallel
  and loop agents, and saved ADK workflows; human input and approvals; HTTP,
  Transform and Delay; If / Switch / Match, Loop, Merge and End. Every
  expression is [JSONata](packages/python/jsonata/README.md). ADK workflows are
  saved as `forge.agent/v1` JSON documents in MongoDB, one per ADK workflow,
  with revision checks so concurrent edits never silently overwrite each
  other.
- **Runs.** The admin API checks a run's input and builds the document, then
  submits the run to the async worker's `adk_workflows` queue on Redis. The
  worker's ADK workflows task runs it on Google ADK's graph engine, its state
  an ADK session in the admin MySQL, from which the run page reads its steps.
  Each run is also a tracked run of the
  [enhanced task framework](packages/python/enhanced-task-framework), which
  keeps its attempts, failures and audit trail, restarts a run cut off by a
  crash, and lets a run that waits (an approval, a person's answer, a long
  delay) give up its worker until it's answered.
- **Agents.** A builder for one Google ADK chat agent (`forge.chat_agent/v1`):
  its instructions and model, its tools and the agents it hands off to. The
  console keeps them in the browser for now; the API and a runtime come later.
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
| `admin` | `apps/forge-admin-api` | 8101 (native), 18201 (Compose) | FastAPI API (`/api/v1`, docs at `/docs`): organizations, users, Casbin roles and permissions, ADK workflows (MongoDB), run submission to the async worker, runs' steps from their ADK sessions, and the assistant |
| `admin-mysql` | `apps/forge-admin-api` | 13326 | MySQL 8.4 for the admin API (Alembic migrations) and ADK workflow runs' sessions, with a persistent volume |
| `async-worker-adk-workflows` | `apps/forge-async-worker` | none (SAQ worker) | Async worker (`forge-async-worker`) serving the `adk_workflows` queue: runs organizations' ADK workflows on Google ADK, their sessions in the admin MySQL, pausing for approvals, questions and waits |
| `async-worker-api` | `apps/forge-async-worker` | 8104 (native), 18204 (Compose) | Background tasks API: what the task framework recorded of each run (attempts, steps, audit trail), and resubmitting, restarting, abandoning or deciding one. The admin API relays it to the web console, checking who may see and act |
| `mongo` | `compose.infrastructure.yaml` | 27037 | Shared MongoDB (Atlas Local 8.3.9, a single-node replica set): ADK workflows (`forge_admin`) and the task framework's runs (`forge_tasks`); persistent volumes |
| `redis` | `compose.infrastructure.yaml` | 16389 | Shared Redis: the async worker's SAQ queues and locks in database 0; persistent AOF, no eviction |

```text
 web console
      ▼
  admin API ──────► MySQL             organizations, users, roles, permissions; ADK sessions
    │  │  └───────► MongoDB           ADK workflows (forge_admin)
    │  └──────────► async-worker-api  runs as background tasks (FORGE_ASYNC_WORKER_TOKEN)
    ▼ start a run
  Redis (SAQ, db 0)
    ▼
  async-worker-adk-workflows ──► MySQL    each run's ADK session, read back for its steps
                             └─► MongoDB  runs, attempts and audit trail (forge_tasks)
```

MongoDB and Redis are defined once in [compose.infrastructure.yaml](compose.infrastructure.yaml),
and every application's `compose.yaml` is a fragment of the root deployment
([compose.yaml](compose.yaml)). Connection settings and published ports live in
`.env.common` (`FORGE_MONGO_*`, `FORGE_REDIS_PORT`, `FORGE_REDIS_URL`); app
`.env` files reference them. Redis database 15 is reserved for the SAQ
integration tests. `make infrastructure-check` validates the rendered stack:
its services, shared connections, queues, service tokens, persistent storage
and loopback-only ports.

## Run locally

Requires Node.js 24+, [uv](https://docs.astral.sh/uv/) (it installs Python
3.13 itself) and, for the container stack, Docker Compose 2.20.3+.

Put your names, email and MS ID in the `FORGE_ADMIN_SITE_ADMIN_*` settings of
`apps/forge-admin-api/.env` first (`make env` creates it): the seed makes you
the site administrator, and the web console signs in as you with a bearer
token. For the assistant and LLM nodes, set `FORGE_GOOGLE_API_KEY` in
`.env.common`.

```sh
make infrastructure    # admin-mysql, migrations, and you as the site administrator
make web-token         # sign the web console in as you (VITE_API_TOKEN)
make start             # build and start web, admin, async-worker-* and their databases
make logs-admin
make down              # MySQL, MongoDB and Redis data persist in volumes
```

For native development, install once and run each app with reload:

```sh
make install           # npm ci in apps/forge-web, uv sync in apps/forge-admin-api and apps/forge-async-worker
make web               # Vite dev server on http://localhost:5190
make admin-deps        # admin-mysql, and the shared mongo and redis
make admin             # admin API on http://localhost:8101; docs at /docs
make async-worker      # mongo and redis, then the async worker on the adk_workflows queue, with its schedules
make async-worker-api  # the background tasks API on http://localhost:8104, which the admin API shows runs with
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
admin's MySQL connection, the shared MongoDB and Redis, the tokens the
services call each other with (`FORGE_ASYNC_WORKER_TOKEN`) and the model API keys (`FORGE_GOOGLE_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). Everything else lives with its app:
`apps/forge-admin-api/.env` and `apps/forge-async-worker/.env`, plus
`apps/forge-web/.env.local` (see `apps/forge-web/.env.example`). `make env`
creates each from its `.env.example` (`.env.common` from
`.env.common.example`, `.env.compose` from `.env.compose.example`) and
generates the token and the admin API's JWT secret, once.

An app's `.env` names each shared value it uses with a `${NAME}` reference,
for example `HYBRID_API__TOKEN=${FORGE_ASYNC_WORKER_TOKEN}`;
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
    src/                  Routes, the ADK workflow and agent builders, Forge composites, shadcn primitives, API and state layers
    tests/                Builder steps, ADK workflow and agent tests, timestamp helpers (node --test)
    scripts/              Generates the builder's copy of the agent JSON Schema, and checks it
    Dockerfile            Static build served by unprivileged nginx, built from the repository root
    compose.yaml          The web service
    nginx.conf            SPA fallback and cache headers
    .env.example          Dev-server settings template
    package.json          The app's scripts and dependencies
    package-lock.json     The app's lockfile; node_modules is installed beside it
  forge-admin-api/        FastAPI admin API over MySQL and MongoDB
    src/forge_admin/      api/ (app, routes), auth/ (Casbin, tokens), agents/ (the assistant), cli/, db/ (sessions, migrations),
                          models/, agent_documents.py and agent.schema.json (the ADK workflow format), adk_runs.py
    tests/                Unit and MySQL integration tests
    Dockerfile            uv-built image running as a non-root user, built from the repository root
    compose.yaml          The admin service, its admin-mysql database and the admin-seed tool
    alembic.ini           Alembic CLI settings for creating and applying revisions
    .env.example          Service settings template
    pyproject.toml        The app's dependencies and tool settings
    uv.lock               The app's lockfile; .venv is installed beside it
  forge-async-worker/     forge-async-worker: generic async task worker (SAQ on Redis), every job tracked by the enhanced task framework
    src/forge_async_worker/  saq_worker.py (run_job, schedules, upkeep), job_control.py, etf_jobs.py, api.py (background tasks API),
                          queue.py, cli.py; no task-specific code
    tests/                The SAQ job, retries and re-drives with toy tasks on in-memory stores; the worker on a real Redis
    Dockerfile            uv-built worker image with the bundled task packages, built from the repository root
    compose.yaml          async-worker-adk-workflows and async-worker-api
    .env.example          Worker and task settings template
    pyproject.toml        The app's dependencies (the bundled task package is the adk-workflows extra) and tool settings
    uv.lock               The app's lockfile; .venv is installed beside it
packages/                 Shared Python libraries and the Forge UI design system (see packages/README.md)
  forge-ui/               Forge UI: shadcn primitives, Forge composites, theme and libraries, a demo app, and the
                          shadcn registry apps install them from (registry.json, public/r)
  python/common/          forge-common: ADK toolset helpers and the shared model-provider YAML and loader
  python/jsonata/         forge-jsonata: the JSONata engine ADK workflows' expressions run on
  python/enhanced-task-framework/  etf: tracks, audits, pauses and recovers async job runs (each ADK workflow run)
  python/tasks/task-sdk/  forge-tasks: the contract between the async worker and a task package
  python/tasks/adk-workflows/  forge-task-adk-workflows: the ADK workflows task, which builds and runs ADK workflows
tests/infrastructure/     Validates the rendered Compose stack (make infrastructure-check)
.claude/                  Agent skills for the Forge UI, data and state conventions; dev-server launch config
.github/workflows/        forge-ui-registry.yml: Forge UI's typecheck, tests and committed-registry check
registry.json             Includes packages/forge-ui/registry.json, for shadcn's owner/repo/item addresses
compose.yaml              Includes compose.infrastructure.yaml and each app's compose.yaml
compose.infrastructure.yaml  The shared MongoDB and Redis
.env.compose.example      Root Compose settings template
.env.common.example       Settings several apps share, which each app's .env references
Makefile                  Checks and local development commands for every app
PRODUCT.md, DESIGN.md     Product context and the design system, for people and design agents
```

Each application is self-contained: its own dependency manifest and
lockfile, `Dockerfile`, `compose.yaml`, `.env.example` and README. There is no
root `package.json`, `node_modules` or virtual environment. Dockerfiles use
the repository root as their build context so they can also copy shared
packages. Dependencies flow from `apps/` to `packages/`; packages never import
application code, and no application imports another.

To add an application, create `apps/<name>/` with the same files, include its
`compose.yaml` in the root `compose.yaml`, and add it to `SERVICES` and the
install and check targets in the `Makefile`.

Run `make` from the repository root. Run `npm` or `uv` inside the app it
belongs to, for example `cd apps/forge-web && npm install <pkg>` or
`cd apps/forge-admin-api && uv add <pkg>`.

## Validation

```sh
make check             # Compose stack; web typecheck, lint, tests and build; admin lint, format and unit tests;
                       # async worker and task packages lint, format, types and unit tests; forge-common, JSONata and etf checks;
                       # Forge UI typecheck, tests and committed registry
make admin-test-mysql  # admin migrations and readiness against real MySQL
make async-worker-test-redis  # the async worker's SAQ queues against redis
make docker-build      # build every service image
```
