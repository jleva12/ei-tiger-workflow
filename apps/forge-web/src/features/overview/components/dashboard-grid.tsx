import * as React from "react"
import { cn } from "cn"
import GridLayout, {
  noCompactor,
  verticalCompactor,
  type EventCallback,
  type Layout,
} from "react-grid-layout"
import { absoluteStrategy } from "react-grid-layout/core"

import {
  DRAG_HANDLE,
  WidgetEditBar,
} from "@/features/overview/components/widget-edit-bar"
import { WidgetTitle } from "@/features/overview/components/widget-title"
import {
  arranged,
  bottomOf,
  GRID_COLUMNS,
  inReadingOrder,
  retitled,
  settle,
  withoutWidget,
  type DashboardWidget,
  type PlacedWidget,
} from "@/features/overview/lib/dashboard"

import "./dashboard-grid.css"

/*
 * A dashboard's widgets on react-grid-layout, with no gaps between them:
 * each draws its own hairlines, so neighbours read as the overview's panel
 * rows. A widget is at least its layout height and grows to fit what it
 * shows, measured before paint; widgets on the same line stretch to the
 * tallest (see `settle`). When the shell is under 900px they stack in
 * reading order instead, one a row.
 *
 * Customizing, each widget is a card with a bar: dragged by its bar, sized
 * from its right edge, bottom edge or corner, renamed or removed there.
 */

// One pixel a row, so heights are exact; margins are the widgets' padding.
const GRID = {
  cols: GRID_COLUMNS,
  rowHeight: 1,
  margin: [0, 0],
  containerPadding: [0, 0],
} as const
const STILL = { enabled: false }
const FIXED = { enabled: false }
const DRAGGED = {
  enabled: true,
  handle: `.${DRAG_HANDLE}`,
  cancel: "button, input",
}
const SIZED = { enabled: true, handles: ["e", "s", "se"] as const }

const NO_HEIGHTS: ReadonlyMap<string, number> = new Map()

/** What each widget's content needs, in pixels. */
function measure(grid: HTMLElement) {
  const heights = new Map<string, number>()
  for (const content of grid.querySelectorAll<HTMLElement>(
    "[data-widget-content]"
  )) {
    const id = content.dataset.widgetContent
    if (id) heights.set(id, Math.ceil(content.getBoundingClientRect().height))
  }
  return heights
}

const sameHeights = (
  a: ReadonlyMap<string, number>,
  b: ReadonlyMap<string, number>
) => a.size === b.size && [...a].every(([id, height]) => b.get(id) === height)

/** Whether react-grid-layout holds the layout the widgets were placed in. */
function holds(held: Layout, placed: readonly PlacedWidget[]) {
  const items = new Map(held.map((item) => [item.i, item]))
  return (
    held.length === placed.length &&
    placed.every((item) => {
      const found = items.get(item.i)
      return (
        found?.x === item.x &&
        found.y === item.y &&
        found.w === item.w &&
        found.h === item.h
      )
    })
  )
}

/** The nearest box that scrolls a node, or the page. */
function scrollerOf(node: HTMLElement) {
  for (let at = node.parentElement; at; at = at.parentElement) {
    const { overflowY } = getComputedStyle(at)
    if (
      /auto|scroll|overlay/.test(overflowY) &&
      at.scrollHeight > at.clientHeight
    )
      return at
  }
  return document.scrollingElement as HTMLElement | null
}

// How near an edge the pointer scrolls the page, and the fastest it does.
const EDGE = 72
const FASTEST = 20

/**
 * While a widget is dragged or sized, the page scrolls when the pointer is
 * held near its top or bottom, so a widget can go anywhere, however tall the
 * dashboard or the widget. The widget keeps to the pointer as the page moves
 * under it: react-grid-layout follows the pointer's moves, so each step
 * repeats the last one.
 */
function useEdgeScroll(
  grid: React.RefObject<HTMLElement | null>,
  active: boolean
) {
  React.useEffect(() => {
    const node = grid.current
    const scroller = active && node ? scrollerOf(node) : null
    if (!scroller) return
    let pointer: { x: number; y: number } | null = null
    const track = (event: MouseEvent) => {
      if (event.isTrusted) pointer = { x: event.clientX, y: event.clientY }
    }
    let frame = requestAnimationFrame(function step() {
      frame = requestAnimationFrame(step)
      if (!pointer) return
      const box =
        scroller === document.scrollingElement
          ? { top: 0, bottom: window.innerHeight }
          : scroller.getBoundingClientRect()
      const top = Math.max(box.top, 0) + EDGE
      const bottom = Math.min(box.bottom, window.innerHeight) - EDGE
      const over =
        pointer.y < top
          ? pointer.y - top
          : pointer.y > bottom
            ? pointer.y - bottom
            : 0
      if (!over) return
      const before = scroller.scrollTop
      scroller.scrollTop +=
        Math.sign(over) *
        Math.ceil(Math.min(1, Math.abs(over) / EDGE) * FASTEST)
      if (scroller.scrollTop === before) return
      document.dispatchEvent(
        new MouseEvent("mousemove", {
          clientX: pointer.x,
          clientY: pointer.y,
          bubbles: true,
        })
      )
    })
    window.addEventListener("mousemove", track)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener("mousemove", track)
    }
  }, [grid, active])
}

/** What customizing a dashboard needs. */
export type GridEditor = {
  /** Each widget type's name and least width, and whether it has a title. */
  catalog: Readonly<
    Record<string, { title: string; minW?: number; untitled?: boolean }>
  >
  onChange: (widgets: DashboardWidget[]) => void
  /** A widget's own settings, for its bar. */
  controls?: (widget: DashboardWidget) => React.ReactNode
}

export function DashboardGrid({
  widgets,
  render,
  bare,
  editor,
}: {
  widgets: DashboardWidget[]
  render: (widget: DashboardWidget) => React.ReactNode
  /** Whether a widget draws its own frame, so its cell adds no hairlines. */
  bare?: (widget: DashboardWidget) => boolean
  /** Customizing: widgets move, resize, and can be renamed or removed. */
  editor?: GridEditor
}) {
  const ref = React.useRef<HTMLDivElement>(null)
  const [width, setWidth] = React.useState<number | null>(null)
  const [heights, setHeights] = React.useState<ReadonlyMap<string, number>>()
  // react-grid-layout takes a new layout in an effect, a render behind, and
  // says so through onLayoutChange; the grid shows once it holds the first
  // measured one, and stays shown.
  const [held, setHeld] = React.useState<Layout | null>(null)
  const [shown, setShown] = React.useState(false)
  // A drag or a resize keeps the layout it started with: a new one would
  // reset the widget under the pointer.
  const [frozen, setFrozen] = React.useState<PlacedWidget[] | null>(null)

  const remeasure = React.useCallback(() => {
    if (!ref.current) return
    const next = measure(ref.current)
    setHeights((last) => (last && sameHeights(last, next) ? last : next))
  }, [])

  // The width before paint, then whenever the shell resizes.
  React.useLayoutEffect(() => {
    const node = ref.current
    if (!node) return
    const read = () => setWidth(node.getBoundingClientRect().width)
    read()
    const observer = new ResizeObserver(read)
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  // Every render can change what a widget needs: new data, a new width.
  React.useLayoutEffect(() => {
    if (width !== null) remeasure()
  })

  // So can a widget on its own, such as a panel's breakdown switch.
  const ids = widgets.map((widget) => widget.id).join(" ")
  const ready = width !== null
  React.useEffect(() => {
    const node = ref.current
    if (!ready || !node) return
    const observer = new ResizeObserver(remeasure)
    for (const content of node.querySelectorAll("[data-widget-content]")) {
      observer.observe(content)
    }
    return () => observer.disconnect()
  }, [ready, ids, remeasure])

  const byId = React.useMemo(
    () => new Map(widgets.map((widget) => [widget.id, widget])),
    [widgets]
  )
  // What the grid starts with, before anything is measured.
  const unmeasured = React.useMemo(() => settle(widgets, NO_HEIGHTS), [widgets])
  const placed = React.useMemo(
    () => (heights ? settle(widgets, heights) : unmeasured),
    [widgets, heights, unmeasured]
  )
  if (!shown && heights && holds(held ?? unmeasured, placed)) setShown(true)

  const editing = Boolean(editor)
  const catalog = editor?.catalog
  useEdgeScroll(ref, frozen !== null)
  const layout = React.useMemo(
    () =>
      (frozen ?? placed).map((item) => {
        const type = byId.get(item.i)?.type
        const minW = type && catalog?.[type]?.minW
        return minW ? { ...item, minW } : item
      }),
    [frozen, placed, byId, catalog]
  )

  const begin = () => setFrozen(placed)
  const onDragStop: EventCallback = (next) => {
    setFrozen(null)
    editor?.onChange(arranged(widgets, next))
  }
  const onResizeStop: EventCallback = (next, from, to) => {
    setFrozen(null)
    editor?.onChange(
      arranged(widgets, next, from && to ? { from, to } : undefined)
    )
  }

  return (
    <div
      ref={ref}
      // Holds the grid's height while it catches up, so what's below stays put.
      style={{ minHeight: bottomOf(placed) }}
      className="min-w-0 @max-[900px]/shell:min-h-0!"
    >
      {width !== null && (
        <GridLayout
          width={width}
          layout={layout}
          gridConfig={GRID}
          dragConfig={editing ? DRAGGED : STILL}
          resizeConfig={editing ? SIZED : FIXED}
          compactor={editing ? verticalCompactor : noCompactor}
          positionStrategy={absoluteStrategy}
          onLayoutChange={setHeld}
          onDragStart={begin}
          onDragStop={onDragStop}
          onResizeStart={begin}
          onResizeStop={onResizeStop}
          className={cn(
            "forge-dashboard relative @max-[900px]/shell:h-auto!",
            editing && "forge-dashboard--editing",
            !shown && "invisible"
          )}
        >
          {inReadingOrder(frozen ?? placed).map((item) => {
            const widget = byId.get(item.i)
            if (!widget) return null
            const first = item.x === 0
            const last = item.x + item.w === GRID_COLUMNS
            const name = catalog?.[widget.type]?.title ?? widget.type
            // Its own frame, as a table's; customizing, every widget's a card.
            const framed = !bare?.(widget)
            return (
              <div
                key={item.i}
                className={cn(
                  "flex flex-col",
                  editing
                    ? "@max-[900px]/shell:[&>.react-resizable-handle]:hidden"
                    : "[&>.react-resizable-handle]:hidden",
                  !first && !editing && framed && "border-l",
                  "@max-[900px]/shell:static! @max-[900px]/shell:h-auto! @max-[900px]/shell:w-auto! @max-[900px]/shell:border-l-0"
                )}
              >
                {editing && (
                  <div
                    aria-hidden="true"
                    className="forge-dashboard-card pointer-events-none absolute inset-1 rounded-(--radius-card) border bg-card @max-[900px]/shell:hidden"
                  />
                )}
                <div
                  data-widget-content={item.i}
                  className={cn(
                    "flex min-w-0 flex-col @max-[900px]/shell:px-0",
                    editing
                      ? "relative px-4 pt-3 pb-3 @max-[900px]/shell:border-t @max-[900px]/shell:pt-5"
                      : framed
                        ? "border-t pt-5 pb-2"
                        : "pb-8",
                    !editing && !first && "pl-6",
                    !editing && !last && "pr-6"
                  )}
                >
                  {editor && (
                    <WidgetEditBar
                      name={name}
                      title={widget.title}
                      controls={editor.controls?.(widget)}
                      onRename={
                        catalog?.[widget.type]?.untitled
                          ? undefined
                          : (title) =>
                              editor.onChange(
                                retitled(widgets, widget.id, title)
                              )
                      }
                      onRemove={() =>
                        editor.onChange(withoutWidget(widgets, widget.id))
                      }
                    />
                  )}
                  <WidgetTitle value={widget.title}>
                    {render(widget)}
                  </WidgetTitle>
                </div>
              </div>
            )
          })}
        </GridLayout>
      )}
    </div>
  )
}
