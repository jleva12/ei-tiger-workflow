# @@NAME@@

@@if ui@@
A Forge chat agent (`@@AGENT_ID@@`, @@VERSION_LABEL@@), served on its own: a Python server
built with [`forge-agent-runtime`](https://pypi.org/project/forge-agent-runtime/)'s
`AgentServer`, and a React UI made of Forge's components with the assistant floating
in the corner. Nothing here calls back to Forge.
@@end@@
@@if !ui@@
A Forge chat agent (`@@AGENT_ID@@`, @@VERSION_LABEL@@), served on its own: a Python server
built with [`forge-agent-runtime`](https://pypi.org/project/forge-agent-runtime/)'s
`AgentServer`, speaking ADK's run API for your own front end or services. Nothing here
calls back to Forge.
@@end@@

@@DRAFT_NOTE@@## How it's set up

| | |
|---|---|
@@CHOICES@@

Each is one line in `main.py`; change it there (and add the extra it needs in
`pyproject.toml`). Where each one is comes from `.env`.

## Run it

1. Fill in `.env`:

   ```sh
   cp .env.example .env
   ```

   It needs:
@@ENV_LIST@@
@@if compose@@
2. Start what it keeps things in (Docker; `compose.yaml` matches `.env.example`):

   ```sh
   docker compose up -d --wait
   ```

3. Install@@if ui@@ and build@@end@@:
@@end@@
@@if !compose@@
2. Install@@if ui@@ and build@@end@@:
@@end@@

   ```sh
   uv sync                      # Python@@RUNTIME_FROM@@
@@if ui@@
   cd web && npm install && npm run build && cd ..
@@end@@
   ```

@@if compose@@
4. Run it:
@@end@@
@@if !compose@@
3. Run it:
@@end@@

   ```sh
   uv run main.py               # http://127.0.0.1:8000
   ```
@@if ui@@

   While working on the UI, run `npm run dev` in `web/` as well: Vite serves it on
   http://localhost:5173 and sends `/api` to the server.
@@end@@

Or with Docker: `docker build -t @@SLUG@@ . && docker run --env-file .env -p 8000:8000 @@SLUG@@`.
@@if data@@
Mount a volume at `/app/data` to keep what it saves there.
@@end@@

## Updating

- **The agent:** generate it again in Forge, or export it, and replace `@@AGENT_FILE@@`.
  The server reads it again when the file changes.
@@if wheels@@
- **The runtime:** put the new wheels in `vendor/` (or, once it's on PyPI, pin its version in
  `pyproject.toml`), then `uv lock --upgrade-package forge-agent-runtime --upgrade-package forge-common --upgrade-package forge-jsonata && uv sync`.
@@end@@
@@if pypi@@
- **The runtime:** `uv lock --upgrade-package forge-agent-runtime && uv sync` takes its newest
  compatible version (`pyproject.toml` says which).
@@end@@

## What's in it

| | |
|---|---|
| `main.py` | The server: `AgentServer()` and what it's made of, one `with_*` line each. Add your own routes and lifespans there. |
| `@@AGENT_FILE@@` | The agent, as Forge exported it: its nodes, edges, and the saved agents it uses. Replace it with a newer export to update the agent. |
| `model_provider.yaml` | The models it may run on. Keys are `${NAME}`s, filled in from `.env`. |
@@if ui@@
| `web/` | The UI (Vite, React, Tailwind). `src/App.tsx` is the page; `src/agent.tsx` the assistant; `src/agent-state.ts` the state your page sends the agent. |
@@end@@
@@if compose@@
| `compose.yaml` | What it keeps things in, for development. |
@@end@@
@@if wheels@@
| `vendor/` | The runtime's wheels, until it's on PyPI. |
@@end@@
| `AGENTS.md` | How the project works, for AI coding agents (and people). |
| `.github/CODEOWNERS` | Who reviews changes. |

## The API

The server speaks ADK's run API under `/api`, so any app can talk to the agent:

```sh
curl -X POST localhost:8000/api/apps/@@AGENT_ID@@/users/u1/sessions -H 'content-type: application/json'@@CURL_AUTH@@ -d '{}'
curl -N -X POST localhost:8000/api/run_sse -H 'content-type: application/json'@@CURL_AUTH@@ -d '{
  "appName": "@@AGENT_ID@@", "userId": "u1", "sessionId": "<id>",
  "newMessage": {"role": "user", "parts": [{"text": "Hello"}]}, "streaming": true
}'
```

`GET /api/agent` says which agent it runs and the state it takes.
@@if a2a@@

### A2A

Other agents call it over [Google's A2A protocol](https://a2a-protocol.org): its card is at
`/.well-known/agent-card.json`, and JSON-RPC at `/a2a` takes A2A 1.0 (`SendMessage`,
`SendStreamingMessage`, `GetTask`, `CancelTask`, …) and 0.3 (`message/send`, `message/stream`,
`tasks/get`, …). Each A2A conversation (`contextId`) is a conversation of the run API's too.

```sh
curl localhost:8000/.well-known/agent-card.json
curl -X POST localhost:8000/a2a -H 'content-type: application/json' -H 'A2A-Version: 1.0'@@CURL_AUTH@@ -d '{
  "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
  "params": {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "Hello"}]}}
}'
```

The state the agent takes goes in the message's `metadata.state`. Set `FORGE_AGENT_PUBLIC_URL`
to the address callers reach it at when that isn't the one requests come to (behind a proxy).
@@end@@
@@NOTES@@
