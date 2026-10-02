# Forge UI

**Forge UI** is the design language of a coding-agent workspace app
(`node-back-pi-coding-agent/apps/web`), extracted into a reusable
shadcn/ui component library: a quiet, light product interface with pale
cool-neutral surfaces, compact 12–14px type, small 4–9px radii, full-width
pastel status bands and one dark primary action.

- **Stack:** React 19 · Tailwind CSS v4 · shadcn/ui (Base UI, `base-rhea`) ·
  Hugeicons · Geist
- **Demo:** every token, primitive and composite, plus a fully interactive
  rebuild of the Forge Workspace built only from this library
- **Distribution:** a shadcn registry (`@forge-ui/*`) that other projects install
  from

## Run the demo

```bash
npm install
npm run dev
```

Open http://localhost:5173 (or the port Vite prints). Useful routes:

| Route                  | What it shows                                               |
| ---------------------- | ----------------------------------------------------------- |
| `#overview`            | Principles, install steps, index of every section           |
| `#shell`               | The full workspace in a resizable frame (container queries) |
| `#workspace`           | The rebuilt Forge Workspace, full screen                    |
| `#assistant-app`       | The ADK assistant against a simulated agent, full screen    |
| `#assistant-modal-app` | The workspace with the assistant as a floating launcher     |
| `#dynamic-shell-app`   | A small app on the dynamic shell, full screen               |
| `#colors` …            | One page per foundation, primitive and composite            |

Press `d` to toggle dark mode, `/` or `⌘K` to find a component.

## Use it in another project

The library is a shadcn registry served straight from the private GitHub
monorepo it lives in,
[`jleva12/ei-tiger-workflow`](https://github.com/jleva12/ei-tiger-workflow),
under `packages/forge-ui`: the generated `registry.json` and
`public/r/*.json` are committed, and apps read them from
`raw.githubusercontent.com` with a GitHub token. There's no server to run.
Reading the registry needs read access to the whole monorepo.

### Start a new app in one command

With the GitHub CLI logged in (`gh auth login`) to an account that can read
the repo:

```bash
FORGE_UI_TOKEN=$(gh auth token) npx shadcn@latest init jleva12/ei-tiger-workflow/base#main --template vite --base base --name my-app
```

shadcn reads the `owner/repo/item` form from the repo's root
`registry.json`; the monorepo's root one includes this package's
`registry.json`, so its items and files resolve from `packages/forge-ui`.
This scaffolds a Vite + React app, writes its `components.json` with the
`@forge-ui` registry and its `${FORGE_UI_TOKEN}` header (the placeholder,
never the token), and installs everything: every primitive and component,
the libraries and the AI assistant. `src/index.css` is this repo's file
byte for byte (`@forge-ui/index-css`), so colours, type sizes, radii and
spacing match the demo exactly. The `base` item carries the preset
settings, so don't add `--preset` — it replaces them. It's for the Vite
template: it writes `src/index.css`, where Vite keeps its stylesheet.

Afterwards, put the token in the new app's `.env.local` (see step 1 below)
so later `shadcn add @forge-ui/…` commands can read the registry. To pick
up theme changes later, re-copy the stylesheet with
`npx shadcn@latest add @forge-ui/index-css --overwrite`; don't add
`@forge-ui/theme` or `@forge-ui/all` to such an app, because they merge the
theme into the stylesheet a second time.

To add Forge UI to an existing app instead, follow these steps:

1. **Get a token that can read the repo.** Either:
   - a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new)
     with _Repository access_ → _Only select repositories_ →
     `ei-tiger-workflow` and _Permissions_ → _Contents_ →
     _Read-only_, or
   - if you use the GitHub CLI, the token it already has:
     `FORGE_UI_TOKEN=$(gh auth token) npx shadcn@latest add …`

   GitHub only lets fine-grained tokens reach repos owned by the token's
   owner or an organization, so collaborators on a personal repo use the
   GitHub CLI token (or move the repo to an organization). Put the token in
   the consuming app's `.env.local`, which Vite apps already git-ignore:

   ```bash
   # .env.local
   FORGE_UI_TOKEN=github_pat_…
   ```

   The shadcn CLI loads `.env.local`, `.env.development.local`,
   `.env.development` and `.env`; an exported shell variable wins over
   them. Only `shadcn add` uses the token — the app never needs it at runtime.
   In CI, store it as a secret and pass it as `FORGE_UI_TOKEN` (don't reuse
   the Actions `GITHUB_TOKEN`, which can only read the repo it runs in).

2. **In the consuming project**, start from the same shadcn preset (Base UI,
   rhea style, Hugeicons, Geist) and register the namespace:

   ```bash
   npx shadcn@latest init --preset b27GdBA3 --base base
   ```

   ```json
   // components.json
   {
     "registries": {
       "@forge-ui": {
         "url": "https://raw.githubusercontent.com/jleva12/ei-tiger-workflow/main/packages/forge-ui/public/r/{name}.json",
         "headers": { "Authorization": "Bearer ${FORGE_UI_TOKEN}" }
       }
     }
   }
   ```

   Commit `components.json` as is: it holds the `${FORGE_UI_TOKEN}`
   placeholder, never the token. To pin a release, replace `main` with a tag
   such as `forge-ui-v0.1.0`.

3. **Install** the theme and everything, or only what you need:

   ```bash
   npx shadcn@latest add @forge-ui/all
   npx shadcn@latest add @forge-ui/theme @forge-ui/app-shell @forge-ui/task-list @forge-ui/kanban
   ```

   Dependencies resolve automatically: `@forge-ui/kanban` pulls in `@forge-ui/task-list`,
   `@forge-ui/status`, the tuned `@forge-ui/button`, `@forge-ui/tooltip`, … Tuned primitives land
   in `components/ui/`, Forge components in `components/forge/`, libraries in
   `lib/`. The libraries and the zustand-based pieces aren't in `@forge-ui/all`;
   add the ones you use:

   ```bash
   npx shadcn@latest add @forge-ui/api-client @forge-ui/query-client @forge-ui/resource \
     @forge-ui/user @forge-ui/preferences @forge-ui/shell @forge-ui/assistant
   ```

   In a brand-new app, pass `--overwrite` the first time so the tuned
   `button` replaces the one `init` created.

4. **Wire the providers** once, at the root:

   ```tsx
   <ThemeProvider>
     <QueryClientProvider client={createQueryClient()}>
       <Toaster>
         <UserProvider access={{ load: loadAccess }}>
           <ShellProvider
             initial={{ rail }}
             currentPath={pathname}
             navigate={navigate}
           >
             <ShellLayout brand={<Brand />}>
               <Routes />
             </ShellLayout>
           </ShellProvider>
         </UserProvider>
       </Toaster>
     </QueryClientProvider>
   </ThemeProvider>
   ```

5. **Take updates later.** The code is yours once installed; to pull a
   newer version of an item, preview it first and merge by hand where you
   changed it:

   ```bash
   npx shadcn@latest add @forge-ui/shell --diff
   ```

This flow was verified end to end: a freshly scaffolded Vite app that
installs `@forge-ui/all` plus every library and standalone item typechecks,
builds, and renders the dynamic shell, DataTable, preferences and access
guards with the same computed colours, type and geometry as the original
app.

### Publishing changes

Apps read whatever is committed, so a change ships when it's pushed:

1. Change the source, then rebuild the registry and commit it with the
   change (from `packages/forge-ui`):

   ```bash
   npm run registry:build
   git add . && git commit
   git push
   ```

   `npm run registry:check` rebuilds and fails if `registry.json` or
   `public/r` has uncommitted changes; the monorepo's `Forge UI registry`
   GitHub Actions workflow (`.github/workflows/forge-ui-registry.yml`) runs
   it, the typecheck and the tests on every push that touches
   `packages/forge-ui`, so a forgotten rebuild shows up as a failed check.

2. Apps that point at `main` see the change within about five minutes
   (raw.githubusercontent.com caches files that long).

3. To cut a version that apps can pin to, tag it. Tags are shared with the
   rest of the monorepo, so Forge UI's carry a `forge-ui-` prefix:

   ```bash
   git tag forge-ui-v0.1.0
   git push origin forge-ui-v0.1.0
   ```

Apps in the monorepo (`apps/forge-web`) can try a change before it's
pushed by installing the built item from disk, e.g.
`npx shadcn@latest add ../../packages/forge-ui/public/r/<item>.json`
after `npm run registry:build`. Only that item comes from disk: its
`@forge-ui/…` dependencies still resolve from the registry URL.

The files are plain JSON, so any static host works too: `npm run build`
copies `public/r` into `dist/r`.

### Registry items

| Item                                         | Contents                                                                                                                                         |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `@forge-ui/theme`                            | All tokens (light/dark), Geist, type scale, radii, elevation, orb utilities, focus ring, scrollbars, reduced motion                              |
| `@forge-ui/all`                              | The theme, the theme provider, every primitive and component                                                                                     |
| `@forge-ui/index-css`                        | The theme as this repo's exact `src/index.css`, replacing a Vite app's stylesheet                                                                |
| `@forge-ui/base`                             | A new Vite app in one `shadcn init`: registry and token header, the exact `index.css`, then everything                                           |
| `@forge-ui/icon`                             | `Icon` + the workspace icon vocabulary                                                                                                           |
| `@forge-ui/status`                           | `StatusBadge`, `StatusSymbol`, `ConnectionDot`, `CountBadge`, `Chip`, `RunStateIcon`                                                             |
| `@forge-ui/avatars`                          | `WorkspaceOrb`, `AgentOrb`, `AgentAvatars`                                                                                                       |
| `@forge-ui/app-shell`                        | `AppShell`, `MainPanel`, `Topbar*`, `ViewToolbar`, `ToolbarFilters`, `PageContent`, `WorkspaceFooter`, `SkipLink`                                |
| `@forge-ui/icon-rail`                        | `IconRail`, `AppMark`, `RailNav`, `RailFooter`, `RailButton`                                                                                     |
| `@forge-ui/workspace-sidebar`                | `WorkspaceSidebar`, `SidebarBrand*`, `CommandButton`, `NavItem`, `ProjectItem`, `SidebarStatus`, …                                               |
| `@forge-ui/toolbar`                          | `ViewTabsList/Trigger`, `LayoutSwitch`, `SearchField`, `ToolbarButton`                                                                           |
| `@forge-ui/task-list`                        | `TaskList`, `TaskGroup`, `TaskTable` (+ parts), `TaskListSkeleton`                                                                               |
| `@forge-ui/kanban`                           | `TaskBoard`, `KanbanBoard`, `KanbanColumn`, `BoardCard`                                                                                          |
| `@forge-ui/illustration`                     | `Illustration` — CSS paper drawings for empty, waiting, error, search, offline and other states                                                  |
| `@forge-ui/empty-state`                      | `EmptyWorkspace`, `EmptyIllustration`, `WorkflowSteps`, `PanelEmpty`, `PageEmpty`                                                                |
| `@forge-ui/feedback`                         | `ErrorCallout`, `PreviewBanner`, `ActiveFilters`, `LoadMore`                                                                                     |
| `@forge-ui/settings-list`                    | `SettingsList`, `SettingsRow` — divided settings rows with a current value and one action                                                        |
| `@forge-ui/integrations`                     | `IntegrationRow`, `IntegrationLogo` and brand logos — OAuth connect rows for GitHub, Jira, Figma, …                                              |
| `@forge-ui/activity`                         | `EventList/Item`, `StatGrid/Stat`, `ViewHeading`, `StatusHeading`, `Footnotes`                                                                   |
| `@forge-ui/task-sheet`                       | `TaskSheetContent/Header`, `TaskForm`, `FormColumns`, `FormFooter`, `DetailList`, `PlanEntry`                                                    |
| `@forge-ui/task-page`                        | Detail header, line tabs, `SubmissionLayout`, `ExecutionPlan`, `WaitingNotice`, `Disclosure`, `RecordList`                                       |
| `@forge-ui/run-log`                          | Job list and the always-dark `LogConsole` family                                                                                                 |
| `@forge-ui/changes`                          | `DiffWorkspace`, `ChangedFile`, `FileDiff`, `DiffView`, `DiffTotals`                                                                             |
| `@forge-ui/handoff`                          | `VerificationRound/Commands/Status`, `HandoffIteration/Agent/Result`, `Finding`                                                                  |
| `@forge-ui/metrics-table`                    | `MetricsTable`, `MetricsRow`, `MetricValue`, `MetricsSummary`                                                                                    |
| `@forge-ui/data-table`                       | `DataTable` + `createColumnHelper` — the spreadsheet-grade TanStack Table v9 grid                                                                |
| `@forge-ui/api-client`                       | `createApiClient`, `ApiError` — the axios wrapper (not in `@forge-ui/all`)                                                                       |
| `@forge-ui/query-client`                     | `createQueryClient` — TanStack Query wired to `@forge-ui/api-client`                                                                             |
| `@forge-ui/resource`                         | `createResource` — typed CRUD hooks for a REST collection                                                                                        |
| `@forge-ui/context-store`                    | `createContextStore` — a Zustand store per Provider, seeded from props or context                                                                |
| `@forge-ui/user-preferences`                 | `UserPreferencesProvider` + hooks — density, text size, contrast, motion and your own preferences                                                |
| `@forge-ui/preferences`                      | `PreferencesPanel` — the display settings form (theme, density, text size, contrast, motion)                                                     |
| `@forge-ui/user-access`                      | `UserAccessProvider`, `Guard`, `useCan` / `useHasRole` / `useGuard` — roles and permissions from a Casbin backend                                |
| `@forge-ui/user`                             | `UserProvider` — preferences plus roles and permissions in one provider                                                                          |
| `@forge-ui/shell`                            | `ShellProvider`, `ShellLayout`, `useShellPage` — the app shell's rail, sub nav and header driven by pages (not in `@forge-ui/all`)               |
| `@forge-ui/assistant`                        | `AssistantScreen`, `AssistantModal`, `useAdkAssistant` — the Google ADK assistant, full screen or floating (not in `@forge-ui/all`)              |
| `@forge-ui/assistant-thread`                 | assistant-ui's thread, composer, conversation list, floating modal and `/` or `@` trigger popover, restyled (installed by `@forge-ui/assistant`) |
| `@forge-ui/agent-workspace`                  | Progress dock, plan / notes / canvas panels, stats, history palette, tools menu, tool cards (not in `@forge-ui/all`)                             |
| `@forge-ui/rich-text-editor`                 | `RichTextEditor`, `RichTextView` — Tiptap rich text in the workspace look (not in `@forge-ui/all`)                                               |
| `@forge-ui/attachments`                      | `ChatAttachmentAdapter` — images, PDFs and text files on messages                                                                                |
| `@forge-ui/timestamps`                       | `parseTimestamp`, `localDay` — API times as UTC, bare dates as local days                                                                        |
| `@forge-ui/use-now`, `…/use-debounced-value` | A shared one-second clock; a value that settles after it stops changing                                                                          |
| `@forge-ui/button`, `@forge-ui/tabs`, …      | The 34 shadcn primitives, tuned to the workspace (incl. `@forge-ui/command`, the cmdk palette, `chart`, `switch`)                                |

## Project layout

```
src/
  index.css                 the theme — single source of truth for tokens
  components/ui/            shadcn primitives (tuned: button, badge, input,
                            textarea, native-select, input-group, toggle,
                            tabs, dropdown-menu, alert, kbd, toast, dialog,
                            sheet, select, checkbox, command)
  components/forge/            the Forge component layer (assistant/ is the ADK
                            assistant)
  components/assistant-ui/  assistant-ui's thread elements (@forge-ui/assistant-thread)
  hooks/                    hooks those elements use
  lib/api/                  the axios API client and its TanStack Query
                            client and CRUD hooks (@forge-ui/api-client,
                            @forge-ui/query-client, @forge-ui/resource)
  lib/context-store.tsx     createContextStore (@forge-ui/context-store)
  lib/user-preferences*     UserPreferencesProvider and hooks (@forge-ui/user-preferences)
  lib/user-access*          UserAccessProvider, Guard and hooks (@forge-ui/user-access)
  lib/user-provider.tsx     UserProvider (@forge-ui/user)
  demo/                     the showcase app (not part of the registry)
scripts/build-registry.mjs  generates registry.json from index.css + files
registry.json               generated — do not edit by hand
public/r/                   built registry served to consumers
```

## Illustrations

`@forge-ui/illustration` grows the workspace's empty-state mark — two tilted sheets
of paper with a checklist — into one drawing per state. Everything is CSS on
theme tokens (no images), so each drawing follows light and dark. The sheets
never change; what's written on the front sheet and one accent (a spark glyph
top right or a round status badge bottom right) tell the states apart.

| Name           | For                                           |
| -------------- | --------------------------------------------- |
| `tasks`        | First run, nothing created yet (the original) |
| `complete`     | All caught up                                 |
| `waiting`      | Queued, no run recorded yet                   |
| `error`        | A run or a load failed                        |
| `search`       | No results for a search or filter             |
| `offline`      | The API or a worker is unreachable            |
| `locked`       | No access                                     |
| `activity`     | An empty timeline                             |
| `logs`         | No log output yet                             |
| `changes`      | No file changes                               |
| `conversation` | An empty assistant thread                     |

```tsx
import { Illustration } from "@/components/forge/illustration"

<Illustration name="search" />
<Illustration name="offline" size="sm" />          // ~¾ size for panels and table cells
<Illustration name="logs" surface="console" />     // recoloured for the dark log console
```

The empty states take a name directly:

```tsx
<EmptyWorkspace illustration={<EmptyIllustration name="search" />} title="No tasks match your filters" />
<PanelEmpty illustration="activity">Your task activity will appear here.</PanelEmpty>
<PageEmpty illustration="waiting" title="No run has been recorded yet" />
<LogEmpty illustration="logs" title="Waiting for output" />
```

`DataTable`'s default no-results row uses the small `search` drawing. The
drawings are decorative (`aria-hidden`); always pair one with a title or
text that says the same thing.

## Integrations

`@forge-ui/integrations` is a settings-page row for connecting third-party apps
over OAuth, built on the generic `@forge-ui/settings-list` rows: the app's logo in
a small app tile, what the connection is for, who it's connected as, and one
outline button for the next step.

| `status`       | Shows                                                                                                      |
| -------------- | ---------------------------------------------------------------------------------------------------------- |
| `disconnected` | "Not connected" and **Connect ↗** (it leaves for the provider)                                             |
| `connecting`   | "Waiting for GitHub…" and a disabled spinner button                                                        |
| `connected`    | A green dot with the `account`, `detail`, and a **Manage** menu (your `menu` items, Reconnect, Disconnect) |
| `error`        | "Needs attention" with the reason in `detail`, and **Reconnect**                                           |

```tsx
// From @/components/forge/integrations, settings-list and brand-logos
<SettingsList>
  <IntegrationRow
    logo={<GitHubLogo />}
    name="GitHub"
    description="Push task branches and open pull requests."
    status={github.status}
    account={github.login}
    detail="Connected Sep 8"
    onConnect={() => window.location.assign("/api/oauth/github/start")}
    onDisconnect={() => disconnect.mutate("github")}
  />
</SettingsList>
```

The row only renders state: your app runs the OAuth flow (redirect or
popup to the provider, then a callback that stores the token) and passes the
result back as `status`. `brand-logos` ships `GitHubLogo`, `GitLabLogo`,
`JiraLogo`, `LinearLogo`, `FigmaLogo`, `SlackLogo` and `GoogleDriveLogo` as
inline SVG in their official colours (GitHub follows the text colour for
dark mode); any 20px SVG works as a `logo`. The marks are their owners'
trademarks — use them only to name the product you connect to.

`SettingsRow` on its own covers any other settings list — security keys, a
phone number, notification channels:

```tsx
<SettingsRow
  title="SMS number"
  description="A one-time code is sent to your registered mobile number."
  meta={<span className="font-medium text-foreground">+4 0123 456 789</span>}
  action={
    <Button variant="outline" size="sm">
      Edit
    </Button>
  }
/>
```

## Data table

`@forge-ui/data-table` is a spreadsheet-grade grid on TanStack Table v9 in the
workspace style: 45px rows (34px compact), 11px muted headers with a
hover-only column menu, hairline dividers, quiet hover, and Forge controls
throughout. It keeps the original codegraph API, so existing call sites only
need the new import path, and adds most of what teams reach for AG Grid for.

```tsx
import { DataTable, createColumnHelper } from "@/components/forge/data-table"

const helper = createColumnHelper<Job>()
const columns = helper.columns([
  helper.accessor("id", { header: "ID" }),
  helper.group({
    id: "work",
    header: "Work",
    columns: helper.columns([
      helper.accessor("name", {
        header: "Name",
        meta: { editable: true, validate: (v) => (v ? undefined : "Required") },
      }),
      helper.accessor("status", {
        header: "Status",
        cell: ({ getValue }) => <StatusBadge status={getValue()} />,
        meta: { filterVariant: "multiSelect", filterOptions: statusOptions },
      }),
    ]),
  }),
  helper.accessor("tokens", {
    header: "Tokens",
    meta: { format: "integer", summary: "sum", editable: true },
  }),
  helper.accessor("due", { header: "Due", meta: { format: "date" } }),
])

<DataTable
  columns={columns}
  data={jobs}
  getRowId={(job) => job.id}
  stateKey="jobs"
  features={{ cellSelection: true, virtualization: true, footers: true }}
  onCellEdit={({ row, columnId, value }) =>
    setJobs((jobs) =>
      jobs.map((job) => (job.id === row.id ? { ...job, [columnId]: value } : job))
    )
  }
/>
```

### What it covers

| Area       | Details                                                                                                                                                                                                                                                                                                             |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Filtering  | Quick search; typed column filters in a header row or a toolbar panel: text, number range, date range, select and multi-select (options faceted from the data, with counts), boolean. The control is inferred from the data when `meta.filterVariant` is omitted. Tree data keeps the parents of matching children. |
| Sorting    | Click to sort, shift-click to add columns (order shown in the header), sort from the column menu.                                                                                                                                                                                                                   |
| Columns    | Column groups (multi-row headers), show/hide, drag headers to reorder, pin left or right, drag or double-click to resize, column menu. Numbers right-align automatically.                                                                                                                                           |
| Grouping   | Group from the column menu, the View menu or by dragging a header onto the grouping bar; aggregates with `aggregationFn`/`aggregatedCell`.                                                                                                                                                                          |
| Rows       | Selection (checkboxes, select all), pinning to top or bottom, expandable detail rows (`renderSubComponent`), tree data (`getSubRows`), drag to reorder (`onRowReorder`), row numbers.                                                                                                                               |
| Cells      | Range selection with the mouse (drag, shift-click, ⌘/Ctrl-click to add or subtract) and keyboard; copy as TSV; paste from a spreadsheet into editable cells (one value fills a selection); Delete clears; cell spanning with `spanRows`/`spanColumns`.                                                              |
| Editing    | `meta.editable` (boolean or per row) with text, number, date, select and checkbox editors, `meta.validate`, and type-preserving commits through `onCellEdit`. Double-click, Enter, F2 or typing starts editing; Enter commits and moves down, Tab moves right, Escape cancels.                                      |
| Formatting | `meta.format`: `number`, `integer`, `compact`, `currency`, `percent`, `date`, `datetime`, `time` (Intl, tuned with `meta.formatOptions`) or a function. Footers from `meta.summary`: `sum`, `mean`, `median`, `min`, `max`, `count`.                                                                                |
| Scale      | Row virtualization for thousands of rows; server-side mode through `tableOptions` (`manualPagination`, `manualSorting`, `manualFiltering`, `rowCount`, controlled `state`), with a loading overlay that keeps the current rows.                                                                                     |
| Output     | CSV export (all rows or selected, raw values and ISO dates), copy rows, a context menu with your own items (`contextMenuItems`).                                                                                                                                                                                    |
| Status     | Row and selection counts, "Showing x–y of n", and Count / Sum / Avg / Min / Max of the selected cells.                                                                                                                                                                                                              |
| Layout     | Density and filter row from the View menu; `stateKey` saves widths, order, visibility, pinning, sorting and density in localStorage.                                                                                                                                                                                |

### Keyboard (with `cellSelection`)

| Keys                                      | Action                                     |
| ----------------------------------------- | ------------------------------------------ |
| Arrows / Shift+Arrows                     | Move / extend the selection                |
| ⌘/Ctrl+Arrows, Home, End, ⌘/Ctrl+Home/End | Jump to an edge (add Shift to extend)      |
| Page Up / Page Down                       | Move by a screen                           |
| Enter, F2, or typing                      | Edit the cell (Enter also expands a group) |
| Space                                     | Toggle row selection                       |
| ⌘/Ctrl+A, ⌘/Ctrl+C, ⌘/Ctrl+V              | Select all, copy, paste                    |
| Delete / Backspace                        | Clear editable selected cells              |
| Escape                                    | Collapse the range to the focused cell     |

### Feature flags

`features` turns parts on or off. On by default: `toolbar`, `globalFilter`,
`columnFilters`, `columnVisibility`, `columnOrdering`, `columnDragging`,
`columnPinning`, `columnSizing`, `sorting`, `multiSort`, `grouping`,
`expanding`, `pagination`, `rowSelection`, `rowPinning`, `faceting`,
`cellSpanning`, `editing`, `export`, `contextMenu`, `statusBar`,
`viewOptions`. Off by default: `footers`, `cellSelection`, `virtualization`
(replaces pagination; defaults the scroll area to `max-h-[600px]`),
`rowNumbers`, `rowReordering`.

Editing needs `onCellEdit`, and row reordering needs `onRowReorder`: the grid
never mutates your data, it reports the change. Pass `getRowId` so selection
and focus survive edits. Anything else TanStack accepts goes through
`tableOptions`, including controlled `state` and `on*Change` callbacks.

### Not included

Pivot mode, integrated charts, Excel (.xlsx) export, a fill handle, undo/redo
and an infinite (scroll-to-load) row model are AG Grid Enterprise features
that TanStack Table does not provide; build them on `tableOptions` if you need
them.

## API client

`@forge-ui/api-client` wraps axios with the plumbing every app rewrites: the access
token on each request, one shared token refresh when requests start failing
with 401, retries with backoff, and a single `ApiError` type for every
failure. It has no React or UI dependencies, so it isn't part of `@forge-ui/all`.

```bash
npx shadcn@latest add @forge-ui/api-client
```

```ts
// lib/api-instance.ts
import { createApiClient } from "@/lib/api"
import { toast } from "@/components/ui/toast"

export const api = createApiClient({
  baseURL: import.meta.env.VITE_API_URL,
  auth: {
    getAccessToken: () => session.accessToken,
    // Runs once for a burst of 401s; each failed request is then retried.
    refresh: async () => {
      const { accessToken } = await api.post<{ accessToken: string }>(
        "/auth/refresh",
        null,
        { skipAuth: true } // required when refreshing through this client
      )
      session.accessToken = accessToken
      return accessToken
    },
    onUnauthorized: () => session.signOut(),
  },
  onError: (error) =>
    toast.add({
      title: "Request failed",
      description: error.message,
      type: "error",
    }),
})

const user = await api.get<User>("/me")
await api.post("/tasks", { title }, { silent: true }) // handle this error yourself
```

| Concern     | Behaviour                                                                                                                                                                                                                   | Per request              |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| Auth        | `Authorization: Bearer <token>` from `auth.getAccessToken` (omit it for cookie sessions)                                                                                                                                    | `skipAuth: true`         |
| 401         | One shared `auth.refresh`, then one retry; `auth.onUnauthorized` when that fails                                                                                                                                            | `skipAuth: true`         |
| Retries     | GET/HEAD/OPTIONS/PUT/DELETE on network errors, timeouts, 408, 429 and 5xx: 2 retries, exponential backoff with jitter, `Retry-After` honoured                                                                               | `retry: false` / `{ … }` |
| Errors      | Every rejection is an `ApiError` with `kind`, `status`, `data`, `message`, `retryAfter`, `attempts` and `cause`; `message` is read from common body shapes (NestJS, FastAPI, RFC 9457, GraphQL, OAuth) or `getErrorMessage` | —                        |
| Global hook | `onError` once per failed request, skipping cancellations and 401s sent to `onUnauthorized`                                                                                                                                 | `silent: true`           |
| Timeout     | 30 s                                                                                                                                                                                                                        | `timeout`                |

Any other option goes straight to `axios.create` (`baseURL`, `headers`,
`withCredentials`, `paramsSerializer`, …). Cancel with `signal` from an
`AbortController`; `api.instance` is the underlying axios instance for full
responses or extra interceptors.

### With TanStack Query

`@forge-ui/query-client` adds `createQueryClient`, which hands retries and error
notices to TanStack Query (installing it pulls in `@forge-ui/api-client`):

```bash
npx shadcn@latest add @forge-ui/query-client
```

```ts
import { createApiClient } from "@/lib/api"
import { createQueryClient } from "@/lib/api/query-client"

export const api = createApiClient({
  baseURL: import.meta.env.VITE_API_URL,
  retry: false, // Query retries; no `onError` either, it would fire per attempt
  auth: {
    getAccessToken, // plus refresh, as above
    onUnauthorized: () => {
      session.signOut()
      queryClient.clear() // drop the signed-out user's cached data
    },
  },
})

export const queryClient = createQueryClient({
  onError: ({ title, error }) =>
    toast.add({ title, description: error.message, type: "error" }),
})

// Pass `signal` so unmounting or a key change cancels the request.
useQuery({
  queryKey: ["tasks"],
  queryFn: ({ signal }) => api.get<Task[]>("/tasks", { signal }),
})
useMutation({
  mutationFn: (input: NewTask) => api.post<Task>("/tasks", input),
  meta: { errorTitle: "Couldn't create the task" },
})
```

- **Typed errors.** `error` is an `ApiError` in every hook and callback
  (`error.status`, `error.data` for field errors). Wrap anything else a query
  function can throw, such as schema validation, in `toApiError`.
- **Retries.** Queries retry twice, only on network errors, timeouts, 408,
  429 and 5xx, waiting for `Retry-After` when the server sends one (a longer
  wait than 30 s fails instead). 4xx and 401 never retry; mutations never
  retry. Query pauses retries while offline or in a background tab, and
  `failureCount` can drive a "Retrying…" state. If the API client retried a
  request itself, Query doesn't retry it again.
- **Error notices.** `onError` runs once per failed mutation and per failed
  background refetch. A query's first-load error is left to the component's
  error state. Cancellations and 401s (the session's job) are skipped, and
  `meta: { silent: true }` skips a query or mutation that shows its own error.
- **Options.** `retries` and `maxRetryDelay` tune the policy; anything else
  goes to `new QueryClient` (`defaultOptions`, …), and a query's own `retry`
  still wins.

### CRUD resources

`@forge-ui/resource` turns a REST collection into typed hooks, so simple CRUD needs
no per-entity query code (installing it pulls in the two items above):

```bash
npx shadcn@latest add @forge-ui/resource
```

```ts
// lib/resources.ts: one line per collection
export const tasks = createResource<Task>({
  api,
  path: "/tasks",
  label: "task",
})
export const projects = createResource<
  Project,
  { create: NewProject; params: ProjectFilters; list: Page<Project> }
>({
  api,
  path: "/projects",
  label: "project",
  updateMethod: "put",
  // Required when the list isn't a plain array: where the entities live.
  mapItems: (page, map) => ({ ...page, items: map(page.items) }),
})

// In components
const { data: openTasks } = tasks.useList({ status: "open" })
const { data: task } = tasks.useDetail(taskId) // waits while taskId is undefined
const create = tasks.useCreate({
  onSuccess: (task) => navigate(`/tasks/${task.id}`),
})
const update = tasks.useUpdate()
update.mutate({ id: task.id, data: { done: true } })
tasks.useDelete().mutate(task.id)
```

| Hook / helper             | Request                          | Cache after success                                                      |
| ------------------------- | -------------------------------- | ------------------------------------------------------------------------ |
| `useList(params)`         | `GET /tasks?…`                   | —                                                                        |
| `useInfiniteList(params)` | `GET /tasks?…&cursor=…` per page | —                                                                        |
| `useDetail(id)`           | `GET /tasks/:id`                 | —                                                                        |
| `useCreate()`             | `POST /tasks`                    | new entity cached as its detail; collection refetches                    |
| `useUpdate()`             | `PATCH /tasks/:id`               | response cached as the detail (refetched on a 204); collection refetches |
| `useDelete()`             | `DELETE /tasks/:id`              | detail dropped; collection refetches                                     |

- **Types.** Only the entity is required; it needs an `id` (string or
  number). Defaults: create body = entity without `id`, update body =
  partial create body, list = `Entity[]`, params = any record. Override any of
  them by name in the second type argument. A list that isn't a plain array
  (a page envelope) also needs `mapItems`, so optimistic edits can reach its
  entities.
- **Options.** Every hook takes the usual TanStack options last (`select`,
  `enabled`, `placeholderData: keepPreviousData`, `onSuccess`, `onMutate`, …).
  Your `onSuccess` runs alongside the cache updates, and a mutation stays
  pending until the collection has refetched, so closing a dialog in
  `onSuccess` never shows stale data. "Collection" is every list plus any
  query keyed under `tasks.keys.all`, except item details.
- **Error notices.** With `label`, failures read "Couldn't create the task",
  "Couldn't save the task" and "Couldn't delete the task"; `meta` overrides
  them.
- **Outside hooks.** `tasks.keys` (`all`, `lists()`, `list(params)`,
  `infiniteLists()`, `infiniteList(params)`, `details()`, `detail(id)`) for
  invalidation; `tasks.listOptions(params)`, `tasks.infiniteListOptions(params)`
  and `tasks.detailOptions(id)` for `useSuspenseQuery`, `prefetchQuery` and
  route loaders; and `tasks.requests` for plain calls.
- **Beyond CRUD.** Custom endpoints still use `api` and `useQuery` directly.
  Key them under the resource, e.g. `[...tasks.keys.all, "stats"]`, and its
  mutations refresh them too. Only active queries refetch; the rest go stale.

#### Nested routes

For collections under a parent (`/projects/:projectId/tasks`), use
`createNestedResource` with the parent's params as the second type argument.
`scope()` returns the same hooks for one parent; each parent is cached and
refreshed separately, and `projectTasks.keys.all` reaches every parent.

```ts
export const projectTasks = createNestedResource<Task, { projectId: string }>({
  api,
  key: "project-tasks", // required: shared root of every scope's keys
  path: ({ projectId }) => `/projects/${projectId}/tasks`,
  label: "task",
})

const tasks = projectTasks.scope({ projectId })
const { data } = tasks.useList() // GET /projects/:projectId/tasks
tasks.useUpdate().mutate({ id, data }) // PATCH /projects/:projectId/tasks/:id
```

#### Optimistic updates

Set `optimistic: true` on the resource, or per hook
(`useUpdate({ optimistic: true })`), to show a change before the server
confirms it. Updates and deletes edit every cached list (and the detail); a
failure rolls them back, resyncs from the server and sends the usual error
notice. Creates also need `placeholder`, which builds the item to show until
the real one arrives:

```ts
createResource<Task>({
  api,
  path: "/tasks",
  optimistic: true,
  placeholder: (body) => ({ ...body, id: `temp-${crypto.randomUUID()}` }),
  // Only if the update body isn't a partial entity:
  applyUpdate: (task, body) => ({ ...task, ...body }),
})
```

When several mutations run at once, lists refetch once the last one settles,
so an in-flight edit is never overwritten by stale data. The refetch also
corrects anything the guess got wrong: filters, sort order, totals.

#### Infinite lists

Describe how the collection pages once; `useInfiniteList` then loads page
after page. `param` is the query-string key the page value goes in:

```ts
const feed = createResource<
  Post,
  { list: { items: Post[]; nextCursor: string | null } }
>({
  api,
  path: "/posts",
  mapItems: (page, map) => ({ ...page, items: map(page.items) }),
  pagination: {
    param: "cursor",
    initialPageParam: null, // first request sends no cursor
    getNextPageParam: (lastPage) => lastPage.nextCursor, // null ends the list
  },
})
// Page numbers: { param: "page", initialPageParam: 1,
//   getNextPageParam: (last, all) => (last.hasMore ? all.length + 1 : undefined) }

const { data, fetchNextPage, hasNextPage, isFetchingNextPage } =
  feed.useInfiniteList(
    { tag: "news" },
    { select: (data) => data.pages.flatMap((page) => page.items) }
  )
```

Mutations and optimistic edits reach infinite lists too. For numbered pages
with Previous/Next buttons, use `useList({ page }, { placeholderData:
keepPreviousData })` instead.

## Context stores

Server data lives in TanStack Query; state that several components share —
selection, filters, view mode, drafts — goes in a context store.
`createContextStore` gives each Provider its own Zustand store, seeded from
the Provider's `initial` prop (props, another context, query data), and
typed hooks to read it with selectors.

```bash
npx shadcn@latest add @forge-ui/context-store
```

```tsx
import { createStore } from "zustand"
import { createContextStore } from "@/lib/context-store"

export const {
  Provider: BoardProvider,
  useStore: useBoardStore,
  useStoreApi: useBoardStoreApi,
} = createContextStore(
  (initial: { view: View; filters: string[] }) =>
    createStore<BoardState>()((set) => ({
      ...initial,
      selectedId: null,
      setView: (view) => set({ view }),
      select: (selectedId) => set({ selectedId }),
    })),
  { name: "Board" }
)

<BoardProvider initial={{ view: user.defaultView, filters: saved }}>
  <Board />
</BoardProvider>

const view = useBoardStore((s) => s.view) // re-renders only when view changes
useBoardStoreApi().getState().select(id)  // outside render, no subscription
```

- The context carries only the store, so it never re-renders consumers;
  selectors decide what re-renders. Object selectors need `useShallow`.
- `initial` seeds the store once. A new `key` on the Provider starts a fresh
  store; to keep an outside value in charge, `setState` it in an effect.
- Middleware (`persist`, `devtools`) goes inside `createStore`, and
  `useStoreApi()` keeps its API. A factory without a parameter makes
  `initial` optional; with one, it's required and type-checked.
- Hooks outside their Provider throw a clear error naming the store.

## User preferences

`UserPreferencesProvider` holds the signed-in user's display settings in a
context store and applies them to the whole app. `PreferencesPanel` is the
form that edits them; put it on a settings page, in a Sheet or a Popover.

| Preference  | Values                                 | Effect                                                               |
| ----------- | -------------------------------------- | -------------------------------------------------------------------- |
| `density`   | `comfortable` (default), `compact`     | Compact tightens every spacing step and list rows (45px → 34px)      |
| `fontScale` | 0.75–2 (the panel offers 90–150%)      | Multiplies every font size, and rem-based spacing with it            |
| `contrast`  | `system` (default), `standard`, `more` | More darkens secondary text and borders (lightens them in dark mode) |
| `motion`    | `system` (default), `reduce`           | Reduce turns animations and transitions off                          |

```bash
npx shadcn@latest add @forge-ui/user-preferences @forge-ui/preferences
```

```tsx
;<ThemeProvider>
  <UserPreferencesProvider
    defaults={{ density: "comfortable" }} // your app's defaults
    initial={user.preferences} // the user's saved settings
    onChange={(preferences) => api.put("/me/preferences", preferences)}
  >
    <App />
  </UserPreferencesProvider>
</ThemeProvider>

const density = useUserPreference("density") // re-renders only on change
const setPreference = useSetUserPreference()
setPreference("fontScale", 1.25)
```

- **Precedence:** library defaults, then `defaults`, then what the browser
  stored (`storageKey`, default `forge-user-preferences`; `false` keeps them in
  memory), then `initial`. `onChange` fires for the user's changes only, with
  the keys that changed. Stored values are validated on load, and other tabs
  follow changes.
- **How it applies:** the provider sets `data-forge-density`,
  `data-forge-contrast`, `data-forge-motion` and `--forge-font-scale` on `<html>`
  before paint, and the theme responds. Font sizes are rem-based so text
  scaling reaches everything (and respects the browser's own font size).
  DataTable and TaskList follow the density; a table's View menu can still
  pick a density for that table (remembered with its layout) or go back to
  Automatic, and an explicit `density` prop wins over the preference.
- **Theme** stays with `ThemeProvider`; the panel's theme row uses it.
- **Your own preferences** are typed by augmenting `CustomUserPreferences`;
  `defaults` then requires them, and `PreferenceRow` + `PreferenceOptions`
  add them to the panel:

```ts
declare module "@/lib/user-preferences" {
  interface CustomUserPreferences {
    defaultTaskView: "list" | "kanban"
  }
}
```

## Dynamic shell

`ShellProvider` holds what the app shell shows — the icon rail, the sidebar
sub nav and the header — and `ShellLayout` draws the Forge shell from it.
Pages change it while they're mounted: their own sub nav, breadcrumbs,
title, active items, and JSX in the header, toolbar, footer and sidebar.

```bash
npx shadcn@latest add @forge-ui/shell
```

```tsx
// App root: the rail is set once; routing is yours.
<ShellProvider
  initial={{
    rail: {
      items: [
        { id: "home", label: "Home", icon: "home", href: "/" },
        { id: "tasks", label: "Tasks", icon: "task", href: "/tasks" },
        { id: "agents", label: "Agents", icon: "robot", href: "/agents",
          permission: ["agents", "read"] },          // hidden without it
      ],
      footer: [{ id: "settings", label: "Settings", icon: "settings", href: "/settings" }],
    },
  }}
  currentPath={location.pathname}   // items whose href matches are active
  navigate={navigate}               // or renderLink={(href) => <Link to={href} />}
>
  <ShellLayout brand={<><WorkspaceOrb /><SidebarBrandName name="Forge" /><SidebarCollapseButton /></>}>
    <Routes />
  </ShellLayout>
</ShellProvider>

// A page: the shell shows this while it's mounted.
function TasksPage() {
  useShellPage({
    header: { breadcrumbs: [{ label: "Tasks", href: "/tasks" }], title: "Needs review" },
    sidebar: { label: "Tasks", sections: [{ id: "views", title: "Views", items: views }] },
  })
  return (
    <>
      <ShellHeaderActions><PrimaryAction onClick={create}>Task</PrimaryAction></ShellHeaderActions>
      <ShellToolbar><ViewToolbar>…</ViewToolbar></ShellToolbar>
      <TaskList … />
    </>
  )
}
```

- **Layers:** `useShellPage` (or `<ShellPage … />`) merges over the base,
  key by key; a page inside a layout overrides the layout, and the shell
  goes back when a page unmounts. Configs can be written inline: they're
  compared by their data, and handlers always call the latest version.
  `sidebar: { hidden: true }` gives a full-width page.
- **Anywhere, persisting across pages:** `useShellApi()` — `configure()`
  (e.g. the rail for the signed-in user), `updateItem(id, patch)` for live
  counts on base items, `setActive()`; read with `useShell(selector)`.
  Items a page defines follow that page's own state instead.
- **Slots** render page JSX, with the page's state and handlers, in the
  shell: `ShellHeaderActions`, `ShellToolbar`, `ShellFooter`,
  `ShellSidebarTop`, `ShellSidebarBottom`.
- **Access:** items with `permission` or `role` hide for users without
  them (`UserAccessProvider`); without an access provider nothing hides.
- **Small screens:** the rail's items lead the mobile menu on phones, and
  still back it when a page hides the sub nav.

## Roles and permissions (Casbin)

`UserAccessProvider` loads the signed-in user's roles and permissions from
your Casbin backend once, then answers access checks locally and
synchronously, so screens show, hide or disable things without waiting.
Casbin stays the authority and enforces every request on the server; these
checks only decide what the UI shows.

```bash
npx shadcn@latest add @forge-ui/user   # UserProvider + user-access + user-preferences
```

The backend returns Casbin's implicit roles and permissions for the user:

```python
@app.get("/me/access")  # FastAPI + pycasbin; Go/Node have the same calls
def my_access(user = Depends(current_user)):
    return {
        "roles": enforcer.get_implicit_roles_for_user(user.id),
        "policies": enforcer.get_implicit_permissions_for_user(user.id),
    }
```

```tsx
<UserProvider
  key={session.userId}   // signing in as someone else starts fresh
  access={{
    load: async ({ signal }) =>
      fromCasbin((await api.get("/me/access", { signal })).data),
    fallback: <AppLoading />,          // optional: hold the app until loaded
  }}
  preferences={{ initial: profile.preferences }}
>
  <App />
</UserProvider>

<Guard permission={["tasks", "create"]}>   {/* Casbin order: [obj, act] */}
  <Button>New task</Button>
</Guard>
<Guard role="admin" fallback={<PanelEmpty illustration="locked">Admins only.</PanelEmpty>}>
  <Billing />
</Guard>
<Guard permissions={[["tasks", "create"], ["tasks", "update"]]} mode="any">…</Guard>

const canEdit = useCan("tasks", "update")
const { allowed } = useGuard({ permission: ["projects/42", "delete"] })
<Button disabled={!allowed}>Delete project</Button>
```

- **Matching** follows Casbin's common matchers: exact, `*`, key patterns
  (`projects/*`, `/projects/:id`, `{id}`), action alternatives
  (`read|write`, `(read)|(write)`), domains, and deny rules (a matching deny
  wins). `fromCasbin(data, ["sub", "dom", "obj", "act"])` reads a model with
  domains, `"eft"` reads effects; pass `domain` to the provider for the
  current tenant, or `matcher` for a model it doesn't cover.
- **Guard** renders `loading` (default nothing) until access loads, then its
  children or `fallback`. Several requirements must all pass; `mode="any"`
  relaxes a list. `when={(access) => …}` adds any other rule.
- **Loading:** the first load runs on mount and is cancelled on unmount;
  `useUserAccess((s) => s.reload)()` refreshes in place (a failed refresh
  keeps what was loaded). `errorFallback={(error, retry) => …}` handles a
  failed first load; `initial` skips it when access is bootstrapped.

## AI assistant

`@forge-ui/assistant` is a full-screen assistant for a
[Google ADK](https://google.github.io/adk-docs/) agent, built on
[assistant-ui](https://www.assistant-ui.com) and its ADK runtime
(`@assistant-ui/react-google-adk`), in the workspace shell: conversations in
the sidebar (or none, with `sidebar={false}`), the active agent and
artifacts in the top bar, the thread, and an optional panel beside it. The
thread, composer and conversation list are assistant-ui's own elements
(`@forge-ui/assistant-thread`); the composer keeps assistant-ui's look. For
the panels, statistics and tool cards around a capable agent, add
[`@forge-ui/agent-workspace`](#agent-workspace).

```bash
npx shadcn@latest add @forge-ui/assistant
```

```tsx
import { AssistantScreen, useAdkAssistant } from "@/components/forge/assistant"

function Assistant({ userId }: { userId: string }) {
  const { runtime, artifacts, modelSettings } = useAdkAssistant({
    adk: { url: import.meta.env.VITE_ADK_URL, appName: "my_agent", userId },
    headers: () => ({ Authorization: `Bearer ${session.token}` }),
    models: [
      { id: "gemini-2.5-pro", name: "Gemini 2.5 Pro" },
      { id: "gemini-2.5-flash", name: "Gemini 2.5 Flash" },
    ],
  })
  return (
    <AssistantScreen
      runtime={runtime}
      artifacts={artifacts}
      showModels
      modelSettings={modelSettings}
      onAuthRequest={openOAuthPopup}
      title="Forge assistant"
      suggestions={["Which tasks are failing?"]}
    />
  )
}
```

| Connection | Option                          | Notes                                                                                                                               |
| ---------- | ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Direct     | `adk: { url, appName, userId }` | An ADK server (`adk api_server --allow_origins …`). Its sessions become the conversations, and artifacts download from the top bar. |
| Proxy      | `api: "/api/chat"`              | Your route, e.g. `createAdkApiRoute` from `@assistant-ui/react-google-adk/server`.                                                  |
| Custom     | `stream`                        | Any `AdkStreamCallback`; the demo's simulated agent is one.                                                                         |

Every other `useAdkRuntime` option passes through: `getCheckpointId` (needed
for edit and regenerate), `adapters` (`attachments` makes the composer's
attach button accept files), `eventHandlers`.

What ADK does, and what the screen shows:

| ADK                        | Shown as                                                                                            |
| -------------------------- | --------------------------------------------------------------------------------------------------- |
| `adk_request_confirmation` | An Approve / Deny card for the gated tool call                                                      |
| `adk_request_credential`   | A sign-in card; `onAuthRequest` opens `authUri` and resolves with the credential                    |
| `adk_request_input`        | A question card: choices for an enum or boolean `response_schema`, otherwise a text answer          |
| `transfer_to_agent`        | The active agent in the top bar and composer; replies are labelled when the answering agent changes |
| `escalate`                 | A banner saying a person will reply                                                                 |
| `artifactDelta`            | The artifacts menu in the top bar (direct mode)                                                     |

Your own tool UIs go in `toolkit` (`defineToolkit` from assistant-ui),
merged over the ADK ones.

### Screen options

Beyond the connection, `AssistantScreen` (and `AssistantModal`, which also
takes `open` / `onOpenChange`) has slots for an app's own parts:

| Option                    | What it does                                                                            |
| ------------------------- | --------------------------------------------------------------------------------------- |
| `sidebar`                 | Extra sidebar content above the conversations; `false` drops the sidebar                |
| `navigation`, `actions`   | Before the title / after the agent and artifacts in the top bar                         |
| `aside`                   | A panel beside the conversation, inside the assistant's providers                       |
| `composerLead`            | A component just above the composer, e.g. task progress                                 |
| `composerContext`         | Shown at the top of the composer: what the agent gets with the next message             |
| `composerActions`         | After the model picker in the composer's bottom row (`composerChip` matches their look) |
| `composerTrailingActions` | A component right before Send, e.g. a chat history button                               |
| `messageMeta`             | A component beside each reply's actions, e.g. its timing and tokens                     |
| `followUps`               | A component above the composer after a reply, in place of the runtime's suggestions     |
| `onOpenArtifact`          | Opens an artifact picked in the top bar (e.g. in a canvas) instead of downloading it    |
| `agents`                  | Names for the app's ADK agents; a call handing a request to one shows as asking it      |
| `approvals`               | Per gated tool: a title, description, preview and button labels for its approval card   |
| `sources`                 | Reads cited passages from tool results; see [Cited sources](#cited-sources)             |

`useAdkAssistant` also takes `runState`, ADK session state sent with every
run (e.g. `() => ({ page_context: currentPage() })`); a request the agent
server refuses or can't receive shows as a failed reply.

### Rich replies

Replies render GitHub-flavoured Markdown with KaTeX maths (`$$ … $$`, and
the `\( … \)` and `\[ … \]` some models write; a single `$` stays text),
` ```mermaid ` blocks drawn as diagrams, and code highlighted by Shiki in
GitHub's light and dark themes. Mermaid and Shiki load on first use.
Reasoning shows as "Thought for 12 seconds", collapsed; ↑ in an empty
composer recalls earlier messages; selecting text in a reply offers to quote
it.

### Approvals

A tool with ADK's `require_confirmation` asks before it runs. The card says
what the agent wants to do, waits for Approve or Deny, then folds to one line
with the outcome. Describe each gated tool in `approvals` to replace its name
and raw arguments:

```tsx
approvals={{
  add_note: {
    title: "Add this note?",
    description: "The assistant wants to save it to this conversation's notes.",
    preview: ({ args }) => <blockquote>{String(args.text)}</blockquote>,
    approveLabel: "Add note",
  },
}}
```

### Cited sources

When an agent's tools return passages with refs and its answer cites them as
`[S3]`, `sources` turns the citations into numbered chips and lists the
documents under the answer. Tell it how to read your tool's result:

```tsx
sources={{
  fromToolCall: (toolName, result) =>
    toolName === "search_docs"
      ? toolResult<{ passages: Passage[] }>(result).passages?.map((p) => ({
          ref: p.ref,
          document: { key: p.file, title: p.file, subtitle: p.team },
          location: p.section,
          excerpt: p.text,
        })) ?? []
      : [],
}}
```

### Feedback

Pass `onFeedback` to `useAdkAssistant` and every agent reply gets thumbs up
and down beside Copy and Refresh. Each rating calls your handler with the
reply, its text, and the ADK session id (direct connections), so you can
store it with the session. A rated button stays pressed, and the user can
switch their rating; without `onFeedback` the buttons are hidden.

```tsx
const { runtime } = useAdkAssistant({
  adk,
  onFeedback: ({ type, sessionId, message, text }) =>
    api.post("/feedback", { type, sessionId, messageId: message.id, text }),
})
```

### Floating assistant

`AssistantModal` takes the same props as `AssistantScreen` (minus the shell's
`sidebar`) and puts the agent behind a launcher in the corner of any page:
a resizable panel with the conversation, past conversations and a
new-conversation button. It opens itself when the agent starts replying,
and stays open while you use the page.

```tsx
import { AssistantModal, useAdkAssistant } from "@/components/forge/assistant"

function Layout({ children }: { children: React.ReactNode }) {
  const { runtime, artifacts, modelSettings } = useAdkAssistant({ adk })
  return (
    <>
      {children}
      <AssistantModal
        runtime={runtime}
        artifacts={artifacts}
        title="Forge assistant"
        showModels
        modelSettings={modelSettings}
        commands={commands}
      />
    </>
  )
}
```

The launcher is fixed to the viewport's bottom-right corner; move it with
`className` (e.g. `bottom-12` above a footer, or `absolute` to pin it inside
a positioned container), and pass `defaultOpen` to start open. The launcher shows `AssistantMark`, the
assistant's own icon: a chat bubble in the agent-orb gradient with a spark
that twinkles on hover and turns while the agent works. It's exported for
use elsewhere, such as an "Ask the assistant" button. For other
layouts, `AssistantProvider` sets up the runtime and settings and
`AssistantThread` renders the configured conversation; `AssistantScreen` and
`AssistantModal` are both built from the two.

### Slash commands

`commands` adds a `/` menu to the composer. It opens when a message starts
with `/`, filters as you type (names first, then descriptions), and picks
with Enter, Tab or a click. What a command does depends on its fields:

```tsx
const commands: AssistantCommand[] = [
  // Runs in the app; the typed /new is removed.
  { id: "new", description: "Start a new conversation", icon: "plus",
    execute: (aui) => aui.threads.switchToNewThread() },
  // Fills the composer with a prompt, and sends it with `send`.
  { id: "failing", description: "Ask which tasks are failing", icon: "failed",
    prompt: "Which tasks are failing?", send: true },
  // Neither: leaves "/deploy " to add arguments to; the agent gets the text.
  { id: "deploy", description: "Deploy a branch, e.g. /deploy main" },
]

<AssistantScreen runtime={runtime} commands={commands} />
```

Commands with neither `execute` nor `prompt` reach the agent as plain text
(`/deploy main`), so describe them in the agent's instructions. The menu is
built on assistant-ui's trigger popover, which assistant-ui still marks
unstable (`unstable_useSlashCommandAdapter`); `ComposerTriggerPopover` in
`@forge-ui/assistant-thread` is its restyled picker, usable for `@` mentions too.

### Voice input

The mic beside the attach button dictates into the composer: the words
appear in the input as they're recognised ("Listening…" until the first
one), and nothing is sent until you press send. By default
`useAdkAssistant` uses the browser's own speech recognition (the Web Speech
API in Chrome, Edge and Safari); where a browser has none, such as Firefox,
the mic is hidden. `dictation: false` turns it off,
`dictation: new WebSpeechDictationAdapter({ language: "fr-FR" })` fixes the
language (the default is the browser's), and any assistant-ui
`DictationAdapter` swaps in your own speech-to-text service. The
microphone needs HTTPS or localhost, and Chrome sends the audio to Google's
speech service to transcribe it.

### Model section

`showModels` (with `modelSettings` from `useAdkAssistant`) adds a model
picker to the composer: one ghost trigger showing the model's mark, name and
thinking level, opening a searchable list grouped by each model's `group`
(e.g. its provider, with its `icon`) and the levels the chosen model offers.
`thinkingLevels` lists every level (default off / low / medium / high;
`extendedThinkingLevels` adds minimal and extra high), and a model's own
`thinkingLevels` narrows them; switching to a model that lacks the chosen
level picks the nearest it has. `models` may arrive after the first render,
e.g. from the agent server. `defaultModel` and `defaultThinkingLevel` pick
the starting values. While it's shown, every run sends the selection to the
agent as ADK session state, `{ model, thinking_level }` (reshape it with
`modelStateDelta`). Apply it in a `before_model_callback`:

```python
from google.genai import types

BUDGETS = {"off": 0, "low": 1024, "medium": 8192, "high": 24576}

def apply_model_settings(callback_context, llm_request):
    state = callback_context.state
    if model := state.get("model"):
        llm_request.model = model
    if (level := state.get("thinking_level")) in BUDGETS:
        llm_request.config.thinking_config = types.ThinkingConfig(
            thinking_budget=BUDGETS[level], include_thoughts=level != "off"
        )

root_agent = Agent(..., before_model_callback=apply_model_settings)
```

### Limits

- assistant-ui merges consecutive assistant messages, so a hand-off inside
  one turn shows as a single reply; the agent label appears on the next
  turn that a different agent answers.
- ADK sessions have no titles. A conversation is named after its first
  message for as long as the page is open.
- Edit and regenerate need `getCheckpointId`, since ADK has to fork the
  session from a checkpoint.

## Agent workspace

`@forge-ui/agent-workspace` is everything around the assistant's
conversation that makes a capable agent's work legible, mounted through
`AssistantScreen`'s slots:

- **Progress** — `ProgressDock` above the composer names the current
  activity and the plan's resolved steps, and expands into a checklist.
  `PlanPanel` (with `PlanButton`) separates the latest plan from the latest
  reply's tool activity; each action opens to its input and result.
- **Notes and documents** — `NotesPanel` / `NotesButton` and `CanvasPanel`,
  which shows the agent's documents (ADK artifacts) with their versions.
- **Statistics** — `ContextMeter` (context used and the conversation's cost)
  and `MessageStats` / `TurnDetails` (each reply's time, tokens and cost).
  Missing prices show as unavailable, never as $0.
- **Navigation** — `ChatHistoryPalette`, a button before Send (and ⌘K)
  opening New chat and the conversations, searched by what was said.
- **Choices** — `ToolsMenu` (which tools the agent may use, remembered) and
  `FollowUps` (suggested next questions).
- **Tool cards** — `toolUIs` and `approvalViews` for the chat API's built-in
  tools (plans, notes, documents, the calculator, dice, time) and
  `AskUserUI` for ADK's `adk_request_input`; `ToolFrame` for your own.

```bash
npx shadcn@latest add @forge-ui/agent-workspace
```

```tsx
import { AssistantScreen } from "@/components/forge/assistant"
import {
  CanvasPanel,
  ChatHistoryPalette,
  ContextMeter,
  FollowUps,
  MessageStats,
  NotesButton,
  NotesPanel,
  PlanButton,
  PlanPanel,
  ProgressDock,
  ToolsMenu,
  approvalViews,
  toolUIs,
  useAgentWorkspace,
  useCanvas,
  workspaceCommands,
} from "@/components/forge/agent-workspace"

const connection = {
  adkUrl: "/api/v1/agents",
  appName: "assistant",
  userId: "local",
}
const History = () => <ChatHistoryPalette {...connection} />
const Followups = () => <FollowUps {...connection} />

export function App() {
  const agent = useAgentWorkspace({ ...connection, chatApi: true })
  return (
    <AssistantScreen
      sidebar={false}
      runtime={agent.runtime}
      artifacts={agent.artifacts}
      {...agent.modelSection}
      commands={workspaceCommands}
      composerLead={ProgressDock}
      composerActions={
        <>
          <ToolsMenu {...agent.tools} />
          <ContextMeter
            {...connection}
            catalog={agent.catalog}
            defaultModel={agent.defaultModel}
          />
        </>
      }
      composerTrailingActions={History}
      messageMeta={MessageStats}
      followUps={Followups}
      toolkit={toolUIs}
      approvals={approvalViews}
      onOpenArtifact={(name) => useCanvas.getState().openDocument(name)}
      actions={
        <>
          <PlanButton />
          <NotesButton />
        </>
      }
      aside={
        <>
          <NotesPanel />
          <PlanPanel />
          <CanvasPanel />
        </>
      }
    />
  )
}
```

`useAgentWorkspace` wraps `useAdkAssistant` with what the panels need:
attachments, the model and tool lists from the chat API (remembered between
visits), and 👍/👎 feedback. Without an `adkUrl` the assistant replies with
how to connect one. `ChatHistoryPalette` takes `shortcut={false}` when the
app already uses ⌘K.

What the workspace expects of the agent (the chat API, adk-chat, follows
these; a plain ADK API server gets the assistant without the parts that need
them):

| Part            | Convention                                                                                                                                   |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Plan            | Session state `plan`: `{ id, title, steps: [{ text, status, started_at?, finished_at? }] }`, kept by `set_plan` and `update_plan_step`       |
| Progress strip  | Shown only when the latest turn called `set_plan` or `update_plan_step`                                                                      |
| Notes           | Session state `notes`: `[{ id, text }]`, kept by `add_note`, `remove_note`, `list_notes`                                                     |
| Documents       | ADK artifacts written by `write_document`, read by `read_document`                                                                           |
| Statistics      | Usage from the persisted ADK events, priced from the model list; `ContextMeter` syncs them for `MessageStats` and the progress strip         |
| Chat API routes | `/apps/{app}/models`, `/apps/{app}/tools`, `…/sessions/{id}/feedback`, `…/sessions/{id}/follow-ups` (`useAgentWorkspace({ chatApi: true })`) |

## Rich text editor

`@forge-ui/rich-text-editor` is a complete rich text field on
[Tiptap](https://tiptap.dev) in the workspace look: a fixed toolbar,
selection and table menus, `/` commands, `@` mentions, block handles, find
and replace (⌘F), an outline, full screen, uploads, YouTube / Twitch /
audio embeds and KaTeX formulas. Its value is HTML, Markdown or Tiptap JSON.
It loads lazily: importing it doesn't pull Tiptap into a page's bundle until
an editor renders.

```bash
npx shadcn@latest add @forge-ui/rich-text-editor
```

```tsx
import { RichTextEditor, RichTextView } from "@/components/forge/rich-text-editor"

<RichTextEditor
  format="markdown"
  value={body}
  onChange={setBody}
  mentions={(query) => people.filter((p) => p.label.includes(query))}
  onUpload={async (file) => (await api.upload(file)).url}
/>

<RichTextEditor preset="standard" placeholder="Add a comment…" />  // lighter fields
<RichTextView value={html} />                                     // saved content, read-only
```

`preset` is `full` (everything), `standard` (no styling, tables or media) or
`minimal` (inline formatting); `features` switches single groups on or off
over a preset. Without `onUpload`, added files are embedded as data URLs.

## Conventions

- **Tokens are Tailwind utilities.** Colours: `text-subtle`,
  `bg-tone-amber`, `bg-status-running`, `text-console-muted`, `bg-diff-insert`, …
  Type below 12px: `text-2xs` (11px), `text-3xs` (10px), `text-4xs` (9px);
  `text-sm` is the 14px `--text-ui` size. Radii and shadows are variables:
  `rounded-(--radius-control)`, `shadow-(--shadow-float)`.
- **Font sizes are rem, never px.** `text-[13px]` ignores the user's text
  size (and the browser's); write `text-[0.8125rem]` or use a token. Row
  heights that should follow density use `--forge-row-height`.
- **Tones cascade.** `TaskGroup` sets `--tone` / `--tone-foreground`;
  children read them with `bg-(--tone)` and `text-(--tone-foreground)`.
- **The shell is a container** (`@container/shell`). Regions respond to the
  shell's own width with `@max-[1050px]/shell:` variants, matching the
  original breakpoints (1700 / 1270 / 1050 / 800 / 600px).
- **Icons** take a vocabulary name or any Hugeicons glyph:
  `<Icon icon="branch" />`, `<Icon icon={Robot01Icon} />`. Inside controls,
  add `data-icon="inline-start"` and let the control size the glyph.
- **Focus** is a 2px `--focus` outline offset 3px, defined unlayered in the
  theme so it wins over `outline-none` on primitives (as in the original).

## Agent skills

Coding agents get context on this codebase from skills in `.agents/skills/`
(symlinked into `.claude/skills/` so Claude Code loads them; the same layout
the `shadcn` skill was installed with):

| Skill            | Teaches                                                                                                                                                                                                                                    |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `forge-ui`       | Building screens: which Forge component or primitive to use, the shell skeleton, tokens, icons, container queries, Base UI differences, the ADK assistant                                                                                  |
| `forge-data`     | Server data: the API client, `createQueryClient`, `createResource` / `createNestedResource`, errors, optimistic updates, infinite lists, with worked recipes                                                                               |
| `forge-state`    | Shared client state: `createContextStore` stores seeded from props or context, selectors, resetting and syncing, what stays in TanStack Query; the built-in user context — preferences, and roles and permissions from Casbin with `Guard` |
| `forge-registry` | Changing this repo: registering items in `build-registry.mjs`, demo sections, README, rebuilding the registry                                                                                                                              |

Agents load a skill's `SKILL.md` when a task matches its description and
open its `references/` as needed. When you change an API, update the
matching skill in the same change so agents don't learn the old one.

### Skills in an app that uses Forge UI

Install the three app-facing skills straight from this package with the
[`skills`](https://www.npmjs.com/package/skills) CLI. It clones with your
GitHub credentials, so the GitHub CLI login that reads the registry works
here too. Point it at `packages/forge-ui`: from the monorepo's root it would
find the copies installed in the root `.claude/skills` instead:

```bash
npx skills add https://github.com/jleva12/ei-tiger-workflow/tree/main/packages/forge-ui --skill forge-ui forge-data forge-state -a claude-code -y
```

They're copied into the app's `.claude/skills/` and recorded in
`skills-lock.json`; commit both so the whole team gets them, and run
`npx skills update` to pull newer versions. Leave out `-a claude-code` to
choose other agents (Cursor, Codex, …). `forge-registry` is for maintaining
this repo, so apps don't need it. The CLI rejects a `SKILL.md` whose
frontmatter isn't strict YAML — quote a `description` that contains `: `.

## Changing the design system

1. Edit tokens in `src/index.css` or components in `src/components/`.
2. Check the result in the demo (`npm run dev`).
3. `npm run registry:build`, then redeploy `public/r` (or `dist/`).
4. In consuming projects, re-run `npx shadcn@latest add @forge-ui/<item>` and review
   the diff with `--dry-run` / `--diff` first.

### Fidelity notes

Values were measured against the running original. Where the original's CSS
and its rendering disagree, the library follows the rendering:

- Outline buttons have no shadow (the original's shadow rule targets a
  `data-variant` attribute shadcn never sets).
- Toolbar menu triggers render at 13px/400 (the original's unlayered
  `font: inherit` beats the button's 12px/450 on trigger elements).
- Line height is 1.5 for `text-xs`/`sm`/`lg`, matching the inherited
  line height the original renders almost everywhere.

Intentional departures from the original:

- The empty-state illustration is a family of states (`@forge-ui/illustration`);
  the original drew only the checklist. Its check chips use darker
  `--illustration-check` values in dark mode instead of the light pastel.

- Dialogs use a rounder 16px corner (`--radius-dialog`; the original used 12px).
- Sheets float: they sit `--sheet-inset` (12px) away from the viewport edges
  with `--radius-dialog` corners and a full border, instead of spanning the
  full height flush to the edge.
