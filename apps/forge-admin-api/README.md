# Forge admin API

`forge-admin` is the administration service of Forge, where organizations
build and run Google ADK workflows: FastAPI on Python 3.13, backed by its own
MySQL 8.4 database through async SQLAlchemy (`aiomysql`), with Alembic
migrations applied at startup, and MongoDB for organizations' ADK workflows
(`pymongo`'s async client). It keeps the organizations, the people and their
access, each organization's ADK workflows, and their runs (in MySQL, beside
their ADK sessions), which the async worker
([apps/forge-async-worker](../forge-async-worker/README.md)) takes from its
queue and runs; and it serves the web console's assistant. Package
`forge-admin`, module `forge_admin`.

## How it's put together

`ApiServer` (in `api/server.py`) wraps the FastAPI setup in one class:

```python
server = ApiServer(settings, routers=[projects.router], middleware=[...])
app = server.create_app()  # built once, the same object afterwards
```

- **Lifespan**: `ApiServer.lifespan` creates the MySQL engine and
  sessionmaker and the Casbin enforcer at startup, and keeps them on
  `app.state` with the clients the routes use: the ADK workflow runs
  (`adk_runs`, the run store on the engine), the async worker's job queues on
  Redis (`embedding`, `adk_workflows/queue.py`), the agents' (ADK workflows')
  MongoDB store (`organization_agents`, `adk_workflows/documents.py`), the
  assistant (`agents`, `assistant/runtime.py`) and the ADK workflow runs'
  sessions (`adk_run_sessions`, ADK's `DatabaseSessionService` on the
  engine). The queues and the MongoDB store are `None` when their settings
  are unset. Nothing connects until it's used, so the API starts while MySQL,
  MongoDB or Redis are down, and `/health/ready` reports MySQL.
- **Routes**: the health probes mount at the root. `/info` and every router
  in `ROUTERS` mount below `FORGE_ADMIN_API_PREFIX` (`/api/v1`).
- **Sign-in**: requests identify the user with an `Authorization: Bearer <JWT>`
  header whose `sub` is their user ID (`auth/security.py`, `auth/tokens.py`).
- **API key**: when `FORGE_ADMIN_API_KEY` is set, every route below the
  prefix requires it in the `X-API-Key` header. The health probes stay open.
- **Public routes**: routers in `PUBLIC_ROUTERS` mount at the root with
  neither the API key nor a user, and check their callers themselves. There
  are none at the moment.
- **Middleware**: CORS is added when `FORGE_ADMIN_CORS_ORIGINS` is set. Pass
  any other Starlette `Middleware` in `middleware=`.

`api/app.py` decides which routers the app serves (`ROUTERS`, and
`PUBLIC_ROUTERS` for callers that aren't Forge users); that's the only file
to touch when you add one.

The package is grouped by concern:

```text
src/forge_admin/
  __main__.py            python -m forge_admin: runs cli/serve.py
  config.py              FORGE_ADMIN_* settings
  env_files.py           .env's ${NAME} references to the repository's .env.common
  adk_workflows/         Organizations' ADK workflows (agents in the API) and their runs
    documents.py         Their documents in MongoDB: checking them, AgentStore
    document_store.py    Organizations' documents in MongoDB: one record per document, revisions, the store
    agent.schema.json    The forge.agent/v1 format's JSON Schema (generated from the web console)
    build.py             Building a document into an ADK Workflow (forge_task_adk_workflows.graph)
    runs.py              ADK workflow runs: checking the input and the build, starting them, answers
    queue.py             The async worker's SAQ job queues on Redis, where jobs that take runs are queued
  assistant/             The in-app assistant (the web console's Forge assistant): Google ADK agents
    __init__.py          ADK_TABLES: the tables ADK manages itself in the admin database
    forge.py             The supervisor, a specialist per toolset, and the toolsets
    screens.py           What the assistant has on each screen, from screens.yaml
    screens.yaml         The bundled screen configuration
    language_models.py   The models it runs on, and those a conversation may choose
    person.py            Who it talks to, looked up from their sign-in
    page_context.py      The page they're on, as the web console sends it: checked, then described
    person_api.py        This API, called in-process as the person
    user_tokens.py       Short-lived tokens for those calls
    route_tools.py       RouteToolset: the base of the toolsets over this API's routes
    access_tools.py      The access toolset
    admin_tools.py       The administration toolset
    runtime.py           AgentRuntime: a runner per agent, the session and artifact services
    wire.py              ADK's models as the JSON the web console reads
  api/                   The HTTP service
    app.py               ROUTERS, PUBLIC_ROUTERS and create_app(): what this service serves
    server.py            ApiServer: FastAPI app, lifespan, routers, middleware
    routes/              One router per resource; common.py holds shared request pieces: adk_workflows.py
                         (ADK workflows, /organizations/{id}/agents), adk_workflow_runs.py (their runs),
                         assistant.py (the assistant's conversations, /agents)
  auth/                  Who is calling and what they may do
    security.py          authenticate: the optional API key, the bearer token, the audit actor
    tokens.py            Minting and verifying bearer tokens (JWTs)
    access.py            Scopes, their Casbin domains, CurrentUser and authorize()
    authorization.py     The enforcer, key formats, and reading and writing policy lines
    casbin_model.conf    The Casbin model every enforcer shares
  cli/                   The console commands
    serve.py             forge-admin: logging, migrations, uvicorn
    seed.py              forge-admin-seed: the site administrator
    token.py             forge-admin-token: a bearer token for a user
  db/                    Persistence
    base.py              Base and AuditBase, which every model derives from; column types
    session.py           Engine, per-request sessions (get_session), ping
    audit.py             The actor and clock behind the audit columns
    migrate.py           Applying the migrations in-process
    migrations/          Alembic environment and the revisions, the baseline with the default roles
  models/                hierarchy.py (organizations), users.py (users), authorization.py
                         (roles, permissions, casbin_rule)
```

The tests mirror it: `tests/adk_workflows`, `tests/assistant`, `tests/api`,
`tests/auth`, `tests/cli`, `tests/db`, `tests/integrations` (routes with their
stores, the worker's queue and sessions stood in: on SQLite, or against MySQL)
and unit tests at the top, with `conftest.py` shared.

## Adding a router

```python
# src/forge_admin/api/routes/projects.py
from fastapi import APIRouter

from forge_admin.api.routes.common import Session
from forge_admin.auth.access import CurrentUser

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("")
async def list_projects(user: CurrentUser, session: Session) -> list[str]: ...
```

Then add it to `ROUTERS` in `api/app.py`. It's served at `/api/v1/projects`,
behind the API key when one is set. `CurrentUser` answers 401 without a
signed-in user; `authorize(session, enforcer, user, "<permission>", scope)`
(`auth/access.py`) checks a permission in a scope.

## Endpoints

All below `/api/v1`, and every one needs a signed-in user unless it says
otherwise. **Needs** is the permission the route checks, in the scope it acts
on: the site, or the organization in the path. A scope that doesn't exist
answers 404 before the permission is checked. A `{scope}` is `site` or
`org:<id>`; organization IDs are lowercase UUIDs.

### Organizations

| Method | Path | What | Needs |
|---|---|---|---|
| GET | `/organizations` | The organizations you can view, by name, each with its `domain` | `organizations:read` in each (the others are left out) |
| POST | `/organizations` | Create one, `{"name", "description"?}`; 409 when the name is taken | `organizations:create` on the site |
| GET | `/organizations/{id}` | One organization | `organizations:read` |
| PATCH | `/organizations/{id}` | Rename or describe it, `{"name"?, "description"?}` | `organizations:update` |
| DELETE | `/organizations/{id}` | Delete it: the roles assigned in it and its agents (ADK workflows) go with it | `organizations:delete` |

### People and access

| Method | Path | What | Needs |
|---|---|---|---|
| GET | `/scopes/{scope}/members` | The roles assigned in the scope, each `{subject_id, role, scope}`; `?above=true` adds the site's assignments that apply there, and on the site `?below=true` adds every organization's | `members:read` there |
| PUT | `/scopes/{scope}/members/{subject}/roles/{role}` | Assign a role there; the role must exist and be of the scope's level; assigning one already held changes nothing | `members:update` there |
| DELETE | `/scopes/{scope}/members/{subject}/roles/{role}` | Revoke it | `members:update` there |
| GET | `/roles` | Every role with its permissions and `member_count` (assignments, every scope) | a signed-in user |
| POST | `/roles` | Create one, `{"key", "name", "description"?, "permission_ids"?}`; 409 for a key in use, 422 for unknown permission IDs | `roles:create` on the site |
| GET | `/roles/{key}` | One role | a signed-in user |
| PATCH | `/roles/{key}` | Edit its name, description or permissions (`permission_ids` replaces them); the key never changes | `roles:update` on the site |
| DELETE | `/roles/{key}` | Delete it, its grants and every assignment of it | `roles:delete` on the site |
| GET | `/permissions` | Every permission, `{id, key, resource, action, description}` | a signed-in user |
| POST | `/permissions` | Create one, `{"key", "description"?}` | `permissions:create` on the site |
| GET | `/permissions/{id}` | One permission | a signed-in user |
| PATCH | `/permissions/{id}` | Edit its key or description; a new key moves every grant of it | `permissions:update` on the site |
| DELETE | `/permissions/{id}` | Delete it and revoke it from every role | `permissions:delete` on the site |
| GET | `/users` | Everyone who uses Forge, by name | a signed-in user |
| POST | `/users` | Add someone, `{"id"?, "first_name", "last_name", "email", "msid"}` (a random UUID without `id`); 409 for an email, MS ID or ID in use | `users:create` on the site |
| GET | `/users/{id}` | One user | a signed-in user |
| PATCH | `/users/{id}` | Edit their names, email or MS ID; their ID, and so their roles, stay | `users:update` on the site |
| DELETE | `/users/{id}` | Remove them, revoking every role they hold; 409 for yourself | `users:delete` on the site |
| GET | `/me` | Who you are: your subject ID and, if you're a user, your profile | a signed-in user |
| GET | `/me/access?scope=` | Your roles and Casbin policies in a scope (the site by default) | a signed-in user |
| GET | `/me/memberships` | Every role you hold, and where | a signed-in user |
| GET | `/me/organizations` | The organizations you hold a role in, with your roles there; a site role alone doesn't list any | a signed-in user |
| GET | `/authz/model` | The shared Casbin model file, as text | no user (the API key when one is set) |
| GET | `/authz/subjects/{subject}/access?scope=` | Anyone's roles and policies in a scope | `members:read` there |

### Agents

The organization's agents (see [Agents](#agents-1)), not the assistant's.

| Method | Path | What | Needs |
|---|---|---|---|
| GET | `/organizations/{id}/agents` | The agents, most recently changed first, each with its whole `forge.agent/v1` `document`, `revision` and who saved it when | `organizations:read` |
| POST | `/organizations/{id}/agents` | Make one, `{"document"}` | `agents:manage` |
| GET | `/organizations/{id}/agents/{agent_id}` | One agent | `organizations:read` |
| PUT | `/organizations/{id}/agents/{agent_id}` | Save its next version, `{"document", "revision"}`; 409 when someone saved it since that revision | `agents:manage` |
| DELETE | `/organizations/{id}/agents/{agent_id}` | Delete it | `agents:manage` |

### ADK workflow runs

Runs of the organization's ADK workflows (its agents); see
[ADK workflow runs](#adk-workflow-runs-1).

| Method | Path | What | Needs |
|---|---|---|---|
| POST | `/organizations/{id}/agents/{agent_id}/runs` | Run it as you, `{"input"}`: 202 with the run, queued | `agents:run` |
| GET | `/organizations/{id}/adk-runs` | The organization's runs, newest first, `?agent_id=&status=&limit=&offset=` (`status` repeatable; `limit` 1–100, 20): `{"items", "total"}` | `organizations:read` |
| GET | `/organizations/{id}/adk-runs/{run_id}` | One run: what it runs, its input and result, what it waits for, its activity and the actions its status allows | `organizations:read` |
| GET | `/organizations/{id}/adk-runs/{run_id}/steps` | A run's steps, read from its ADK session: `{"steps", "session_id"}` | `organizations:read` |
| POST | `/organizations/{id}/adk-runs/{run_id}/decisions` | Approve or reject the approval it waits at, `{"request_id", "approved", "comment"?}`: 202 with the run | `agents:approve` or `agents:run`, as the approval step names its approvers |
| POST | `/organizations/{id}/adk-runs/{run_id}/answers` | Answer the question (human input) it waits at, `{"request_id", "answer"}`: 202 with the run | `agents:run` |
| POST | `/organizations/{id}/adk-runs/{run_id}/retry`, `.../resubmit`, `.../abandon` | Retry a failed run as its next attempt (202); run a finished one again as a new run (202 with the new run); give one up (200) | `agents:manage_runs` |

### Assistant routes

| Method | Path | What |
|---|---|---|
| GET | `/agents/apps/{app}/models` | The models an agent's conversations may run on, its default first, with each one's thinking levels (see [The assistant's models](#the-assistants-models)) |
| POST | `/agents/apps/{app}/users/{you}/capabilities` | What the agent has for you on a page: the screens it matched, its toolsets and, with `tools`, their tools, and the prompts to suggest (see [What the assistant has on each screen](#what-the-assistant-has-on-each-screen)) |
| GET, POST | `/agents/apps/{app}/users/{you}/sessions` | Your conversations with an agent, most recent first; start one (see [The assistant](#the-assistant)) |
| GET, DELETE | `/agents/apps/{app}/users/{you}/sessions/{id}` | A conversation with its events; delete it and its files |
| GET | `/agents/apps/{app}/users/{you}/sessions/{id}/artifacts` | The files the agent saved in it |
| GET, DELETE | `/agents/apps/{app}/users/{you}/sessions/{id}/artifacts/{name}` | A file (`?version=`, or `/versions` and `/versions/{n}`); delete it |
| POST | `/agents/run_sse` | Run your turn and stream the agent's events (Server-Sent Events) |

Every assistant route needs a signed-in user, and `{you}` must be them.

### Outside the API prefix

| Method | Path | What | Who |
|---|---|---|---|
| GET | `/health/live` | The process is up | anyone |
| GET | `/health/ready` | MySQL answers within `FORGE_ADMIN_READY_TIMEOUT`; 503 otherwise | anyone |
| GET | `/docs`, `/openapi.json` | The API's documentation, when `FORGE_ADMIN_DOCS_ENABLED` | anyone |

`/api/v1/info` (the service's name and version) and `/api/v1/authz/model`
need no user either; with an API key set, they need the key.

## Access control (Casbin)

The hierarchy is the site, then organizations. The work happens in
organizations: their ADK workflows (agents) and their runs. Access
is role-based, evaluated by [Casbin](https://casbin.org) (`pycasbin` with its
async SQLAlchemy adapter), and stored as data, so roles, permissions and
assignments change through the API without a deploy.

| Table | Holds |
|---|---|
| `organizations` | The organizations: `id` (a UUID), a unique `name`, `description` |
| `authz_permissions` | Permission keys, `resource:action`, e.g. `agents:run`; either part may be `*` |
| `authz_roles` | Roles, keyed `<level>:<name>`, e.g. `org:admin`; the level (`site` or `org`) is where it is assigned |
| `casbin_rule` | Casbin policy lines: grants and assignments |
| `users` | The people roles are assigned to: `id` (their subject ID and token `sub`), `first_name`, `last_name`, and `email` and `msid` (MS ID), each unique and lowercase |

**One model everywhere.** [`casbin_model.conf`](src/forge_admin/auth/casbin_model.conf)
is the model for this API, the web console and any other service; fetch it
from `GET /api/v1/authz/model`, or read `casbin_rule` with your language's
Casbin adapter. A request is `(subject, domain, resource, action)`, where the
domain is the scope acted in: `site`, or `org:<id>`.

**Grants** (`p`) give a role a permission in every scope:
`p, org:member, agents, run`. **Assignments** (`g`) give a subject a role
in a scope, stored as the scope's domain followed by `*`:
`g, <user>, org:member, org:<id>*`; the site's pattern is `*`, which covers
every organization. Casbin's built-in `keyMatch` is registered as `g`'s
domain matching function, which makes those patterns match. Every enforcer
of this model must register it (the model file shows how for Python,
JavaScript and Go). Every organization response includes its `domain`, ready
to pass to `enforce`.

**Default roles** come from the migrations (the baseline, `0002agents` for
`agents:manage`, `0003adk_runs` for `agents:run` and `agents:approve`,
`0004adk_only`, which takes away the permissions only Forge workflows and
inbound events used, and `0005adk_run_store`, which renames
`background_tasks:manage` to `agents:manage_runs`), written once; edit them
freely afterwards.

| Role | Name | Grants |
|---|---|---|
| `site:admin` | Site administrator | `*:*`: everything, in every organization |
| `org:admin` | Organization administrator | `organizations:read`, `organizations:update`, `members:read`, `members:update`, `agents:manage`, `agents:run`, `agents:approve`, `agents:manage_runs` |
| `org:member` | Organization member | `organizations:read`, `members:read`, `agents:manage`, `agents:run` |
| `org:viewer` | Organization viewer | `organizations:read`, `members:read` |

The default permissions, and what checks them:

| Permission | Checked by |
|---|---|
| `organizations:read` | Reading an organization and everything in it: its agents (ADK workflows), their runs and steps |
| `organizations:create` / `update` / `delete` | Creating one (on the site); renaming or describing one; deleting one |
| `members:read` / `update` | Listing a scope's assignments and reading anyone's access there; assigning and revoking roles there |
| `users:create` / `update` / `delete` | Adding, editing and removing people (on the site) |
| `roles:create` / `update` / `delete` | Creating, editing and deleting roles (on the site) |
| `permissions:create` / `update` / `delete` | Creating, editing and deleting permissions (on the site) |
| `agents:manage` | Making, saving and deleting agents |
| `agents:run` | Running ADK workflows (agents); answering their runs' questions and deciding the approvals any member may |
| `agents:approve` | Deciding the ADK workflow runs' approvals the organization's administrators decide |
| `agents:manage_runs` | Retrying, resubmitting and abandoning the organization's ADK workflow runs |
| `*:*` | Everything |

Reading roles, permissions and users needs only a signed-in user.

A role is assigned at its own level (`org:*` roles in organizations,
`site:*` roles on the site); a site role applies in every organization too.
Endpoints check `authorize(session, enforcer, user, "agents:run",
Scope(Level.ORG, organization_id))`, which reloads the policy from MySQL
first, so every instance answers from the current rules; add a Casbin
watcher if that becomes a bottleneck.

```sh
# As a site administrator:
ORG=$(curl -s -X POST localhost:8101/api/v1/organizations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name": "Acme"}' | jq -r .id)
curl -X PUT localhost:8101/api/v1/scopes/org:$ORG/members/alice@acme.com/roles/org:admin \
  -H "Authorization: Bearer $TOKEN"
curl localhost:8101/api/v1/authz/subjects/alice@acme.com/access?scope=org:$ORG \
  -H "Authorization: Bearer $TOKEN"
# {"subject": "alice@acme.com", "scope": "org:...", "domain": "org:...",
#  "roles": ["org:admin"], "policies": [["org:admin", "agents", "manage_runs"], ...]}
```

Subject IDs are letters, digits and `._@:+-` (a UUID or email works) and must
not look like a role key, since roles and subjects share Casbin's namespace.
Deleting an organization revokes the roles assigned in it; its ADK workflow
runs are forgotten and its agents removed from MongoDB afterwards, and a
failure there is only logged.

## Agents

An organization's agents are the `forge.agent/v1` documents its members
build in the web console's agent builder: Google ADK graphs of nodes (LLM
agents with their sub-agents, sequential, parallel and loop agents,
other saved agents, human input, and Forge's own steps: approvals, HTTP
requests, transforms, delays, If / Switch / Match, loops, merges and ends)
joined by edges and run from their start. They're kept in the `agents`
collection (`adk_workflows/documents.py`, on `adk_workflows/document_store.py`), each found only
through its organization: a save names the revision it was made from (409
when someone saved it since), a deleted one stays out of every read, and the
routes answer 503 while MongoDB isn't set up or answering. Their IDs are
`ag_` and ten lowercase letters and digits. `agents:manage` makes,
saves and deletes them (`org:admin` and `org:member` by default);
`organizations:read` reads them.

The document must match `src/forge_admin/adk_workflows/agent.schema.json`, generated from
the web's `src/features/adk-workflows/lib/schema.ts`, and be at most
`FORGE_ADMIN_AGENTS_MAX_BYTES` (1 MiB) of JSON; otherwise 422. Only the
schema is checked, so a draft that can't run yet is saved.

`adk_workflows.build.build_agent(document, model=..., model_callbacks=...,
resolve=...)` builds a document into one ADK `Workflow`, each node the ADK
node its kind stands for and named by its name as a Python identifier (as the
builder shows it). Expressions are JSONata over what a node can read
(`input`, `previous`, `steps.<id>.output`, `state`); a run's start hands on
its input, its text parsed when it's JSON. It refuses what can't run (no start, clashing names, a
missing or self-running saved agent, edges it can't follow) with an
`AgentBuildError` naming the node. The async worker runs them: see
[ADK workflow runs](#adk-workflow-runs-1).

## ADK workflow runs

An organization's ADK workflows (its agents) run on the async worker, whose
ADK workflows task
([packages/python/adk-workflows](../../packages/python/adk-workflows/README.md))
runs them on Google ADK's graph engine (`adk_workflows/runs.py`,
`api/routes/adk_workflow_runs.py`). A run is kept in this database, beside
its ADK session, by the run store (`forge_task_adk_workflows.run_store`,
tables `adk_runs` and `adk_run_events`, which `0005adk_run_store` creates):
this API starts, lists, reads and acts on runs there, and the worker takes
each from its `adk_workflows` queue, runs it and records where it is.

| `status` | When |
|---|---|
| `queued` | Waiting for a worker: new, decided, answered, retried, or queued again after a hiccup |
| `running` | A worker has it |
| `paused` | Waiting for a person: the approval or question in its `pause` |
| `waiting` | Waiting for a time (a long delay), until `waiting_until` |
| `succeeded`, `failed`, `abandoned` | Finished |

Whenever a run is to be taken (started, decided, answered, retried,
resubmitted), a `run_adk` job naming it (`{"run_id"}`, key
`adk-run:<run_id>:<random>`) is queued with `FORGE_ADMIN_EMBEDDING_REDIS_URL`:
503 without it. If Redis doesn't answer, the run stays queued, the warning is
logged, and the worker's maintenance finds it; the caller isn't refused.

**Starting one.** `POST /organizations/{id}/agents/{agent_id}/runs` with
`{"input"}` needs `agents:run` (`org:admin` and `org:member` by default).
Before anything is kept:

- the input is checked against the start's `input_schema` (JSON Schema
  Draft 2020-12): 422 with what doesn't fit, or when there's no start;
- the saved ADK workflows it runs (its `saved` nodes', and theirs, and so on)
  are read from the store, and the document is built with them
  (`build_agent`): 422 with the build's reason, naming the node, e.g. a saved
  one that isn't there, or one that would run itself.

The run is answered 202, queued. Its payload, what the worker runs
(`RunPayload`), is `{"tenant_id", "agent_id", "revision", "name", "document",
"saved": {<id>: <document>}, "input", "session_id", "run_as", "run_as_name",
"trigger": {"type": "manual", "by": <user>}}`: the ADK workflow and the saved
ones **as they're saved now** (later saves never change it), and the member
it **acts as**. Its state is its **ADK session**, which the worker keeps in
this database too (ADK's `DatabaseSessionService`, app `adk_workflows`, user
`run_as`, the `session_id` made here).

**A run** as every route answers it (times ISO 8601 in UTC with their offset,
`2026-10-01T12:00:00+00:00`, or null):

```json
{"id": "<32 hex>", "organization_id": "...", "agent_id": "ag_...", "agent_name": "Ship",
 "revision": 3, "session_id": "...", "status": "paused", "attempt": 1,
 "requested_by": {"id": "...", "name": "Mia Member"}, "resubmit_of": null,
 "created_at": "...", "updated_at": "...", "started_at": "...", "finished_at": null,
 "duration_ms": null, "waiting_until": null, "waiting_reason": null,
 "pause": {"id": "<request_id>", "kind": "approval", "reason": "Ship it?",
           "details": {"kind": "approval", "approvers": "org:admin", "step": "review",
                       "step_name": "Review", ...},
           "requested_at": "...", "deadline": null},
 "error": null}
```

`duration_ms` is from its start to its finish; `error` is `{"message",
"category": "failed" | "error" | "transient" | "interrupted", "step",
"occurred_at"}`: why it failed, or, on a queued or running run, the last
hiccup it was queued again after. `GET /organizations/{id}/adk-runs` lists
them newest first (`?agent_id=` one ADK workflow's, `?status=` repeated for
some statuses). `GET .../adk-runs/{run_id}` adds what the run's page shows:
its `input`, its `result` (the graph's, once it ended), the `document` it
runs, its `trigger`, its activity (`events`, oldest first: `{"id", "at",
"kind", "message", "actor": {"id", "name"} | null, "attributes"}`, the kinds
`created`, `started`, `resumed`, `note`, `paused`, `decided`, `answered`,
`declined`, `timed_out`, `waiting`, `succeeded`, `failed`, `retried`,
`recovered`, `abandoned`), and the `actions` its status allows (`retry`,
`resubmit`, `abandon`, `decide`, `answer`; the web checks the permissions).

**Its steps.** `GET /organizations/{id}/adk-runs/{run_id}/steps`
(`organizations:read`) reads the run's ADK session here (503 while the
database doesn't answer), and answers `{"steps", "session_id"}`: each node of
the document it runs, in order (`forge_task_adk_workflows.steps`):

```json
{"id": "review", "name": "Review", "kind": "approval", "status": "waiting",
 "output": null, "error": null,
 "started_at": "2026-09-28T12:00:01.250Z", "finished_at": null}
```

| `status` | When |
|---|---|
| `done` | It handed on its `output`; with an `error` too when it took its Error way |
| `failed` | It failed the run: `error` is `{"message", "code"}` |
| `waiting` | It waits for a person (an approval, human input) or the timer (a long delay) |
| `running` | It started and hasn't finished |
| `not_reached` | Nothing of it ran (yet); every step before the worker made the session |

A step inside a loop's body shows its last item's; a team's sub-agents and a
saved ADK workflow's own steps count for their node.

**What it waits for.** A paused run's `pause` says what: its `kind` is
`approval` or `human_input`, its `details` `{"kind", "approvers"?,
"expires_at"?, "response_schema"?, "step", "step_name", "workflow_name",
"message", ...}`, and its `id` is the `request_id` that answers it:

| Route (below `/organizations/{id}/adk-runs/{run_id}`) | Who | What |
|---|---|---|
| `POST /decisions` | `agents:approve` for an approval whose approvers are `org:admin` (and anything else it names); `agents:run` for `org:member` | `{"request_id", "approved", "comment"?}` (the comment up to 4,000 characters): the run carries on down the approved or rejected way. 409 for human input |
| `POST /answers` | `agents:run` | `{"request_id", "answer"}`: the answer must fit the question's `response_schema` (422 listing what doesn't, or when its JSON is over 4,000 characters); it's kept as an approval whose comment is the answer as JSON. 409 for an approval |

Both answer 202 with the run, queued, and 409 when the request isn't open any
more (answered, or the run moved on). Nobody deciding by the approval's
deadline is a rejection the worker makes.

**Retrying, resubmitting, abandoning** need `agents:manage_runs`
(`org:admin` by default), and record who did it in the run's activity:

| Route (below `/organizations/{id}/adk-runs/{run_id}`) | What |
|---|---|
| `POST /retry` | A failed run's next attempt: it carries on from what it kept. 202 with the run, queued; 409 unless it failed |
| `POST /resubmit` | A finished run's payload again, as a new run (`resubmit_of` names the old; another invocation of the same ADK session). 202 with the new run; 409 unless it's finished |
| `POST /abandon` | Give a queued, paused, waiting or failed run up: it never carries on. 200 with the run; 409 while it's running or once it's finished otherwise |

| Answer | When |
|---|---|
| 403 | The caller lacks `organizations:read`, or the permission the route needs, in the organization |
| 404 | Another organization's run, or none |
| 409 | The run's status doesn't allow it, `detail` saying so |
| 422 | Input or an answer that doesn't fit, a document that doesn't build, or a malformed query |
| 503 | The runs' database isn't answering, or the worker's queue isn't set up |

## The assistant

The web console's assistant talks to a [Google ADK](https://google.github.io/adk-docs/)
agent served by this API under `/agents`, in the protocol of ADK's own API
server (`adk api_server`), which its SDK (`@assistant-ui/react-google-adk`,
direct mode) is written against. The one app is `forge`
(`assistant/forge.py`), on the models of the shared model provider
configuration, or else Gemini (`gemini-3.5-flash`; see
[The assistant's models](#the-assistants-models)): a supervisor and its
specialists (below). ADK's events for tool calls and results, confirmations,
sign-in and input requests, agent transfers and artifacts stream through
unchanged.

### A supervisor and its specialists

The person talks with `forge`, a supervisor with no tools of its own. Each
toolset (see [The assistant's toolsets](#the-assistants-toolsets)) is held by
a specialist agent named after it (`access`, `administration`), and the
supervisor hands each request to the one whose part it is, then answers from
its report. With a handful of tools each, the specialists pick the right one
more reliably than one agent offered every tool.

- **ADK task agents.** Each specialist is an `LlmAgent` with `mode="task"`,
  which ADK offers the supervisor as a tool named after it, taking a
  `request`. It sees that request, who the person is and their page, not the
  conversation, so the supervisor writes a request that stands on its own. It
  reports back by calling `finish_task` with `{"result": ...}`, which reaches
  the supervisor as the tool's result; it never hands the conversation on
  itself (`disallow_transfer_to_parent` and `_to_peers`).
- **It can ask the person.** A specialist may answer with a question instead
  of reporting: the person's reply goes to it, not the supervisor, until it
  reports. A change that asks the person to confirm it waits in the
  specialist, and runs there once they approve.
- **One at a time.** A request that spans parts goes to one specialist, then
  the next, with what the first found.
- **The same everywhere else.** Specialists run on the conversation's model
  and thinking level, and their tools act as the person.
- In the event stream, a specialist's events have its name as `author`.

```sh
curl -N localhost:8101/api/v1/agents/run_sse -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"appName": "forge", "userId": "<you>",
  "sessionId": "<id>", "streaming": true,
  "newMessage": {"role": "user", "parts": [{"text": "What can I do here?"}]}}'
# :ok
# data: {"content":{"parts":[{"text":"Your","thought":true}],"role":"model"},"partial":true,...}
# data: {"content":{"parts":[...the whole reply...]},"author":"forge",...}
```

- **Streaming:** with `streaming`, text and reasoning arrive as `partial`
  events as they're written, then as one whole event. A failure after the
  stream starts arrives as an event with `errorCode` and `errorMessage`,
  which the assistant shows as the reply.
- **Yours only:** the `{you}` in a path, and `userId` in a run, must be the
  signed-in user; anyone else's answers 403.
- **Model and thinking:** the web console's model section sends the session
  state `model` (`provider/model`, as `GET /agents/apps/{app}/models` lists
  it) and `thinking_level` (off, minimal, low, medium, high, xhigh) with each
  run. A model the route doesn't list runs on the default; a level the model
  doesn't offer, on the nearest it does.
- **Who and where:** each run looks up who the person is from their sign-in
  (name, email, the organizations they hold roles in with those roles, and
  their site-wide roles; `assistant/person.py`) and hands it to the agent as
  `temp:person`, for that run only. The web console sends the page they're
  on as `page_context` (path and route, title and crumbs, the workspace
  scope, the records on screen and the one in focus; `null` when they stop
  sharing it), which is kept with the message's event. It's checked for
  shape and size, and left out rather than refused when malformed
  (`assistant/page_context.py`). The agent's instruction describes both, with
  every value from the browser quoted as data. The page grants nothing:
  tools still act as the person.
- **State a run may set:** only `model`, `thinking_level` and `page_context`.
  Anything else answers 422: app-wide (`app:`) state every user shares,
  state across a user's conversations (`user:`), and `temp:person`.
- **Storage:** conversations are ADK sessions in this API's MySQL. ADK
  creates and migrates its own tables there on first use (`sessions`,
  `events`, `app_states`, `user_states`, `adk_internal_metadata`), and
  Alembic's autogenerate skips them (`ADK_TABLES`). Files agents save are
  kept in memory for now, so they're gone after a restart.
- **Differences from ADK's server:** binary data (`inlineData`, e.g. pictures)
  is standard base64, which browsers decode, where ADK sends URL-safe base64;
  creating a conversation answers 201 and deleting one 204.

Without a model provider configuration or a Gemini API key, the assistant
answers every message by saying it isn't set up: set
`FORGE_ADMIN_GOOGLE_API_KEY` in `.env` (a key from
[Google AI Studio](https://aistudio.google.com/apikey); `GOOGLE_API_KEY` and
`GEMINI_API_KEY` work too), or name the shared `model_provider.yaml` in
`FORGE_ADMIN_MODEL_PROVIDER_CONFIG`, and restart the API.

### The assistant's models

The assistant reads the same `model_provider.yaml` as ADK workflows' LLM
nodes: one file, in [`packages/python/common`](../../packages/python/common/README.md#model-provider-configuration),
names the providers, their models and how to reach them, and
`FORGE_ADMIN_MODEL_PROVIDER_CONFIG` names the file, e.g.
`../../packages/python/common/src/forge_common/model_provider/model_provider.openai.yaml`
(the image keeps the shared files at that path relative to `/app`). Its
`${NAME}` references resolve from the process environment, then from
`.env`; a missing one stops startup. The agent runs on one
`forge_common.adk.models.ProviderModels` (`assistant/language_models.py`), which
sends each turn to the model the conversation chose: Gemini through ADK's own
client, OpenAI (Responses or Chat Completions) and Anthropic through LiteLLM.
`FORGE_ADMIN_AGENT_MODEL` picks the default among its models (its own
default otherwise) and `FORGE_ADMIN_AGENT_MODELS` which of them a
conversation may choose (all of them otherwise).

Without the file, the assistant runs on Gemini: the models
`FORGE_ADMIN_AGENT_MODEL` (by default `gemini-3.5-flash`) and
`FORGE_ADMIN_AGENT_MODELS` name, with `FORGE_ADMIN_GOOGLE_API_KEY`, built into
the same configuration (provider `google`).

```sh
curl localhost:8101/api/v1/agents/apps/forge/models -H "Authorization: Bearer $TOKEN"
# {"defaultModel": "openai/gpt-5.2",
#  "models": [{"id": "openai/gpt-5.2", "provider": "openai", "providerName": "OpenAI API",
#              "model": "gpt-5.2", "name": "gpt-5.2", "api": "openai-responses",
#              "reasoning": true, "input": ["text"], "contextWindow": 128000,
#              "maxTokens": 16384,
#              "thinkingLevels": ["off", "minimal", "low", "medium", "high"]}]}
```

A model's `thinkingLevels` are `off` only unless it `reasoning`; `xhigh`
only when its `thinkingLevelMap` maps it; none mapped to null. The route
says nothing about how a model is reached: no URLs, headers or keys. A
provider's sign-in failure arrives as an error event with `errorCode`
`MODEL_AUTH_FAILED`, and an OpenAI or Anthropic refusal with
`MODEL_ERROR_<status>`.

### What the assistant has on each screen

The assistant's tools, part of its instructions, its default model and the
prompts suggested under a new conversation's composer depend on the screen
the person is on, as `assistant/screens.yaml` configures it (or the file
`FORGE_ADMIN_AGENT_SCREENS` names). Each screen's `when` matches the page
context the web console sends with every message: the router's `route`, its
`search` parameters (e.g. `view: config`) and the kinds of record on screen
(`entity: organization`). Every screen that matches adds its `toolsets` (a
toolset by name, or `{name, tools}` for some of its tools), `instructions`
and `prompts` (at most six are suggested); `everywhere` applies on every
screen.

- **Built once.** The supervisor and a specialist per toolset that's set up
  are built at startup. On every model call, `assistant/screens.py`'s `Screens`
  says which toolsets the conversation's page gives: the supervisor is
  offered only those specialists (`offer_specialists` withdraws the others'
  declarations), and each specialist's `ScreenToolset` gives it that
  toolset's tools there (all, or those the screens name). A specialist's
  instruction has its toolset's guidance, and the supervisor's says which
  other pages have the specialists it doesn't.
- **Sticky.** A conversation keeps every toolset it has had, in its session
  state (`assistant_toolsets`, which the browser can't set), so a specialist
  or tool in its history, or a change waiting for the person to confirm it,
  stays when they move on.
- **Checked at startup.** A toolset, tool or model the file names that
  doesn't exist fails the start. A toolset that isn't set up here is left
  out: without `FORGE_ADMIN_JWT_SECRET`, none is.
- **For the person.** `POST /agents/apps/{app}/users/{user}/capabilities`,
  `{"pageContext", "sessionId"?, "tools"?}`, answers what the agent has for
  them on a page: the screens it matched, its toolsets (those a conversation
  kept from earlier marked `fromEarlier`), with `tools` each tool and whether
  it asks first, and the prompts to suggest.
- The page comes from the browser: it decides what's offered, never what a
  tool may do. Every tool acts as the person.

### The assistant's toolsets

Each toolset is registered in `assistant/forge.py` (`toolsets()`), held by its
own specialist, and given to the assistant by `screens.yaml`. Every tool
calls this API's own routes as the person (`assistant/person_api.py`): in the
process, over an ASGI transport, with a token minted for the conversation's
user (10 minutes, reused for 5, signed with `FORGE_ADMIN_JWT_SECRET`;
`assistant/user_tokens.py`) and sent to the address they reached the API at, so
every call is authorized, scoped and audited as theirs, exactly as in the
web console, and any URL a route writes is the one they know.
`assistant/route_tools.py`'s `RouteToolset` is their base: IDs are checked
before they go into a path, tools that change something ask the person to
confirm first (ADK's tool confirmation, which the console renders), and, as
`ForgeBaseToolset`s (`packages/python/common`), no tool raises: a refusal
answers `failed` with the route's reason and what to do about it, and large
results are cut down.

| Toolset | Module | What the assistant can do |
|---|---|---|
| `access` | `access_tools.py` | The person's profile, organizations, memberships and access, and why something was refused; the roles there are and what each grants; finding people; a scope's members and someone's access; giving or removing roles (`members:update`) |
| `administration` | `admin_tools.py` | Read, create and update organizations, role definitions, permission definitions and users; never deletes |

No toolset reaches ADK workflows, their runs or agents yet; on those pages
the assistant says so (`screens.yaml`'s `organization` screen). Left out on
purpose: deleting organizations, roles, permissions or users.

## Sign-in and the local site administrator

Every request signs in the way it will in production: an
`Authorization: Bearer <JWT>` header whose `sub` is the user's ID. The API
checks the signature (HS256, `FORGE_ADMIN_JWT_SECRET`), the expiry, and the
issuer and audience when `FORGE_ADMIN_JWT_ISSUER` / `_AUDIENCE` are set; the
user then acts as themselves: `request.state.user_id`, the audit actor and
`/me`. A request without a token has no user, so every route that needs one
answers 401.

For local development, the first user is you, the site administrator:

1. Fill in the `FORGE_ADMIN_SITE_ADMIN_*` settings in
   `apps/forge-admin-api/.env` (your names, email and MS ID; the ID can
   stay). `make env` creates `.env` from `.env.example` and generates
   `FORGE_ADMIN_JWT_SECRET` there.
2. `make infrastructure` starts `admin-mysql` and runs the seed
   (`forge-admin-seed`, the one-shot `admin-seed` Compose service), which
   applies the migrations (they create the default roles and permissions),
   adds you to `users` and gives you `site:admin` on the site. Rerunning it
   changes nothing.
3. `make web-token` mints a 90-day token for you (`forge-admin-token`, by
   default for `FORGE_ADMIN_SITE_ADMIN_MSID`) and sets it as `VITE_API_TOKEN`
   in `apps/forge-web/.env.local` and `.env.compose`, which the web console
   sends. Rerun it when it expires. `forge-admin-token --msid <msid>` (or
   `--email`, `--days`) mints one for anyone else in `users`, e.g. to try the
   console as them.

```sh
make infrastructure
make web-token
make admin
TOKEN=$(cd apps/forge-admin-api && uv run forge-admin-token)
curl -H "Authorization: Bearer $TOKEN" localhost:8101/api/v1/me/access
# {"subject": "...", "scope": "site", "domain": "site", "roles": ["site:admin"],
#  "policies": [["site:admin", "*", "*"]]}
```

`FORGE_ADMIN_LOCAL_USER_ID` makes requests without a token act as one user,
and the API logs a warning while it is set. The tests use it; leave it unset
otherwise, and never set it in a deployment.

The web console reaches the API at `VITE_API_URL`:
`http://localhost:8101/api/v1` for `make web` (as in
`apps/forge-web/.env.example`), `http://localhost:18201/api/v1` for `make up`
(`.env.compose`). `.env.example` already allows both consoles' origins in
`FORGE_ADMIN_CORS_ORIGINS`.

## Run locally

From the repository root:

```sh
make start            # or make up: web, admin, the async worker and their databases in
                      # containers; admin on http://localhost:18201, web on 18190
make admin-deps       # or: admin-mysql on 127.0.0.1:13326, and the shared MongoDB and Redis
make admin            # then the API natively with reload on http://localhost:8101
make async-worker     # the worker that runs ADK workflow runs (the adk_workflows queue)
make up-all-local     # or all of it natively in one terminal; Ctrl-C stops the apps
```

`make admin` runs `forge-admin`, which applies pending migrations and then
serves. Compose passes the same `.env` to the container and points it at
`admin-mysql:3306` and the shared `mongo` and `redis`. Without the launcher's
migrations you can also run
`uv run uvicorn forge_admin.api.app:create_app --factory --reload` from
`apps/forge-admin-api`.

## Database and migrations

The schema starts from one baseline revision
(`db/migrations/versions/0001baseline_baseline.py`), which creates the
tables (`organizations`, `users`, `authz_permissions`, `authz_roles`,
`casbin_rule`, `organization_event_endpoints`, `organization_event_types`,
`organization_events`) and writes the default permissions, roles and grants
once, so later edits made through the API are never reverted. `0002agents`
adds `agents:manage` the same way, granted to `org:admin` and `org:member`;
`0003adk_runs` adds `agents:run` (`org:admin`, `org:member`) and
`agents:approve` (`org:admin`). `0004adk_only` drops the inbound events'
tables and the permissions only Forge workflows and events used
(`events:manage`, `workflows:manage`, `workflows:run`, `workflows:approve`)
with their grants, and rewords the default descriptions that named them
where they're unchanged. `0005adk_run_store` creates the ADK workflow runs'
tables, `adk_runs` and `adk_run_events`, exactly as the run store
(`forge_task_adk_workflows.run_store.metadata`) describes them
(`tests/db/test_run_store_migration.py` holds it to that), and renames
`background_tasks:manage` to `agents:manage_runs` with every grant of it.

Define models on `forge_admin.db.base.AuditBase` under `models/` and import
them in `models/__init__.py`, then from `apps/forge-admin-api`:

```sh
uv run alembic revision --autogenerate -m "add projects"
uv run alembic upgrade head        # or make admin-migrate from the root
```

Autogenerate leaves alone the tables ADK manages and the run store's, and
`alembic.ini` runs ruff over each new revision. A change to the run store's
tables needs a revision of its own, written to match. The launcher applies pending migrations at startup unless
`FORGE_ADMIN_MIGRATE_ON_START=false`; with several replicas, turn that off
and migrate once per release. Handlers get a session from
`Depends(get_session)` (`Session` in `api/routes/common.py`) and commit
explicitly.

### Audit columns

Every table inherits four columns from `AuditBase`, filled in automatically on
every insert and update made through SQLAlchemy, bulk `update()` statements
included. Route code never sets them.

| Column | Value |
|---|---|
| `created_at`, `updated_at` | UTC, microsecond precision; equal until the row is first updated |
| `created_by`, `updated_by` | The actor making the change |

The actor is set once per request by `authenticate` in `auth/security.py`: the
signed-in user, else `api-key` when the API key was verified, `anonymous` when
no key is configured, and `system` outside requests (migrations). The seed
writes as `seed`. Replacing a
role's permissions only inserts and deletes what changed, so a grant keeps
the audit record of when it was first given. `tests/db/test_audit.py` fails
for any new table that skips `AuditBase`.

## Configuration

Precedence, lowest to highest: defaults, `.env` in the working directory, the
process environment. See `.env.example` for every setting.

Values the admin shares with other apps (the MySQL connection, MongoDB,
Redis, model keys) live once in the repository's `.env.common`, and `.env`
names each with a `${NAME}` reference, e.g.
`FORGE_ADMIN_MYSQL_PASSWORD=${FORGE_MYSQL_PASSWORD}`
(`forge_admin.env_files`). A reference resolves to an earlier line of `.env`,
otherwise to `.env.common`; a name defined in neither stops startup unless
the process environment sets that variable. `.env.common` is read from
`../../.env.common`, or `FORGE_ENV_COMMON_FILE` (empty disables it), and none
of it reaches the settings unless `.env` references it. `make env` creates
`.env.common`.

| Variable | Default | Purpose |
|---|---|---|
| `FORGE_ADMIN_NAME` | `forge-admin` | The service's name, in `/info` and the API docs |
| `FORGE_ADMIN_API_PREFIX` | `/api/v1` | Prefix for `/info` and every router in `ROUTERS` |
| `FORGE_ADMIN_DOCS_ENABLED` | `true` | Serve `/docs` and `/openapi.json` |
| `FORGE_ADMIN_CORS_ORIGINS` | `[]` | JSON array of allowed browser origins (`.env.example`: both web consoles) |
| `FORGE_ADMIN_API_KEY` | unset | Require this `X-API-Key` on routes below the prefix |
| `FORGE_ADMIN_JWT_SECRET` | unset | HS256 key for bearer tokens (32+ characters); unset, tokens are refused and the assistant has no tools |
| `FORGE_ADMIN_JWT_ISSUER` / `_AUDIENCE` | unset | When set, tokens must carry this `iss` / `aud`, and minted ones do (`.env.example`: `forge-local` / `forge-admin`) |
| `FORGE_ADMIN_LOCAL_USER_ID` | unset | Local development and tests only: the user of requests without a token |
| `FORGE_ADMIN_SITE_ADMIN_ID` / `_FIRST_NAME` / `_LAST_NAME` / `_EMAIL` / `_MSID` | unset | The user `forge-admin-seed` adds with `site:admin` |
| `FORGE_ADMIN_WEB_URL` | `http://localhost:5190` | The web console, for links to its pages |
| `FORGE_ADMIN_EMBEDDING_REDIS_URL` | unset | The Redis the async worker's SAQ queues run on, where the jobs that take ADK workflow runs are queued (`${FORGE_REDIS_URL}`); unset, starting, deciding, answering, retrying and resubmitting a run answer 503 |
| `FORGE_ADMIN_MONGO_URI` | unset | MongoDB for organizations' agents (ADK workflows) (`${FORGE_MONGO_URI}`); unset, the agent routes answer 503 |
| `FORGE_ADMIN_MONGO_DATABASE` | `forge_admin` | Its database |
| `FORGE_ADMIN_MONGO_TIMEOUT` | `5` | Seconds to find a MongoDB server before a request answers 503 |
| `FORGE_ADMIN_AGENTS_MAX_BYTES` | `1048576` | The largest agent document saved, in bytes of JSON |
| `FORGE_ADMIN_MODEL_PROVIDER_CONFIG` | unset | The shared `model_provider.yaml` the assistant's models come from (see [The assistant's models](#the-assistants-models)); unset, Gemini |
| `FORGE_ADMIN_GOOGLE_API_KEY` | unset | Without a model provider configuration: the assistant's Gemini API key (`GOOGLE_API_KEY` or `GEMINI_API_KEY` also work), `${FORGE_GOOGLE_API_KEY}` from `.env.common`; unset, it says it isn't set up |
| `FORGE_ADMIN_AGENT_MODEL` | the configuration's default, or `gemini-3.5-flash` | The model the assistant runs on until a conversation chooses: `provider/model`, or an id only one provider has |
| `FORGE_ADMIN_AGENT_MODELS` | `[]` | JSON array: with a model provider configuration, which of its models a conversation may choose (all when empty); without, other Gemini models |
| `FORGE_ADMIN_AGENT_SCREENS` | unset | A screen configuration shaped like `assistant/screens.yaml`, which is used when this is unset |
| `FORGE_ADMIN_HOST` / `_PORT` | `127.0.0.1` / `8101` | Listener; the image sets `0.0.0.0` |
| `FORGE_ADMIN_RELOAD` | `false` | Restart on source changes (`make admin` sets it) |
| `FORGE_ADMIN_LOGGING__LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` (any case), for every logger |
| `FORGE_ADMIN_LOGGING__FORMAT` | console on a terminal, else json | `json`: one object per line, for log collectors; `console`: key=value (`.env.example` sets it) |
| `FORGE_ADMIN_LOGGING__ACCESS_LOG` | `true` | One `http.request` line per request, with its status, duration and request id |
| `FORGE_ADMIN_LOGGING__ACCESS_LOG_EXCLUDE_PATHS` | `["/health","/health/live","/health/ready"]` | JSON array of paths without access lines |
| `FORGE_ADMIN_LOGGING__LEVELS` | `httpx`, `httpx2`, `httpcore`, `watchfiles` at `WARNING` | JSON object of per-logger levels; replaces the default |
| `FORGE_ADMIN_MYSQL_HOST` / `_PORT` | `127.0.0.1` / `3306` | MySQL server (`${FORGE_MYSQL_*}` from `.env.common`) |
| `FORGE_ADMIN_MYSQL_DATABASE` / `_USER` | `forge_admin` | Database and account |
| `FORGE_ADMIN_MYSQL_PASSWORD` | required | No default, so no deployment falls back to a local credential |
| `FORGE_ADMIN_MYSQL_POOL_SIZE` / `_MAX_OVERFLOW` | `5` / `10` | The connection pool |
| `FORGE_ADMIN_MYSQL_POOL_RECYCLE` | `1800` | Seconds before a pooled connection is replaced; keep it below the server's `wait_timeout` |
| `FORGE_ADMIN_MYSQL_CONNECT_TIMEOUT` | `10` | Seconds to connect |
| `FORGE_ADMIN_MIGRATE_ON_START` | `true` | Apply pending migrations before serving |
| `FORGE_ADMIN_READY_TIMEOUT` | `2` | Seconds MySQL has to answer `/health/ready` |

## Checks

```sh
make admin-check       # ruff lint, format check and the unit tests
make admin-test-mysql  # starts admin-mysql (and the shared MongoDB and Redis), then the
                       # tests marked mysql: migrations, readiness, routes, and agents
                       # against MongoDB (skipped without FORGE_ADMIN_MONGO_URI)
make admin-fmt         # format and autofix the sources
```
