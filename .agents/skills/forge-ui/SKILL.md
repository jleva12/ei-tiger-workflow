---
name: forge-ui
description: How to build screens and components with the Forge UI design system — the Forge Workspace look (quiet light surfaces, compact 12–14px type, small radii, pastel status bands, one dark primary action) delivered as shadcn/ui primitives on Base UI in `@/components/ui`, Forge composites in `@/components/forge` (app shell, sidebar, toolbar, task lists, kanban, sheets, detail pages, run logs, diffs, data table, empty/error states, a full-screen Google ADK AI assistant), theme tokens as Tailwind utilities and the `Icon` vocabulary. Use this skill whenever you create or change a page, layout, form, table, dialog, sheet, list, empty/loading/error state, badge or any styled UI in an app that uses these components, or pick colours, spacing, type sizes or icons, or build an AI assistant / chat screen — even for a "quick" UI tweak, and before reaching for raw Tailwind colours or new components.
---

# Building UI with the Forge UI design system

Forge UI is the design language of the Forge Workspace: a quiet, light product
interface — pale cool-neutral surfaces, compact 12–14px type, 4–9px radii,
full-width pastel status bands and one dark primary action per view. Screens
look right when they're assembled from the existing pieces and tokens; they
drift the moment code reaches for raw colours, default Tailwind sizes or
home-made versions of components that already exist.

## Choosing what to use

Work down this list and stop at the first thing that fits:

1. **A Forge composite** (`@/components/forge/*`) — whole regions and patterns:
   shell, sidebar, toolbar, task list, board, sheet form, detail page,
   empty/error states. Inventory below; props in
   [references/components.md](references/components.md).
2. **A tuned primitive** (`@/components/ui/*`) — Button, Input, Field,
   Dialog, Sheet, DropdownMenu, Tabs, Badge, Tooltip, Toast… These are
   shadcn components on **Base UI** (not Radix), restyled for Forge.
3. **Plain elements styled with tokens** — only for genuinely new UI; use
   the token utilities (below), `cn` from `"cn"`, and `Icon`.

Check what's installed before assuming: `ls src/components/forge
src/components/ui`. In a consuming app, a missing piece is added with
`npx shadcn@latest add @forge-ui/<item>` (the `shadcn` skill covers the CLI).
Don't fork a primitive to restyle it for one screen; compose around it.

## Screen skeleton

Most screens live inside the app shell. The nesting, with real names:

```tsx
<AppShell>
  <SkipLink href="#content">Skip to content</SkipLink>
  <IconRail>
    <AppMark aria-label="Home" />
    <RailNav aria-label="Shortcuts">
      <RailButton icon="home" label="Overview" active />
    </RailNav>
    <RailFooter>…</RailFooter>
  </IconRail>
  <WorkspaceSidebar>
    <SidebarBrand>
      <WorkspaceOrb /> <SidebarBrandName name="Forge" suffix="Workspace" />
      <SidebarCollapseButton />
    </SidebarBrand>
    <SidebarSection variant="primary">
      <CommandButton onClick={openCommand} />
      <NavItem icon="home" active meta={2}>Home</NavItem>
    </SidebarSection>
  </WorkspaceSidebar>
  <MainPanel>
    <Topbar>
      <TopbarBreadcrumb>
        <TopbarCrumb icon="task" onClick={goToTasks}>Tasks</TopbarCrumb>
        <TopbarCrumbSeparator />
        <TopbarPage icon="dashboard">Coding tasks</TopbarPage> {/* the page's h1 */}
      </TopbarBreadcrumb>
      <TopbarActions>
        <SearchField value={q} onChange={(e) => setQ(e.target.value)} />
        <PrimaryAction onClick={create}>Task</PrimaryAction>
      </TopbarActions>
    </Topbar>
    <ViewToolbar>
      <Tabs value={view} onValueChange={(v) => setView(String(v))}>
        <ViewTabsList aria-label="Views">
          <ViewTabsTrigger value="list" icon="list">List</ViewTabsTrigger>
        </ViewTabsList>
      </Tabs>
      <ToolbarFilters>…</ToolbarFilters>
    </ViewToolbar>
    <PageContent id="content">{/* the scrolling body */}</PageContent>
    <WorkspaceFooter>…</WorkspaceFooter>
  </MainPanel>
</AppShell>
```

When the rail, sub nav or header should change from page to page, don't
hand-write this skeleton in every page: use the dynamic shell
(`@/components/forge/shell`) — `ShellProvider` + `ShellLayout` at the root,
`useShellPage` and the slot components in pages. See
[references/components.md](references/components.md#shell-shellindexts--dynamic-app-shell).

Imports: shell pieces from `@/components/forge/app-shell`, rail from
`icon-rail`, sidebar from `workspace-sidebar`, view tabs / search / layout
switch / toolbar buttons from `toolbar`. `AppShell` also provides the
tooltip delay and the sidebar context — `WorkspaceSidebar`,
`SidebarCollapseButton` and `Topbar` throw outside it (use
`<Topbar sidebarTrigger={false}>` if you must render one standalone).

A detail view replaces toolbar, content and footer with `TaskPage`
(`@/components/forge/task-page`): `TaskPageHeader` → `TaskPageEyebrow` +
`TaskTitleRow`, then `Tabs` → `TaskTabBar` → `TaskTabsList` →
`TaskTabsTrigger`, and `TabsContent` holding `SubmissionLayout`,
`RunLogLayout`, `DiffWorkspace`, `MetricsTable`, … A single-column page
(settings, activity) uses `WorkspaceView` + `ViewHeading` from
`@/components/forge/activity` inside `PageContent`.

## What exists (by need)

| Need | Use | From `@/components/forge/…` |
| --- | --- | --- |
| Grouped task rows with pastel status bands | `TaskList > TaskGroup > TaskTable` (+ `TaskListSkeleton`) | `task-list` |
| Board / columns | `TaskBoard` (data-driven) or `KanbanBoard > KanbanColumn > BoardCard` | `kanban` |
| Big generic data grid (sort, filter, group, edit, virtualize, export) | `DataTable` + `createColumnHelper` | `data-table` |
| Numeric report tables | `MetricsTable > MetricsBody > MetricsRow`, `MetricValue` | `metrics-table` |
| Create / edit form in a side panel | `Sheet` + `TaskSheetContent > TaskSheetHeader`, `TaskForm`, `FormColumns`, `FormFooter` | `task-sheet` |
| Read-only key/value facts | `DetailList > DetailRow`, `RecordList > RecordRow`, `AsideSection > AsideFact` | `task-sheet`, `task-page` |
| Status | `StatusBadge`, `StatusSymbol`, `Chip`, `CountBadge`, `ConnectionDot`, `RunStateIcon` | `status` |
| People / agents | `AgentAvatars`, `AgentOrb`, `WorkspaceOrb` | `avatars` |
| Empty states | `PageEmpty` (page), `PanelEmpty` (inside a panel), `EmptyWorkspace` (first run) | `empty-state` |
| State illustrations (empty, waiting, error, no results, offline, locked, …) | `Illustration name=…`, or the `illustration` prop on the empty states and `LogEmpty` | `illustration` |
| Errors, notices, filters, pagination | `ErrorCallout`, `PreviewBanner`, `ActiveFilters`, `LoadMore` | `feedback` |
| Settings page rows (title + description, current value, one action) | `SettingsList > SettingsRow` | `settings-list` |
| Connect third-party apps over OAuth (GitHub, Jira, Figma, Slack…) | `SettingsList > IntegrationRow` with a logo from `brand-logos` | `integrations` |
| Timelines and dashboards | `EventList > EventItem`, `StatGrid > Stat`, `ViewHeading`, `StatusHeading`, `Footnotes` | `activity` |
| Logs, diffs, review results | `run-log`, `changes`, `handoff` families | as named |
| An app shell whose rail, sub nav and header change per page | `ShellProvider` + `ShellLayout`, pages call `useShellPage({ header, sidebar, active })` and render `ShellHeaderActions` / `ShellToolbar` / `ShellFooter`; `useShellApi()` for app-wide changes | `shell` |
| Command palette or a searchable action list | `CommandDialog > Command > CommandInput, CommandList > CommandGroup > CommandItem` | `@/components/ui/command` |
| Display settings (theme, density, text size, contrast, motion) | `PreferencesPanel` (+ `PreferenceRow`, `PreferenceOptions` for app preferences); state from `useUserPreference` | `preferences`, `@/lib/user-preferences` |
| Show, hide or disable UI by role or permission (Casbin) | `Guard` (`fallback` → `PageEmpty`/`PanelEmpty illustration="locked"`), `useGuard`, `useCan` | `@/lib/user-access-provider`, `@/lib/user-access` |
| AI assistant / chat for a Google ADK agent (conversations, tool approvals, sign-in, hand-offs, model and thinking picker, voice input, / commands, reply feedback) | `AssistantScreen` (full screen) or `AssistantModal` (floating launcher on any page) + `useAdkAssistant` (`showModels` for the composer's model section) | `assistant` |
| Around an agent's conversation: task progress, plan / notes / canvas panels, response and context stats, chat history palette, follow-ups, tools menu, cards for tools | `useAgentWorkspace` + `ProgressDock`, `PlanPanel`, `NotesPanel`, `CanvasPanel`, `ContextMeter`, `MessageStats`, `ChatHistoryPalette`, `ToolsMenu`, `toolUIs` in `AssistantScreen`'s slots | `agent-workspace` |
| Rich text (formatted descriptions, comments, documents, Markdown or HTML values) | `RichTextEditor` (`preset`, `format`), `RichTextView` | `rich-text-editor` |

Primitives worth knowing: `Button` (variants `default` = the dark primary,
`outline`, `secondary`, `ghost`, `destructive` (tinted, not solid), `link`;
sizes `default` h-8, `xs`, `sm`, `lg`, `icon`, `icon-xs`, `icon-sm`,
`icon-lg`), `Field` family, `Input`, `Textarea`, `Select` (every dropdown),
`Checkbox`, `InputGroup`, `Dialog`, `Sheet`, `DropdownMenu`, `ContextMenu`,
`Command` (cmdk palette; `CommandDialog` behind ⌘K),
`Popover`, `Tooltip`, `Tabs` (`variant="line"` on `TabsList`), `ToggleGroup`,
`Badge`, `Kbd`, `Alert` (`variant="error"`), `Skeleton`, `Spinner`,
`Progress`, `Table`, toasts.

## Forms

```tsx
<Field data-invalid={Boolean(errors.name)}>
  <FieldLabel htmlFor="name">Workspace name</FieldLabel>
  <Input id="name" aria-invalid={Boolean(errors.name)} value={name} onChange={…} />
  <FieldDescription>Shown in the sidebar.</FieldDescription>
  {errors.name && <FieldError>{errors.name}</FieldError>}
</Field>
```

Dropdowns are always the custom `Select`, which opens the same light menu
surface as every other Forge menu. Don't use `NativeSelect` or a raw
`<select>`: the browser's own picker breaks the look (`NativeSelect` is
still in the library only for apps that already use it).

```tsx
const plans = [
  { value: "default", label: "Default coding workflow" },
  { value: "review", label: "Review-heavy · 4 agents" },
]

<Field>
  <FieldLabel htmlFor="plan">Agent plan</FieldLabel>
  <Select items={plans} value={plan} onValueChange={(value) => value && setPlan(value)}>
    <SelectTrigger id="plan" className="w-full">
      <SelectValue />
    </SelectTrigger>
    <SelectContent alignItemWithTrigger={false}>
      {plans.map((p) => (
        <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>
      ))}
    </SelectContent>
  </Select>
</Field>
```

`items` is what lets `SelectValue` show the label instead of the raw value;
`alignItemWithTrigger={false}` drops the list below the trigger like the
other menus (every Select in the library does this); the `id` goes on
`SelectTrigger` so the label names it; `onValueChange` can pass `null`.
Use `defaultValue` instead of `value` for uncontrolled forms, and
`<SelectTrigger size="sm">` in toolbars.

Group fields with `FieldGroup` (or `FieldSet` + `FieldLegend`). Inside a
sheet, wrap them in `TaskForm` (it tightens the fields to the sheet's
density) with a `FormFooter` holding the buttons. Submit buttons show
progress with `<Spinner data-icon="inline-start" />` and `disabled` while
saving. Put a form-level failure in an `ErrorCallout` above the fields.

## Loading, empty, error — every data view has all three

| State | Show |
| --- | --- |
| Loading | `TaskListSkeleton` for task lists; `Skeleton` blocks shaped like the content; `DataTable isLoading` |
| Empty | `PageEmpty` / `PanelEmpty` with a one-line explanation and, if there's a next step, an action; add the matching `illustration` (`search` for no results, `waiting` before a run, `offline` when unreachable…) |
| Error | `ErrorCallout title="…" action={<Button variant="outline" size="sm" onClick={() => refetch()}>Retry</Button>}>{error.message}</ErrorCallout>` |
| Background failures, saved/failed actions | a toast: `toast.add({ title, description, type: "success" \| "error" \| "info" \| "warning" \| "loading" })` from `@/components/ui/toast` (needs `<Toaster>` wrapping the app) |

With the data layer (`forge-data` skill), failed mutations and background
refetches already toast; render only first-load errors inline.

## Tokens — never raw colours

Colours, sizes, radii and shadows are Tailwind utilities generated from
`src/index.css`. Use them instead of hex values, `rgb()`, arbitrary colours
(`bg-[#…]`) or the default palette (`text-gray-500`, `bg-blue-600`), which
don't follow light/dark or the Forge look. The ones you'll use most:

- **Text:** `text-foreground` (primary), `text-muted-foreground`,
  `text-subtle` (quietest), `text-destructive`
- **Surfaces / lines:** `bg-background`, `bg-card`, `bg-muted`,
  `bg-accent`, `border-border`, `border-input`
- **Status and tones:** `bg-status-<neutral|queued|running|review|success|failed>`
  (+ `-foreground`), `bg-tone-<neutral|amber|pink|green|red>` (+ `-foreground`),
  `text-signal-<running|success|failed>`, `bg-danger-surface`,
  `bg-warning-surface`, `bg-success-surface`, `bg-notice-surface`
- **Type:** body is 13px; `text-sm` is the 14px UI size, `text-xs` 12px;
  below that `text-2xs` (11px), `text-3xs` (10px), `text-4xs` (9px); one-off sizes in rem, never px, so the user's text-size preference reaches them
- **Radii / elevation:** `rounded-(--radius-control)` (7px, controls),
  `rounded-(--radius-chip)`, `rounded-(--radius-item)`,
  `rounded-(--radius-band)`, `rounded-(--radius-card)`,
  `rounded-(--radius-dialog)`; `shadow-(--shadow-float)`,
  `shadow-(--shadow-raised)`, `shadow-(--shadow-paper)`

Full list and the tone/status model: [references/tokens.md](references/tokens.md).

Tones cascade: `TaskGroup` sets `--tone` / `--tone-foreground` and children
paint with `bg-(--tone)` / `text-(--tone-foreground)`. To tone your own
region, add the class string from `toneVars` in `@/components/forge/variants`
— `className={cn(toneVars[tone], "bg-(--tone)")}` — rather than building
`[--tone:…]` classes from template strings (Tailwind only generates classes
it can see literally, and `toneVars` spells them out).

## Responsive: container queries, not viewport breakpoints

The shell is `@container/shell`, and regions respond to the shell's own
width with `@max-[1050px]/shell:`-style variants at 1700 / 1270 / 1050 /
800 / 600px. Use those instead of `md:`/`lg:` inside the shell. Portaled
UI (dialogs, sheets, menus, tooltips) is outside the container, so there
use viewport variants (`max-[600px]:`). `DataTable` is its own
`@container/data-table`.

## Icons

```tsx
import { Icon } from "@/components/forge/icon"
<Icon icon="branch" />                  // vocabulary name (17px, 1.6 stroke)
<Icon icon={PaintBoardIcon} />          // any glyph from @hugeicons/core-free-icons
<Button><Icon icon="plus" data-icon="inline-start" />New task</Button>
```

Every Forge `icon` prop takes a name or a glyph. Inside controls, mark icons
`data-icon="inline-start"` / `"inline-end"` and let the control size them
(don't pass `size`). Names: home, search, bell, folder, task, team,
dashboard, layers, settings, sidebar, command, list, kanban, activity,
calendar, clock, sun, moon, down, up, left, right, external, plus,
addCircle, refresh, sort, filter, close, more, link, copy, download, view,
grip, table, pin, stop, branch, commit, review, code, file, robot, message,
comment, sparkles, coins, check, completed, failed, loading, info, warning.
Icon-only buttons (`size="icon"` / `"icon-xs"`) always get an `aria-label`.

## Base UI, not Radix

These primitives are Base UI underneath, so a few habits from Radix-based
shadcn are wrong here:

- No `asChild`. Use `render`: `<DropdownMenuTrigger render={<ToolbarButton icon="filter" value="Status" />}>Filter</DropdownMenuTrigger>`, `<TooltipTrigger render={<Button … />}>`.
- A `Button` rendered as a link needs `nativeButton={false}`: `<Button nativeButton={false} render={<a href="/docs" />}>Docs</Button>`. (`NavItem` and `Badge` take `render={<a …/>}` directly.)
- `ToggleGroup` values are arrays (`value={[view]}`); Tabs and menu radio values are loosely typed — wrap with `String(value)`.
- Menu radio/checkbox items don't close the menu unless you pass `closeOnClick`; `DropdownMenuLabel` must sit inside a `DropdownMenuGroup`.
- `Select` needs its `items` prop for `<SelectValue />` to show the label.
- Open/active styles key off Base UI data attributes (`data-open`, `data-active`, `data-panel-open`).

## Details that are easy to get wrong

- Headings: `TopbarPage` renders an `<h1>` (the page name in the topbar),
  and so do `ViewHeading` and `TaskTitleRow` (the big title in the body).
  The original app pairs them, so a shell page with a body title has two
  `<h1>`s — that's the library's known behaviour, not your bug; don't try
  to work around it, and don't add a third. Section titles below them are
  `<h2>` (`PageSection`, `WaitingNotice`) or `<h3>` (`AsideSection`).
- `ToolbarFilters` hides its **last** child at ≤600px and pushes it right at
  ≤800px — put the least important control last. `WorkspaceFooter` pushes
  its **second** child right.
- `TaskTable` is at least 760px wide and relies on `PageContent` to scroll;
  `TaskPage` is its own scroll container.
- Focus rings are global (a 2px `--focus` outline, deliberately beating
  `outline-none`); don't restyle focus per component. Give scrollable
  regions `tabIndex={0}`.
- Dropdowns: `Select`, never `NativeSelect` or `<select>` (see Forms).
- `DataTable`: define `columns` at module scope or in `useMemo`, pass
  `getRowId`, and handle `onCellEdit` / `onRowReorder` yourself (it never
  mutates data).
- Embedding `AppShell` in a smaller frame: override
  `className="h-[640px] min-w-0"` (it's `h-dvh min-w-[360px]` by default).

## Examples in this repo

The demo app (`npm run dev`, routes by hash) shows every piece in use; read
the matching file before building something similar:

| Topic | File |
| --- | --- |
| Whole workspace (shell, sidebar, toolbar, list/board, sheet) | `src/demo/workspace-preview.tsx` |
| Detail pages: task, submission, run log, changes, handoff, metrics | `src/demo/detail-previews.tsx` |
| Create-task sheet form | `src/demo/create-task-sheet.tsx` |
| Buttons, forms, menus, overlays, tabs, badges, feedback, toasts | `src/demo/sections/primitives.tsx` |
| Colours, type, radii, icons | `src/demo/sections/foundations.tsx` |
| Navigation, task list, kanban, empty states, activity, sheets | `src/demo/sections/workspace.tsx` |
| DataTable configurations | `src/demo/sections/data-table.tsx` |
| ADK assistant against a simulated agent (every ADK feature) | `src/demo/assistant-preview.tsx`, `src/demo/assistant-mock.ts` |
| Agent workspace (plan, progress, notes, citations) against a scripted agent | `src/demo/sections/agent-workspace.tsx`, `src/demo/agent-workspace-mock.ts` |
| Rich text editor presets, Markdown value, mentions, read-only view | `src/demo/sections/rich-text-editor.tsx` |
| A small app on the dynamic shell (per-page sub nav, header, slots, access) | `src/demo/dynamic-shell-preview.tsx` |

In a consuming app the demo isn't installed; the patterns above and
[references/components.md](references/components.md) carry the same
information.
