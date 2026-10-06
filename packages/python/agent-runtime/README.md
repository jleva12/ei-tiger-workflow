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
| `POST /run_sse` | Run a turn; ADK events as Server-Sent Events |

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
| `FORGE_AGENT_API_PREFIX` | Mount the run API under a path (`--prefix`; none by default, `/api` in `AgentServer`) |
| `FORGE_AGENT_A2A` | `true` serves Google's A2A protocol too, at `/a2a` (`--a2a`; the `a2a` extra) |
| `FORGE_AGENT_A2A_TASKS` | Where A2A tasks are kept: `memory`, or a database URL; the conversations' database by default |
| `FORGE_AGENT_PUBLIC_URL` | The address A2A cards give callers (`https://agent.example.com`); the one each request came to by default |

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
  calling (an API key, a sign-in setting `request.state.user_id`), and `A2A_USER_<contextId>`
  otherwise, as ADK's own A2A server names them.
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
    .with_sessions(env("DATABASE_URL"))      # PostgreSQL
    .with_artifacts(env("ARTIFACTS_URL"))    # s3://bucket/prefix
    .with_memory(env("MONGODB_URI"))         # MongoDB Atlas
    .with_streaming(False)                   # replies arrive whole
    .with_web(HERE / "web" / "dist")         # the built UI, served at /
    .with_a2a()                              # Google's A2A too: /a2a and its card
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
`thinking_level` (the model picker's) and the fields the agent's **input
schema** declares (its `state_schema`), each of its declared type. Anything
else is refused with a 422.

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
app.include_router(create_a2a_router(executor, path="/a2a", tasks=task_store("sqlite:///tasks.db")), prefix="/agents")
```

You can also run turns directly, with `async for event in executor.run(RunRequest(...))`,
or use `build_app(document, models=…)` to get the ADK `App` for your own
`Runner`. An `AgentSource` is anything with
`async get(AgentRef) -> ResolvedAgent`, so agents can come from your own store.

To decide per agent who may call it, pass `authorize=` to both routers: an
`AuthorizeCall`, `async (request, agent, *, action, user_id) -> None`, called
once the agent is found and before anything is built or run, for each
action (`agent`, `sessions`, `run`, `card`, `a2a`; `user_id` is ADK's
`userId` when the call reads or adds to someone's conversations). It refuses
by raising `HTTPException`. The hosted runtime uses it to allow only callers
with `agents:run` in the agent's organization, into their own conversations.

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

Exports never carry secrets: write them as `${NAME}` in the builder, and set
them in the environment the agent runs in.

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
