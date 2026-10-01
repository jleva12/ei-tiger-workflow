import * as React from "react"
import { createStore } from "zustand"

import { createContextStore } from "@/lib/context-store"
import type { PermissionCheck } from "@/lib/user-access"
import type { IconProp } from "@/components/forge/icons"

/* -------------------------------------------------------------------------- */
/* Config                                                                     */
/* -------------------------------------------------------------------------- */

/** An entry in the icon rail or the sidebar. */
export type ShellNavItem = {
  id: string
  label: string
  icon?: IconProp
  /** Where it goes. Active automatically when it matches the current path. */
  href?: string
  /** Runs on click (as well as navigating to `href`). */
  onSelect?: () => void
  /** Trailing sidebar detail: a count, a dot, a chevron. */
  meta?: React.ReactNode
  /** Nested entries (sidebar only), shown as a sub-list. */
  items?: ShellNavItem[]
  /** Hidden unless the user has it (needs `UserAccessProvider`). */
  permission?: PermissionCheck
  /** Hidden unless the user has this role (needs `UserAccessProvider`). */
  role?: string
}

export type ShellNavSection = {
  id: string
  /** The section heading; omit for an untitled group. */
  title?: string
  /** Trailing heading control, e.g. an add button. */
  action?: React.ReactNode
  /** `primary` for the top block (padded, divided); `flush` for the last. */
  variant?: "default" | "primary" | "flush"
  items: ShellNavItem[]
}

export type ShellCrumb = {
  id?: string
  label: string
  icon?: IconProp
  href?: string
  onSelect?: () => void
}

export type ShellRail = {
  items: ShellNavItem[]
  /** Pinned to the bottom of the rail (settings, help…). */
  footer: ShellNavItem[]
}

export type ShellSidebar = {
  /** Hide the sub nav entirely (e.g. a full-width dashboard). */
  hidden: boolean
  /** The sidebar's accessible name (and the mobile sheet's title). */
  label?: string
  sections: ShellNavSection[]
}

export type ShellHeader = {
  /** The page name in the top bar (its h1). */
  title?: string
  icon?: IconProp
  /** Crumbs before the title. */
  breadcrumbs: ShellCrumb[]
}

export type ShellActive = {
  /** Id of the active rail item; default: the one matching the path. */
  rail?: string
  /** Id of the active sidebar item; default: the one matching the path. */
  sidebar?: string
}

/** Any part of the shell. Later layers override earlier ones, key by key. */
export type ShellConfig = {
  rail?: Partial<ShellRail>
  sidebar?: Partial<ShellSidebar>
  header?: Partial<ShellHeader>
  active?: ShellActive
}

/** What the shell shows: every layer merged. */
export type ResolvedShell = {
  rail: ShellRail
  sidebar: ShellSidebar
  header: ShellHeader
  active: ShellActive
}

export type ShellSlotName =
  | "header-actions"
  | "toolbar"
  | "footer"
  | "sidebar-header"
  | "sidebar-top"
  | "sidebar-bottom"

const emptyShell: ResolvedShell = {
  rail: { items: [], footer: [] },
  sidebar: { hidden: false, sections: [] },
  header: { breadcrumbs: [] },
  active: {},
}

const defined = <T extends object>(value: T | undefined) =>
  value
    ? (Object.fromEntries(
        Object.entries(value).filter(([, entry]) => entry !== undefined)
      ) as Partial<T>)
    : {}

/** Merges configs in order; `undefined` values leave the earlier one. */
export function resolveShell(configs: readonly ShellConfig[]): ResolvedShell {
  return configs.reduce<ResolvedShell>(
    (shell, config) => ({
      rail: { ...shell.rail, ...defined(config.rail) },
      sidebar: { ...shell.sidebar, ...defined(config.sidebar) },
      header: { ...shell.header, ...defined(config.header) },
      active: { ...shell.active, ...defined(config.active) },
    }),
    emptyShell
  )
}

function patchItems(
  items: ShellNavItem[] | undefined,
  id: string,
  patch: Partial<ShellNavItem>
): ShellNavItem[] | undefined {
  return items?.map((item) =>
    item.id === id
      ? { ...item, ...patch }
      : item.items
        ? { ...item, items: patchItems(item.items, id, patch) }
        : item
  )
}

function patchConfig(
  config: ShellConfig,
  id: string,
  patch: Partial<ShellNavItem>
): ShellConfig {
  return {
    ...config,
    ...(config.rail && {
      rail: {
        ...config.rail,
        ...(config.rail.items && {
          items: patchItems(config.rail.items, id, patch),
        }),
        ...(config.rail.footer && {
          footer: patchItems(config.rail.footer, id, patch),
        }),
      },
    }),
    ...(config.sidebar?.sections && {
      sidebar: {
        ...config.sidebar,
        sections: config.sidebar.sections.map((section) => ({
          ...section,
          items: patchItems(section.items, id, patch) ?? [],
        })),
      },
    }),
  }
}

/* -------------------------------------------------------------------------- */
/* Store                                                                      */
/* -------------------------------------------------------------------------- */

type ShellLayer = { id: string; order: number; config: ShellConfig }

export type ShellLinkRenderer = (href: string) => React.ReactElement

export type ShellStoreState = {
  /** The initial config plus everything `configure` changed. */
  base: ShellConfig
  /** Page layers, in the order their components first rendered. */
  layers: ShellLayer[]
  /** The merged result the shell renders. */
  shell: ResolvedShell
  currentPath: string | undefined
  navigate: ((href: string) => void) | undefined
  renderLink: ShellLinkRenderer | undefined
  /** Slot targets by name; the last registered one is used. */
  slots: Partial<Record<ShellSlotName, HTMLElement[]>>
  /** Changes the base config (merged in, key by key). Persists across pages. */
  configure: (
    config: ShellConfig | ((shell: ResolvedShell) => ShellConfig)
  ) => void
  /** Marks items active, overriding path matching. */
  setActive: (active: ShellActive) => void
  /** Patches a rail or sidebar item by id, in every layer (counts, labels…). */
  updateItem: (id: string, patch: Partial<ShellNavItem>) => void
  /** Adds, replaces (keeping its place) or, with null, removes a page layer. */
  setLayer: (id: string, order: number, config: ShellConfig | null) => void
  /** A number for ordering layers: parents render before their children. */
  nextOrder: () => number
  registerSlot: (name: ShellSlotName, element: HTMLElement) => () => void
}

export type ShellStoreInit = {
  initial?: ShellConfig
  currentPath?: string
  navigate?: (href: string) => void
  renderLink?: ShellLinkRenderer
}

function createShellStore({
  initial = {},
  currentPath,
  navigate,
  renderLink,
}: ShellStoreInit) {
  let order = 0
  return createStore<ShellStoreState>()((set, get) => {
    const commit = (base: ShellConfig, layers: ShellLayer[]) =>
      set({
        base,
        layers,
        shell: resolveShell([base, ...layers.map((layer) => layer.config)]),
      })

    return {
      base: initial,
      layers: [],
      shell: resolveShell([initial]),
      currentPath,
      navigate,
      renderLink,
      slots: {},
      configure: (config) => {
        const { base, layers, shell } = get()
        const next = typeof config === "function" ? config(shell) : config
        commit(resolveShellConfig(base, next), layers)
      },
      setActive: (active) => get().configure({ active }),
      updateItem: (id, patch) => {
        const { base, layers } = get()
        commit(
          patchConfig(base, id, patch),
          layers.map((layer) => ({
            ...layer,
            config: patchConfig(layer.config, id, patch),
          }))
        )
      },
      setLayer: (id, layerOrder, config) => {
        const { base, layers } = get()
        const rest = layers.filter((layer) => layer.id !== id)
        commit(
          base,
          config
            ? [...rest, { id, order: layerOrder, config }].sort(
                (a, b) => a.order - b.order
              )
            : rest
        )
      },
      nextOrder: () => ++order,
      registerSlot: (name, element) => {
        set((state) => ({
          slots: {
            ...state.slots,
            [name]: [...(state.slots[name] ?? []), element],
          },
        }))
        return () =>
          set((state) => ({
            slots: {
              ...state.slots,
              [name]: (state.slots[name] ?? []).filter(
                (target) => target !== element
              ),
            },
          }))
      },
    }
  })
}

/** Merges `next` into `base` part by part (rail, sidebar, header, active). */
function resolveShellConfig(base: ShellConfig, next: ShellConfig): ShellConfig {
  return {
    rail: { ...base.rail, ...defined(next.rail) },
    sidebar: { ...base.sidebar, ...defined(next.sidebar) },
    header: { ...base.header, ...defined(next.header) },
    active: { ...base.active, ...defined(next.active) },
  }
}

export const {
  Provider: ShellStoreProvider,
  useStore: useShellStore,
  useStoreApi: useShellStoreApi,
} = createContextStore(createShellStore, { name: "Shell" })

/* -------------------------------------------------------------------------- */
/* Hooks                                                                      */
/* -------------------------------------------------------------------------- */

/** Reads the shell as displayed: `useShell((shell) => shell.header.title)`. */
export function useShell<T>(selector: (shell: ResolvedShell) => T): T {
  return useShellStore((state) => selector(state.shell))
}

/**
 * Changes the shell from anywhere, and the changes stay when the page
 * changes: `configure` (rail, sidebar, header, active), `setActive`,
 * `updateItem(id, { meta: 3 })` for live counts.
 */
export function useShellApi() {
  const api = useShellStoreApi()
  return React.useMemo(() => {
    const { configure, setActive, updateItem } = api.getState()
    return { configure, setActive, updateItem }
  }, [api])
}

const useIsomorphicLayoutEffect =
  typeof window === "undefined" ? React.useEffect : React.useLayoutEffect

// A string that changes when the config's data changes. Functions compare as
// equal (the latest ones are always called), elements by type, key and props.
function signatureOf(config: ShellConfig) {
  return JSON.stringify(config, (_, value: unknown) => {
    if (typeof value === "function") return "ƒ"
    if (React.isValidElement(value)) {
      const type = value.type as
        string | { displayName?: string; name?: string }
      return {
        element:
          typeof type === "string" ? type : (type.displayName ?? type.name),
        key: value.key,
        props: value.props,
      }
    }
    return value
  })
}

// Swaps every function for one that calls the latest version at that path,
// so handlers stay fresh without re-publishing the layer on every render.
function bindLatest<T>(
  value: T,
  latest: () => unknown,
  path: (string | number)[] = []
): T {
  if (typeof value === "function") {
    return ((...args: unknown[]) => {
      const current = path.reduce<unknown>(
        (node, key) =>
          (node as Record<string | number, unknown> | undefined)?.[key],
        latest()
      )
      return typeof current === "function" ? current(...args) : undefined
    }) as T
  }
  if (
    React.isValidElement(value) ||
    value === null ||
    typeof value !== "object"
  )
    return value
  if (Array.isArray(value)) {
    return value.map((entry, index) =>
      bindLatest(entry, latest, [...path, index])
    ) as T
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, entry]) => [
      key,
      bindLatest(entry, latest, [...path, key]),
    ])
  ) as T
}

/**
 * What the shell shows while this component is mounted: its rail, sidebar
 * sub nav, header and active items, over the base config. Call it in a page
 * (or a layout); when the page unmounts the shell goes back. Deeper
 * components win over their parents, and later pages over earlier ones.
 *
 * ```tsx
 * useShellPage({
 *   header: { title: "All tasks", breadcrumbs: [{ label: "Tasks", href: "/tasks" }] },
 *   sidebar: { sections: taskSections },
 *   active: { sidebar: "all" },
 * })
 * ```
 */
export function useShellPage(config: ShellConfig) {
  const api = useShellStoreApi()
  const id = React.useId()
  const [order] = React.useState(() => api.getState().nextOrder())
  const latest = React.useRef(config)
  const signature = signatureOf(config)

  useIsomorphicLayoutEffect(() => {
    latest.current = config
  })
  useIsomorphicLayoutEffect(() => {
    api.getState().setLayer(
      id,
      order,
      bindLatest(latest.current, () => latest.current)
    )
  }, [api, id, order, signature])
  useIsomorphicLayoutEffect(
    () => () => api.getState().setLayer(id, order, null),
    [api, id, order]
  )
}

/* -------------------------------------------------------------------------- */
/* Active items                                                               */
/* -------------------------------------------------------------------------- */

const matchesPath = (href: string | undefined, path: string | undefined) =>
  href !== undefined &&
  path !== undefined &&
  (path === href || path.startsWith(href.endsWith("/") ? href : `${href}/`))

/** The id of the item whose `href` is the longest match for `path`. */
export function matchActive(
  items: readonly ShellNavItem[],
  path: string | undefined
): string | undefined {
  let best: { id: string; length: number } | undefined
  const visit = (list: readonly ShellNavItem[]) => {
    for (const item of list) {
      if (
        matchesPath(item.href, path) &&
        (!best || item.href!.length > best.length)
      ) {
        best = { id: item.id, length: item.href!.length }
      }
      if (item.items) visit(item.items)
    }
  }
  visit(items)
  return best?.id
}
