# ei-tiger-agent-workflow-builder

Forge's standalone **business workflow engine**: a visual workflow builder
in a web console, the API that keeps organizations' workflows and decides
who may do what, and an async worker that runs them. People draw a workflow
as steps on a canvas: LLM agent steps, human approvals, HTTP calls, data
transforms, delays, branches and loops. They run it, from the console or
from another workflow, and watch each run step by step. Other systems send
an organization events through inbound webhooks, and a workflow's Start
step names the event types meant to start it. An AI assistant on every page
helps build, run and explain workflows.

Deployable applications live under `apps/`, shared libraries under
`packages/`. Each application owns its dependencies (`package.json` and
`node_modules`, or `pyproject.toml`, `uv.lock` and `.venv`); the root only
orchestrates them with one Compose stack and one Makefile.

## What it does

- **Organizations.** The hierarchy is site → organizations. A workflow, its
  runs, its event types and its members belong to one organization. Site
  administrators manage organizations, users, roles and permissions.
- **Access control.** Casbin roles and permissions (`resource:action`), assigned
  to users in a scope (the site or an organization) and enforced by the API on
  every request. People and other systems call the API with bearer tokens.
- **Workflow builder.** Steps: Start, Agent (an LLM call, no tools), Approval,
  HTTP, Transform, Delay, Run workflow, If / Switch / Match, Loop, Merge and End.
  Every expression is [JSONata](packages/python/jsonata/README.md), evaluated by
  the worker. Workflows are saved as `forge.workflow/v1` JSON documents in
  MongoDB, one per workflow, with revision checks so concurrent edits never
  silently overwrite each other.
- **Runs.** The admin API submits a run to the async worker's Redis queue. The
  worker runs it step by step as a tracked run of the
  [enhanced task framework](packages/python/enhanced-task-framework): status,
  step outputs, failures and an audit trail are kept, a run cut off by a crash
  is restarted, and a run that waits (an approval, a long delay, another
  workflow) lets its worker go.
- **Event triggers.** Each organization defines event types, each with a JSON
  Schema, and turns on an inbound endpoint
  (`/hooks/events/<endpoint>/<event type>`, authenticated with the endpoint's
  token). Every event is kept and checked against its schema. A workflow's
  Start step names the event types that trigger it; the link lives in the
  workflow document, so the event type page and the builder edit the same
  setting.
- **AI assistant.** A Google ADK agent served by the admin API (`/api/v1/agents`),
  on Gemini by default or the models of the shared model-provider YAML. It
  acts as the signed-in person, within their permissions: it drafts a workflow
  from what they describe and checks it as the builder and the runner would,
  creates or saves it, runs workflows, follows their runs and decides
  approvals, and answers questions about events and access. It asks the
  person to confirm every change first.

## Services

| Service | Module | Port | Role |
|---|---|---|---|
| `web` | `apps/forge-web` | 5190 (dev), 18190 (Compose) | React web console on the Forge UI design system: the workflow builder, runs, events, administration of organizations, users, roles and permissions, and the assistant panel |
| `admin` | `apps/forge-admin-api` | 8101 (native), 18201 (Compose) | FastAPI API (`/api/v1`, docs at `/docs`): organizations, users, Casbin roles and permissions, workflows (MongoDB), event types and inbound events, run submission to the async worker, the service routes the worker calls back (`/internal/workflows`), and the assistant |
| `admin-mysql` | `apps/forge-admin-api` | 13326 | MySQL 8.4 for the admin API (Alembic migrations), with a persistent volume |
| `async-worker-workflows` | `apps/forge-async-worker` | none (SAQ worker) | Async worker (`forge-async-worker`) serving the `workflows` queue: runs organizations' workflows from the builder, pausing for approvals and waits |
| `async-worker-adk-workflows` | `apps/forge-async-worker` | none (SAQ worker) | The same worker serving the `adk_workflows` queue: runs organizations' ADK workflows on Google ADK, their sessions in the admin MySQL |
| `async-worker-api` | `apps/forge-async-worker` | 8104 (native), 18204 (Compose) | Background tasks API: what the task framework recorded of each run (attempts, steps, audit trail), and resubmitting, restarting, abandoning or deciding one. The admin API relays it to the web console, checking who may see and act |
| `mongo` | `compose.infrastructure.yaml` | 27037 | Shared MongoDB (Atlas Local 8.3.9, a single-node replica set): workflows (`forge_admin`) and the task framework's runs (`forge_tasks`); persistent volumes |
| `redis` | `compose.infrastructure.yaml` | 16389 | Shared Redis: the async worker's SAQ queues and locks in database 0; persistent AOF, no eviction |

```text
 web console ───────┐
 other systems ─────┤ /hooks/events/…
                    ▼
                admin API ──────► MySQL             organizations, users, roles, permissions, events
                  │  │  └───────► MongoDB           workflows (forge_admin)
                  │  └──────────► async-worker-api  runs as background tasks (FORGE_ASYNC_WORKER_TOKEN)
                  ▼ start a run
                Redis (SAQ, db 0)
                  ▼
                async-worker-workflows ──► MongoDB  runs, steps and audit trail (forge_tasks)
                  └──► admin API /internal/workflows  Run workflow steps (FORGE_WORKFLOWS_TOKEN)
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
token. For the assistant and agent steps, set `FORGE_GOOGLE_API_KEY` in
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
make async-worker      # mongo and redis, then the async worker on the workflows queue, with its schedules
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
services call each other with (`FORGE_ASYNC_WORKER_TOKEN`,
`FORGE_WORKFLOWS_TOKEN`) and the model API keys (`FORGE_GOOGLE_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). Everything else lives with its app:
`apps/forge-admin-api/.env` and `apps/forge-async-worker/.env`, plus
`apps/forge-web/.env.local` (see `apps/forge-web/.env.example`). `make env`
creates each from its `.env.example` (`.env.common` from
`.env.common.example`, `.env.compose` from `.env.compose.example`) and
generates the tokens and the admin API's JWT secret, once.

An app's `.env` names each shared value it uses with a `${NAME}` reference,
for example `HYBRID_WORKFLOWS__ADMIN_TOKEN=${FORGE_WORKFLOWS_TOKEN}`;
`.env.common` never reaches an app by itself, so an app only gets the shared
values it references. A reference resolves to an earlier line of the same
file, otherwise to `.env.common`, and a name defined in neither stops the app
at startup. The process environment still overrides any app setting. Native
runs, including IDE run configurations started in `apps/<name>`, read
`../../.env.common`; `FORGE_ENV_COMMON_FILE` points elsewhere, or disables it
when empty. Compose resolves the references itself from `.env.common`, which
the Makefile passes with `--env-file`. The web console's `VITE_*` values are
build-time only and share nothing.

Agent steps and the assistant run on Gemini with `FORGE_GOOGLE_API_KEY`
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
    src/                  Routes, the workflow builder, Forge composites, shadcn primitives, API and state layers
    tests/                Workflow expression and layout tests, timestamp helpers (node --test)
    scripts/              Generates the builder's copy of the workflow JSON Schema, and checks it
    Dockerfile            Static build served by unprivileged nginx, built from the repository root
    compose.yaml          The web service
    nginx.conf            SPA fallback and cache headers
    .env.example          Dev-server settings template
    package.json          The app's scripts and dependencies
    package-lock.json     The app's lockfile; node_modules is installed beside it
  forge-admin-api/        FastAPI admin API over MySQL and MongoDB
    src/forge_admin/      api/ (app, routes), auth/ (Casbin, tokens), agents/ (the assistant), cli/, db/ (sessions, migrations),
                          models/, workflows.py and workflow.schema.json (the workflow format), events.py
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
    compose.yaml          async-worker-workflows, async-worker-adk-workflows and async-worker-api
    .env.example          Worker and task settings template
    pyproject.toml        The app's dependencies (the bundled task packages are the workflows and adk-workflows extras) and tool settings
    uv.lock               The app's lockfile; .venv is installed beside it
packages/                 Shared Python libraries (see packages/README.md)
  python/common/          forge-common: ADK toolset helpers and the shared model-provider YAML and loader
  python/jsonata/         forge-jsonata: the JSONata engine workflow expressions run on
  python/enhanced-task-framework/  etf: tracks, audits, pauses and recovers async job runs
  python/tasks/task-sdk/  forge-tasks: the contract between the async worker and a task package
  python/tasks/workflows/ forge-task-workflows: the workflows task, which runs a workflow's steps
tests/infrastructure/     Validates the rendered Compose stack (make infrastructure-check)
.claude/                  Agent skills for the Forge UI, data and state conventions; dev-server launch config
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
                       # async worker and task packages lint, format, types and unit tests; forge-common, JSONata and etf checks
make admin-test-mysql  # admin migrations and readiness against real MySQL
make async-worker-test-redis  # the async worker's SAQ queues against redis
make docker-build      # build every service image
```
