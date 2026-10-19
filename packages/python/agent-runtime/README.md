# forge-agent-runtime

Runs Forge chat agents on Google ADK. Build an agent in Forge's Agents
builder, export its JSON (`forge.chat_agent/v1`), and run it over ADK's run
API (for chat UIs) and [Google's A2A protocol](https://a2a-protocol.org) (for
other agents):

- **on its own**, with `forge-agent serve agent.json`;
- **as a project of its own**: a server and a chat UI, made by the builder's
  **Generate standalone agent** button or `forge-agent new`;
- **in your app**, with the `AgentServer` builder, by mounting the run API in
  your FastAPI app, or by calling the executor yourself.

Forge's hosted runtime is this package too: it reads agents from its database
instead of files.

```sh
pip install 'forge-agent-runtime[server]'        # ADK's run API
pip install 'forge-agent-runtime[server,a2a]'    # and A2A
```

## Serve an agent

```sh
export OPENAI_API_KEY=sk-…
forge-agent validate support-assistant.chat-agent.json
forge-agent serve support-assistant.chat-agent.json \
  --model-provider model_provider.yaml --port 8000
```

The agent's `appName` is its ID (`ca_…`). The server speaks ADK's own run API
(`adk api_server`), so `@assistant-ui/react-google-adk` and other ADK clients
work against it unchanged:

```sh
curl -X POST localhost:8000/apps/ca_x1y2z3/users/u1/sessions -H 'content-type: application/json' -d '{}'
curl -N -X POST localhost:8000/run_sse -H 'content-type: application/json' -d '{
  "appName": "ca_x1y2z3", "userId": "u1", "sessionId": "<id from above>",
  "newMessage": {"role": "user", "parts": [{"text": "Where is my order?"}]},
  "streaming": true,
  "stateDelta": {"customer_tier": "pro"}
}'
```

| Route | |
|---|---|
| `GET /apps/{app}` | The agent: name, version, the state it takes |
| `GET` / `POST /apps/{app}/users/{user}/sessions` | A person's conversations |
| `GET` / `DELETE /apps/{app}/users/{user}/sessions/{id}` | One conversation |
| `GET /apps/{app}/users/{user}/sessions/{id}/workflow-runs/{call}` | How the workflow a call in it started is going: its steps so far (hosted runtime: `WorkflowRunner.progress`) |
| `POST /run_sse` | Run a turn; ADK events as Server-Sent Events |

Each streamed turn begins and ends with an ADK control event whose
`customMetadata["forge:turn"]` contains `version: 1`, a 32-character `runId`,
and `status`. The start status is `running`; the terminal status is
`succeeded`, `failed`, or `cancelled`. Failed outcomes also carry `errorCode`
and `errorMessage`. These events have no conversation content. They surround
all model, tool, sub-agent, and follow-up events, and the terminal event follows
execution and audit cleanup. A tool-confirmation pause successfully ends the
current invocation while its ADK long-running call remains open.

A conversation runs one turn at a time: `run_sse` takes the conversation's
lease before it streams, and a turn sent while another runs waits a moment
(`AgentExecutor(turn_wait=…)`, 5 seconds) then answers 409 with
`Retry-After`; A2A rejects the message. Leases are this process's
(`MemoryTurnLeases`) unless the executor is given shared ones (the hosted
runtime keeps them in MySQL), and run out unless renewed, so a stopped
process frees its conversations.

Clients must treat EOF without a terminal event as an interrupted response,
keep partial text, and show an error. A model's final text event is not the
whole invocation's completion signal. Forge UIs opt into this check with
`requireTurnOutcome: true`; deploy the server protocol before those clients.
Generic ADK connections can omit this option. Quiet streams send a keepalive
comment every 15 seconds, including the hosted assistant. On disconnect the
server cancels execution and finalizes its audit as cancelled (or retains an
already observed failure); it cannot deliver a terminal event to a disconnected
client. Hosted audit rows use the same `runId` for correlation.

Failed and cancelled turns also save a contentless terminal event in their
ADK session, including failures after the final model message. Reopening a
conversation therefore retains its outcome and any saved partial answer.
The outer transport (or the executor for direct calls) owns this write, after
error mapping and cleanup. Writes are bounded to five seconds; storage failures
are logged without replacing the original failure or indefinitely holding the
stream open. Native error messages remain in the session, and the UI avoids
showing the terminal marker as a duplicate error.

`forge-agent serve ./agents/` serves every agent in a folder. When it holds
several versions of an agent, `ca_x` runs the newest, and `ca_x@3` pins one.
`--a2a` serves them over A2A too (below).

Settings are flags or `FORGE_AGENT_*` variables:

| Setting | |
|---|---|
| `FORGE_AGENT_MODEL_PROVIDER_CONFIG` | The model_provider.yaml agents run on, with keys as `${NAME}`. Defaults to forge-common's shared one. |
| `FORGE_AGENT_SESSIONS` | `memory` (the default), or a database URL to keep conversations: `sqlite:///sessions.db`, `postgresql://…` (`sessions-postgres` extra), `mysql://…` (`sessions-mysql`). URLs as hosts hand them out get their async driver. |
| `FORGE_AGENT_ARTIFACTS` | Where files the agents' tools save are kept: `memory` (the default), a folder, or `s3://bucket/prefix` (`artifacts-s3` extra; `AWS_*` from the environment, `AWS_ENDPOINT_URL_S3` for S3 that isn't Amazon's). `FORGE_AGENT_ARTIFACTS_CREATE_BUCKET=true` makes the bucket on first use. |
| `FORGE_AGENT_MEMORY_ATLAS_URI` | Long-term memory in MongoDB Atlas, embedded with OpenAI (needs the `memory-atlas` extra) |
| `FORGE_AGENT_STREAMING` | `true` or `false` to stream replies, or not, whatever requests ask; as each asks by default |
| `FORGE_AGENT_API_KEYS` | Keys the API asks for (`["k1","k2"]`): callers send `Authorization: Bearer <key>` or `X-API-Key` |
| `FORGE_AGENT_ALLOW_PRIVATE`, `FORGE_AGENT_ALLOWED_HOSTS` | What HTTP tools may reach. Private networks are refused by default. |
| `FORGE_AGENT_SANDBOX_MODE` | Where Code execution tools (and skills' scripts) run the code agents send (`--sandbox-mode`): `off` (the default; agents with one don't build); `local`, ADK's `UnsafeLocalCodeExecutor`, a new Python process on this machine for each call, with the server's environment, files and network (development only); or `openshell`, a new NVIDIA OpenShell sandbox for each call, deleted after it (the `openshell` extra). |
| `FORGE_AGENT_OPENSHELL__GATEWAY` | For `openshell`: a gateway the `openshell` CLI registered (its name; the active one by default), or its URL with `FORGE_AGENT_OPENSHELL__TLS_DIR` (`ca.crt`, `tls.crt`, `tls.key`) and/or `__TOKEN`. `__WORKSPACE`, `__IMAGE` (needs `python3`; the gateway's default by default) and `__POLICY` (a `SandboxPolicy` YAML/JSON file; by default no network, only `/sandbox` and `/tmp` writable) shape the sandboxes. |
| `FORGE_AGENT_API_PREFIX` | Mount the run API under a path (`--prefix`; none by default, `/api` in `AgentServer`) |
| `FORGE_AGENT_A2A` | `true` serves Google's A2A protocol too, at `/a2a` (`--a2a`; the `a2a` extra) |
| `FORGE_AGENT_A2A_TASKS` | Where A2A tasks are kept: `memory`, or a database URL; the conversations' database by default |
| `FORGE_AGENT_PUBLIC_URL` | The address A2A cards give callers (`https://agent.example.com`); the one each request came to by default |

HTTP tools and OpenAPI requests validate DNS at connection time and connect only
to the checked IP addresses, while retaining the original Host header and TLS
hostname. Custom headers carrying credentials are dropped when a redirect changes
the scheme, host, or port. Deployment host allowlists still permit private endpoints.
When supplying an HTTP client to the runtime, use `forge_agent_runtime.http_client()`;
an ordinary HTTPX network transport is refused. `MockTransport` and `ASGITransport`
are supported for tests. Configured HTTP proxy routes are refused because the proxy
would resolve the destination outside the IP guard; they are never bypassed silently.

## Over A2A

With the `a2a` extra, every agent is an A2A agent too
([`forge_agent_runtime.a2a`](src/forge_agent_runtime/a2a.py), on `a2a-sdk` 1.x):

| Route | |
|---|---|
| `GET /a2a/{app}/.well-known/agent-card.json` | Its agent card: name, description, version, skills (it, and the agents it hands off to), where to call it, how to sign in |
| `POST /a2a/{app}` | JSON-RPC: A2A 1.0 (`SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`) and 0.3 (`message/send`, `message/stream`, `tasks/get`, `tasks/cancel`, `tasks/resubscribe`) on the same URL |

`{app}` is the run API's `appName` (`ca_x`, `ca_x@3`, `ca_x@draft`). A server of
one agent (`AgentServer`) serves it at `/a2a` itself, and its card at
`/.well-known/agent-card.json`, where A2A clients look first.

```sh
curl localhost:8000/.well-known/agent-card.json
curl -X POST localhost:8000/a2a -H 'content-type: application/json' -H 'A2A-Version: 1.0' -d '{
  "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
  "params": {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "Where is my order?"}],
             "metadata": {"state": {"customer_tier": "pro"}}}}
}'
```

- A turn runs as a chat turn does: the same agent, its input checked, `{{ request.* }}`
  filled in, usage recorded. The state the agent's input schema declares goes in the
  message's `metadata.state` (as a chat sends `stateDelta`); anything else is rejected.
- An A2A conversation (`contextId`, at most 36 characters) is the agent's ADK session of
  that ID, so the run API sees it too. Its user is the caller when the server knows who's
  calling (a sign-in setting `request.state.user_id`, or each API key a caller of its
  own, `api-key:<hash>`), and `A2A_USER_<contextId>` otherwise, as ADK's own A2A server
  names them: on a server that asks for neither, whoever knows a conversation's ID
  reaches it.
- Each message is a task: completed with the reply as its artifact, failed saying why, or
  `input-required` on a tool that asks for confirmation. Tasks are kept per agent and
  caller (`task_store`: memory or a database); listing them needs a caller the server
  knows, since on an open server it would show everyone's. Push notifications aren't
  offered.
- The API's keys and `with_auth` guard `/a2a` too; the card stays open, and says how to sign in.

## Start a project

```sh
forge-agent new support-assistant.chat-agent.json --out support-assistant
```

Writes a whole project around the agent, the way Spring Initializr writes a
Spring Boot app; Forge's **Generate standalone agent** button downloads the
same project as a zip, made of what its dialog picks:

```
support-assistant/
  main.py                the AgentServer below, one with_* line per choice
  agent/                 the agent's JSON, and its JSON Schema
  model_provider.yaml    the models it runs on, keys as ${NAME}
  .env.example           every ${NAME} the models, tools and choices ask for
  pyproject.toml         forge-agent-runtime and the extras the choices need, from PyPI or vendor/*.whl
  web/                   Vite + React: a blank page with the assistant at the bottom right (with the UI)
  compose.yaml           the database, S3 and Atlas the choices need, for development
  AGENTS.md  CLAUDE.md   how the project works, for AI coding agents
  .github/CODEOWNERS     who reviews changes
  Dockerfile  Makefile  README.md
```

| Choice | Flag | |
|---|---|---|
| Conversations | `--sessions memory\|sqlite\|postgresql\|mysql` | Where they're kept |
| Files | `--artifacts memory\|folder\|s3` | Where the agent's tools' files (ADK artifacts) are kept |
| Long-term memory | `--memory memory\|atlas` | In memory, or MongoDB Atlas searched by meaning |
| Replies | `--no-streaming` | Whole, rather than streamed as the model writes them |
| Interface | `--api-only` | The API alone, without the chat UI |
| A2A | `--no-a2a` | Without Google's A2A protocol (on by default: `/a2a` and its card) |
| Access | `--api-key`, `--cors-origin URL` | A key the API asks for (API only); pages elsewhere that may call it |
| Owners | `--code-owner @org/team` | `.github/CODEOWNERS` |

Copy `.env.example` to `.env` and fill it in (where it keeps things is already
filled in for `compose.yaml`), `docker compose up -d --wait` if it has one,
then `cd web && npm install && npm run build` with the UI, and `uv run
main.py`. Its own README covers development and Docker.

`--wheels dist/` bundles the runtime's wheels (`make starter-wheels`) into
`vendor/` until the packages are on PyPI; without it, the project pins the
installed version from PyPI. `--zip` writes a zip; `--name` names it.

## A server of your own

`AgentServer` is a builder: each `with_*` returns it, `build()` returns the
FastAPI app, and `run()` serves it. The generated `main.py` is this:

```python
from pathlib import Path

from forge_agent_runtime import AgentServer, env

HERE = Path(__file__).parent

server = (
    AgentServer()  # FORGE_AGENT_* settings, from the environment and .env
    .with_agent(HERE / "agent/support-assistant.chat-agent.json")
    .with_models(HERE / "model_provider.yaml")
    .with_sessions(env("DATABASE_URL"))  # PostgreSQL
    .with_artifacts(env("ARTIFACTS_URL"))  # s3://bucket/prefix
    .with_memory(env("MONGODB_URI"))  # MongoDB Atlas
    .with_streaming(False)  # replies arrive whole
    .with_web(HERE / "web" / "dist")  # the built UI, served at /
    .with_a2a()  # Google's A2A too: /a2a and its card
    # .with_auth(Depends(verify_token))      # guards the run API only
    # .with_routes(my_router)
)
app = server.build()

if __name__ == "__main__":
    server.run()
```

| Method | |
|---|---|
| `with_agent(path \| document \| AgentSource)` | The agent it serves |
| `with_models(path \| ModelProviderConfig \| ProviderModels)` | The models it may run on |
| `with_sessions("memory" \| url \| BaseSessionService)` | Where conversations are kept |
| `with_artifacts("memory" \| folder \| "s3://…" \| BaseArtifactService)` | Where files the agent's tools save are kept |
| `with_memory("memory" \| atlas_uri \| BaseMemoryService)` | Where long-term memory is kept |
| `with_streaming(True \| False \| None)` | Replies streamed, whole, or as each request asks |
| `with_services(workflows=…, mcp_servers=…, knowledge_bases=…)` | What only Forge's runtime has, provided by you |
| `with_auth(*dependencies)`, `with_api_keys(*keys)` | FastAPI dependencies on the run API; keys it asks for (for servers calling it, not pages) |
| `with_routes`, `with_lifespan`, `with_middleware`, `with_cors` | The rest of your app |
| `with_web(dir)` | A built single-page app, served at `/` (every unknown path gets its `index.html`) |
| `with_a2a(tasks=…, public_url=…)` | Google's A2A protocol too: `/a2a`, its card at `/.well-known/agent-card.json` (the `a2a` extra) |

It serves `GET /api/agent` (the agent's ID, name, version and input schema,
which the generated UI reads), the run API under `/api`, and `GET /healthz`.
Methods override `AgentServerSettings`, which reads `FORGE_AGENT_*` from the
environment and `.env`; `${NAME}`s in the model provider config and tools are
filled from both. `FORGE_AGENT_HOST`, `FORGE_AGENT_PORT`, `FORGE_AGENT_WEB_DIR`,
`FORGE_AGENT_CORS_ORIGINS` and `FORGE_AGENT_TITLE` are settings too. `env("NAME")`
reads your own settings the same way, and stops the server, saying which, when
one isn't set (`env("NAME", "default")` for one that may not be). As it starts,
it logs where it keeps things.

## What the chat sends

Every request is ADK's run request: `appName`, `userId`, `sessionId`,
`newMessage`, `streaming` and `stateDelta`. `stateDelta` may set `model` and
`thinking_level` (the model picker's), `forge:suggest_follow_ups` and the
fields the agent's **input schema** declares (its `state_schema`), each of its
declared type. Anything else is refused with a 422.

`forge:suggest_follow_ups` (`true`, or `{"count": 1–5}`) asks for follow-up
questions after the reply: once the turn ends with one, its model (without
thinking) writes a few short messages the person might send next, and the
run's last event sets them in the state as `forge:follow_ups`
(`{"suggestions": [...]}`), for the chat to show above its composer. Each turn
clears the last turn's; one that ends waiting on the person gets none.
forge-ui's assistant sends it with `useAdkAssistant({ suggestFollowUps: true })`
(`forge_common.adk.follow_ups`). The schema's patterns run on forge-common's bounded
engine (`forge_common.patterns`), not Python's `re`: one that runs away on a
value refuses it within half a second, rather than holding the interpreter
lock, and every other conversation with it.

Instructions read both as `{{ … }}` templates (JSONata), filled in before each
model call:

```
You're helping {{ request.userId }}, on the {{ state.customer_tier }} plan.
```

## In your own app

```python
from fastapi import Depends, FastAPI
from forge_common.adk.models import ProviderModels
from forge_common.model_provider import load_model_provider_config
from forge_agent_runtime import AgentExecutor, FileAgentSource
from forge_agent_runtime.server import create_router

executor = AgentExecutor(
    FileAgentSource("support-assistant.chat-agent.json"),
    models=ProviderModels(load_model_provider_config("model_provider.yaml")),
)
app = FastAPI()
app.include_router(create_router(executor, dependencies=[Depends(your_auth)]), prefix="/agents")
# And over A2A: POST /agents/a2a/{app}, its card at /agents/a2a/{app}/.well-known/agent-card.json
from forge_agent_runtime.a2a import create_a2a_router, task_store

app.include_router(
    create_a2a_router(executor, path="/a2a", tasks=task_store("sqlite:///tasks.db")), prefix="/agents"
)
```

You can also run turns directly, with `async for event in executor.run(RunRequest(...))`,
or use `build_app(document, models=…)` to get the ADK `App` for your own
`Runner`. An `AgentSource` is anything with
`async get(AgentRef) -> ResolvedAgent`, so agents can come from your own store.

To decide who may call an agent, pass these to both routers; each refuses by
raising `HTTPException`:

- `admit=`: an `AdmitCall`, `async (request, agent, *, action) -> None`,
  called first with only the agent's reference and organization
  (`LocatedAgent`, from `AgentExecutor.locate`: a source with `locate(ref)`
  answers without reading the definition). Nothing of an agent a caller
  isn't let in to is read for them; an agent that can't be built answers
  without why (the server logs it).
- `authorize=`: an `AuthorizeCall`, `async (request, agent, *, action,
  user_id) -> None`, once the agent is found and before anything is built or
  run, for each action (`agent`, `sessions`, `run`, `card`, `a2a`).
- `owner=` (run API): a `ConversationOwner`, `async (request, user_id) ->
  str`: the user ID the conversations a call names are kept under, from who
  the caller is. Every route uses it, so a caller reaches only their own;
  `owned_by(caller, user_id)` keeps an API key's end users apart from every
  other caller's (`<caller>/<user>`). Responses show the `userId` the call
  named. The standalone server (`AgentServer`) binds each key's or
  `with_auth` caller's conversations so.

The hosted runtime admits callers with `agents:run` in the agent's
organization (`agents:manage` for its draft), into their own conversations.
Every turn checks the chat agent's rule as it starts (`AgentExecutor.refusal`,
403 before the stream), and each agent's rule holds with those of the agents
above it, so taking access away stops a conversation already handed on. A
published version never runs a workflow's draft, and an HTTP tool's host is
its author's to write: the model's arguments can't pick it, and a run's
value may only for a tool sending no headers of its own.

Other kinds of agent can share the A2A router's URLs, by their names'
prefix: pass `services=[A2aService(prefix, find, card, executor)]` (the
hosted runtime serves its workflows, `ag_…`, so). `AgentRef` (`ca_x`,
`ca_x@3`, `ca_x@draft`; `forge_agent_runtime.refs`) names workflows the same
way, and a workflow tool's `version` picks which of its versions it calls.
A saved agent from another organization never builds: it would bring that
organization's MCP servers and knowledge bases with it.

## What runs where

| Node | Standalone | Notes |
|---|---|---|
| Chat agent, sub-agents | ✓ | Sub-agents are handed the conversation (`chat`, `task`, `single_turn`) or called as tools |
| HTTP tool | ✓ | `${NAME}` in its URL and headers is filled in from the environment |
| OpenAPI | ✓ | Spec from a URL or pasted in |
| MCP server (by URL) | ✓ | |
| MCP server (one of the organization's) | hosted only | Pass `RuntimeServices(mcp_servers=…)` (or `with_services`) to provide your own |
| Memory | ✓ | In memory, or Atlas with the `memory-atlas` extra |
| Files (ADK artifacts) | ✓ | In memory, a folder, or S3 with the `artifacts-s3` extra; none of the builder's nodes save files yet, your own tools can |
| Saved agent | ✓ | Exported in the agent's `dependencies` |
| Workflow | hosted only | Pass `RuntimeServices(workflows=…)` to provide your own |
| Knowledge base | hosted only | Pass `RuntimeServices(knowledge_bases=…)` to provide your own |
| Code execution | ✓ | Off until `FORGE_AGENT_SANDBOX_MODE=openshell` (a new OpenShell sandbox for each call) or `local` (unsandboxed: development only); or pass `RuntimeServices(code_executor=…)`, any of ADK's code executors |
| Rich answers | ✓ | A `present` tool answering `{"shown": true}`: Forge's chats draw its call with assistant-ui's Generative UI (its 35 components and Forge's Choices). Each Rich answers node picks the components (`components`), and only those go into the agent's model calls; what the person clicks or fills in comes back as their next message. API and A2A callers get the raw call, and the agent is told to write a sentence about it too. Built from `present.catalog.json`, written by the web console's `npm run generate:agent-schema` |

Exports never carry secrets: write them as `${NAME}` in the builder, and set
them in the environment the agent runs in.

Any agent node may have an access rule (`config.access`): which of the
organization's agent permissions, by ID, someone needs to use it, as
`{"permission": 12}`, `{"all": [...]}` or `{"any": [...]}`, nesting three
deep. The runtime checks it each time the agent starts, for whoever the call
is for (`RuntimeServices(access=…)`, `forge_agent_runtime.access`): an agent
they may not use answers with what it needs, and an agent handing off to
it, calling it or giving it a task is answered that instead, so the
conversation stays with it. Without an `access` service nobody holds any
permission, so an exported agent's nodes with rules refuse until you pass
one.

## Checks

```sh
make agent-runtime-check   # ruff, format check, mypy, pytest, uv build
make starter-wheels        # the wheels standalone projects bundle, into dist/
```

The project template is `src/forge_agent_runtime/starter/template/`. Its UI
components (`starter/web_vendor/`) are copied from the `@forge-ui` registry by
`npm run starter:build` in `packages/forge-ui`; don't edit them by hand.

`chat_agent.schema.json` is generated from the web builder
(`npm run generate:agent-schema` in `apps/forge-web`). Don't edit it by hand.
