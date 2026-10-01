# Forge admin API

`forge-admin` is the administration service of Forge, a business workflow
engine: FastAPI on Python 3.13, backed by its own MySQL 8.4 database through
async SQLAlchemy (`aiomysql`), with Alembic migrations applied at startup,
and MongoDB for organizations' workflows (`pymongo`'s async client). It
keeps the organizations, the people and their access, each organization's
inbound events and workflows, submits workflow runs to the async worker
([apps/forge-async-worker](../forge-async-worker/README.md)), and serves the
web console's assistant. Package `forge-admin`, module `forge_admin`.

## How it's put together

`ApiServer` (in `api/server.py`) wraps the FastAPI setup in one class:

```python
server = ApiServer(settings, routers=[projects.router], middleware=[...])
app = server.create_app()  # built once, the same object afterwards
```

- **Lifespan**: `ApiServer.lifespan` creates the MySQL engine and
  sessionmaker and the Casbin enforcer at startup, and keeps them on
  `app.state` with the clients the routes use: the async worker's background
  tasks API (`background_tasks`), its job queues on Redis (`embedding`), the
  workflows' and agents' MongoDB stores (`workflows`, `organization_agents`),
  the assistant (`agents`) and the ADK workflow runs' sessions
  (`adk_run_sessions`, ADK's `DatabaseSessionService` on the engine). Each
  client is `None` when its settings are unset. Nothing connects until it's
  used, so the API starts while MySQL, MongoDB or Redis are down, and
  `/health/ready` reports MySQL.
- **Routes**: the health probes mount at the root. `/info` and every router
  in `ROUTERS` mount below `FORGE_ADMIN_API_PREFIX` (`/api/v1`).
- **Sign-in**: requests identify the user with an `Authorization: Bearer <JWT>`
  header whose `sub` is their user ID (`auth/security.py`, `auth/tokens.py`).
- **API key**: when `FORGE_ADMIN_API_KEY` is set, every route below the
  prefix requires it in the `X-API-Key` header. The health probes stay open.
- **Public routes**: routers in `PUBLIC_ROUTERS` mount at the root with
  neither the API key nor a user, and check their callers themselves: the
  inbound event endpoints under `/hooks`, which other systems call with an
  endpoint token (see [Inbound events](#inbound-events)), and the workflow
  service routes under `/internal/workflows`, which only the async worker
  calls, with `FORGE_ADMIN_WORKFLOWS_TOKEN` (see
  [Workflow service routes](#workflow-service-routes)).
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
  events.py              Inbound events: endpoint tokens, checking event types' JSON Schemas and payloads
  workflows.py           Organizations' workflows in MongoDB: checking documents, revisions, the store
  workflow.schema.json   The forge.workflow/v1 format's JSON Schema (generated from the web console)
  workflow.catalog.json  Each step kind's settings, defaults, ways out and output (generated likewise)
  workflow_runs.py       Checking a run's input and submitting the run to the async worker
  adk_runs.py            ADK workflow runs: checking the input and the build, submitting them, answers
  workflow_drafts.py     Completing and checking the workflows the assistant drafts
  embedding.py           The async worker's SAQ job queues on Redis, where runs are submitted
  background_tasks.py    The async worker's background tasks API: runs, their attempts, approvals
  agents/                The assistant: Google ADK agents
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
    workflow_tools.py    The workflows toolset
    event_tools.py       The events toolset
    access_tools.py      The access toolset
    admin_tools.py       The administration toolset
    runtime.py           AgentRuntime: a runner per agent, the session and artifact services
    wire.py              ADK's models as the JSON the web console reads
  api/                   The HTTP service
    app.py               ROUTERS, PUBLIC_ROUTERS and create_app(): what this service serves
    server.py            ApiServer: FastAPI app, lifespan, routers, middleware
    routes/              One router per resource; common.py holds shared request pieces
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
    migrations/          Alembic environment and the baseline revision, with the default roles
  models/                hierarchy.py (organizations), users.py (users), authorization.py
                         (roles, permissions, casbin_rule), events.py (event endpoints,
                         event types, events)
```

The tests mirror it: `tests/agents`, `tests/api`, `tests/auth`, `tests/cli`,
`tests/db`, `tests/integrations` (against MySQL, with fake worker clients) and
unit tests at the top, with `conftest.py` shared.

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
| DELETE | `/organizations/{id}` | Delete it: the roles assigned in it, its inbound events and its workflows go with it | `organizations:delete` |

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

### Event endpoint, types and events

| Method | Path | What | Needs |
|---|---|---|---|
| GET | `/organizations/{id}/event-endpoint` | The inbound endpoint (`url`, `enabled`, `token_hint`), or `null` before it's first turned on | `organizations:read` |
| PUT | `/organizations/{id}/event-endpoint` | Turn it on or off, `{"enabled"}`; the first time makes it, and that answer alone carries its `token` | `events:manage` |
| POST | `/organizations/{id}/event-endpoint/token` | Replace its token; the old one stops working at once, and the answer carries the new one; 404 before it's made | `events:manage` |
| GET | `/organizations/{id}/event-types` | The event types by name, each with its `status` (`draft`, `active`, `paused`), `payload_schema`, `schema_version`, `event_count`, `invalid_count` and `last_received_at` | `organizations:read` |
| POST | `/organizations/{id}/event-types` | Define one, a draft, `{"key", "name", "description"?, "payload_schema"}`; 409 for a key in use, 422 for a schema it can't use | `events:manage` |
| GET | `/organizations/{id}/event-types/{event_type_id}` | One event type | `organizations:read` |
| PATCH | `/organizations/{id}/event-types/{event_type_id}` | Change it, or turn it on or pause it, `{"status": "active" \| "paused"}`; a schema that checks differently bumps `schema_version` | `events:manage` |
| DELETE | `/organizations/{id}/event-types/{event_type_id}` | Delete it and its events | `events:manage` |
| GET | `/organizations/{id}/events` | The events received, latest first, without payloads, `?event_type_id=&status=valid\|invalid&limit=&offset=` (`limit` 1–100, 50) | `organizations:read` |
| GET | `/organizations/{id}/events/{event_id}` | One event with its `payload` | `organizations:read` |
| POST | `/organizations/{id}/event-schemas/validate` | Check a sample `payload` against a `payload_schema`, saved or not, as the endpoint would: `{"valid", "errors"}`; nothing is stored | `organizations:read` |

### Workflows, runs and background tasks

| Method | Path | What | Needs |
|---|---|---|---|
| GET | `/organizations/{id}/workflows` | The workflows, most recently changed first, each with its whole `forge.workflow/v1` `document`, `revision` and who saved it when | `organizations:read` |
| POST | `/organizations/{id}/workflows` | Make one, `{"document"}` | `workflows:manage` |
| GET | `/organizations/{id}/workflows/{workflow_id}` | One workflow | `organizations:read` |
| PUT | `/organizations/{id}/workflows/{workflow_id}` | Save its next version, `{"document", "revision"}`; 409 when someone saved it since that revision | `workflows:manage` |
| DELETE | `/organizations/{id}/workflows/{workflow_id}` | Delete it | `workflows:manage` |
| POST | `/organizations/{id}/workflows/{workflow_id}/runs` | Run it as you, `{"input"}`; see [Running a workflow](#running-a-workflow) | `workflows:run` |
| GET | `/organizations/{id}/workflows/{workflow_id}/runs` | Its runs, newest first, `?limit=&offset=` (`limit` 1–100, 20): `{"items", "total"}` | `organizations:read` |
| GET | `/organizations/{id}/background-tasks` | The organization's background tasks (its workflow runs), newest first, `?task_type=&exclude_task_type=&status=&limit=&offset=` (each filter repeatable; `limit` 1–100, 50): `{"items", "total"}` | `organizations:read` |
| GET | `/organizations/{id}/background-tasks/{task_id}` | One of them: its input, attempts, failures with their stack traces, audit trail, the approval it waits at, and the actions its state allows | `organizations:read` |
| POST | `/organizations/{id}/background-tasks/{task_id}/resubmit`, `.../restart`, `.../abandon` | Run it again as a new task; retry it as its next attempt; give up on it | `background_tasks:manage` |
| POST | `/organizations/{id}/background-tasks/{task_id}/decisions` | Approve or reject the approval a workflow run waits at, `{"request_id", "approved", "comment"?}`; 409 for an ADK workflow run's, decided at its own route | `workflows:approve` or `workflows:run`, as the approval step names its approvers |

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

Runs of the organization's ADK workflows (its agents), apart from Forge
workflows' runs; see [ADK workflow runs](#adk-workflow-runs-1).

| Method | Path | What | Needs |
|---|---|---|---|
| POST | `/organizations/{id}/agents/{agent_id}/runs` | Run it as you, `{"input"}`: 202 with `{"queue", "key", "agent_id", "revision", "session_id"}` | `agents:run` |
| GET | `/organizations/{id}/agents/{agent_id}/runs` | Its runs, newest first, `?limit=&offset=` (`limit` 1–100, 20): `{"items", "total"}`, as the background tasks list | `organizations:read` |
| GET | `/organizations/{id}/adk-runs/{task_id}/steps` | A run's steps, read from its ADK session: `{"steps", "session_id"}` | `organizations:read` |
| POST | `/organizations/{id}/adk-runs/{task_id}/decisions` | Approve or reject the approval it waits at, `{"request_id", "approved", "comment"?}` | `agents:approve` or `agents:run`, as the approval step names its approvers |
| POST | `/organizations/{id}/adk-runs/{task_id}/answers` | Answer the question (human input) it waits at, `{"request_id", "answer"}` | `agents:run` |

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
| POST | `/hooks/events/{endpoint_id}/{event_key}` | Another system sends an organization an event; see [Inbound events](#inbound-events) | the endpoint's token; never a user or the API key |
| POST | `/internal/workflows/workflows/{workflow_id}/runs` | A run's Run workflow step starts another of the organization's workflows; see [Workflow service routes](#workflow-service-routes) | `FORGE_ADMIN_WORKFLOWS_TOKEN`, then `workflows:run` for the member the run acts as |

`/api/v1/info` (the service's name and version) and `/api/v1/authz/model`
need no user either; with an API key set, they need the key.

## Access control (Casbin)

The hierarchy is the site, then organizations. The work happens in
organizations: their workflows, their runs and their inbound events. Access
is role-based, evaluated by [Casbin](https://casbin.org) (`pycasbin` with its
async SQLAlchemy adapter), and stored as data, so roles, permissions and
assignments change through the API without a deploy.

| Table | Holds |
|---|---|
| `organizations` | The organizations: `id` (a UUID), a unique `name`, `description` |
| `authz_permissions` | Permission keys, `resource:action`, e.g. `workflows:run`; either part may be `*` |
| `authz_roles` | Roles, keyed `<level>:<name>`, e.g. `org:admin`; the level (`site` or `org`) is where it is assigned |
| `casbin_rule` | Casbin policy lines: grants and assignments |
| `users` | The people roles are assigned to: `id` (their subject ID and token `sub`), `first_name`, `last_name`, and `email` and `msid` (MS ID), each unique and lowercase |

**One model everywhere.** [`casbin_model.conf`](src/forge_admin/auth/casbin_model.conf)
is the model for this API, the web console and any other service; fetch it
from `GET /api/v1/authz/model`, or read `casbin_rule` with your language's
Casbin adapter. A request is `(subject, domain, resource, action)`, where the
domain is the scope acted in: `site`, or `org:<id>`.

**Grants** (`p`) give a role a permission in every scope:
`p, org:member, workflows, run`. **Assignments** (`g`) give a subject a role
in a scope, stored as the scope's domain followed by `*`:
`g, <user>, org:member, org:<id>*`; the site's pattern is `*`, which covers
every organization. Casbin's built-in `keyMatch` is registered as `g`'s
domain matching function, which makes those patterns match. Every enforcer
of this model must register it (the model file shows how for Python,
JavaScript and Go). Every organization response includes its `domain`, ready
to pass to `enforce`.

**Default roles** come from the migrations (the baseline, `0002agents` for
`agents:manage` and `0003adk_runs` for `agents:run` and `agents:approve`),
written once; edit them freely afterwards.

| Role | Name | Grants |
|---|---|---|
| `site:admin` | Site administrator | `*:*`: everything, in every organization |
| `org:admin` | Organization administrator | `organizations:read`, `organizations:update`, `members:read`, `members:update`, `events:manage`, `workflows:manage`, `workflows:run`, `workflows:approve`, `background_tasks:manage`, `agents:manage`, `agents:run`, `agents:approve` |
| `org:member` | Organization member | `organizations:read`, `members:read`, `workflows:manage`, `workflows:run`, `agents:manage`, `agents:run` |
| `org:viewer` | Organization viewer | `organizations:read`, `members:read` |

The default permissions, and what checks them:

| Permission | Checked by |
|---|---|
| `organizations:read` | Reading an organization and everything in it: its event endpoint, event types and events, workflows, runs and background tasks, agents; checking a sample payload |
| `organizations:create` / `update` / `delete` | Creating one (on the site); renaming or describing one; deleting one |
| `members:read` / `update` | Listing a scope's assignments and reading anyone's access there; assigning and revoking roles there |
| `users:create` / `update` / `delete` | Adding, editing and removing people (on the site) |
| `roles:create` / `update` / `delete` | Creating, editing and deleting roles (on the site) |
| `permissions:create` / `update` / `delete` | Creating, editing and deleting permissions (on the site) |
| `events:manage` | Turning the inbound endpoint on or off, replacing its token, defining event types |
| `workflows:manage` | Making, saving and deleting workflows |
| `workflows:run` | Running workflows (and, in the service routes, a run's Run workflow steps); deciding approvals any member may |
| `workflows:approve` | Deciding approvals the organization's administrators decide |
| `background_tasks:manage` | Resubmitting, restarting and abandoning background tasks |
| `agents:manage` | Making, saving and deleting agents |
| `agents:run` | Running ADK workflows (agents); answering their runs' questions and deciding the approvals any member may |
| `agents:approve` | Deciding the ADK workflow runs' approvals the organization's administrators decide |
| `*:*` | Everything |

Reading roles, permissions and users needs only a signed-in user.

A role is assigned at its own level (`org:*` roles in organizations,
`site:*` roles on the site); a site role applies in every organization too.
Endpoints check `authorize(session, enforcer, user, "workflows:run",
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
#  "roles": ["org:admin"], "policies": [["org:admin", "background_tasks", "manage"], ...]}
```

Subject IDs are letters, digits and `._@:+-` (a UUID or email works) and must
not look like a role key, since roles and subjects share Casbin's namespace.
Deleting an organization revokes the roles assigned in it and deletes its
inbound endpoint, event types and events; its workflows are removed from
MongoDB afterwards, and a failure there is only logged.

## Inbound events

Other systems (an incident manager, a ticketing tool, a CI pipeline) send an
organization events, which Forge keeps and checks against a JSON Schema.
`events:manage` in the organization configures it (`org:admin` by default,
`site:admin` through `*:*`); `organizations:read` reads it.

- **The endpoint** (`organization_event_endpoints`, one per organization):
  turning it on the first time makes it, with a random token (`fevt_…`, 256
  bits) shown only in that answer. Only its SHA-256 is stored, with its last
  four characters (`token_hint`) to tell tokens apart. Turning it off refuses
  every event and keeps the token; replacing the token retires the old one
  at once.
- **Event types** (`organization_event_types`): a key unique in the
  organization, which senders put in the URL (`incident.opened`: lowercase
  letters, digits and `. _ -`, starting with a letter), a name, a status, and
  the JSON Schema its payload must match: draft 2020-12 (or the draft its
  `$schema` names), `"type": "object"` at the root, at most 64 KiB, `$ref`s
  only within itself (nothing is ever fetched). The schema is checked against
  its meta-schema when saved, so a bad `pattern` or `type` answers 422. A
  schema that checks differently bumps `schema_version`; one that only
  reorders properties is saved without. A new type is a `draft`, which the
  endpoint refuses, so nothing arrives before the organization has reviewed
  its schema; turning it on makes it `active`, and it can be `paused` and
  turned on again, never made a draft again. A new key changes the URL
  senders use.
- **Events** (`organization_events`): every event of a known, active type,
  valid or not: the payload as sent, the schema version it was checked
  against, `status` (`valid` or `invalid`) and up to 50 `errors`, each a
  JSONPath (`$.service.name`), a message and the failed keyword, plus its
  size, content type, user agent and source IP. Formats are checked: `date`,
  `date-time` and `time` (RFC 3339), `email`, `uri`, `uuid`, `ipv4`, `ipv6`,
  `regex`. Deleting an event type deletes its events; deleting the
  organization deletes everything.

Schemas and payloads are stored as JSON text (`JSONText`, MEDIUMTEXT), not
MySQL's JSON type, which sorts object keys: the order of a schema's
properties is the order people chose, and a payload's the order it was sent.

Senders post each event's JSON body to
`<FORGE_ADMIN_PUBLIC_URL>/hooks/events/<endpoint id>/<event type key>` with
the token as `Authorization: Bearer <token>`, or `X-Forge-Token: <token>`
for senders that can't set Authorization. `/hooks` sits outside `/api/v1`,
needs no Forge user and never the API key, so a deployment can expose it
alone. An `Idempotency-Key` header (up to 255 characters) makes retries safe:
the same key for the same type answers with the event already kept.

```sh
curl -X POST "$ENDPOINT/incident.opened" \
  -H "Authorization: Bearer $FORGE_EVENTS_TOKEN" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: INC-1042" -d '{"id": "INC-1042", "severity": "sev2"}'
```

| Answer | When |
|---|---|
| 202 | Kept, and it matches the schema: `{"id", "event_type", "status": "valid", "schema_version", "received_at"}` |
| 200 | Already received with this `Idempotency-Key`: the event kept then, with `"duplicate": true` |
| 422 | Kept, but it doesn't match: the same receipt with `"status": "invalid"` and its `errors` |
| 400 | The body is empty or isn't JSON (NaN and Infinity aren't) |
| 401 | No token, a wrong one, or no such endpoint (the same answer for both) |
| 403 | The endpoint is turned off |
| 404 / 409 | The organization has no event type with the key / it's a draft or paused |
| 413 | The body is over `FORGE_ADMIN_EVENTS_MAX_BYTES` (256 KiB) |

Nothing is kept for the refusals. Validation runs in a worker thread, so a
large payload doesn't hold up other requests; the patterns in a schema are
the organization's, and run on every payload of the type. Changes made
through `/hooks` are audited as `endpoint:<endpoint id>`.

**Events don't start workflows yet.** The endpoint stores and validates each
event, and that's all: nothing starts a workflow run when an event arrives,
whatever the workflows' Start steps say (see [Workflows](#workflows)).

## Workflows

An organization's workflows are the `forge.workflow/v1` documents its
members build in the web console's workflow builder (steps, their
connections and settings), saved as they're edited and shared by the whole
organization. They're kept in MongoDB (`FORGE_ADMIN_MONGO_URI`, the
`workflows` collection of `FORGE_ADMIN_MONGO_DATABASE`), one record per
workflow; without a URI the routes answer 503, as they do while MongoDB
doesn't answer within `FORGE_ADMIN_MONGO_TIMEOUT`. `workflows:manage` in the
organization makes, saves and deletes them (`org:admin` and `org:member` by
default, `site:admin` through `*:*`); `organizations:read` reads them.

- **Scope:** each record carries its `organization_id`, and is only ever
  read or changed through its organization: another organization's ID
  answers 404. Deleting an organization removes its workflows, deleted ones
  too.
- **The document** is stored as sent, keys in order (JSON Schemas in it rely
  on that), after the API sets its `id`, `organization_id`, `created_at` and
  `updated_at`; a create's body takes only `document`. It must match
  `src/forge_admin/workflow.schema.json`, the format's JSON Schema, generated
  from the web's `src/lib/workflows/schema.ts` (`npm run
  generate:workflow-schema` in `apps/forge-web`; its `npm run check` fails
  when the copy here is out of date), and be at most
  `FORGE_ADMIN_WORKFLOWS_MAX_BYTES` (1 MiB) of JSON; otherwise 422, naming
  what's wrong. The schema requires every setting, so a setting a step kind
  gains later is listed in `ADDED_SETTINGS` (`workflows.py`; the web's
  `document.ts` has the same): a document saved before it gets its default
  when it's saved again, e.g. an Agent step's `thinking_level`. The same
  command writes `src/forge_admin/workflow.catalog.json`, each step kind's
  settings, defaults, ways out and output, from the builder's model: the
  assistant drafts workflows from it (`workflow_drafts.py`).
- **Steps:** Start (`entry`), Agent, Approval, HTTP request, Transform,
  Delay, Run workflow (`subworkflow`), If / else, Switch, Match, Loop, Merge
  and End. Expressions in their settings are JSONata.
- **Revisions:** a save names the `revision` it was made from and goes up by
  one; a save from an older one answers 409, so two people's changes never
  silently overwrite each other (the builder then offers the other version
  or saving over it). Each record also keeps who saved it last, by name
  (`updated_by_name`).
- **IDs** are `wf_` and ten lowercase letters and digits, always made by the
  API. Deleting marks the record deleted (`deleted_at`, `deleted_by`) and
  keeps it out of every read; its ID is never used again.
- **Indexes** are made on first use: an organization's workflows by
  recency, and by the event types their Start steps name.

**How runs start.** A Start step declares a `trigger`: `manual`, `event`
(with the `event_types` it starts on) or `schedule` (with a `cron` and a
`timezone`), and the API saves whichever it names. Only runs you start are
implemented: nothing currently starts a run when an event arrives (the
`/hooks` endpoint stores and validates events only) or on a schedule. A run
starts in one of two ways:

- `POST /organizations/{id}/workflows/{workflow_id}/runs`, by a person (or
  the assistant, as them), whatever the Start step's trigger;
- another run's **Run workflow** step, through the
  [workflow service routes](#workflow-service-routes).

### Running a workflow

`POST /organizations/{id}/workflows/{workflow_id}/runs` with `{"input"}`
needs `workflows:run` in the organization (`org:admin` and `org:member` by
default). It checks the input against the Start step's `input_schema` (422
with what doesn't fit, or when the workflow has no Start step) and submits
the run to the async worker's `workflows` queue on
`FORGE_ADMIN_EMBEDDING_REDIS_URL` (503 without it, or while Redis doesn't
answer), answering 202 with `{"queue", "key", "workflow_id", "revision"}`.
The run ([packages/python/tasks/workflows](../../packages/python/tasks/workflows/README.md)):

- takes the workflow **as it's saved now**: later saves never change it;
- **acts as the caller**: it records who started it (`trigger: {"type":
  "manual", "by": <user>}`), and its Run workflow steps start other
  workflows as them, checked again each time;
- is one of the organization's **background tasks**, labelled with the
  workflow, so `GET .../workflows/{workflow_id}/runs` lists a workflow's
  runs, newest first, as the background tasks list does.

### Background tasks

The async worker ([apps/forge-async-worker](../forge-async-worker/README.md#background-tasks-api))
tracks each workflow run as a background task of the organization it ran
for: each attempt, its failures (type, message, stack trace, and whether
it's retried automatically) and an audit trail. The admin relays the
worker's background tasks API with `FORGE_ADMIN_ASYNC_WORKER_URL` and
`_TOKEN`, and checks who may see and act: a task belongs to the organization
its `tenant` label names, and every route about one task reads it first and
answers 404 for another organization's. Answers are the worker API's own
JSON.

Anyone who can view the organization (`organizations:read`) can list and read
its tasks. Acting on one needs `background_tasks:manage` in the
organization, which `org:admin` has by default:

| Route (below `/organizations/{id}/background-tasks/{task_id}`) | What |
|---|---|
| `POST /resubmit` | The same job, with the same input, as a new task; 202 with `{"queue", "key"}` of the job that will run it. Not while it's still running |
| `POST /restart` | A failed or stopped task's next attempt, run by a worker, skipping what it finished; 202 with `{"queue", "key"}` |
| `POST /abandon` | Give up on a failed or stopped task: no more attempts, automatic or not; the task, `ABANDONED` |

A run waiting at an Approval step (`AWAITING_VALIDATION`, its `approval`
naming the question and who decides) is decided here too, by whom the step
names rather than by `background_tasks:manage`:

| Route | Who | What |
|---|---|---|
| `POST /decisions` | `workflows:approve` for a step whose approvers are `org:admin` (and for anything else it names); `workflows:run` for one whose approvers are `org:member` | `{"request_id", "approved", "comment"}` (the comment up to 4,000 characters): the run carries on down the step's approved or rejected way, on a worker, with who decided and the comment for its later steps; 202 with `{"queue", "key"}`. 409 when that approval isn't open (someone decided it, or the run moved on) |

The worker records who acted (the caller's ID and name) in the task's audit
trail.

| Answer | When |
|---|---|
| 403 | The caller lacks `organizations:read`, or the permission the action needs, in the organization |
| 404 | Another organization's task, or none |
| 409 | The task's status doesn't allow it, `detail` saying so |
| 422 | A `task_type` or `exclude_task_type` other than lowercase letters, digits, `_` and `-` |
| 502 | The worker refused Forge's token (`FORGE_ADMIN_ASYNC_WORKER_TOKEN` must equal its `HYBRID_API__TOKEN`), or answered unexpectedly |
| 503 | Background tasks aren't set up, or the worker is unavailable |

### Workflow service routes

The async worker's workflows task holds no one's tokens: when a run's Run
workflow step starts another workflow, it asks this API at
`/internal/workflows/...` (outside the API prefix, never behind a user's
token), with `FORGE_ADMIN_WORKFLOWS_TOKEN` (`FORGE_WORKFLOWS_TOKEN` in
`.env.common`) as its bearer token; without the token set here, or with
another, the routes answer 401. Every call names the organization and the
member the run acts as, and the route checks the member still holds
`workflows:run` there, so a member removed from the organization fails the
run's next such step. Keep `/internal` off any public ingress; the routes
are left out of `/docs`.

| Route (below `/internal/workflows`) | What |
|---|---|
| `POST /workflows/{workflow_id}/runs` | Start another of the organization's workflows as the member: `{"organization_id", "user_id", "input", "parent", "depth"}`, where `parent` is the starting run and step and `depth` 1–20. The input is checked as for a person's run; the same step asking again is the same run. 202 with `{"queue", "key", "workflow_name"}` |

## Agents

An organization's agents are the `forge.agent/v1` documents its members
build in the web console's agent builder: Google ADK graphs of nodes (LLM
agents with their sub-agents, sequential, parallel and loop agents,
functions, routers, joins, human input, other saved agents) joined by edges
and run from their start. They're kept like workflows, in the `agents`
collection (`agent_documents.py`, sharing `document_store.py` with
`workflows.py`), with the same scope, revisions, deletion and 503s; their
IDs are `ag_` and ten lowercase letters and digits. `agents:manage` makes,
saves and deletes them (`org:admin` and `org:member` by default);
`organizations:read` reads them.

The document must match `src/forge_admin/agent.schema.json`, generated from
the web's `src/lib/agents/schema.ts`, and be at most
`FORGE_ADMIN_AGENTS_MAX_BYTES` (1 MiB) of JSON; otherwise 422. Only the
schema is checked, so a draft that can't run yet is saved.

`agent_build.build_agent(document, model=..., model_callbacks=...,
resolve=...)` builds a document into one ADK `Workflow`, each node the ADK
node its kind stands for and named by its name as a Python identifier (as the
builder shows it). Functions and routers evaluate their JSONata over
`{"input", "state"}`; a run's start hands on the person's message, its text
parsed when it's JSON. It refuses what can't run (no start, clashing names, a
missing or self-running saved agent, edges it can't follow) with an
`AgentBuildError` naming the node. The async worker runs them: see
[ADK workflow runs](#adk-workflow-runs-1).

## ADK workflow runs

An organization's ADK workflows (its agents) run on the async worker's
`adk_workflows` queue, as its ADK workflows task
([packages/python/tasks/adk-workflows](../../packages/python/tasks/adk-workflows/README.md))
runs them on Google ADK's graph engine. They're kept apart from Forge
workflows' runs: their own queue, task, labels, permissions and routes
(`adk_runs.py`, `api/routes/adk_workflow_runs.py`). Only the background tasks
are shared: a run is one of the organization's background tasks, its task
type `adk_workflows`, read, resubmitted, restarted and abandoned there.

**Starting one.** `POST /organizations/{id}/agents/{agent_id}/runs` with
`{"input"}` needs `agents:run` (`org:admin` and `org:member` by default).
Before anything is submitted:

- the input is checked against the start's `input_schema` (JSON Schema
  Draft 2020-12): 422 with what doesn't fit, or when there's no start;
- the saved ADK workflows it runs (its `saved` nodes', and theirs, and so on)
  are read from the store, and the document is built with them
  (`build_agent`): 422 with the build's reason, naming the node, e.g. a saved
  one that isn't there, or one that would run itself.

The run is submitted with `FORGE_ADMIN_EMBEDDING_REDIS_URL` (503 without it,
or while Redis or MongoDB doesn't answer) and answered 202 with `{"queue":
"adk_workflows", "key", "agent_id", "revision", "session_id"}`:

- its job's key is `adk_workflows.run:<agent_id>:<uuid>`, its labels
  `{"adk_workflow": <agent_id>, "adk_session": <session_id>}`;
- its payload is `{"tenant_id", "agent_id", "revision", "name", "document",
  "saved": {<id>: <document>}, "input", "session_id", "run_as",
  "run_as_name", "trigger": {"type": "manual", "by": <user>}}`: the ADK
  workflow and the saved ones **as they're saved now** (later saves never
  change it), and the member it **acts as**;
- its state is its **ADK session**, which the worker keeps in this database
  (ADK's `DatabaseSessionService`, app `adk_workflows`, user `run_as`, the
  `session_id` made here).

`GET .../agents/{agent_id}/runs` lists an ADK workflow's runs, newest first,
as the background tasks list does.

**Its steps.** `GET /organizations/{id}/adk-runs/{task_id}/steps`
(`organizations:read`) reads the run's task (404 unless it's one of the
organization's ADK workflow runs), then its ADK session here (503 while the
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

**What it waits for.** A paused run waits at its background task's
`approval`, whose `details` say what: `{"kind": "approval" | "human_input",
"approvers"?, "expires_at"?, "response_schema"?, "step", "step_name",
"workflow_name", "message"}`. Both are answered here, never at the
background tasks' decisions route (409 there):

| Route (below `/organizations/{id}/adk-runs/{task_id}`) | Who | What |
|---|---|---|
| `POST /decisions` | `agents:approve` for an approval whose approvers are `org:admin` (and anything else it names); `agents:run` for `org:member` | `{"request_id", "approved", "comment"?}` (the comment up to 4,000 characters): the run carries on down the approved or rejected way. 409 for human input |
| `POST /answers` | `agents:run` | `{"request_id", "answer"}`: the answer must fit the question's `response_schema` (422 listing what doesn't, or when its JSON is over 4,000 characters); it's sent as an approval whose comment is the answer as JSON. 409 for an approval |

Both answer 202 with the worker's `{"queue", "key"}`, 409 when the request
isn't open any more (answered, or the run moved on), and 404 for a task
that isn't one of the organization's ADK workflow runs.

## The assistant

The web console's assistant talks to a [Google ADK](https://google.github.io/adk-docs/)
agent served by this API under `/agents`, in the protocol of ADK's own API
server (`adk api_server`), which its SDK (`@assistant-ui/react-google-adk`,
direct mode) is written against. The one app is `forge`
(`agents/forge.py`), on the models of the shared model provider
configuration, or else Gemini (`gemini-3.5-flash`; see
[The assistant's models](#the-assistants-models)): a supervisor and its
specialists (below). ADK's events for tool calls and results, confirmations,
sign-in and input requests, agent transfers and artifacts stream through
unchanged.

### A supervisor and its specialists

The person talks with `forge`, a supervisor with no tools of its own. Each
toolset (see [The assistant's toolsets](#the-assistants-toolsets)) is held by
a specialist agent named after it (`workflows`, `events`, ...), and the
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
  "newMessage": {"role": "user", "parts": [{"text": "What do our workflows do?"}]}}'
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
  their site-wide roles; `agents/person.py`) and hands it to the agent as
  `temp:person`, for that run only. The web console sends the page they're
  on as `page_context` (path and route, title and crumbs, the workspace
  scope, the records on screen and the one in focus; `null` when they stop
  sharing it), which is kept with the message's event. It's checked for
  shape and size, and left out rather than refused when malformed
  (`agents/page_context.py`). The agent's instruction describes both, with
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

The assistant reads the same `model_provider.yaml` as workflows' Agent
steps: one file, in [`packages/python/common`](../../packages/python/common/README.md#model-provider-configuration),
names the providers, their models and how to reach them, and
`FORGE_ADMIN_MODEL_PROVIDER_CONFIG` names the file, e.g.
`../../packages/python/common/src/forge_common/model_provider/model_provider.openai.yaml`
(the image keeps the shared files at that path relative to `/app`). Its
`${NAME}` references resolve from the process environment, then from
`.env`; a missing one stops startup. The agent runs on one
`forge_common.adk.models.ProviderModels` (`agents/language_models.py`), which
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
the person is on, as `agents/screens.yaml` configures it (or the file
`FORGE_ADMIN_AGENT_SCREENS` names). Each screen's `when` matches the page
context the web console sends with every message: the router's `route`, its
`search` parameters (e.g. `view: events`) and the kinds of record on screen
(`entity: background_task`). Every screen that matches adds its `toolsets` (a
toolset by name, or `{name, tools}` for some of its tools), `instructions`
and `prompts` (at most six are suggested); `everywhere` applies on every
screen.

- **Built once.** The supervisor and a specialist per toolset that's set up
  are built at startup. On every model call, `agents/screens.py`'s `Screens`
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

Each toolset is registered in `agents/forge.py` (`toolsets()`), held by its
own specialist, and given to the assistant by `screens.yaml`. Every tool
calls this API's own routes as the person (`agents/person_api.py`): in the
process, over an ASGI transport, with a token minted for the conversation's
user (10 minutes, reused for 5, signed with `FORGE_ADMIN_JWT_SECRET`;
`agents/user_tokens.py`) and sent to the address they reached the API at, so
every call is authorized, scoped and audited as theirs, exactly as in the
web console, and any URL a route writes is the one they know.
`agents/route_tools.py`'s `RouteToolset` is their base: IDs are checked
before they go into a path, tools that change something ask the person to
confirm first (ADK's tool confirmation, which the console renders), and, as
`ForgeBaseToolset`s (`packages/python/common`), no tool raises: a refusal
answers `failed` with the route's reason and what to do about it, and large
results are cut down.

| Toolset | Module | What the assistant can do |
|---|---|---|
| `workflows` | `workflow_tools.py` | Read an organization's workflows and their runs; run a workflow; list and decide the approvals runs wait at; read, resubmit, restart or abandon background tasks; build a workflow from a description: `get_workflow_building_blocks` (the format's rules, the step catalog `workflow.catalog.json`, the models Agent steps may run on and their thinking levels, from `GET /agents/apps/forge/models`, and the organization's event types), `check_workflow_draft` (`workflow_drafts.py`: the JSON Schema, the runner's own parse and expression compile, the builder's graph and settings checks, that Agent steps name models Forge offers and that Run workflow steps name the organization's workflows), then `create_workflow` or `save_workflow` |
| `events` | `event_tools.py` | An organization's inbound endpoint (turning an existing one on or off), its event types and their schemas (define, change, delete), the events it received and why any were rejected, and checking sample payloads; never its token |
| `access` | `access_tools.py` | The person's profile, organizations, memberships and access, and why something was refused; the roles there are and what each grants; finding people; a scope's members and someone's access; giving or removing roles (`members:update`) |
| `administration` | `admin_tools.py` | Read, create and update organizations, role definitions, permission definitions and users; never deletes |

Left out on purpose: deleting workflows (the builder does), the endpoint's
token and turning the endpoint on for the first time (its token is only
shown once, on the Events page), and deleting organizations, roles,
permissions or users.

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
make async-worker     # the worker that runs workflow runs (the workflows queue)
make async-worker-api # its background tasks API on http://localhost:8104: runs and approvals
make up-all-local     # or all of it natively in one terminal; Ctrl-C stops the apps
```

`make admin` runs `forge-admin`, which applies pending migrations and then
serves. Compose passes the same `.env` to the container and points it at
`admin-mysql:3306`, the shared `mongo` and `redis`, and the async worker's
background tasks API. Without the launcher's migrations you can also run
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
`agents:approve` (`org:admin`).

Define models on `forge_admin.db.base.AuditBase` under `models/` and import
them in `models/__init__.py`, then from `apps/forge-admin-api`:

```sh
uv run alembic revision --autogenerate -m "add projects"
uv run alembic upgrade head        # or make admin-migrate from the root
```

Autogenerate leaves ADK's tables alone, and `alembic.ini` runs ruff over each
new revision. The launcher applies pending migrations at startup unless
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
writes as `seed`, and `/hooks` as `endpoint:<endpoint id>`. Replacing a
role's permissions only inserts and deletes what changed, so a grant keeps
the audit record of when it was first given. `tests/db/test_audit.py` fails
for any new table that skips `AuditBase`.

## Configuration

Precedence, lowest to highest: defaults, `.env` in the working directory, the
process environment. See `.env.example` for every setting.

Values the admin shares with other apps (the MySQL connection, MongoDB,
Redis, the service tokens, model keys) live once in the repository's
`.env.common`, and `.env` names each with a `${NAME}` reference, e.g.
`FORGE_ADMIN_WORKFLOWS_TOKEN=${FORGE_WORKFLOWS_TOKEN}`
(`forge_admin.env_files`). A reference resolves to an earlier line of `.env`,
otherwise to `.env.common`; a name defined in neither stops startup unless
the process environment sets that variable. `.env.common` is read from
`../../.env.common`, or `FORGE_ENV_COMMON_FILE` (empty disables it), and none
of it reaches the settings unless `.env` references it. `make env` creates
`.env.common` and generates its `FORGE_ASYNC_WORKER_TOKEN` and
`FORGE_WORKFLOWS_TOKEN`.

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
| `FORGE_ADMIN_WEB_URL` | `http://localhost:5190` | The web console, for the assistant's links to a workflow's builder |
| `FORGE_ADMIN_ASYNC_WORKER_URL` | unset | The async worker's background tasks API, scheme and host only (`http://127.0.0.1:8104` natively, as in `.env.example`); unset, background tasks and workflow run lists answer 503 |
| `FORGE_ADMIN_ASYNC_WORKER_TOKEN` | unset | Its bearer token, its `HYBRID_API__TOKEN`: `${FORGE_ASYNC_WORKER_TOKEN}` from `.env.common` |
| `FORGE_ADMIN_ASYNC_WORKER_TIMEOUT` | `10` | Seconds a call to it may take |
| `FORGE_ADMIN_EMBEDDING_REDIS_URL` | unset | The Redis the async worker's SAQ queues run on, where runs are submitted (`${FORGE_REDIS_URL}`); unset, starting a run answers 503 |
| `FORGE_ADMIN_PUBLIC_URL` | unset | Where other systems reach this API, the base of the event endpoint URLs; unset, the address each request came in on |
| `FORGE_ADMIN_EVENTS_MAX_BYTES` | `262144` | The largest event body an endpoint accepts (at most 16 MiB) |
| `FORGE_ADMIN_MONGO_URI` | unset | MongoDB for organizations' workflows and agents (`${FORGE_MONGO_URI}`); unset, the workflow and agent routes answer 503 |
| `FORGE_ADMIN_MONGO_DATABASE` | `forge_admin` | Its database |
| `FORGE_ADMIN_MONGO_TIMEOUT` | `5` | Seconds to find a MongoDB server before a request answers 503 |
| `FORGE_ADMIN_WORKFLOWS_MAX_BYTES` | `1048576` | The largest workflow document saved, in bytes of JSON |
| `FORGE_ADMIN_AGENTS_MAX_BYTES` | `1048576` | The largest agent document saved, in bytes of JSON |
| `FORGE_ADMIN_WORKFLOWS_TOKEN` | unset | What the async worker's workflows task calls the [workflow service routes](#workflow-service-routes) with (32+ characters), `${FORGE_WORKFLOWS_TOKEN}` from `.env.common`; unset, they answer 401 |
| `FORGE_ADMIN_MODEL_PROVIDER_CONFIG` | unset | The shared `model_provider.yaml` the assistant's models come from (see [The assistant's models](#the-assistants-models)); unset, Gemini |
| `FORGE_ADMIN_GOOGLE_API_KEY` | unset | Without a model provider configuration: the assistant's Gemini API key (`GOOGLE_API_KEY` or `GEMINI_API_KEY` also work), `${FORGE_GOOGLE_API_KEY}` from `.env.common`; unset, it says it isn't set up |
| `FORGE_ADMIN_AGENT_MODEL` | the configuration's default, or `gemini-3.5-flash` | The model the assistant runs on until a conversation chooses: `provider/model`, or an id only one provider has |
| `FORGE_ADMIN_AGENT_MODELS` | `[]` | JSON array: with a model provider configuration, which of its models a conversation may choose (all when empty); without, other Gemini models |
| `FORGE_ADMIN_AGENT_SCREENS` | unset | A screen configuration shaped like `agents/screens.yaml`, which is used when this is unset |
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
                       # tests marked mysql: migrations, readiness, routes, and workflows
                       # against MongoDB (skipped without FORGE_ADMIN_MONGO_URI)
make admin-fmt         # format and autofix the sources
```
