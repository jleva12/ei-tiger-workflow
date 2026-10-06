import * as React from "react"
import { createStore } from "zustand"

import { createContextStore } from "@/lib/context-store"

/*
 * What the person is looking at, for the assistant: the records on screen
 * and the one they're working on. Pages and components declare it with
 * `usePageContext` / `usePageFocus` while they're mounted, the way they
 * declare the shell with `useShellPage`; the assistant reads it, with the
 * route, the page's title and the workspace scope, each time a message is
 * sent (`app-assistant.tsx`) and the agent gets it as `page_context`.
 *
 * It describes the screen; it never grants anything. The agent's tools still
 * act as the person, and the server checks the shape and size of what
 * arrives (forge-admin-api `agents/page_context.py`). Declare IDs and short
 * labels, never whole records, secrets or unsaved drafts.
 */

/**
 * A kind of Forge record. Lowercase words; the agent reads the kind with the
 * ID, e.g. an `organization`'s ID is what the organization tools take.
 */
export type PageEntityKind =
  | "organization"
  | "user"
  | "role"
  | "adk_run"
  | "knowledge_base"
  | "knowledge_document"

/** A record on screen. */
export type PageEntity = {
  kind: PageEntityKind
  id: string
  /** What the page calls it: a name, `owner/name`, a task's title. */
  label?: string | undefined
  /** A short qualifier: a node's kind, a task's status. */
  detail?: string | undefined
}

/** A primitive the page's view can be described with. */
export type PageViewValue = string | number | boolean

/** One component's part of the page context, while it's mounted. */
export type PageContextLayer = {
  /** Records on screen, outermost first: organization, then task, then run. */
  entities?: PageEntity[] | undefined
  /** The one they're working on: selected, opened, inspected. */
  focus?: PageEntity | null | undefined
  /** How the page is shown: its tab, pane, filters, counts. */
  view?: Record<string, PageViewValue | undefined> | undefined
}

/** The page context as the agent gets it with a message. */
export type PageContext = {
  /** The URL's path. */
  path: string
  /** The route's pattern, e.g. `/organizations/$organizationId`. */
  route?: string | undefined
  params: Record<string, string>
  /** The URL's search parameters (primitives only). */
  search: Record<string, PageViewValue>
  /** The page's name in the top bar, and the crumbs before it. */
  title?: string | undefined
  breadcrumbs: string[]
  /** The workspace scope: `admin` or `org:<id>`. */
  scope: string | null
  entities: PageEntity[]
  focus: PageEntity | null
  view: Record<string, PageViewValue>
}

/* -------------------------------------------------------------------------- */
/* Limits                                                                     */
/* -------------------------------------------------------------------------- */

// The same limits the server checks (agents/page_context.py); a context over
// them is dropped there, so they're applied here first.
export const PAGE_CONTEXT_LIMITS = {
  text: 200,
  label: 120,
  path: 500,
  entities: 12,
  breadcrumbs: 8,
  entries: 12,
} as const

const KEY = /^[A-Za-z_][A-Za-z0-9_]{0,39}$/

const clip = (value: string, length: number) =>
  value.length > length ? `${value.slice(0, length - 1)}…` : value

const clipOptional = (value: string | undefined, length: number) =>
  value === undefined ? undefined : clip(value, length)

function clipEntity(entity: PageEntity): PageEntity {
  return {
    kind: entity.kind,
    id: clip(entity.id, PAGE_CONTEXT_LIMITS.text),
    label: clipOptional(entity.label, PAGE_CONTEXT_LIMITS.label),
    detail: clipOptional(entity.detail, PAGE_CONTEXT_LIMITS.label),
  }
}

/** Primitive entries with plain keys, at most `entries` of them. */
export function primitives(
  record: Record<string, unknown> | undefined
): Record<string, PageViewValue> {
  const entries: [string, PageViewValue][] = []
  for (const [key, value] of Object.entries(record ?? {})) {
    if (entries.length === PAGE_CONTEXT_LIMITS.entries) break
    if (!KEY.test(key)) continue
    if (typeof value === "string") {
      entries.push([key, clip(value, PAGE_CONTEXT_LIMITS.text)])
    } else if (
      typeof value === "boolean" ||
      (typeof value === "number" && Number.isFinite(value))
    ) {
      entries.push([key, value])
    }
  }
  return Object.fromEntries(entries)
}

/** The context within the server's limits: long text clipped, extras left out. */
export function clipPageContext(context: PageContext): PageContext {
  const { text, path, label, entities, breadcrumbs } = PAGE_CONTEXT_LIMITS
  return {
    path: clip(context.path, path),
    route: clipOptional(context.route, text),
    params: Object.fromEntries(
      Object.entries(primitives(context.params)).map(([key, value]) => [
        key,
        String(value),
      ])
    ),
    search: primitives(context.search),
    title: clipOptional(context.title, label),
    breadcrumbs: context.breadcrumbs
      .slice(-breadcrumbs)
      .map((crumb) => clip(crumb, label)),
    scope: context.scope === null ? null : clip(context.scope, text),
    // The innermost records matter most.
    entities: context.entities.slice(-entities).map(clipEntity),
    focus: context.focus ? clipEntity(context.focus) : null,
    view: primitives(context.view),
  }
}

/* -------------------------------------------------------------------------- */
/* Store                                                                      */
/* -------------------------------------------------------------------------- */

type Layer = { id: string; order: number; value: PageContextLayer }

type PageContextState = {
  /** Declared parts, in the order their components first rendered. */
  layers: Layer[]
  /** Adds, replaces (keeping its place) or, with null, removes a layer. */
  setLayer: (id: string, order: number, value: PageContextLayer | null) => void
  /** A number for ordering layers: parents render before their children. */
  nextOrder: () => number
}

export const {
  Provider: PageContextProvider,
  useStore: usePageContextStore,
  useStoreApi: usePageContextApi,
} = createContextStore(
  () => {
    let order = 0
    return createStore<PageContextState>()((set) => ({
      layers: [],
      setLayer: (id, layerOrder, value) =>
        set(({ layers }) => {
          const rest = layers.filter((layer) => layer.id !== id)
          return {
            layers: value
              ? [...rest, { id, order: layerOrder, value }].sort(
                  (a, b) => a.order - b.order
                )
              : rest,
          }
        }),
      nextOrder: () => ++order,
    }))
  },
  { name: "PageContext" }
)

/**
 * The declared parts as one: records joined outermost first (a record
 * declared twice keeps its first place and its latest label), the deepest
 * focus, and the view merged in order.
 */
export function mergePageContext(
  layers: readonly Layer[]
): Pick<PageContext, "entities" | "focus" | "view"> {
  const entities = new Map<string, PageEntity>()
  let focus: PageEntity | null = null
  const view: Record<string, PageViewValue> = {}
  for (const { value } of layers) {
    for (const entity of value.entities ?? []) {
      const key = `${entity.kind}:${entity.id}`
      entities.set(key, { ...entities.get(key), ...entity })
    }
    if (value.focus) focus = value.focus
    for (const [key, entry] of Object.entries(value.view ?? {})) {
      if (entry !== undefined) view[key] = entry
    }
  }
  return { entities: [...entities.values()], focus, view }
}

/* -------------------------------------------------------------------------- */
/* Hooks                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * Tells the assistant what this component shows while it's mounted: the
 * records on screen, the one in focus and how the page is shown. Deeper
 * components win over their parents, and later pages over earlier ones;
 * when the component unmounts its part goes away.
 *
 * ```tsx
 * usePageContext({
 *   entities: [{ kind: "organization", id: organizationId, label: name }],
 *   view: { pane },
 * })
 * ```
 */
export function usePageContext(layer: PageContextLayer) {
  const api = usePageContextApi()
  const id = React.useId()
  const [order] = React.useState(() => api.getState().nextOrder())
  // Plain data, so the JSON is the whole layer: it's republished only when
  // something in it changes, and from the JSON so it's never stale.
  const signature = JSON.stringify(layer)
  React.useEffect(() => {
    api
      .getState()
      .setLayer(id, order, JSON.parse(signature) as PageContextLayer)
  }, [api, id, order, signature])
  React.useEffect(
    () => () => api.getState().setLayer(id, order, null),
    [api, id, order]
  )
}

/**
 * The record the person is working on in this component (a selected row, an
 * inspected node), or null when there's none, which leaves a parent's focus.
 * It's the page's selection, not the keyboard's focus: typing to the
 * assistant doesn't change it.
 */
export function usePageFocus(entity: PageEntity | null | undefined) {
  usePageContext({ focus: entity ?? undefined })
}

/** The deepest focus declared, re-rendering only when it changes. */
export function useFocusedEntity(): PageEntity | undefined {
  return usePageContextStore((state) => {
    for (let index = state.layers.length - 1; index >= 0; index--) {
      const focus = state.layers[index]!.value.focus
      if (focus) return focus
    }
    return undefined
  })
}
