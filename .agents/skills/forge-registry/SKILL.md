---
name: forge-registry
description: How to change the Forge UI design system repo itself and publish it — adding or editing a Forge component, a tuned shadcn primitive, a theme token or a lib module (like the API client), wiring it into scripts/build-registry.mjs, the demo app and the README, and rebuilding the shadcn registry (`npm run registry:build`, `registry.json`, `public/r`). Use this skill whenever work touches src/components/forge, src/components/ui, src/lib, src/index.css or scripts/build-registry.mjs in the design-system repo, or the user asks to add, ship, publish, version or register a component or item — even if they only say "add a component" or "make this installable".
---

# Maintaining the Forge UI design system

This repo is a shadcn registry: consuming apps install its source with
`npx shadcn@latest add @forge-ui/<item>`. A change is only finished when it
exists everywhere it's consumed — the source, the registry definition, the
demo, the README and the agent skills — and the registry has been rebuilt.
Skipping one means an app can't install the change, or installs something
nobody (human or agent) knows how to use.

## Where things live

| Path | What | Installs into a consuming app at |
| --- | --- | --- |
| `src/components/ui/<name>.tsx` | shadcn primitives, tuned to the workspace look | `@/components/ui/<name>.tsx` |
| `src/components/forge/<file>` | Forge components (composites built from primitives) | `@/components/forge/<file>` |
| `src/lib/api/*.ts` | Non-visual libraries (API client, query client, resources) | `@/lib/api/*.ts` |
| `src/lib/context-store.tsx` | `createContextStore` (Zustand store per Provider) | `@/lib/context-store.tsx` |
| `src/lib/user-preferences*.ts(x)` | User preferences store and provider | `@/lib/…` |
| `src/lib/user-access*.ts(x)`, `src/lib/user-provider.tsx` | Roles and permissions (Casbin), `Guard`, `UserProvider` | `@/lib/…` |
| `src/components/assistant-ui/elements/`, `src/hooks/` | assistant-ui's thread elements and their hooks (`@forge-ui/assistant-thread`, a standalone object in `build-registry.mjs`) | `@/components/assistant-ui/elements/*`, `@/hooks/*` |
| `src/index.css` | The theme — single source of truth for tokens | Merged into the app's CSS by `@forge-ui/theme` |
| `src/demo/` | Showcase app; **not** shipped | — |
| `scripts/build-registry.mjs` | Defines every registry item | — |
| `registry.json`, `public/r/*.json` | **Generated** — never edit by hand | — |
| `.agents/skills/forge-*` (linked from `.claude/skills/`) | Agent skills teaching the components and data layer | — |

Because files install at the same aliases (`@/components/ui`,
`@/components/forge`, `@/lib`), imports inside shipped files must use those
aliases or relative paths within the same item — never `@/demo/...` or
anything the item doesn't also ship.

## Adding or changing something

1. **Write the source** in the right folder, following the neighbouring
   files: named exports, `cn` from `"cn"` (as every component imports it),
   tokens as Tailwind utilities (never raw colours), `Icon` from
   `@/components/forge/icon` for glyphs. See the `forge-ui` skill for the
   component conventions.
2. **Register it** in `scripts/build-registry.mjs` (details below).
3. **Show it in the demo** so it can be reviewed visually (details below).
4. **Document it** in `README.md`: add a row to the "Registry items" table;
   for anything with an API worth explaining, a short section with a usage
   snippet, matching the tone of the existing ones.
5. **Update the agent skills** that teach it: `.agents/skills/forge-ui`
   (components, tokens — `references/components.md`, `references/tokens.md`)
   or `.agents/skills/forge-data` (API client, query client, resources) or
   `.agents/skills/forge-state` (context stores). A
   skill that describes the old API is worse than none, because agents
   trust it.
6. **Rebuild and check** (details below).

### Registering in build-registry.mjs

Pick the list that matches the file's kind:

- **Tuned primitive** → the `ui` array:
  `[name, npmDeps, registryDeps, description]`. `npmDeps` is `[]` or
  `icons` (Hugeicons); `registryDeps` are other primitives by bare name
  (`"button"`), converted to `@forge-ui/…` automatically. File must be
  `src/components/ui/<name>.tsx`.
- **Forge component** → the `components` array:
  `[name, title, files, registryDeps, description]`. `files` are paths
  relative to `src/components/forge/`; list every file the component needs
  (shared helpers like `variants.ts` belong to exactly one item — depend on
  that item instead of duplicating the file). `registryDeps` mixes bare
  primitive names (`"tooltip"`) and `forge("icon")` for Forge items. Every Forge item
  gets `@base-ui/react`, `class-variance-authority`, `cn` and Hugeicons as
  npm deps; an extra npm package needs the same kind of special case as
  `data-table` has (and must be in `package.json` dependencies — `version()`
  reads the range from there).
- **A folder with subfolders** (`agent-workspace`, `rich-text-editor`) → a
  standalone object whose `files` come from `forgeFolder("<folder>")`, which
  ships every file, nested ones and CSS (as `registry:file`) included. Add
  standalone items to `items` near the bottom, and to `base` if new apps
  should get them.
- **Library** (e.g. `apiClient`, `queryClient`, `resource`) → a standalone
  object with `type: "registry:lib"`, files targeting `@lib/...`,
  `dependencies: [version("<npm package>")]` and `registryDependencies`
  on the items it imports. Add it to the `items` array near the bottom.
- **Theme token** → just edit `src/index.css`; `@forge-ui/theme` is derived from
  it on every build.

`@forge-ui/all` automatically includes the theme, the theme provider, every `ui`
item and every `components` item. Libraries are deliberately left out of
it (they pull in npm dependencies apps may not want).

Descriptions are one sentence: what it is plus what's distinctive, in the
voice of the existing ones ("Dark compact tooltip.", "Collapsible pastel
status bands and the flat task table.").

### Rebuild and check

```bash
npm run registry:build   # regenerates registry.json and public/r
npx tsc -b               # typecheck (what `npm run build` runs)
npm test                 # node --test over tests/*.test.ts
npx eslint <changed files>
npx prettier --check <changed files>
```

`registry:build` must print your item in its "Building …" list; open
`public/r/<name>.json` to confirm `files`, `dependencies` and
`registryDependencies` are what an app needs. Lint may report pre-existing
problems in files you didn't touch; only your files need to be clean.

### Publishing

Apps install from the files committed to the private GitHub repo
`jleva12/forge-ui` (`raw.githubusercontent.com/jleva12/forge-ui/<ref>/public/r/{name}.json`,
with a token in `FORGE_UI_TOKEN`), so a change is published when it's
pushed — and only if the rebuilt `registry.json` and `public/r` are
committed with it. `npm run registry:check` rebuilds and fails on
uncommitted registry changes; the `Registry` workflow in
`.github/workflows/registry.yml` runs it and `tsc -b` on every push.
Releases are git tags (`v0.1.0`) that apps can pin in their registry URL.
The `base` item (`registry:base`) starts new apps with
`npx shadcn@latest init jleva12/forge-ui/base#main --template vite --base base`:
its `config` writes the registry and token header into the new
`components.json` and its `registryDependencies` install everything, so add
new top-level libraries to it too. `REGISTRY_URL` overrides the URL it writes.
`base` ships `src/index.css` verbatim through `index-css` and leaves
`theme` out of its tree (with `extends: "none"` to skip shadcn's default
style): `init` writes files first and merges CSS afterwards, so any item in
the tree that carries `cssVars`/`css` — or depends on `theme` — rewrites
the copied stylesheet. Keep `theme` out of other items' dependencies.
Commit, push or tag only when the user asks.

The ESLint config allows non-component exports from `src/components/ui`
only for listed names (`buttonVariants`, `toast`, …); a new primitive that
exports a `cva` helper needs its name added to `allowExportNames` in
`eslint.config.js`. Forge components keep non-component exports (variants,
contexts, constants) in a sibling `.ts` file — `variants.ts`, `icons.ts`,
`app-shell-context.ts` — so React Refresh keeps working; ship that file in
the same registry item.

### Adding a demo section

The demo (`src/demo`, not shipped) is how changes get reviewed, so every
new component or variant appears there. Pages are routed by URL hash.

1. **Nav entry** — add `{ id: "my-thing", label: "My thing", icon: "layers",
   group: "Workspace" }` to `sections` in `src/demo/nav.ts`. A new group
   also needs an entry in `sectionGroups` (same file) and in `firstInGroup`
   in `src/App.tsx` (typed, so TypeScript flags it).
2. **Section component** — in the matching `src/demo/sections/<file>.tsx`
   (foundations, primitives, workspace, detail, data-table), export
   `MyThingSection`, built from `SectionPage` (`icon?`, `eyebrow`, `title`,
   `description`) and one `Specimen` per state or variant (`title`,
   `description?`, `code?` — the snippet shown behind the "code" toggle —
   and `variant?: "padded" | "flush" | "canvas"`) from `src/demo/specimen.tsx`.
3. **Route** — import it in `src/App.tsx` and add
   `case "my-thing": return <MyThingSection />` to `SectionView`. There's no
   default case: a missing case renders a blank page, not an error.
4. The sidebar, rail, search and Overview grid all read `sections`, so
   nothing else is needed. `#workspace` is reserved for the full-screen
   workspace preview.

Check it in the browser (`npm run dev`, or the `components` entry in
`.claude/launch.json` with the preview tools, port 5180) at `#my-thing`, in
light and dark (`d` toggles), and at narrow widths — the demo chrome is
itself an `AppShell`, so container breakpoints behave as they do in apps.
The `reference-app` entry (port 5190) runs the original app the library was
extracted from, for comparing a component against what it was extracted from.

## Principles

- **Fidelity to the original app.** The README's "Fidelity notes" record
  where the library follows the rendered original over its CSS, and the
  intentional departures. Keep changes consistent with them, and add a note
  when you depart deliberately.
- **Consumers edit their copy.** Shipped code is copied into apps, so keep
  it readable and self-contained, with the same comment density as the
  surrounding files; avoid cleverness that makes local edits risky.
- **Don't break installed APIs casually.** Renaming an export or a prop
  breaks every app on its next `shadcn add`. If a breaking change is
  worth it, say so in the README section and in your summary.
