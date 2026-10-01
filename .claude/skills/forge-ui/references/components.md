# Forge component catalogue

Props are `name: type` (required) or `name?` (optional, `=default`); plain
`className` / `children` passthrough is omitted. Imports are
`@/components/forge/<file>` unless noted. When a detail here disagrees with the
source, the source wins — read it.

## Contents

- icon, status, avatars
- app-shell, icon-rail, workspace-sidebar, toolbar
- task-list, kanban
- task-sheet, task-page
- empty-state, feedback, activity
- run-log, changes, handoff, metrics-table
- data-table
- assistant (Google ADK)
- Tuned primitives (`@/components/ui`)

## icon (`icon.tsx`, `icons.ts`)

- `Icon` — `icon: IconProp`, `size?=17`, `strokeWidth?=1.6`; always
  `aria-hidden`. `IconProp = IconName | IconSvgElement` (a Hugeicons glyph).
- `iconNames`, `resolveIcon(icon)`, `IconName`.

## status (`status.tsx`, `variants.ts`)

- `StatusBadge` — `status: TaskStatus`, `size?: "default" | "lg"`;
  children replace the label.
- `StatusSymbol` — `kind?: SymbolKind="backlog"`, `tone?: Tone`; 14px band
  glyph coloured by `--tone-foreground`.
- `Chip` — `tone?: ChipTone="neutral"`, `icon?: IconProp`.
- `CountBadge` — monospace count (group bands, toolbars).
- `ConnectionDot` — `online?=true`.
- `RunStateIcon` — `state: RunState`, `size?=17`.

Types (`variants.ts`):

| Type | Values |
| --- | --- |
| `TaskStatus` | `pending`, `enqueued` ("Queued"), `running`, `review` ("Running · Reviewing"), `completed`, `failed`, `cancelled` |
| `SymbolKind` | `backlog`, `progress`, `review`, `completed`, `failed`, `cancelled` → tones neutral, amber, pink, green, red, neutral (`symbolTones`) |
| `Tone` | `neutral`, `amber`, `pink`, `green`, `red`; `toneVars[tone]` is a class string setting `--tone` / `--tone-foreground` |
| `ChipTone` | `neutral`, `success`, `warning`, `danger`, `notice`, `outline` |
| `RunState` | `running`, `completed`, `failed`, `planned` |

Also exported: `statusLabels`, `statusBadgeVariants`, `chipVariants`.

## avatars (`avatars.tsx`)

- `WorkspaceOrb` — `size?: "default" | "lg"`, `render?` (pass
  `render={<button type="button" aria-label="…" />}` to make it interactive).
- `AgentOrb` — `variant?: number` (cycles 5 gradients), `size?`.
- `AgentAvatars` — `agents: Agent[]` (`{ id, name, role? }`), `max?=5`,
  `size?`, `emptyLabel?`, `hideEmptyLabel?`; overlapping orbs with tooltips.

## app-shell (`app-shell.tsx`, `app-shell-context.ts`)

- `AppShell` — `sidebarOpen?`, `defaultSidebarOpen?=true`,
  `onSidebarOpenChange?`. Provides the shell context and a
  `TooltipProvider delay={250}`; renders the `@container/shell` root
  (`h-dvh min-w-[360px]`).
- `useAppShell()` (throws outside a shell), `useOptionalAppShell()` (null).
- `SkipLink`, `MainPanel` (`<main>`).
- `Topbar` — `sidebarTrigger?=true` (needs `AppShell` when true);
  `TopbarBreadcrumb`; `TopbarCrumb` (`icon?` + button props; hidden ≤800px);
  `TopbarCrumbSeparator`; `TopbarPage` (`children` required, `icon?`;
  renders the **h1**); `TopbarActions`; `TopbarAgents` (hidden ≤1270px);
  `PrimaryAction` (`icon?="addCircle"` + Button props; icon-only ≤600px);
  `SidebarTrigger`.
- `ViewToolbar`; `ToolbarFilters` (last child hidden ≤600px);
  `PageContent` (the scroll region, `tabIndex=-1`, give it `id="content"`
  for the skip link).
- `WorkspaceFooter` (second child pushed right); `FooterShortcut` —
  `keys: string`, children.

## icon-rail (`icon-rail.tsx`)

- `IconRail` (hidden ≤600px) > `AppMark` (`icon?="layers"`), `RailNav`,
  `RailFooter`.
- `RailButton` — `icon: IconProp`, `label: string` (tooltip + aria-label),
  `active?`.

## workspace-sidebar (`workspace-sidebar.tsx`)

- `WorkspaceSidebar` — children, `label?`, `description?`. Renders the
  `<aside>`, and the same children in a left `Sheet` at ≤1050px. Needs
  `AppShell`.
- `SidebarBrand` > `WorkspaceOrb`, `SidebarBrandName` (`name`, `suffix?`),
  `SidebarBrandAction`, `SidebarCollapseButton` (needs `AppShell`).
- `SidebarSection` — `variant?: "default" | "primary" | "flush"`.
- `CommandButton` — `shortcut?="⌘ K"`; children default to "Command".
- `NavItem` — `icon?`, `active?`, `meta?` (trailing count/dot/chevron),
  `size?: "default" | "sm"`, `render?` (links: `render={<a href="…" />}`);
  closes the mobile sheet on click; works outside a shell.
- `NavSectionHeading` — `action?`. `SubNav` (nest `NavItem size="sm"`).
- `ProjectItem` — `color?: 0-3` + NavItem props (no icon/size).
- `SidebarHint`, `SidebarStatus` (`online?`, `icon?="check"`).

## toolbar (`toolbar.tsx`)

- `ViewTabsList` > `ViewTabsTrigger` (`value`, `icon?`) — inside `<Tabs>`.
- `LayoutSwitch` — `value: string`, `onValueChange`, `options: { value,
  label, icon }[]`; hidden ≤800px.
- `SearchField` — input props + `shortcut?="/"`; accepts `ref`.
- `ToolbarButton` — `icon?`, `value?` (renders "Label **value**"),
  `active?` (dot). Use as `render` of a `DropdownMenuTrigger`.

## task-list (`task-list.tsx`)

`TaskList > TaskGroup > TaskTable`.

- `TaskItem` = `{ id, title, status, statusLabel?, repository?, updated?,
  agents?, runCount? }`.
- `TaskList` — `density?: "default" | "compact"`.
- `TaskGroup` — `title: string`, `count?`, `symbol?: SymbolKind="backlog"`,
  `tone?` (defaults from the symbol), `actions?` (use `TaskGroupAction` —
  `icon: IconProp` + aria-label), `layout?: "list" | "board"`, `open?`,
  `defaultOpen?=true`, `onOpenChange?`. Collapsible, sets the tone.
- `TaskTable` — `tasks: TaskItem[]`, `onSelect?`, `emptyLabel?`; min 760px
  wide.
- Custom rows: `TaskTableHead` (`icon?: IconProp | null="sort"`),
  `TaskTableRow`, `TaskTableCell`, `TaskTitle` (`id`, `title`, `runCount?`),
  `RepoLabel`, `DateLabel`.
- `TaskListSkeleton` — `groups?=3`, `rows?=3`.

## kanban (`kanban.tsx`)

- `TaskBoard` — `columns: KanbanColumnData[]` (`{ id, title, symbol?,
  tone?, tasks }`), `onSelect?`, `actions?: (column) => ReactNode`.
- Manual: `KanbanBoard > KanbanColumn` (TaskGroup props minus `layout`, +
  `emptyLabel?`) `> BoardCard` (`task: TaskItem`).

## task-sheet (`task-sheet.tsx`) — forms in a side panel

```tsx
<Sheet open={open} onOpenChange={setOpen}>
  <TaskSheetContent>
    <TaskSheetHeader title="New task" eyebrow="Tasks" description="…" />
    <TaskForm onSubmit={submit}>
      <FieldGroup>…fields…</FieldGroup>  {/* FormColumns for side-by-side */}
      <FormFooter hint="Runs on a new branch">
        <Button variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
        <Button type="submit">Create</Button>
      </FormFooter>
    </TaskForm>
  </TaskSheetContent>
</Sheet>
```

- `TaskSheetContent` — SheetContent props; 560px right sheet.
- `TaskSheetHeader` — `title`, `eyebrow?`, `eyebrowIcon?="task"`,
  `description?`.
- `TaskForm` — a `<form>` that restyles descendant Field/Input/Textarea/
  Select triggers to the sheet's 36px / 12px density.
- `FormColumns`; `FormFooter` — `hint?`, `hintIcon?="branch"`, children =
  buttons.
- `DetailList > DetailRow` (`label`); `PlanEntry` (`title`, `meta?`,
  `description?`) `> PlanAgent` (`name`, `detail?`, `icon?="robot"`).

## task-page (`task-page.tsx`) — detail pages

- `TaskPage` (scroll container) > `TaskPageHeader` > `TaskPageEyebrow`
  (`backLabel?`, `onBack?`, `reference?`, `label?`) + `TaskTitleRow`
  (`title`, `meta?`, `actions?`; renders the **h1**). `TaskMeta` (`icon?`).
- `<Tabs>` > `TaskTabBar` > `TaskTabsList` > `TaskTabsTrigger` (`value`,
  `icon?`, `live?`); `LiveLabel` (`online?`).
- `SubmissionLayout` (`aside?`) > `AsideSection` (`title`) > `AsideFact`
  (`label`); `PageSection` (`title`, `meta?`, `description?`); `Prose`.
- `WaitingNotice` — `title`, `icon?="clock"`, `state?`.
- `ExecutionPlan > ExecutionStep` — `step`, `name`, `role?`, `status?`,
  `detail?`, `completed?`.
- `Disclosure` — `summary`, children, `defaultOpen?`.
- `RecordList > RecordRow` (`label`).

## illustration (`illustration.tsx`)

- `Illustration` — `name`, `size?: "default" | "sm"` (112×102px, or ~¾ for
  panels and table cells), `surface?: "paper" | "console"` (recolours it
  for the always-dark log console), `className?`. Pure CSS on theme tokens,
  `aria-hidden`; always pair it with text that says the same thing.
- `IllustrationName` — `tasks` (first run), `complete` (all caught up),
  `waiting` (queued / no run yet), `error` (a run or load failed), `search`
  (no results), `offline` (API or worker unreachable), `locked` (no
  access), `activity` (empty timeline), `logs` (no output), `changes` (no
  diff), `conversation` (empty assistant thread). Pick by what the user is
  waiting for, not by the page it's on.

## empty-state (`empty-state.tsx`)

- `PageEmpty` — `title`, `icon?="clock"`, `illustration?: IllustrationName`
  (drawn instead of the icon), `description?`, children (actions).
- `PanelEmpty` — children, `icon?="activity"`, `illustration?` (small
  drawing instead of the icon).
- `EmptyWorkspace` — `title`, `eyebrow?`, `description?`, `actions?`,
  `steps?: { icon, label }[]`, `illustration?` (a node; default
  `<EmptyIllustration />`); `WorkflowSteps`, `EmptyIllustration` (`name?="tasks"`,
  the drawing with the spacing `EmptyWorkspace` expects — use
  `illustration={<EmptyIllustration name="search" />}`).

## settings-list (`settings-list.tsx`)

- `SettingsList` — divided rows; its own `@container/settings`, so rows
  stack their value and action under the text below 560px.
- `SettingsRow` — `title`, `description?`, `media?` (leading icon or logo
  tile), `badge?` (after the title), `meta?` (current value or status,
  right-aligned), `action?` (usually one `Button variant="outline"
  size="sm"`, `min-w-[104px]` to line the buttons up).

## integrations (`integrations.tsx`, `brand-logos.tsx`)

- `IntegrationRow` — `logo`, `name` (string; used in "Connect GitHub"
  button labels), `description?`, `status?: "disconnected" | "connecting" |
  "connected" | "error"`, `account?` (shown when connected), `detail?`
  (connected date, or the failure reason), `onConnect?`, `onDisconnect?`,
  `menu?` (extra `DropdownMenuItem`s in the connected Manage menu). Renders
  state only — start the OAuth redirect/popup in `onConnect` and pass the
  result back as `status`. Put rows in a `SettingsList`.
- `IntegrationLogo` — the 36px app tile that frames a 20px logo.
- Brand marks (inline SVG, `size?=20`): `GitHubLogo` (currentColor),
  `GitLabLogo`, `JiraLogo`, `LinearLogo`, `FigmaLogo`, `SlackLogo`,
  `GoogleDriveLogo`. For other apps pass any 20px SVG as `logo`; don't draw
  brand marks with `Icon`.

## feedback (`feedback.tsx`)

- `ErrorCallout` — `title?`, `action?`, `icon?="info"`, children (the
  message). Built on `Alert variant="error"`.
- `PreviewBanner` — `label`, `explanation?`, `action?`, `onAction?`,
  `icon?`.
- `ActiveFilters` — children (chips), `onClear?`, `clearLabel?`.
- `LoadMore` — Button props + `label?`, `hint?`.

## activity (`activity.tsx`)

- `WorkspaceView` (920px column) > `ViewHeading` (`title`, `icon?`,
  `description?`; renders the **h1**).
- `EventList > EventItem` (`type`, `time?`, `error?`, `reference?`).
- `StatusHeading` (`title`, `online?`, `status?`); `StatGrid > Stat`
  (`label`, `value`); `Footnotes > Footnote` (`icon?`).

## run-log (`run-log.tsx`)

`RunLogLayout > [JobsSidebar, LogPane]`.

- `JobsSidebar` > `JobsLabel` (`trailing?`), `JobButton` (`name`, `state?:
  RunState`, `icon?`, `details?: ReactNode[]`, `count?`, `active?`),
  `RunStats > RunStat` (`label`).
- `LogPane` > `LogPaneHeading` (`title`, `state?`, `subtitle?`, `actions?`),
  `LogConsole` (always dark) > `LogToolbar` (`LogSearch`, `LogToolbarText`,
  `LogToolbarButton` with `aria-pressed`), `LogScroll > LogEntry` (`title`,
  `number?`, `kind?: "event" | "tool" | "message"`, `status?`, `time?`,
  `defaultOpen?`), `LogEmpty` (`title`, `icon?`, `illustration?` — drawn
  on console paper), `LogFooter` (`online?`, `trailing?`).

## changes (`changes.tsx`)

`DiffWorkspace > [FileNavigator, FileDiff]`.

- `FileNavigator` > `FileFilter`, `FileListLabel` (`count?`), `FileList >
  ChangedFile` (`path`, `added?`, `removed?`, `icon?="code"`, `active?`).
- `FileDiff` > `FileDiffHeader` (`path`, `added?`, `removed?`, `actions?`),
  `DiffView` (`lines: DiffLine[]`, `wrap?`); `DiffLine = { type: "context"
  | "insert" | "delete" | "hunk", old?, new?, content }`.
- `DiffTotals` — `added`, `removed`, `label?`.

## handoff (`handoff.tsx`)

- `VerificationStatus` — `result: "passed" | "failed" | "skipped" |
  "not_run" | "unable_to_verify"`, `withIcon?`.
- `VerificationRound` (`title`, `result?`, `meta?`, `note?`) >
  `VerificationCommands` (`commands`); `CommandOutput` (`summary?`,
  `defaultOpen?`, `reason?`).
- `HandoffIteration` (`title`, `latest?`, `count?`, `summary?`,
  `defaultOpen?=true`) > `HandoffAgent` (`name`, `state?`, `status?`,
  `meta?`) > `HandoffResult` (`title?`, `verdict?: "approved" |
  "needs-changes"`), `Finding` (`severity`, `location?`, `note?`).

## metrics-table (`metrics-table.tsx`)

- `MetricsTable` (`label` for aria, `minWidth?=925`) >
  `MetricsHeader | MetricsBody | MetricsFooter` > `MetricsRow` (`variant?:
  "group" | "column" | "band" | "row" | "subtotal" | "total"`) > `<th>` /
  `<td>` / `MetricValue` (`value`, `secondary?`, `strong?`, `divider?`),
  `Unreported`.
- `MetricsSummary` — `total?`, `totalLabel?`.

## data-table (`data-table/index.ts`)

One component; its sub-parts are internal.

```tsx
import { DataTable, createColumnHelper } from "@/components/forge/data-table"

const helper = createColumnHelper<Repo>()
const columns = helper.columns([          // module scope or useMemo
  helper.accessor("name", { header: "Repository", meta: { label: "Repository" } }),
  helper.accessor("files", { header: "Files", meta: { label: "Files", align: "right" } }),
])

<DataTable title="Repositories" columns={columns} data={repos} getRowId={(r) => r.id} />
```

- Required: `columns`, `data`. Common optional: `title`, `description`,
  `features: Partial<DataTableFeatureConfig>`, `density: "default" |
  "compact"` (omit it to follow the user's density preference; the View menu
  can override per table or go back to Automatic), `isLoading`, `emptyState`, `getRowId`, `getSubRows`,
  `initialState`, `onRowClick`, `onCellEdit`, `onRowReorder`,
  `selectedRowsActions`, `toolbarActions`, `contextMenuItems`, `stateKey`
  (persists layout to localStorage), `tableHeight` (a class like
  `max-h-[560px]`), `tableOptions` (TanStack passthrough),
  `exportFileName`.
- Column `meta`: `label`, `align`, `className`, `filterVariant`,
  `filterOptions`, `format`, `formatOptions`, `editable`, `editor`,
  `validate`, `summary`, `wrap`. Filter variant and numeric right-alignment
  are inferred.
- Off by default: footers, cell selection, virtualization, row numbers, row
  reordering. Virtualization disables pagination. Page size 10.
- It never mutates `data`: apply edits/reorders in your handlers.

## assistant (`assistant/index.ts`) — Google ADK assistant, full screen or floating

A full-screen assistant on assistant-ui and `@assistant-ui/react-google-adk`,
in the workspace shell. Don't hand-build chat UI; use this. The thread,
composer and conversation list come from `@/components/assistant-ui/elements`
(assistant-ui's own elements, `@forge-ui/assistant-thread`). Keep the composer on
assistant-ui's look (round buttons, 1rem radius): the user chose it over a
Forge restyle.

```tsx
import { AssistantScreen, useAdkAssistant } from "@/components/forge/assistant"

const { runtime, artifacts, modelSettings } = useAdkAssistant({
  adk: { url: ADK_URL, appName: "my_agent", userId }, // or api: "/api/chat", or stream
  headers: () => ({ Authorization: `Bearer ${token}` }),
  models: [{ id: "gemini-2.5-pro", name: "Gemini 2.5 Pro" }], // optional
})

<AssistantScreen
  runtime={runtime}
  artifacts={artifacts}
  showModels
  modelSettings={modelSettings}
  onAuthRequest={openOAuthPopup}
  title="Forge assistant"
  welcome={{ title: "How can I help?", description: "…" }}
  suggestions={["Which tasks are failing?"]}
/>
```

- **Floating instead of full screen**: `AssistantModal` takes the same
  props (no `sidebar`; `actions` go in its header) and renders a launcher
  fixed to the viewport's bottom-right corner that opens a resizable panel
  with the conversation, past conversations and a new-conversation button.
  It opens itself when a run starts. Move the launcher with `className`
  (`bottom-12`, or `absolute` inside a positioned container); `defaultOpen`
  starts it open. Render it once per app. Its launcher shows
  `AssistantMark` (the assistant's icon: an orb-gradient chat bubble with a
  spark; `active` turns the spark), which is exported for other entry points
  to the assistant. Don't use a generic bot glyph for the assistant.
- **Custom layouts**: `AssistantProvider` (same props plus `children`) sets up
  the runtime, ADK tool UIs and settings; `AssistantThread` inside it renders
  the conversation with every configured Forge part.
- `useAdkAssistant(options)` → `{ runtime, artifacts, modelSettings }`.
  Transport: `adk: { url, appName, userId }` (direct to `adk api_server`;
  sessions become conversations, artifacts load), `api` (proxy route, e.g.
  `createAdkApiRoute`), or `stream` (custom `AdkStreamCallback`). Also
  `headers` (object or per-request function), `dictation` (default: the
  browser's Web Speech API for the composer mic; `false` to turn off, or an
  assistant-ui `DictationAdapter`), `onFeedback` (shows thumbs up / down on
  replies and receives `{ type, message, text, sessionId, comment? }`; hidden
  without it), every `useAdkRuntime` option
  (`getCheckpointId` enables edit/regenerate, `adapters.attachments` makes
  the attach button work), and the model section's `models`,
  `defaultModel`, `thinkingLevels` (default off/low/medium/high),
  `defaultThinkingLevel` (default "medium"), `modelStateDelta`.
- `AssistantScreen` props: `runtime` (required), `artifacts?`,
  `onAuthRequest?: (request) => Promise<AdkAuthCredential>` (open
  `request.authUri`, resolve with `{ authType: "oauth2", oauth2: {
  authResponseUri } }`), `title?="Assistant"`, `welcome?`, `suggestions?`,
  `toolkit?` (your `defineToolkit` tool UIs, merged over the ADK ones),
  `sidebar?`, `actions?` (top bar), `commands?: AssistantCommand[]` (the
  `/` menu), `sources?: AssistantSourcesConfig` (cited sources, below),
  `showModels?` + `modelSettings`
  (required together; TypeScript enforces it).
- **Cited sources** (`sources`): the agent's tools return passages that each
  carry a `ref` ("S3"), and the agent cites them inline as `[S3]` (or
  `[S1, S3]`). `sources.fromToolCall(toolName, result)` turns a finished tool
  call's result into `AssistantSource`s (`{ ref, document: { key, title,
  subtitle?, href? }, location?, excerpt? }`). Citations render as numbered
  chips (numbered per answer, in citation order) that open the passage on
  hover or press; `MessageSources` lists the cited documents under the
  finished answer, then the other documents its searches found. The tool
  results stream in the same message as the text, so nothing else is sent,
  and a reopened conversation shows its sources again. A ref no tool
  returned stays plain text, and code is never touched. Give each passage a
  stable ref on the agent side (an ADK `after_tool_callback`), and tell the
  model the citation format in its instruction.
- **Model section** (`showModels`): the agent's name (the active ADK agent,
  else `title`), the model picker when `models` has more than one, and the
  thinking level. While it's shown, every run sends the selection as ADK
  `stateDelta` — `{ model, thinking_level }` by default — which the agent
  applies in a `before_model_callback` (set `llm_request.model` and
  `llm_request.config.thinking_config`). Nothing is sent without
  `showModels`.
- **Slash commands** (`commands`): `{ id, description?, icon?, prompt?,
  send?, execute? }`. `execute(aui)` runs in the app (e.g.
  `aui.threads.switchToNewThread()`) and removes the typed `/id`; `prompt`
  fills the composer (and sends it with `send`); neither leaves `/id ` for
  arguments and the agent receives it as text. The menu only opens for a
  `/` at the start of the message.
- Rendered automatically: `adk_request_confirmation` → Approve/Deny card,
  `adk_request_credential` → sign-in card, `adk_request_input` → question
  card (choices from an enum/boolean `response_schema`), `transfer_to_agent`
  → active agent in the top bar plus author labels, `escalate` → banner,
  `artifactDelta` → artifacts menu.
- Parts for custom layouts: `AssistantCommandMenu`, `AdkAgentIndicator`, `AdkArtifactsMenu`,
  `AdkEscalationBanner`, `AdkMessageAuthor`, `AdkModelSection`,
  `AdkConfirmationUI`, `AdkAuthRequestUI`, `AdkInputRequestUI`,
  `AdkToolFallback`, `adkToolkit`. They read `AssistantSettingsContext`, so
  render them inside `AssistantScreen` or provide the context yourself.
- `Thread` (`@/components/assistant-ui/elements/thread.aui`) takes
  `components` overrides, including Forge's `MessageHeader` (above each
  assistant message), `ComposerActions` (after the attach and mic
  buttons) and `ComposerTriggers` (trigger popovers such as
  `ComposerTriggerPopover` for `/` commands or `@` mentions), plus a
  `placeholder` prop. The mic shows whenever the runtime has a dictation adapter; the
  recognised words stream into the input, and nothing sends until the user
  does.
- Limits: consecutive assistant messages merge, so a hand-off within one
  turn shows as one reply; ADK sessions have no titles (conversations are
  named after their first message while the page is open).

## shell (`shell/index.ts`) — dynamic app shell

The Forge shell drawn from a store that pages change. Root:

```tsx
<ShellProvider initial={{ rail: { items, footer } }} currentPath={pathname} navigate={navigate}>
  <ShellLayout brand={…}>{routes}</ShellLayout>
</ShellProvider>
```

- `ShellProvider` — `initial?: ShellConfig`, `currentPath?` (items whose
  `href` matches, as a whole path prefix, are active; the longest wins),
  `navigate?(href)` (client routing; otherwise items render as links),
  `renderLink?(href) → element` (router links, e.g. `<Link to={href} />`).
- `ShellLayout` — `brand?` (sidebar brand row), `mark?` (default `AppMark`
  going to the first rail item), `bare?` (don't wrap in `PageContent`),
  `className?` (on `AppShell`). Renders rail, sub nav sections, top bar
  (breadcrumbs, `TopbarPage` title, actions slot), toolbar/footer slots.
- `ShellConfig` — `rail: { items, footer }`, `sidebar: { hidden, label,
  sections: { id, title?, action?, variant?, items }[] }`,
  `header: { title, icon, breadcrumbs: { label, icon?, href?, onSelect? }[] }`,
  `active: { rail?, sidebar? }` (ids; override path matching). Items:
  `{ id, label, icon?, href?, onSelect?, meta?, items? (sub-list),
  permission?, role? }` — `permission`/`role` hide items for users without
  them when a `UserAccessProvider` is present.
- Pages: `useShellPage(config)` / `<ShellPage {...config} />` — applied
  while mounted, merged key by key over the base; deeper components win;
  inline configs are fine (compared by data; handlers stay current).
- Slots (page JSX in the shell, keeping the page's state):
  `ShellHeaderActions`, `ShellToolbar` (a `ViewToolbar`), `ShellFooter` (a
  `WorkspaceFooter`), `ShellSidebarTop`, `ShellSidebarBottom`; generic
  `ShellSlot name=…`.
- Anywhere: `useShellApi()` → `configure(config | (shell) => config)`,
  `updateItem(id, patch)` (base items; page items follow page state),
  `setActive({ rail?, sidebar? })`; `useShell(selector)` reads the
  displayed shell.

## Tuned primitives (`@/components/ui/<name>`)

32 files: alert, avatar, badge, breadcrumb, button, checkbox, collapsible,
command, context-menu, dialog, dropdown-menu, empty, field, input-group, input, kbd,
label, native-select, popover, progress, select, separator, sheet, skeleton,
spinner, table, tabs, textarea, toast, toggle-group, toggle, tooltip.

What differs from stock shadcn:

- **button** — 7px radius, 12px/450 labels. Variants `default` (dark
  primary), `outline`, `secondary`, `ghost`, `destructive` (tinted), `link`;
  sizes `default` (h-8), `xs`, `sm`, `lg`, `icon`, `icon-xs`, `icon-sm`,
  `icon-lg`. `buttonVariants` exported.
- **badge** — 4px radius; variants default, secondary, destructive,
  outline, ghost, link; `render` prop.
- **alert** — adds `variant="error"` and `AlertAction`.
- **kbd** — `variant?: "default" | "ghost" | "outline"`.
- **dialog** — 16px corners; `DialogContent showCloseButton?=true`,
  `DialogFooter showCloseButton?=false`.
- **sheet** — floats 12px from the edges with 16px corners;
  `side?="right"`, `showCloseButton?=true`. Override width with the same
  `data-[side=right]:` selector the base uses.
- **tabs** — `TabsList variant?: "default" | "line"`.
- **toggle / toggle-group** — variants default, outline; sizes default,
  sm, lg; group values are arrays.
- **command** — cmdk palette on the same light menu surface (9px band,
  32px/12px items, 11px muted group headings, accent highlight).

  ```tsx
  <CommandDialog open={open} onOpenChange={setOpen}>  {/* title/description are sr-only props */}
    <Command>
      <CommandInput placeholder="Search commands…" />
      <CommandList>
        <CommandEmpty>No commands found.</CommandEmpty>
        <CommandGroup heading="Tasks">
          <CommandItem onSelect={createTask}>
            <Icon icon="plus" /> New task <CommandShortcut>N</CommandShortcut>
          </CommandItem>
        </CommandGroup>
        <CommandSeparator />
      </CommandList>
    </Command>
  </CommandDialog>
  ```

  `CommandDialog` sits 20% from the top, 560px wide, with a 48px search row.
  Inline `Command` (e.g. inside a `Popover`) has no border of its own; the
  container provides it. Items always go inside a `CommandGroup`; filtering
  is cmdk's fuzzy match on the item text (pass `value` / `keywords` to
  tune it); `disabled` dims an item; `data-checked` on an item shows the
  tick. Wire ⌘K yourself (a `keydown` listener that opens the dialog).
- **select** — the dropdown for every choice list. `Select` (`items`,
  `value` / `defaultValue`, `onValueChange`, `disabled`) > `SelectTrigger`
  (`size?: "sm" | "default"`; put the field's `id` here) > `SelectValue`;
  `SelectContent` (pass `alignItemWithTrigger={false}` to drop below the
  trigger like other menus; `align`, `side`) > `SelectGroup` > `SelectItem`
  (`value`); `SelectLabel`, `SelectSeparator`. Typed values:
  `<Select<number> …>`.
- **input, textarea, checkbox, input-group** — bordered on the page
  background, 7px radius. `InputGroupAddon align`, `InputGroupButton size`.
- **native-select** — the browser's `<select>`, kept for existing apps;
  don't use it for new UI (use `Select`).
- **dropdown-menu, context-menu, popover, select content** — light 9px
  surface, ≥180px, 32px/12px items; items take `variant?: "default" |
  "destructive"` and `inset?`; `DropdownMenuContent` defaults to
  `align="start"`.
- **tooltip** — dark and compact; `TooltipProvider delay` defaults to 0
  (the shell sets 250).
- **toast** — bottom-centre. `<Toaster>` wraps the app once;
  `toast.add({ title, description?, type })` with `type` success, info,
  warning, error or loading; also `toast.close(id?)`, `toast.update(id, …)`,
  `toast.promise(promise, …)`.
- **field** — `Field` (`data-invalid`, `data-disabled`), `FieldLabel`,
  `FieldDescription`, `FieldError`, `FieldGroup`, `FieldSet`,
  `FieldLegend`, `FieldContent`, `FieldTitle`, `FieldSeparator`.
