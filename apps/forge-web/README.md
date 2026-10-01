# Forge web console

The Forge web console: the visual workflow builder and the workspace around it.
React 19 on Vite, TanStack Router (file-based routes) and TanStack Query,
shadcn/ui primitives on Base UI, Tailwind CSS 4, React Flow for the builder's
canvas and the Forge UI composites. It is the `@forge/web` package, with its
own lockfile and `node_modules` in this directory.

What it has:

- **Organization workspaces** (`/organizations/$organizationId`): the
  organization's workflows and their runs (Workflows), its inbound events
  endpoint and event types (Events), and who holds which role in it
  (Members). Only an organization's members open its workspace; the switcher
  at the top of the sidebar picks one.
- **The workflow builder**
  (`/organizations/$organizationId/workflows/$workflowId`): steps on a canvas
  (Start, Agent, Approval, HTTP request, Transform, Delay, Run workflow,
  If / else, Switch, Match, Loop, Merge, End), each step's settings with
  JSONata expressions checked as you type, autosave with revisions, runs and
  their approvals.
- **Site administration** (`/admin`, site administrators only):
  organizations and their members, users, roles and permissions.
- **The assistant**: the admin API's Forge agent, in a panel or its own
  window, aware of the page you're on.

The workflow format (`src/lib/workflows/`) is the source of truth for the
admin API's copy: `npm run generate:workflow-schema` writes the JSON Schema
and step catalog into `apps/forge-admin-api`, and `npm run
check:workflow-schema` fails when they're out of date.

## Develop

From the repository root:

```sh
make web-install    # npm ci in apps/forge-web
make web            # http://localhost:5190
make web-check      # typecheck, lint and the workflow schema check
make web-test       # the workflow and timestamp tests
make web-build      # production build into apps/forge-web/dist
```

Or run `npm` directly in this directory (`npm run dev`, `npm install <pkg>`).

Copy `.env.example` to `.env.local` in this directory:

- `VITE_API_URL` is the backend base URL used by the API client in
  `src/lib/api-instance.ts`. Vite inlines it into the bundle, so it must not
  hold credentials.
- `FORGE_UI_TOKEN` authenticates the private `@forge-ui` shadcn registry
  declared in `components.json`. Only the shadcn CLI reads it.

Add components from this directory, where `components.json` lives:

```sh
npx shadcn@latest add @forge-ui/<item>   # Forge UI registry
npx shadcn@latest add <component>        # shadcn registry
```

## Structure

- `src/routes/` holds file-based routes. The router plugin regenerates
  `src/routeTree.gen.ts` while `vite` runs; commit it and never edit it by hand.
- `src/components/workflows/` is the builder; `src/lib/workflows/` the
  workflow format, its expressions, layout and validation.
- `src/components/forge/` holds Forge composites (shell, sidebar, toolbar,
  data table, sheets, the assistant); `src/components/ui/` the tuned shadcn
  primitives.
- `src/lib/api/` is the API client, TanStack Query client and resource hooks;
  `src/lib/context-store.tsx` the per-Provider Zustand stores.
- The root `.claude/skills/` documents these conventions for AI coding assistants:
  `forge-ui` (screens and components), `forge-data` (server data) and
  `forge-state` (shared client state); their `src/` paths are relative to this
  app. `skills-lock.json` pins their source.

## Container

`Dockerfile` builds the static bundle with the repository root as context and
serves it from unprivileged nginx on port 8080. `nginx.conf` falls back to
`index.html` for client-side routes and caches hashed assets for a year.

```sh
make start      # from the repository root; http://localhost:18190
```

The root `.env.compose` sets `WEB_PORT` and the `VITE_API_URL` build argument.
