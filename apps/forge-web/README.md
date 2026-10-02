# Forge web console

The Forge web console: builders for Google ADK workflows and Google ADK chat
agents, and the workspace around them.
React 19 on Vite, TanStack Router (file-based routes) and TanStack Query,
shadcn/ui primitives on Base UI, Tailwind CSS 4, React Flow for the builder's
canvas and the Forge UI composites. It is the `@forge/web` package, with its
own lockfile and `node_modules` in this directory.

What it has:

- **Organization workspaces** (`/organizations/$organizationId`): the
  organization's ADK workflows and their runs (ADK workflows, the default),
  its chat agents (Agents), and who holds which role in it (Members). Only an
  organization's members open its workspace; the switcher at the top of the
  sidebar picks one.
- **The ADK workflow builder**
  (`/organizations/$organizationId/agents/$agentId`): a Google ADK graph on a
  canvas (LLM, sequential, parallel and loop agents, saved workflows, human
  input, and Forge's steps: Approval, HTTP request, Transform, Delay, If /
  else, Switch, Match, Loop, Merge, End), each node's settings with JSONata
  expressions checked as you type, autosave with revisions, runs and what
  they wait for.
- **The chat agent builder**
  (`/organizations/$organizationId/chat-agents/$chatAgentId`): a Google ADK
  agent with its sub-agents and tools. Kept in the browser until the admin
  API stores agents.
- **Site administration** (`/admin`, site administrators only):
  organizations and their members, users, roles and permissions.
- **The assistant**: the admin API's Forge agent, in a panel or its own
  window, aware of the page you're on.

The ADK workflow format (`src/features/adk-workflows/lib/`) is the source of truth for the
admin API's copy: `npm run generate:agent-schema` writes its JSON Schema into
`apps/forge-admin-api`, and `npm run check:agent-schema` fails when it's out
of date.

## Develop

From the repository root:

```sh
make web-install    # npm ci in apps/forge-web
make web            # http://localhost:5190
make web-check      # typecheck, lint and the agent schema check
make web-test       # the step, agent and timestamp tests
make web-build      # production build into apps/forge-web/dist
```

Or run `npm` directly in this directory (`npm run dev`, `npm install <pkg>`).

Copy `.env.example` to `.env.local` in this directory:

- `VITE_API_URL` is the backend base URL used by the API client in
  `src/lib/api-instance.ts`. Vite inlines it into the bundle, so it must not
  hold credentials.
- `FORGE_UI_TOKEN` authenticates the private `@forge-ui` shadcn registry
  declared in `components.json`, a GitHub token that can read this repo.
  Only the shadcn CLI reads it.

Add components from this directory, where `components.json` lives:

```sh
npx shadcn@latest add @forge-ui/<item>   # Forge UI registry
npx shadcn@latest add <component>        # shadcn registry
```

The `@forge-ui` registry is [`packages/forge-ui`](../../packages/forge-ui/README.md),
served from its files committed on `main`. To try a change to an item before
it's pushed, rebuild it there (`npm run registry:build`) and install the item
from disk; its `@forge-ui/…` dependencies still come from `main`:

```sh
npx shadcn@latest add ../../packages/forge-ui/public/r/<item>.json
```

## Structure

Everything a feature owns sits in `src/features/<feature>/`, split into
`components/` (React) and `lib/` (types, data hooks and logic). Folders are
named as the UI names them.

```
src/
  routes/            file-based routes (URLs); thin pages that compose features
  app/               the app's shell: layout, sub nav, scope switcher, footer,
                     route states, and settings/ (the settings dialog)
  features/
    adk-workflows/   ADK workflows: the list, the builder's nodes and fields,
                     and the workflow format (lib/schema.ts, model, validate)
    agents/          Agents (chat agents): the list, the builder and the format
    runs/            ADK workflow runs: the run page, lists, the run dialog,
                     approvals and input (pause panels); lib/runs.ts holds the
                     API types and hooks, lib/display.ts how a run reads
    builder/         the builder kit both builders share: canvas, library, step
                     dialog, fields, layout and routing
    steps/           Forge's steps as ADK workflows take them: kinds, settings,
                     JSONata expressions, scopes and checks
    json/            viewing and building JSON and JSON Schema
    assistant/       the Forge assistant: its panel and window, launcher,
                     models and what it knows about the page (page context)
    organizations/   an organization's workspace views and sub nav
    admin/           site administration: organizations, users, roles
  components/        the Forge UI design system (from @forge-ui; don't edit
                     here): ui/ primitives, forge/ composites, assistant-ui/
  lib/               app-wide helpers: access, hierarchy, users, format and
                     api-instance (the API's base URL); from the registry:
                     api/ (client, query client, resource hooks),
                     context-store, timestamps, user-* providers, utils
  hooks/             registry hooks
```

- The router plugin regenerates `src/routeTree.gen.ts` while `vite` runs;
  commit it and never edit it by hand.
- Import across features with the `@/` alias
  (`@/features/runs/lib/runs`); within a feature, `./` is fine.
- `components/`, `hooks/` and the registry's files in `lib/` come from the
  `@forge-ui` registry (`packages/forge-ui`); change them there.
- Tests are in `tests/` (`node --test`, loading `src` through Vite).
- The root `.claude/skills/` documents these conventions for AI coding assistants:
  `forge-ui` (screens and components), `forge-data` (server data) and
  `forge-state` (shared client state); their `src/` paths are relative to this
  app. `skills-lock.json` pins their source, `packages/forge-ui`.

## Container

`Dockerfile` builds the static bundle with the repository root as context and
serves it from unprivileged nginx on port 8080. `nginx.conf` falls back to
`index.html` for client-side routes and caches hashed assets for a year.

```sh
make start      # from the repository root; http://localhost:18190
```

The root `.env.compose` sets `WEB_PORT` and the `VITE_API_URL` build argument.
