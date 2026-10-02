#!/usr/bin/env node
/**
 * Generates registry.json for the Forge UI design system.
 *
 * - The `theme` item is derived from src/index.css so tokens, utilities and
 *   global rules never drift from what the demo renders.
 * - `ui` items ship the tuned shadcn primitives from src/components/ui.
 * - Forge items ship src/components/forge, installed under @components/forge/.
 *
 * Run `npm run registry:build` to regenerate registry.json and public/r.
 */
import { readdirSync, readFileSync, writeFileSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"))
// The registry is served from the private monorepo's raw files, under
// packages/forge-ui; see "Use it in another project" in the README.
const homepage =
  process.env.REGISTRY_HOMEPAGE ??
  "https://github.com/jleva12/ei-tiger-workflow/tree/main/packages/forge-ui"
// Where apps fetch items from; the `base` item writes it into a new app's
// components.json. Override to host the same files somewhere else.
const registryUrl =
  process.env.REGISTRY_URL ??
  "https://raw.githubusercontent.com/jleva12/ei-tiger-workflow/main/packages/forge-ui/public/r/{name}.json"

/* -------------------------------------------------------------------------- */
/* Minimal nested CSS parser (enough for index.css)                           */
/* -------------------------------------------------------------------------- */

function parseBlock(source, start = 0) {
  const node = {}
  let buffer = ""
  let index = start
  while (index < source.length) {
    const char = source[index]
    if (char === "{") {
      const key = buffer.trim()
      const [child, next] = parseBlock(source, index + 1)
      node[key] = { ...(node[key] ?? {}), ...child }
      buffer = ""
      index = next
      continue
    }
    if (char === "}") {
      addStatement(node, buffer)
      return [node, index + 1]
    }
    if (char === ";") {
      addStatement(node, buffer)
      buffer = ""
      index += 1
      continue
    }
    buffer += char
    index += 1
  }
  addStatement(node, buffer)
  return [node, index]
}

function addStatement(node, raw) {
  const statement = raw.trim()
  if (!statement) return
  if (statement.startsWith("@")) {
    node[statement] = {}
    return
  }
  const colon = statement.indexOf(":")
  node[statement.slice(0, colon).trim()] = statement.slice(colon + 1).trim()
}

const css = readFileSync(resolve(root, "src/index.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  ""
)
const [sheet] = parseBlock(css)

const isColor = (value) => /^(oklch|oklab|rgb|hsl|#)/.test(value)

function splitVars(block = {}) {
  const colors = {}
  const other = {}
  for (const [key, value] of Object.entries(block)) {
    if (key.startsWith("--") && isColor(value)) colors[key.slice(2)] = value
    else other[key] = value
  }
  return { colors, other }
}

const light = splitVars(sheet[":root"])
const dark = splitVars(sheet[".dark"])
// shadcn keeps the base radius alongside the light colours.
light.colors.radius = sheet[":root"]["--radius"]
delete light.other["--radius"]

const themeVars = Object.fromEntries(
  Object.entries(sheet["@theme inline"]).map(([key, value]) => [
    key.replace(/^--/, ""),
    value,
  ])
)

const skipTopLevel = new Set([
  '@import "tailwindcss"',
  '@import "tw-animate-css"',
  '@import "shadcn/tailwind.css"',
  "@custom-variant dark (&:is(.dark *))",
  ":root",
  ".dark",
  "@theme inline",
])
// `shadcn add` never overwrites CSS variables a project already defines (only
// `init`/`apply` do), so the shadcn semantic tokens a preset sets are mirrored
// into `css`, whose declarations do replace existing values.
const semantic = (colors) =>
  Object.fromEntries(
    Object.entries(colors)
      .filter(
        ([key]) =>
          sheet[":root"][`--${key}`] !== undefined && isShadcnToken(key)
      )
      .map(([key, value]) => [`--${key}`, value])
  )
function isShadcnToken(key) {
  return /^(background|foreground|card|popover|primary|secondary|muted|accent|destructive|border|input|ring|chart-\d|radius|sidebar)(-|$)/.test(
    key
  )
}
const globalCss = {
  ":root": { ...semantic(light.colors), ...light.other },
  ".dark": { ...semantic(dark.colors), ...dark.other },
}
for (const [key, value] of Object.entries(sheet)) {
  if (!skipTopLevel.has(key)) globalCss[key] = value
}

/* -------------------------------------------------------------------------- */
/* Items                                                                      */
/* -------------------------------------------------------------------------- */

const version = (name) => {
  const range = pkg.dependencies[name]
  return range ? `${name}@${range}` : name
}
const base = ["@base-ui/react", "class-variance-authority", "cn"].map(version)
const icons = ["@hugeicons/react", "@hugeicons/core-free-icons"].map(version)
const forge = (name) => `@forge-ui/${name}`

/** Tuned shadcn primitives: [name, npm deps, registry deps, description]. */
const ui = [
  ["alert", [], [], "Alert with the Forge error callout variant."],
  ["avatar", [], [], "Avatar primitive."],
  ["badge", [], [], "Badge with 4px chip radius."],
  ["breadcrumb", icons, [], "Breadcrumb primitive."],
  [
    "button",
    [],
    [],
    "Button: 7px radius, 12px/450 labels, colour-only transitions.",
  ],
  [
    "chart",
    [version("recharts")],
    [],
    "Recharts container, tooltip and legend on the chart, data-viz and graph tokens.",
  ],
  ["checkbox", icons, [], "Bordered 4px checkbox."],
  ["collapsible", [], [], "Collapsible primitive."],
  [
    "command",
    [...icons, version("cmdk")],
    ["dialog", "input-group"],
    "cmdk command palette on the light 9px menu surface: 32px/12px items, quiet group headings and a dialog variant.",
  ],
  [
    "context-menu",
    icons,
    [],
    "Right-click menu on the light 9px menu surface, 32px/12px items.",
  ],
  ["dialog", icons, ["button"], "Dialog primitive."],
  [
    "dropdown-menu",
    icons,
    [],
    "Light 180px menus, 9px radius, 32px/12px items.",
  ],
  ["empty", [], [], "Empty state primitive."],
  ["field", [], ["label", "separator"], "Field, FieldGroup and friends."],
  ["input", [], [], "Bordered input on the page background."],
  [
    "input-group",
    [],
    ["button", "input", "textarea"],
    "Input group with addons.",
  ],
  ["kbd", [], [], "Keyboard key with default, ghost and outline variants."],
  ["label", [], [], "Label primitive."],
  ["native-select", icons, [], "Bordered native select."],
  [
    "popover",
    [],
    [],
    "Popover with the band radius, hairline border and 12px text.",
  ],
  ["progress", [], [], "Progress primitive."],
  [
    "select",
    icons,
    [],
    "Select with a bordered trigger and the light 9px menu surface.",
  ],
  ["separator", [], [], "Separator primitive."],
  ["sheet", icons, ["button"], "Sheet primitive."],
  ["skeleton", [], [], "Skeleton primitive."],
  ["spinner", icons, [], "Spinner primitive."],
  [
    "switch",
    [],
    [],
    "On/off switch: a 32 × 18px track, the dark primary when on.",
  ],
  ["table", [], [], "Table primitive."],
  ["tabs", [], [], "Tabs with 7px radius."],
  ["textarea", [], [], "Bordered textarea."],
  [
    "toast",
    icons,
    ["button"],
    "Base UI toast, restyled as a notice at the top centre, under the top bar.",
  ],
  ["toggle", [], [], "Toggle with 7px radius."],
  ["toggle-group", [], ["toggle"], "Toggle group."],
  ["tooltip", [], [], "Dark compact tooltip."],
].map(([name, deps, registryDeps, description]) => ({
  name,
  type: "registry:ui",
  title: name.replace(
    /(^|-)(\w)/g,
    (_, dash, c) => (dash ? " " : "") + c.toUpperCase()
  ),
  description,
  dependencies: [...base, ...deps],
  registryDependencies: registryDeps.map(forge),
  files: [
    {
      path: `src/components/ui/${name}.tsx`,
      type: "registry:ui",
      target: `@ui/${name}.tsx`,
    },
  ],
}))

const forgeFile = (file) => ({
  path: `src/components/forge/${file}`,
  type: "registry:component",
  target: `@components/forge/${file}`,
})

/** Forge components: [name, title, files, registry deps, description]. */
const components = [
  [
    "icon",
    "Icon",
    ["icons.ts", "icon.tsx"],
    [],
    "The workspace icon vocabulary (Hugeicons, 17px, 1.6 stroke).",
  ],
  [
    "status",
    "Status",
    ["variants.ts", "status.tsx"],
    ["badge", forge("icon")],
    "Status badges, band symbols, presence dots, chips and run-state icons.",
  ],
  [
    "avatars",
    "Avatars",
    ["avatars.tsx"],
    ["avatar", "tooltip", forge("icon")],
    "Workspace orb, gradient agent avatars with tooltips, and initials avatars for people.",
  ],
  [
    "app-shell",
    "App shell",
    ["app-shell-context.ts", "app-shell.tsx"],
    ["breadcrumb", "button", "dropdown-menu", "kbd", "tooltip", forge("icon")],
    "Container-query app shell: main panel, topbar, breadcrumb, view toolbar, content and footer.",
  ],
  [
    "icon-rail",
    "Icon rail",
    ["icon-rail.tsx"],
    ["button", "tooltip", forge("icon")],
    "The 56px icon rail with tooltip shortcuts.",
  ],
  [
    "workspace-sidebar",
    "Workspace sidebar",
    ["workspace-sidebar.tsx"],
    [
      "button",
      "kbd",
      "sheet",
      forge("app-shell"),
      forge("icon"),
      forge("status"),
    ],
    "The 224px workspace navigation with brand, nav items, projects and status.",
  ],
  [
    "toolbar",
    "Toolbar",
    ["toolbar.tsx"],
    ["button", "input-group", "kbd", "tabs", "toggle-group", forge("icon")],
    "View tabs, layout switch, search field and toolbar buttons.",
  ],
  [
    "task-list",
    "Task list",
    ["task-list.tsx"],
    [
      "button",
      "collapsible",
      "skeleton",
      "table",
      forge("avatars"),
      forge("icon"),
      forge("status"),
    ],
    "Collapsible pastel status bands and the flat task table.",
  ],
  [
    "kanban",
    "Kanban",
    ["kanban.tsx"],
    [forge("avatars"), forge("status"), forge("task-list")],
    "Kanban board, columns and task cards.",
  ],
  [
    "illustration",
    "Illustration",
    ["illustration.tsx"],
    [forge("icon")],
    "CSS paper illustrations for empty, waiting, error, search, offline and other states.",
  ],
  [
    "empty-state",
    "Empty state",
    ["empty-state.tsx"],
    ["empty", forge("icon"), forge("illustration")],
    "Workspace, panel and page empty states.",
  ],
  [
    "settings-list",
    "Settings list",
    ["settings-list.tsx"],
    [],
    "Divided settings rows: title and description beside a current value and one action.",
  ],
  [
    "integrations",
    "Integrations",
    ["integrations.tsx", "brand-logos.tsx"],
    [
      "button",
      "dropdown-menu",
      "spinner",
      forge("icon"),
      forge("settings-list"),
      forge("status"),
    ],
    "OAuth app connection rows with brand logos, account status and Connect, Manage or Reconnect.",
  ],
  [
    "feedback",
    "Feedback",
    ["feedback.tsx"],
    ["alert", "button", forge("icon")],
    "Error callouts, preview banner, active filters and load more.",
  ],
  [
    "activity",
    "Activity",
    ["activity.tsx"],
    [forge("icon"), forge("status")],
    "Event timeline, stat grid, view headings and footnotes.",
  ],
  [
    "task-sheet",
    "Task sheet",
    ["task-sheet.tsx"],
    ["sheet", forge("icon")],
    "560px create/inspect sheets, compact forms, detail lists and plans.",
  ],
  [
    "task-page",
    "Task page",
    ["task-page.tsx"],
    ["button", "tabs", forge("icon"), forge("status")],
    "Detail page header, line tabs, submission layout, execution plan and notices.",
  ],
  [
    "run-log",
    "Run log",
    ["run-log.tsx"],
    ["button", "input", forge("icon"), forge("illustration"), forge("status")],
    "Job list and the always-dark searchable log console.",
  ],
  [
    "changes",
    "Changes",
    ["changes.tsx"],
    ["input-group", forge("icon")],
    "Changed-file navigator and GitHub-style diff view.",
  ],
  [
    "handoff",
    "Handoff",
    ["handoff.tsx"],
    ["collapsible", "table", forge("icon"), forge("status")],
    "Verification rounds, handoff iterations, verdicts and findings.",
  ],
  [
    "metrics-table",
    "Metrics table",
    ["metrics-table.tsx"],
    ["table"],
    "Grouped numeric tables with bands, subtotals and totals.",
  ],
  [
    "data-table",
    "Data table",
    // Every module in the folder ships; index.ts is the public entry.
    readdirSync(resolve(root, "src/components/forge/data-table"))
      .sort()
      .map((file) => `data-table/${file}`),
    [
      "button",
      "checkbox",
      "context-menu",
      "dropdown-menu",
      "input",
      "input-group",
      "label",
      "popover",
      "select",
      "skeleton",
      "spinner",
      "table",
      forge("icon"),
      forge("illustration"),
      forge("status"),
      forge("timestamps"),
      forge("toolbar"),
    ],
    "Spreadsheet-grade TanStack Table v9 grid: typed filters, multi-sort, grouping and aggregation, column groups, pinning, resizing, drag to reorder or group, range selection with copy/paste, inline editing, virtualization, tree data, cell spanning, row drag, CSV export, context menu, status bar and persisted layouts.",
  ],
].map(([name, title, files, registryDeps, description]) => ({
  name,
  type: "registry:component",
  title,
  description,
  dependencies: [
    ...base,
    ...icons,
    ...(name === "data-table"
      ? [version("@tanstack/react-table"), version("@tanstack/react-virtual")]
      : []),
  ],
  registryDependencies: registryDeps.map((dep) =>
    dep.startsWith("@") ? dep : forge(dep)
  ),
  files: files.map(forgeFile),
}))

const theme = {
  name: "theme",
  type: "registry:theme",
  title: "Forge Workspace theme",
  description:
    "Tokens (colours, tones, statuses, console, diff), Geist, the compact type scale, radii, elevation, orb utilities, focus ring, scrollbars and reduced motion.",
  dependencies: ["@fontsource-variable/geist", "tw-animate-css"],
  cssVars: { theme: themeVars, light: light.colors, dark: dark.colors },
  css: globalCss,
}

const themeProvider = {
  name: "theme-provider",
  type: "registry:component",
  title: "Theme provider",
  description: "Light/dark/system theme provider with a `d` shortcut.",
  files: [
    {
      path: "src/components/theme-provider.tsx",
      type: "registry:component",
      target: "@components/theme-provider.tsx",
    },
  ],
}

const apiClient = {
  name: "api-client",
  type: "registry:lib",
  title: "API client",
  description:
    "Axios wrapper: bearer auth, single-flight token refresh on 401, retries with backoff, and one ApiError type.",
  dependencies: [version("axios")],
  files: ["client.ts", "errors.ts", "index.ts"].map((file) => ({
    path: `src/lib/api/${file}`,
    type: "registry:lib",
    target: `@lib/api/${file}`,
  })),
}

const queryClient = {
  name: "query-client",
  type: "registry:lib",
  title: "Query client",
  description:
    "TanStack Query client for @forge-ui/api-client: errors typed as ApiError, Retry-After-aware retries and one error-notice hook.",
  dependencies: [version("@tanstack/react-query")],
  registryDependencies: [forge("api-client")],
  files: [
    {
      path: "src/lib/api/query-client.ts",
      type: "registry:lib",
      target: "@lib/api/query-client.ts",
    },
  ],
}

const resource = {
  name: "resource",
  type: "registry:lib",
  title: "Resource",
  description:
    "createResource and createNestedResource: typed CRUD hooks for REST collections, with cache updates, optional optimistic updates and infinite lists.",
  dependencies: [version("@tanstack/react-query")],
  registryDependencies: [forge("query-client")],
  files: [
    {
      path: "src/lib/api/resource.ts",
      type: "registry:lib",
      target: "@lib/api/resource.ts",
    },
  ],
}

const contextStore = {
  name: "context-store",
  type: "registry:lib",
  title: "Context store",
  description:
    "createContextStore: a Zustand store per React Provider, seeded from props or context, with typed selector hooks.",
  dependencies: [version("zustand")],
  files: [
    {
      path: "src/lib/context-store.tsx",
      type: "registry:lib",
      target: "@lib/context-store.tsx",
    },
  ],
}

const userPreferences = {
  name: "user-preferences",
  type: "registry:lib",
  title: "User preferences",
  description:
    "UserPreferencesProvider and hooks: density, text size, contrast, motion and app-specific preferences, saved in the browser and applied to the page.",
  dependencies: [version("zustand")],
  // The density, contrast and motion rules it switches live in the theme,
  // which apps get from `theme`, `all` or `base`. Depending on `theme` here
  // would make `base` merge it over the verbatim index.css.
  registryDependencies: [forge("context-store")],
  files: ["user-preferences.ts", "user-preferences-provider.tsx"].map(
    (file) => ({
      path: `src/lib/${file}`,
      type: "registry:lib",
      target: `@lib/${file}`,
    })
  ),
}

const userAccess = {
  name: "user-access",
  type: "registry:lib",
  title: "User access",
  description:
    "UserAccessProvider, Guard and access hooks: the user's roles and permissions loaded once from a Casbin backend, checked locally with Casbin-style matching.",
  dependencies: [version("zustand")],
  registryDependencies: [forge("context-store")],
  files: ["user-access.ts", "user-access-provider.tsx"].map((file) => ({
    path: `src/lib/${file}`,
    type: "registry:lib",
    target: `@lib/${file}`,
  })),
}

const user = {
  name: "user",
  type: "registry:lib",
  title: "User",
  description:
    "UserProvider: the signed-in user's context — display preferences plus roles and permissions — in one provider.",
  registryDependencies: [forge("user-preferences"), forge("user-access")],
  files: [
    {
      path: "src/lib/user-provider.tsx",
      type: "registry:lib",
      target: "@lib/user-provider.tsx",
    },
  ],
}

// Standalone like the assistant: the store needs zustand.
const shell = {
  name: "shell",
  type: "registry:component",
  title: "Dynamic shell",
  description:
    "ShellProvider, ShellLayout and hooks: the app shell's icon rail, sidebar sub nav and header driven by pages, with slots for page actions, toolbars and footers.",
  dependencies: [...base, ...icons, version("zustand")],
  registryDependencies: [
    forge("dropdown-menu"),
    forge("app-shell"),
    forge("icon-rail"),
    forge("workspace-sidebar"),
    forge("icon"),
    forge("context-store"),
    forge("user-access"),
  ],
  files: readdirSync(resolve(root, "src/components/forge/shell"))
    .sort()
    .map((file) => forgeFile(`shell/${file}`)),
}

// Standalone rather than in `components`, so @forge-ui/all doesn't pull in zustand.
const preferences = {
  name: "preferences",
  type: "registry:component",
  title: "Preferences",
  description:
    "Display settings panel: theme, density, text size, contrast and motion as segmented controls, with rows for app preferences.",
  dependencies: [...base, ...icons],
  registryDependencies: [
    ...["button", "field", "toggle-group"].map(forge),
    forge("theme-provider"),
    forge("user-preferences"),
  ],
  files: [forgeFile("preferences.tsx")],
}

// assistant-ui's Base UI elements, installed where assistant-ui's own CLI
// puts them, restyled for Forge and with Hugeicons.
const assistantThread = {
  name: "assistant-thread",
  type: "registry:component",
  title: "Assistant thread",
  description:
    "assistant-ui thread, composer, conversation list, floating modal and trigger popover: streaming markdown, reasoning, tool groups, attachments, dictation, and slots for message headers, composer actions and triggers.",
  dependencies: [
    ...base,
    ...icons,
    version("@assistant-ui/react"),
    version("@assistant-ui/react-markdown"),
    version("remark-gfm"),
    version("remark-math"),
    version("rehype-katex"),
    version("katex"),
    // Loaded on first use: Mermaid for ```mermaid blocks, Shiki for code.
    version("mermaid"),
    version("shiki"),
    version("zustand"),
  ],
  registryDependencies: [
    ...[
      "avatar",
      "button",
      "collapsible",
      "command",
      "dialog",
      "input",
      "popover",
      "skeleton",
      "textarea",
      "tooltip",
    ].map(forge),
    forge("icon"),
    forge("attachments"),
  ],
  files: [
    ...readdirSync(resolve(root, "src/components/assistant-ui/elements"))
      .sort()
      .map((file) => ({
        path: `src/components/assistant-ui/elements/${file}`,
        type: file.endsWith(".css") ? "registry:file" : "registry:component",
        target: `@components/assistant-ui/elements/${file}`,
      })),
    ...["use-attachment-src.ts", "use-copy-to-clipboard.ts"].map((file) => ({
      path: `src/hooks/${file}`,
      type: "registry:hook",
      target: `@hooks/${file}`,
    })),
  ],
}

const assistant = {
  name: "assistant",
  type: "registry:component",
  title: "Assistant",
  description:
    "Google ADK assistant, full screen in the workspace shell or as a floating launcher: conversations, agent hand-offs, artifacts, inline tool approvals, sign-in and input cards, / commands, voice input and an optional model section.",
  dependencies: [
    ...base,
    ...icons,
    version("@assistant-ui/react"),
    version("@assistant-ui/react-google-adk"),
  ],
  registryDependencies: [
    forge("assistant-thread"),
    ...[
      "button",
      "collapsible",
      "dropdown-menu",
      "input",
      "popover",
      "spinner",
    ].map(forge),
    forge("app-shell"),
    forge("avatars"),
    forge("icon"),
    forge("status"),
    forge("workspace-sidebar"),
  ],
  // Every module in the folder ships; index.ts is the public entry.
  files: readdirSync(resolve(root, "src/components/forge/assistant"))
    .sort()
    .map((file) => forgeFile(`assistant/${file}`)),
}

/** Parses API timestamps (UTC, ISO 8601) and formats local days. */
const timestamps = {
  name: "timestamps",
  type: "registry:lib",
  title: "Timestamps",
  description:
    "parseTimestamp and localDay: API times read as UTC and bare dates as local days, for tables and forms.",
  files: [
    {
      path: "src/lib/timestamps.ts",
      type: "registry:lib",
      target: "@lib/timestamps.ts",
    },
  ],
}

/** Composer attachments the agent can read: images, PDFs and text files. */
const attachments = {
  name: "attachments",
  type: "registry:lib",
  title: "Attachments",
  description:
    "ChatAttachmentAdapter: images, PDFs and text files on messages, named so the thread can show them.",
  dependencies: [version("@assistant-ui/react")],
  files: [
    {
      path: "src/lib/attachments.ts",
      type: "registry:lib",
      target: "@lib/attachments.ts",
    },
  ],
}

const hooks = [
  [
    "use-now",
    "A one-second clock shared by every component showing elapsed time.",
  ],
  [
    "use-debounced-value",
    "A value that settles a moment after it stops changing.",
  ],
].map(([name, description]) => ({
  name,
  type: "registry:hook",
  title: name,
  description,
  files: [
    {
      path: `src/hooks/${name}.ts`,
      type: "registry:hook",
      target: `@hooks/${name}.ts`,
    },
  ],
}))

const tiptap = Object.keys(pkg.dependencies)
  .filter((name) => name.startsWith("@tiptap/"))
  .map(version)

/** Every file under a Forge folder, subfolders included. */
const forgeFolder = (folder) =>
  readdirSync(resolve(root, "src/components/forge", folder), {
    recursive: true,
    withFileTypes: true,
  })
    .filter((entry) => entry.isFile())
    .map(
      (entry) =>
        `${entry.parentPath.slice(resolve(root, "src/components/forge").length + 1)}/${entry.name}`
    )
    .sort()
    .map((file) => ({
      ...forgeFile(file),
      ...(file.endsWith(".css") && { type: "registry:file" }),
    }))

const richTextEditor = {
  name: "rich-text-editor",
  type: "registry:component",
  title: "Rich text editor",
  description:
    "Tiptap editor in the workspace look: toolbar, bubble menus, / commands, mentions, tables, code with highlighting, maths, media, find and replace, outline, drag handles and Markdown in and out.",
  dependencies: [
    ...base,
    ...icons,
    ...tiptap,
    version("highlight.js"),
    version("katex"),
    version("lowlight"),
  ],
  registryDependencies: [
    "button",
    "dialog",
    "dropdown-menu",
    "field",
    "input",
    "kbd",
    "popover",
    "select",
    "separator",
    "skeleton",
    "spinner",
    "textarea",
    "toast",
    "toggle",
    "tooltip",
  ].map(forge),
  files: forgeFolder("rich-text-editor"),
}

const agentWorkspace = {
  name: "agent-workspace",
  type: "registry:component",
  title: "Agent workspace",
  description:
    "Everything around the assistant's conversation for a capable agent: progress by the composer, plan, notes and canvas panels, response and context stats, a chat history palette, follow-ups, a tools menu and tool cards for the chat API's built-in tools.",
  dependencies: [
    ...base,
    ...icons,
    version("@assistant-ui/react"),
    version("@assistant-ui/react-google-adk"),
    version("highlight.js"),
    version("react-markdown"),
    version("remark-gfm"),
    version("zustand"),
  ],
  registryDependencies: [
    forge("assistant"),
    forge("assistant-thread"),
    forge("attachments"),
    forge("icon"),
    ...[
      "button",
      "checkbox",
      "command",
      "dialog",
      "dropdown-menu",
      "input",
      "native-select",
      "popover",
      "sheet",
      "skeleton",
      "spinner",
      "switch",
      "tabs",
      "textarea",
      "tooltip",
    ].map(forge),
  ],
  files: forgeFolder("agent-workspace"),
}

const all = {
  name: "all",
  type: "registry:item",
  title: "Everything",
  description: "The theme, every tuned primitive and every Forge component.",
  registryDependencies: [
    forge("theme"),
    forge("theme-provider"),
    ...ui.map((item) => forge(item.name)),
    ...components.map((item) => forge(item.name)),
  ],
}

/**
 * The theme as this repo's exact src/index.css — same tokens, type scale,
 * spacing, comments and rule order — replacing a Vite app's stylesheet.
 * `theme` merges the same CSS into an existing stylesheet instead, which
 * reorders it (and moves the keyframes into @theme).
 */
const indexCss = {
  name: "index-css",
  type: "registry:item",
  title: "Theme stylesheet (Vite)",
  description:
    "The theme as the repo's exact src/index.css, replacing a Vite app's stylesheet. Used by base; add with --overwrite to refresh it.",
  // index.css imports shadcn/tailwind.css, so the shadcn package ships too.
  dependencies: [...theme.dependencies, version("shadcn")],
  files: [
    { path: "src/index.css", type: "registry:file", target: "src/index.css" },
  ],
}

/**
 * Starts a new app on Forge UI in one command:
 *   FORGE_UI_TOKEN=$(gh auth token) npx shadcn@latest init \
 *     jleva12/ei-tiger-workflow/base#main --template vite --base base --name my-app
 * (the monorepo's root registry.json includes this package's registry.json).
 * `init` merges `config` into the new components.json (so the private
 * @forge-ui registry and its token header are set up), then installs
 * everything below.
 */
const appBase = {
  name: "base",
  type: "registry:base",
  // Skip shadcn's default style: `init` merges its colours after files are
  // written, over the verbatim stylesheet. Its lib/utils.ts ships below.
  extends: "none",
  title: "New Forge UI app",
  description:
    "Sets up a new app: the @forge-ui registry with its FORGE_UI_TOKEN header, the theme, every component and every library.",
  config: {
    style: "base-rhea",
    iconLibrary: "hugeicons",
    menuColor: "inverted-translucent",
    menuAccent: "subtle",
    registries: {
      "@forge-ui": {
        url: registryUrl,
        headers: { Authorization: "Bearer ${FORGE_UI_TOKEN}" },
      },
    },
  },
  dependencies: [version("cn")],
  files: [
    { path: "src/lib/utils.ts", type: "registry:lib", target: "@lib/utils.ts" },
  ],
  // Everything in `all` except `theme`: its merge runs after files are
  // written, so it would rewrite the verbatim stylesheet from `index-css`.
  registryDependencies: [
    forge("index-css"),
    ...all.registryDependencies.filter((dep) => dep !== forge("theme")),
    forge("api-client"),
    forge("query-client"),
    forge("resource"),
    forge("user"),
    forge("preferences"),
    forge("shell"),
    forge("assistant"),
    forge("agent-workspace"),
    forge("rich-text-editor"),
    ...hooks.map((hook) => forge(hook.name)),
  ],
}

const registry = {
  $schema: "https://ui.shadcn.com/schema/registry.json",
  name: "forge-ui",
  homepage,
  items: [
    theme,
    themeProvider,
    ...ui,
    ...components,
    apiClient,
    queryClient,
    resource,
    contextStore,
    userPreferences,
    preferences,
    userAccess,
    user,
    shell,
    timestamps,
    attachments,
    ...hooks,
    assistantThread,
    assistant,
    agentWorkspace,
    richTextEditor,
    all,
    indexCss,
    appBase,
  ],
}

writeFileSync(
  resolve(root, "registry.json"),
  `${JSON.stringify(registry, null, 2)}\n`
)
console.log(
  `registry.json: ${registry.items.length} items (${ui.length} ui, ${components.length} Forge components)`
)
