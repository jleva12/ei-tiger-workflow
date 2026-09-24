---
name: forge-state
description: How to share client-side state between components in apps built on the Forge UI design system — `createContextStore` (`@/lib/context-store`), a Zustand store per React Provider that is seeded from props, another context or server data, then read and written through typed selector hooks — and the built-in user context: preferences (density, text size, contrast, motion, app settings) and roles and permissions from a Casbin backend with Guard components. Use this skill whenever several components need the same UI state (selection, filters, view mode, drafts, wizard steps, panel open state), a feature needs a store scoped to one subtree or page instance, or someone reaches for React context with useState/useReducer, a Zustand `create()` global, or prop drilling — even if they only say "store", "shared state", "zustand", "user settings", "preferences", "roles", "permissions", "Casbin", "RBAC" or "guard".
---

# Forge client state

Server data lives in TanStack Query (see the `forge-data` skill). Everything
else that several components share — what's selected, which filters are on,
the view mode, an unsaved draft, which step a wizard is on — goes in a
**context store**: a Zustand store created per Provider by
`createContextStore` from `@/lib/context-store`.

Why this shape rather than the alternatives:

- **React context holding state** re-renders every consumer on every change.
  Here the context only carries the store object, which never changes;
  components subscribe to slices with selectors and re-render only when their
  slice changes.
- **A global Zustand `create()` store** is one instance for the whole app:
  it leaks between tests, between two instances of the same widget on a page,
  and between requests when server-rendering. A context store is one instance
  per Provider, seeded from that Provider's `initial` values.

## Define a store

Put it in a `.ts` file next to the feature (`board-store.ts`). The factory
receives the Provider's `initial` value and returns a Zustand store; name the
exports after the feature.

```ts
import { createStore } from "zustand"

import { createContextStore } from "@/lib/context-store"

type View = "list" | "kanban"
type BoardInit = { view: View; filters: string[] }
type BoardState = BoardInit & {
  selectedId: string | null
  setView: (view: View) => void
  toggleFilter: (filter: string) => void
  select: (id: string | null) => void
}

export const {
  Provider: BoardProvider,
  useStore: useBoardStore,
  useStoreApi: useBoardStoreApi,
} = createContextStore(
  (initial: BoardInit) =>
    createStore<BoardState>()((set) => ({
      ...initial,
      selectedId: null,
      setView: (view) => set({ view }),
      toggleFilter: (filter) =>
        set((s) => ({
          filters: s.filters.includes(filter)
            ? s.filters.filter((f) => f !== filter)
            : [...s.filters, filter],
        })),
      select: (selectedId) => set({ selectedId }),
    })),
  { name: "Board" } // error messages and devtools names
)
```

A factory with no parameter makes `initial` optional
(`<CounterProvider>`); with one, `initial` is required and type-checked.
Middleware goes inside `createStore` as usual — `persist`, `devtools`,
`subscribeWithSelector` — and `useStoreApi()` keeps their extra API
(`useSettingsStoreApi().persist.rehydrate()`).

## Provide it, seeded from props or context

```tsx
<BoardProvider initial={{ view: user.defaultView, filters: savedFilters }}>
  <BoardToolbar />
  <BoardContent />
</BoardProvider>
```

Seeding from another context or from query data is the same thing: read it
in the component that renders the Provider and pass it as `initial`.

`initial` is read **once**, when the Provider mounts. That's deliberate — the
user's changes win. When the seed should start over (another project, another
record), give the Provider a `key`:
`<BoardProvider key={projectId} initial={…}>`. When an outside value must keep
overriding the store, sync it explicitly in an effect:
`useEffect(() => api.setState({ filters }), [api, filters])`.

## Read and write

```tsx
const view = useBoardStore((s) => s.view)            // re-renders on view only
const setView = useBoardStore((s) => s.setView)      // actions are stable
const { view, filters } = useBoardStore(
  useShallow((s) => ({ view: s.view, filters: s.filters }))
) // object/array selectors need useShallow (zustand/react/shallow)

// Outside render (event handlers, effects, callbacks): no subscription.
const api = useBoardStoreApi()
onRowClick={(row) => api.getState().select(row.id)}
```

## Rules of thumb

- Select the smallest slice a component needs; never `useBoardStore()` (the
  whole state) in components that render often.
- Keep derived values out of the store — compute them in the selector or
  with `useMemo`.
- Don't copy server data into a store to "cache" it; TanStack Query already
  does. Seeding a store from server data is fine for **drafts** the user
  edits before saving (then save with a resource mutation).
- One store per feature/page region; put the Provider as low as it can go
  while still covering every consumer.
- Tests: render the component inside its Provider with the `initial` the
  test needs; every render gets a fresh store.
- Hooks used outside their Provider throw
  `Board hooks must be used inside <BoardProvider>.` — move the Provider up.

## Built in: user preferences

`@/lib/user-preferences` is a context store the library ships for the
signed-in user's display settings — `density`, `fontScale`, `contrast`,
`motion` — wrapped by `UserPreferencesProvider`
(`@/lib/user-preferences-provider`), which saves them in the browser and
applies them to the page. Don't build another store for these; read them
with `useUserPreference("density")`, change them with
`useSetUserPreference()`, and add app-level settings (default views, digest
emails, …) to the same store by augmenting `CustomUserPreferences`:

```ts
declare module "@/lib/user-preferences" {
  interface CustomUserPreferences {
    defaultTaskView: "list" | "kanban"
  }
}
// then <UserPreferencesProvider defaults={{ defaultTaskView: "list" }}>
```

Precedence: library defaults < `defaults` < stored in the browser <
`initial` (e.g. the user's profile); `onChange(preferences, changed)` fires
for user changes only, so it's where to save them to the server.

## Built in: roles and permissions (Casbin)

`@/lib/user-access` holds the signed-in user's roles and permissions, loaded
once by `UserAccessProvider` (`@/lib/user-access-provider`) from the Casbin
backend (`get_implicit_roles_for_user` / `get_implicit_permissions_for_user`
turned into permissions with `fromCasbin`). `UserProvider`
(`@/lib/user-provider`) combines it with the preferences — use it at the
app root, keyed by the user's id.

- **Show / hide:** `<Guard permission={["tasks", "create"]}>`,
  `<Guard role="admin" fallback={…}>`, `permissions` / `roles` lists with
  `mode="all" | "any"`, `when={(access) => …}` for anything else. Checks use
  Casbin's order: `[object, action]` (optional third item: domain).
- **Disable instead of hide:** `const { allowed } = useGuard({ … })`;
  booleans: `useCan(object, action)`, `useHasRole(role)`. They're false
  until access has loaded.
- **Event handlers:** `useUserAccessApi().getState().can(object, action)`.
- **Denied pages:** `fallback={<PageEmpty illustration="locked" …/>}` (or
  `PanelEmpty` inside a panel).
- **After a role change:** `useUserAccess((s) => s.reload)()`.
- Never treat these checks as security: the server must enforce with
  Casbin. They only keep the UI honest about what the user can do.
