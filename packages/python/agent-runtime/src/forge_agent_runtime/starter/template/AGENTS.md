# AGENTS.md

Instructions for AI coding agents (and people) working in this project.

## What this is

@@NAME@@ is a chat agent built in Forge's Agents builder and generated as a project of its
own. It runs without Forge:

@@if ui@@
- **`main.py`** is the whole server: `forge-agent-runtime`'s `AgentServer`, a builder whose
  `with_*` lines say what it's made of. `server.build()` is a FastAPI app; `server.run()`
  serves it with uvicorn.
- **`@@AGENT_FILE@@`** is the agent: a `forge.chat_agent/v1` document of nodes (the chat
  agent, its tools, sub-agents) and edges. The runtime builds Google ADK agents from it
  when the server starts.
- **`web/`** is the UI: Vite, React 19, Tailwind 4 and Forge's components (copied in from
  the `@forge-ui` registry). A blank page with the assistant modal in the corner, talking
  to the server's `/api`.
@@end@@
@@if !ui@@
- **`main.py`** is the whole server: `forge-agent-runtime`'s `AgentServer`, a builder whose
  `with_*` lines say what it's made of. `server.build()` is a FastAPI app; `server.run()`
  serves it with uvicorn. It has no UI: front ends and services call its API.
- **`@@AGENT_FILE@@`** is the agent: a `forge.chat_agent/v1` document of nodes (the chat
  agent, its tools, sub-agents) and edges. The runtime builds Google ADK agents from it
  when the server starts.
@@end@@

How it's set up:

| | |
|---|---|
@@CHOICES@@

## Layout

| Path | What it is | Change it? |
|---|---|---|
| `main.py` | The server's builder chain | Yes: add routes, lifespans, middleware, auth, services |
| `@@AGENT_FILE@@` | The agent | Prefer Forge (export again); by hand, see below |
| `model_provider.yaml` | The models the agent may run on; keys are `${NAME}`s | Yes, keeping keys as `${NAME}`s |
| `.env.example` | Every setting `.env` needs | Yes, when you add one |
| `.env` | The real settings and secrets | Never commit it; never print its values |
| `pyproject.toml` | Python dependencies (uv) | Yes, with `uv add` |
@@if wheels@@
| `vendor/` | The runtime's wheels, until it's on PyPI | Only to replace them with newer ones |
@@end@@
| `agent/chat_agent.schema.json` | The agent's JSON Schema, for editors and checks | No |
@@if ui@@
| `web/src/App.tsx` | The page | Yes |
| `web/src/agent.tsx` | The assistant, wired to the server | Yes |
| `web/src/agent-state.ts` | The state the page sends the agent (its input schema, typed) | Yes, matching the agent's `state_schema` |
| `web/src/components/` | Forge's components, copied in | Yes, they're this project's now |
@@end@@
@@if compose@@
| `compose.yaml` | What it keeps things in, for development | Yes, matching `.env.example` |
@@end@@
| `.github/CODEOWNERS` | Who reviews changes | Yes |

## Commands

```sh
# Install the Python dependencies
uv sync
@@if compose@@
# Start what it keeps things in (compose.yaml)
docker compose up -d --wait
@@end@@
# Serve on http://127.0.0.1:8000
uv run main.py
@@if ui@@
# Build the UI (type-checks it too); or serve it with hot reload on :5173, /api sent to :8000
cd web && npm install && npm run build
cd web && npm run dev
@@end@@
# Check the agent after editing it
uv run forge-agent validate @@AGENT_FILE@@
# One image
docker build -t @@SLUG@@ .
```

## Before you finish a change

1. `uv run forge-agent validate @@AGENT_FILE@@` passes, if you touched the agent.
2. `uv run python -c "import main"` builds the app without errors (it needs `.env`).
@@if ui@@
3. `cd web && npm run build` passes, if you touched `web/`.
4. The server starts (`uv run main.py`) and `curl localhost:8000/healthz` answers `{"status":"ok"}`.
@@end@@
@@if !ui@@
3. The server starts (`uv run main.py`) and `curl localhost:8000/healthz` answers `{"status":"ok"}`.
@@end@@

## The agent's JSON

- Nodes have an `id`, a `kind` and a `config`; edges join a node's output (`tools`, `agents`)
  to another node. The first `agent` node is the entry point.
- Instructions are templates: `{{ request.userId }}` and `{{ state.customer_tier }}` are
  filled in (JSONata) before each model call.
- `state_schema` on the entry agent declares what callers may send as ADK's `stateDelta`;
  anything else is refused with a 422. `model` and `thinking_level` are always allowed.
- Secrets are never in the JSON: HTTP tools and MCP servers use `${NAME}` in their URLs
  and headers, filled in from `.env`. Add each new one to `.env.example`.
- Saved agents it uses are bundled in its `dependencies`.
- When you can, change the agent in Forge and generate or export it again: the builder
  checks it as you go. After editing by hand, validate it.

## The server

- `AgentServer` methods override `FORGE_AGENT_*` settings (`.env` or the environment). Read
  your own settings with `env("NAME")` (or `env("NAME", "default")`), which stops the
  server, saying what's missing, when one isn't set.
- The run API is ADK's, under `/api`: `POST /api/run_sse` (Server-Sent Events) and
  `/api/apps/{agent}/users/{user}/sessions`. `GET /api/agent` describes the agent;
  `GET /healthz` is the health check.
- Add your own endpoints with `.with_routes(router)` (a FastAPI `APIRouter`), startup and
  shutdown with `.with_lifespan(...)`, and auth with `.with_auth(Depends(...))`, which
  guards the run API.
- Tools only Forge hosts (its workflows, knowledge bases, MCP servers) need
  `.with_services(...)`: see the commented lines in `main.py`, if any.

## Rules

- Don't put secrets in code, the agent JSON or `model_provider.yaml`: `${NAME}` and `.env`.
- Don't commit `.env`, `data/` or build output.
- Keep `main.py` a builder chain: put larger code in modules beside it and pass it in.
- Python 3.11+, managed with uv (`uv add <package>`, never `pip install` into the venv).
@@if ui@@
- The UI uses Forge's design tokens (`web/src/index.css`): use them, not raw colours.
@@end@@
