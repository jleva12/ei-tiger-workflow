import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type { ZodType } from "zod"

import { api } from "@/lib/api-instance"

/*
 * A dashboard's layout: which widgets it shows, where, and how big. The code
 * holds each dashboard's widget types and its default layout; a person's
 * saved layout (/me/dashboards/{dashboard}) replaces the default. A saved one
 * is read forgivingly, so a widget type that's gone or a setting that no
 * longer fits never breaks the page. The grid is react-grid-layout's: twelve
 * columns across, pixels down.
 */

/** The grid's columns; a widget's x and w count them. */
export const GRID_COLUMNS = 12

/**
 * The layout document's shape. 2 put the read-out and the ledger in the
 * grid; a layout of 1 is read with them on top, as they were. One of a
 * version this doesn't know is left unread.
 */
export const LAYOUT_VERSION = 2

/** A widget's least height when the layout doesn't say, below any panel's own. */
export const FIT_HEIGHT = 100

const MAX_HEIGHT = 4000

/** The dashboards a person lays out, each saved apart. */
export type DashboardId = "organization-overview"

/** One widget on a dashboard: an instance of a widget type, placed on the grid. */
export type DashboardWidget = {
  /** Its ID in the layout, unique there; react-grid-layout's `i`. */
  id: string
  /** Its type: a key of the dashboard's widget catalog. */
  type: string
  /** Its first column, from 0. */
  x: number
  /**
   * Where it starts, in pixels down. Widgets rise as far as they can, so
   * this orders them more than it places them.
   */
  y: number
  /** Its width in columns. */
  w: number
  /** Its least height in pixels; it grows to fit what it shows. */
  h: number
  /** Its own title, in place of its type's. */
  title?: string
  /** Its type's settings. */
  settings?: Record<string, unknown>
}

export type DashboardLayout = { version: number; widgets: DashboardWidget[] }

/** What reading a saved layout needs to know of a widget type. */
export type WidgetSpec = {
  /** Its least width in columns. */
  minW?: number
  /** Its settings, with their defaults; saved ones that don't fit are dropped. */
  settings?: ZodType<Record<string, unknown>>
}

/* The organization overview ---------------------------------------------- */

export const OVERVIEW_WIDGET_TYPES = [
  "readout",
  "ledger",
  "tokens",
  "models",
  "activity",
  "outcomes",
  "spend",
  "people",
  "steps",
  "attention",
] as const

export type OverviewWidgetType = (typeof OVERVIEW_WIDGET_TYPES)[number]

const overviewWidget = (
  type: OverviewWidgetType,
  x: number,
  row: number,
  w: number
): DashboardWidget => ({ id: type, type, x, y: row, w, h: FIT_HEIGHT })

/**
 * What heads the overview: the read-out, then the ledger, the width of it,
 * each as tall as its text or its rows (a sentence is under a panel's least).
 */
const HEAD = [
  { ...overviewWidget("readout", 0, 0, GRID_COLUMNS), h: 40 },
  { ...overviewWidget("ledger", 0, 1, GRID_COLUMNS), h: 40 },
]

/**
 * The overview until a person lays out their own: the read-out and the
 * ledger, then its panels in three rows of thirds, tokens taking two.
 */
export const OVERVIEW_LAYOUT: DashboardLayout = {
  version: LAYOUT_VERSION,
  widgets: [
    ...HEAD,
    overviewWidget("tokens", 0, 2, 8),
    overviewWidget("models", 8, 2, 4),
    overviewWidget("activity", 0, 3, 4),
    overviewWidget("outcomes", 4, 3, 4),
    overviewWidget("spend", 8, 3, 4),
    overviewWidget("people", 0, 4, 4),
    overviewWidget("steps", 4, 4, 4),
    overviewWidget("attention", 8, 4, 4),
  ],
}

/**
 * A saved layout brought to this version: one of 1 had the read-out and the
 * ledger above its grid, so they go back on top, where they were.
 */
function upgraded(saved: Record<string, unknown>) {
  if (saved.version !== 1 || !Array.isArray(saved.widgets)) return saved
  const below = saved.widgets.map((widget: unknown) =>
    isRecord(widget) && typeof widget.y === "number"
      ? { ...widget, y: widget.y + HEAD.length }
      : widget
  )
  return { version: LAYOUT_VERSION, widgets: [...HEAD, ...below] }
}

/* Reading a saved layout -------------------------------------------------- */

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

const whole = (value: unknown, fallback: number) =>
  typeof value === "number" && Number.isFinite(value)
    ? Math.round(value)
    : fallback

const clamp = (value: number, least: number, most: number) =>
  Math.min(most, Math.max(least, value))

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

/**
 * A widget's settings as its type reads them, keeping only those that aren't
 * its defaults: a layout that's the default in all but name is the default.
 */
function settingsOf(spec: WidgetSpec, saved: unknown) {
  if (!spec.settings) return undefined
  const parsed = spec.settings.safeParse(isRecord(saved) ? saved : {})
  const defaults = spec.settings.safeParse({})
  const base = defaults.success ? defaults.data : {}
  const own = Object.entries(parsed.success ? parsed.data : base).filter(
    ([name, value]) => !same(value, base[name])
  )
  return own.length > 0 ? Object.fromEntries(own) : undefined
}

/**
 * A saved layout as the dashboard can draw it, or the default when there's
 * none or it's of another version. Widgets of types the catalog doesn't have
 * and repeated IDs are left out, positions and sizes are kept on the grid,
 * and settings that don't fit their type any more go back to its defaults
 * (which a widget keeps only by leaving them out).
 */
export function resolveLayout(
  saved: unknown,
  catalog: Readonly<Record<string, WidgetSpec>>,
  fallback: DashboardLayout
): DashboardLayout {
  if (isRecord(saved)) saved = upgraded(saved)
  if (
    !isRecord(saved) ||
    saved.version !== LAYOUT_VERSION ||
    !Array.isArray(saved.widgets)
  ) {
    return fallback
  }
  const widgets: DashboardWidget[] = []
  const seen = new Set<string>()
  for (const raw of saved.widgets) {
    if (!isRecord(raw)) continue
    const { id, type } = raw
    if (typeof id !== "string" || !id || seen.has(id)) continue
    if (typeof type !== "string" || !Object.hasOwn(catalog, type)) continue
    seen.add(id)
    const spec = catalog[type]
    const w = clamp(
      whole(raw.w, GRID_COLUMNS),
      Math.min(spec.minW ?? 1, GRID_COLUMNS),
      GRID_COLUMNS
    )
    const title = typeof raw.title === "string" ? raw.title.trim() : ""
    const settings = settingsOf(spec, raw.settings)
    widgets.push({
      id,
      type,
      x: clamp(whole(raw.x, 0), 0, GRID_COLUMNS - w),
      y: Math.max(0, whole(raw.y, 0)),
      w,
      h: clamp(whole(raw.h, FIT_HEIGHT), 1, MAX_HEIGHT),
      ...(title && { title }),
      ...(settings && { settings }),
    })
  }
  return { version: LAYOUT_VERSION, widgets }
}

/* Placing widgets --------------------------------------------------------- */

/** A widget where the grid draws it: react-grid-layout's item, in pixels down. */
export type PlacedWidget = {
  i: string
  x: number
  y: number
  w: number
  h: number
}

const sideBySide = (a: { x: number; w: number }, b: { x: number; w: number }) =>
  a.x < b.x + b.w && b.x < a.x + a.w

/**
 * Where each widget goes, given what its content needs (`heights`, in
 * pixels): in order of y then x, each rises until it meets one above it in
 * its columns, as react-grid-layout's vertical compaction has it, and is
 * its layout height or its content's, whichever is more. Widgets that start
 * on the same line with the same layout height are a row: they stretch to
 * the tallest, which can push what's below further down, so it settles
 * again until nothing grows.
 */
export function settle(
  widgets: readonly DashboardWidget[],
  heights: ReadonlyMap<string, number>
): PlacedWidget[] {
  const order = widgets
    .map((widget, index) => ({ widget, index }))
    .sort(
      (a, b) =>
        a.widget.y - b.widget.y || a.widget.x - b.widget.x || a.index - b.index
    )
    .map(({ widget }) => widget)
  const tall = new Map(
    order.map((widget) => [
      widget.id,
      Math.max(widget.h, Math.ceil(heights.get(widget.id) ?? 0)),
    ])
  )
  let placed: PlacedWidget[] = []
  // Each pass settles at least one more row, so this many always do.
  for (let pass = 0; pass <= order.length; pass += 1) {
    placed = []
    for (const widget of order) {
      const y = placed.reduce(
        (top, other) =>
          sideBySide(widget, other) ? Math.max(top, other.y + other.h) : top,
        0
      )
      placed.push({
        i: widget.id,
        x: widget.x,
        y,
        w: widget.w,
        h: tall.get(widget.id) ?? widget.h,
      })
    }
    const rows = new Map<string, number>()
    const rowOf = (index: number) => `${placed[index].y}:${order[index].h}`
    placed.forEach((item, index) =>
      rows.set(rowOf(index), Math.max(rows.get(rowOf(index)) ?? 0, item.h))
    )
    let grew = false
    placed.forEach((item, index) => {
      const row = rows.get(rowOf(index)) ?? item.h
      if (row > item.h) {
        tall.set(item.i, row)
        grew = true
      }
    })
    if (!grew) break
  }
  return placed
}

/** The bottom of the lowest placed widget: the grid's height. */
export const bottomOf = (placed: readonly PlacedWidget[]) =>
  placed.reduce((bottom, item) => Math.max(bottom, item.y + item.h), 0)

/** Placed widgets top to bottom, left to right: the order they stack in. */
export const inReadingOrder = (placed: readonly PlacedWidget[]) =>
  [...placed].sort((a, b) => a.y - b.y || a.x - b.x)

/* Editing ----------------------------------------------------------------- */

/** The most a widget's own title may hold. */
export const TITLE_LENGTH = 120

/**
 * Widgets where react-grid-layout left them after a drag or a resize: each
 * at its column and line. A resized one takes its new width, and its new
 * height as its least height only when the person changed the height, so
 * dragging a side doesn't fix a height that was its content's.
 */
export function arranged(
  widgets: readonly DashboardWidget[],
  layout: readonly PlacedWidget[],
  resized?: { from: PlacedWidget; to: PlacedWidget }
): DashboardWidget[] {
  const items = new Map(layout.map((item) => [item.i, item]))
  return widgets.map((widget) => {
    const item = items.get(widget.id)
    if (!item) return widget
    const next = { ...widget, y: Math.max(0, item.y) }
    if (resized?.to.i === widget.id) {
      next.w = clamp(resized.to.w, 1, GRID_COLUMNS)
      if (resized.to.h !== resized.from.h) {
        next.h = clamp(resized.to.h, 1, MAX_HEIGHT)
      }
    }
    // On the grid whatever it reports, as a saved layout must be.
    next.x = clamp(item.x, 0, GRID_COLUMNS - next.w)
    return next
  })
}

/** A widget of a type added under the rest, at its type's width. */
export function withWidget(
  widgets: readonly DashboardWidget[],
  type: string,
  w: number
): DashboardWidget[] {
  const taken = new Set(widgets.map((widget) => widget.id))
  let id = type
  for (let n = 2; taken.has(id); n += 1) id = `${type}-${n}`
  const y = widgets.reduce((last, widget) => Math.max(last, widget.y + 1), 0)
  return [
    ...widgets,
    { id, type, x: 0, y, w: clamp(w, 1, GRID_COLUMNS), h: FIT_HEIGHT },
  ]
}

export const withoutWidget = (
  widgets: readonly DashboardWidget[],
  id: string
): DashboardWidget[] => widgets.filter((widget) => widget.id !== id)

/** A widget's own title; a blank one puts its type's back. */
export function retitled(
  widgets: readonly DashboardWidget[],
  id: string,
  title: string
): DashboardWidget[] {
  const own = title.trim().slice(0, TITLE_LENGTH)
  return widgets.map((widget) => {
    if (widget.id !== id) return widget
    const next: DashboardWidget = { ...widget, title: own }
    if (!own) delete next.title
    return next
  })
}

/**
 * A widget's setting changed; set back to its default, it's left out, as
 * a saved layout's are.
 */
export function withSetting(
  widgets: readonly DashboardWidget[],
  id: string,
  name: string,
  value: unknown,
  byDefault: unknown
): DashboardWidget[] {
  return widgets.map((widget) => {
    if (widget.id !== id) return widget
    const settings: Record<string, unknown> = { ...widget.settings }
    if (same(value, byDefault)) delete settings[name]
    else settings[name] = value
    const next: DashboardWidget = { ...widget, settings }
    if (Object.keys(settings).length === 0) delete next.settings
    return next
  })
}

/** Several of a widget's settings changed at once, as `withSetting`. */
export const withSettings = (
  widgets: readonly DashboardWidget[],
  id: string,
  values: Readonly<Record<string, unknown>>,
  defaults: Readonly<Record<string, unknown>>
): DashboardWidget[] =>
  Object.entries(values).reduce<DashboardWidget[]>(
    (next, [name, value]) => withSetting(next, id, name, value, defaults[name]),
    [...widgets]
  )

const canonical = (widgets: readonly DashboardWidget[]) =>
  JSON.stringify(
    [...widgets]
      .sort((a, b) => a.id.localeCompare(b.id))
      .map(({ id, type, x, y, w, h, title, settings }) => [
        id,
        type,
        x,
        y,
        w,
        h,
        title ?? null,
        settings ?? null,
      ])
  )

/** Whether two layouts hold the same widgets, placed and set the same. */
export const sameWidgets = (
  a: readonly DashboardWidget[],
  b: readonly DashboardWidget[]
) => canonical(a) === canonical(b)

/* Saving ------------------------------------------------------------------ */

/** A person's saved layout of a dashboard; `layout` is null until they save one. */
export type SavedLayout = {
  dashboard: DashboardId
  layout: unknown
  updated_at: string | null
}

export const dashboardKeys = {
  all: ["dashboard-layouts"] as const,
  of: (dashboard: DashboardId) => [...dashboardKeys.all, dashboard] as const,
}

const pathOf = (dashboard: DashboardId) =>
  `/me/dashboards/${encodeURIComponent(dashboard)}`

/**
 * The caller's saved layout of a dashboard. Only they change it, from this
 * page, so it's read once; a failed read isn't retried, and the dashboard
 * draws its default.
 */
export function useSavedLayout(dashboard: DashboardId) {
  return useQuery({
    queryKey: dashboardKeys.of(dashboard),
    queryFn: ({ signal }) =>
      api.get<SavedLayout>(pathOf(dashboard), { signal }),
    staleTime: Infinity,
    retry: false,
  })
}

/** Save the caller's layout of a dashboard, in place of the last. */
export function useSaveLayout(dashboard: DashboardId) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (layout: DashboardLayout) =>
      api.put<SavedLayout>(pathOf(dashboard), layout),
    meta: { errorTitle: "Couldn't save the layout" },
    onSuccess: (saved) =>
      queryClient.setQueryData(dashboardKeys.of(dashboard), saved),
  })
}

/** Forget the caller's layout of a dashboard, so it draws its default again. */
export function useResetLayout(dashboard: DashboardId) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.delete<void>(pathOf(dashboard)),
    meta: { errorTitle: "Couldn't reset the layout" },
    onSuccess: () =>
      queryClient.setQueryData<SavedLayout>(dashboardKeys.of(dashboard), {
        dashboard,
        layout: null,
        updated_at: null,
      }),
  })
}
