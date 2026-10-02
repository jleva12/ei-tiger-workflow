import * as React from "react"
import { flushSync } from "react-dom"
import {
  useReactFlow,
  type InternalNode,
  type ReactFlowInstance,
} from "@xyflow/react"
import { useShallow } from "zustand/react/shallow"

import type { Measure } from "@/lib/builder/layout"
import {
  documentOf,
  useBuilder,
  useBuilderApi,
  type FlowEdge,
  type FlowNode,
} from "./store"

/* The builder kit's helpers that aren't components. */

/** What a step dragged from the library carries. */
export const STEP_MIME = "application/x-forge-step"

/** A second type naming the kind, since a drag's data is hidden until the drop. */
export const kindMime = (kind: string) =>
  `${STEP_MIME}-${kind.replaceAll("_", "-")}`

/** The kind a drag carries, from its types; `isKind` says which the builder has. */
export function kindOfDrag(
  types: readonly string[],
  isKind: (value: unknown) => boolean
): string | undefined {
  for (const type of types) {
    if (!type.startsWith(`${STEP_MIME}-`)) continue
    const kind = type.slice(STEP_MIME.length + 1).replaceAll("-", "_")
    if (isKind(kind)) return kind
  }
}

/** Whether motion is off, by the person's preference or their system's. */
export const reducedMotion = () =>
  document.documentElement.dataset.forgeMotion === "reduce" ||
  window.matchMedia("(prefers-reduced-motion: reduce)").matches

/**
 * Make a change that moves things around as a view transition, so what
 * moves glides to where it lands; at once when motion is off or the
 * browser can't. `after` runs once the change is on the page.
 */
export function withViewTransition(change: () => void, after?: () => void) {
  const run = () => {
    flushSync(change)
    after?.()
  }
  if (typeof document.startViewTransition !== "function" || reducedMotion())
    run()
  else document.startViewTransition(run)
}

/** The details panel beside the canvas, for the toggle that shows and hides it. */
export const DETAILS_PANEL_ID = "builder-details"

/** A card in the step dialog: the step's settings, or the panel beside them. */
export const DIALOG_CARD =
  "pointer-events-auto flex min-h-0 flex-col overflow-hidden rounded-(--radius-dialog) bg-popover text-popover-foreground shadow-xl ring-1 ring-foreground/5 outline-none dark:ring-foreground/10"

/**
 * The panel beside a step's settings, where a setting opens a larger
 * editor (a schema's fields). One at a time; the step dialog owns it.
 */
export type Companion = {
  /** Where the open editor renders. */
  slot: HTMLElement | null
  /** Which setting's editor is open. */
  open: string | null
  show: (key: string) => void
  hide: () => void
  /** Let go of it without motion, when its setting goes away. */
  release: (key: string) => void
}

export const CompanionContext = React.createContext<Companion | null>(null)
export const useCompanion = () => React.useContext(CompanionContext)

/** What Tidy up needs from the canvas: each step's size and where its ways in and out sit. */
export function measureFlow(flow: {
  getInternalNode: (id: string) => InternalNode | undefined
}): Measure {
  return {
    size: (id) => {
      const measured = flow.getInternalNode(id)?.measured
      return measured?.width && measured.height
        ? { width: measured.width, height: measured.height }
        : undefined
    },
    port: (id, handle, type) => {
      const bounds = flow.getInternalNode(id)?.internals.handleBounds?.[type]
      const found = bounds?.find((h) => h.id === handle)
      return found ? found.y + found.height / 2 : undefined
    },
  }
}

/**
 * Open a step's settings, from somewhere other than the step itself (an
 * issue naming it), at the setting it names: the canvas comes back, with
 * the step in the middle of it for when its settings close.
 */
export function useOpenStep() {
  const api = useBuilderApi()
  const flow = useReactFlow<FlowNode, FlowEdge>()
  return React.useCallback(
    (id: string, field?: string) => {
      const store = api.getState()
      store.setView("canvas")
      store.edit(id, field)
      const node = store.nodes.find((n) => n.id === id)
      if (!node) return
      const width = node.measured?.width ?? 240
      const height = node.measured?.height ?? 64
      void flow.setCenter(
        node.position.x + width / 2,
        node.position.y + height / 2,
        {
          zoom: Math.max(flow.getZoom(), 0.8),
          duration: reducedMotion() ? 0 : 250,
        }
      )
    },
    [api, flow]
  )
}

/**
 * Pan, at the same zoom, just enough that these steps are wholly in view;
 * the last of them wins when they can't all fit.
 */
export function revealSteps(
  flow: ReactFlowInstance<FlowNode, FlowEdge>,
  frame: HTMLElement | null,
  ids: string[]
) {
  const box = frame?.getBoundingClientRect()
  const nodes = ids.flatMap((id) => {
    const node = flow.getNode(id)
    return node ? [node] : []
  })
  if (!box?.width || !nodes.length) return
  const { x, y, zoom } = flow.getViewport()
  const pad = 24
  const extent = (list: FlowNode[]) => ({
    left: Math.min(...list.map((n) => n.position.x)) * zoom + x,
    top: Math.min(...list.map((n) => n.position.y)) * zoom + y,
    right:
      Math.max(...list.map((n) => n.position.x + (n.measured?.width ?? 240))) *
        zoom +
      x,
    bottom:
      Math.max(...list.map((n) => n.position.y + (n.measured?.height ?? 64))) *
        zoom +
      y,
  })
  let e = extent(nodes)
  if (
    e.right - e.left > box.width - pad * 2 ||
    e.bottom - e.top > box.height - pad * 2
  ) {
    e = extent(nodes.slice(-1))
  }
  const dx =
    e.left < pad
      ? pad - e.left
      : e.right > box.width - pad
        ? box.width - pad - e.right
        : 0
  const dy =
    e.top < pad
      ? pad - e.top
      : e.bottom > box.height - pad
        ? box.height - pad - e.bottom
        : 0
  if (!dx && !dy) return
  void flow.setViewport(
    { x: x + dx, y: y + dy, zoom },
    { duration: reducedMotion() ? 0 : 200 }
  )
}

/** The document as the builder holds it right now; follows every edit. */
export function useBuilderDocument<Doc>(): Doc {
  const state = useBuilder(
    useShallow((s) => ({
      adapter: s.adapter,
      meta: s.meta,
      nodes: s.nodes,
      edges: s.edges,
    }))
  )
  return React.useMemo(() => documentOf(state) as Doc, [state])
}

/** Saves JSON as a file in the browser's downloads. */
export function downloadJson(value: unknown, fileName: string) {
  const blob = new Blob([`${JSON.stringify(value, null, 2)}\n`], {
    type: "application/json",
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = fileName
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * The details panel's width, in rem so the text-size preference
 * scales it. Steps are set up in their own dialog, so the panel only holds
 * the overview and stays narrow.
 */
export const DETAILS_WIDTH = { initial: 22, min: 18, max: 36 }

const clampWidth = (rem: number, max = DETAILS_WIDTH.max) =>
  Math.round(Math.min(Math.max(rem, DETAILS_WIDTH.min), max) * 4) / 4

/**
 * The details panel's width and a way to change it. It's this viewer's
 * own setting for each builder (`key`), remembered in this browser;
 * without storage it's the default every time.
 */
export function useDetailsWidth(key: string) {
  const [width, setWidth] = React.useState(() => {
    try {
      const stored = Number(window.localStorage.getItem(key))
      return stored ? clampWidth(stored) : DETAILS_WIDTH.initial
    } catch {
      return DETAILS_WIDTH.initial
    }
  })
  const change = React.useCallback(
    (
      next: number,
      { save = true, max }: { save?: boolean; max?: number } = {}
    ) => {
      const settled = clampWidth(next, max)
      setWidth(settled)
      if (!save) return
      try {
        window.localStorage.setItem(key, String(settled))
      } catch {
        // Not remembered; it still applies until the page closes.
      }
    },
    [key]
  )
  return [width, change] as const
}

/** ⌘Z and ⇧⌘Z (or ⌘Y), except while typing, where they undo the typing. */
export function useUndoKeys() {
  const api = useBuilderApi()
  React.useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return
      const target = event.target as HTMLElement | null
      if (
        target?.closest(
          "input, textarea, [contenteditable='true'], [role=dialog]"
        )
      )
        return
      const key = event.key.toLowerCase()
      if (key === "z") {
        event.preventDefault()
        if (event.shiftKey) api.getState().redo()
        else api.getState().undo()
      } else if (key === "y") {
        event.preventDefault()
        api.getState().redo()
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [api])
}
