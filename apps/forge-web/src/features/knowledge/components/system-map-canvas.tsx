import * as React from "react"
import { MinusSignIcon } from "@hugeicons/core-free-icons"
import cytoscape, {
  type Core,
  type NodeCollection,
  type StylesheetJson,
} from "cytoscape"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Toggle } from "@/components/ui/toggle"
import { tokenColor } from "@/features/code-graph/components/token-color"
import type { CodeRepository } from "@/features/code-repositories/lib/api"
import {
  CONNECTION_KINDS,
  type Connection,
  type MapLayout,
  type MapPoint,
  type SystemMapLayout,
  type SystemMapUpdate,
} from "../lib/system"

/** What the map has selected: an application, a connection, or nothing. */
export type MapSelection = {
  type: "application" | "connection"
  id: string
} | null

type Layout = MapLayout

const LAYOUTS = [
  { value: "cose", label: "Force" },
  { value: "breadthfirst", label: "Layered" },
  { value: "circle", label: "Circle" },
]

// The four project colours, when their tokens can't be read.
const PROJECT_FALLBACK = ["#b5abd2", "#9abbb0", "#d7b38e", "#9badc9"]

const NODE_HEIGHT = 30
const NODE_PADDING = 28
const FONT_SIZE = 12
const FIT_PADDING = 40
// Fitting a few applications stops here rather than blowing them up.
const MOST_FITTED_ZOOM = 1.25

/** Fit the map to the canvas, no closer than a comfortable reading size. */
function fitView(cy: Core) {
  cy.fit(undefined, FIT_PADDING)
  if (cy.zoom() > MOST_FITTED_ZOOM) {
    cy.zoom(MOST_FITTED_ZOOM)
    cy.center()
  }
}

function arrange(cy: Core, mode: Layout, fit = true) {
  const common = { animate: false, fit: false }
  if (mode === "breadthfirst") {
    cy.layout({
      ...common,
      name: "breadthfirst",
      directed: true,
      spacingFactor: 1.4,
    }).run()
  } else if (mode === "circle") {
    cy.layout({ ...common, name: "circle", spacingFactor: 1.3 }).run()
  } else {
    // Pills are wide: strong repulsion and weak gravity keep applications
    // without connections apart.
    cy.layout({
      ...common,
      name: "cose",
      randomize: false,
      nodeDimensionsIncludeLabels: true,
      nodeRepulsion: () => 400_000,
      nodeOverlap: 40,
      idealEdgeLength: () => 140,
      gravity: 0.3,
      componentSpacing: 80,
      numIter: 1000,
    }).run()
  }
  if (fit) fitView(cy)
}

/** Where each node is now, by application (repository) ID. */
function positionsOf(nodes: NodeCollection) {
  const positions: Record<string, MapPoint> = {}
  nodes.forEach((node) => {
    const { x, y } = node.position()
    positions[node.data("recordID")] = { x: Math.round(x), y: Math.round(y) }
  })
  return positions
}

/**
 * Places applications the saved map doesn't have in a column to the right
 * of those it does, so a new one never lands on top of another.
 */
function placeBeside(cy: Core, placed: string[], unplaced: string[]) {
  const box = cy
    .nodes()
    .filter((node) => placed.includes(node.id()))
    .boundingBox()
  unplaced.forEach((id, index) => {
    cy.getElementById(id).position({
      x: box.x2 + 140,
      y: box.y1 + index * (NODE_HEIGHT + 30),
    })
  })
}

/** How wide an application's node is: its name in the canvas's font, padded. */
function widthFor(label: string, font: string) {
  const context = document.createElement("canvas").getContext("2d")
  if (!context) return label.length * 7 + NODE_PADDING
  context.font = `500 ${FONT_SIZE}px ${font}`
  return Math.ceil(context.measureText(label).width) + NODE_PADDING
}

/**
 * The map's stylesheet, from the theme's tokens: applications as pills
 * tinted with their colour (dashed while they aren't in the code graph),
 * connections as arrows labelled with how they connect.
 */
function styles(font: string): StylesheetJson {
  const colors = PROJECT_FALLBACK.map((fallback, index) =>
    tokenColor(`--project-${index + 1}`, fallback)
  )
  const panel = tokenColor("--card", "#ffffff")
  const ink = tokenColor("--foreground", "#1f2328")
  const muted = tokenColor("--muted-foreground", "#6a727e")
  return [
    {
      selector: "node",
      style: {
        shape: "round-rectangle",
        "corner-radius": "8",
        width: "data(width)",
        height: NODE_HEIGHT,
        label: "data(label)",
        "text-valign": "center",
        "text-halign": "center",
        "font-family": font,
        "font-size": FONT_SIZE,
        "font-weight": 500,
        color: ink,
        "background-opacity": 0.3,
        "border-width": 1.5,
        "min-zoomed-font-size": 6,
      },
    },
    ...colors.map((color, index) => ({
      selector: `node[color = ${index}]`,
      style: { "background-color": color, "border-color": color },
    })),
    { selector: "node.pending", style: { "border-style": "dashed" } },
    {
      selector: "edge",
      style: {
        width: 1.5,
        "line-color": muted,
        "line-opacity": 0.55,
        "target-arrow-color": muted,
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.9,
        "curve-style": "bezier",
        "control-point-step-size": 48,
        label: "data(label)",
        "font-family": font,
        "font-size": 10,
        color: muted,
        "text-rotation": "autorotate",
        "text-background-color": panel,
        "text-background-opacity": 1,
        "text-background-padding": "2px",
        "min-zoomed-font-size": 7,
      },
    },
    {
      selector: "edge.selected",
      style: {
        "line-color": ink,
        "line-opacity": 1,
        "target-arrow-color": ink,
        color: ink,
        width: 2.5,
      },
    },
    {
      selector: "node.selected",
      style: {
        "border-width": 2.5,
        "border-color": ink,
        "border-style": "solid",
      },
    },
    { selector: ".muted", style: { opacity: 0.2 } },
    { selector: "edge.no-label", style: { label: "" } },
  ]
}

/**
 * A system's applications as a pannable, zoomable map: one pill per
 * application (code repository), tinted with its colour in the list, and an
 * arrow per connection from one to another, labelled with how. Selecting an
 * application dims everything it isn't connected to; double-clicking one
 * opens its code graph. It opens as people left it (`saved`, the same for
 * everyone): where applications were dragged, and the layout picked. Those
 * who may change it save each drag and arrangement (`onSave`); for anyone
 * else, moving things only lasts until they leave. Places others save
 * meanwhile are followed when the saved map is read again.
 */
export function SystemMapCanvas({
  saved,
  onSave,
  applications,
  connections,
  selection,
  onSelect,
  onOpen,
}: {
  /** The map as people left it. */
  saved: SystemMapLayout
  /** Saves applications' new places, or a layout; absent for viewers. */
  onSave?: (update: SystemMapUpdate) => void
  /** The knowledge base's applications, in the list's order (their colours follow it). */
  applications: CodeRepository[]
  /** Connections between them. */
  connections: Connection[]
  selection: MapSelection
  onSelect: (selection: MapSelection) => void
  /** Open an application's code graph. */
  onOpen: (id: string) => void
}) {
  const container = React.useRef<HTMLDivElement>(null)
  const instance = React.useRef<Core | null>(null)
  const handlers = React.useRef({ onSelect, onOpen })
  // Whether the view is the fitted one, not one the user moved.
  const fitted = React.useRef(true)
  const [labels, setLabels] = React.useState(true)
  const [layout, setLayout] = React.useState<Layout>(saved.layout ?? "cose")
  // The latest saved map and saver, for the canvas's own event handlers.
  const latest = React.useRef({ saved, onSave })
  React.useEffect(() => {
    latest.current = { saved, onSave }
  }, [saved, onSave])

  React.useEffect(() => {
    handlers.current = { onSelect, onOpen }
  }, [onSelect, onOpen])

  React.useEffect(() => {
    if (!container.current) return
    const font = getComputedStyle(document.body).fontFamily
    const cy = cytoscape({
      container: container.current,
      elements: [],
      style: styles(font),
      minZoom: 0.2,
      maxZoom: 3,
      boxSelectionEnabled: false,
      autounselectify: true,
    })
    instance.current = cy
    cy.on("tap", "node", (e) =>
      handlers.current.onSelect({
        type: "application",
        id: e.target.data("recordID"),
      })
    )
    cy.on("tap", "edge", (e) =>
      handlers.current.onSelect({
        type: "connection",
        id: e.target.data("recordID"),
      })
    )
    cy.on("dbltap", "node", (e) =>
      handlers.current.onOpen(e.target.data("recordID"))
    )
    cy.on("tap", (e) => {
      if (e.target === cy) handlers.current.onSelect(null)
    })
    // Until the user pans, zooms or drags, the map keeps fitting the canvas
    // as it resizes.
    fitted.current = true
    cy.on("dragpan scrollzoom pinchzoom grabon", () => {
      fitted.current = false
    })
    // Save where an application was dropped: that one only, so people
    // moving different ones don't undo each other.
    cy.on("dragfree", "node", (e) =>
      latest.current.onSave?.({ positions: positionsOf(e.target) })
    )
    const resize = new ResizeObserver(() => {
      cy.resize()
      if (fitted.current) fitView(cy)
    })
    resize.observe(container.current)
    // The theme provider switches the root's light/dark class.
    const theme = new MutationObserver(() => cy.style(styles(font)))
    theme.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-forge-contrast"],
    })
    return () => {
      resize.disconnect()
      theme.disconnect()
      cy.destroy()
      instance.current = null
    }
  }, [])

  React.useEffect(() => {
    const cy = instance.current
    if (!cy) return
    const font = getComputedStyle(document.body).fontFamily
    const ids = new Set([
      ...applications.map((repo) => `p:${repo.id}`),
      ...connections.map((link) => `l:${link.id}`),
    ])
    const existing = cy.nodes().length
    let added = false
    const placed: string[] = []
    const unplaced: string[] = []
    cy.batch(() => {
      cy.elements()
        .filter((element) => !ids.has(element.id()))
        .remove()
      applications.forEach((repo, index) => {
        const id = `p:${repo.id}`
        const data = {
          label: repo.name,
          width: widthFor(repo.name, font),
          color: index % PROJECT_FALLBACK.length,
        }
        const found = cy.getElementById(id)
        if (found.length) {
          // Renamed, or moved in the list.
          found.data(data)
        } else {
          added = true
          const remembered = latest.current.saved.positions[repo.id]
          ;(remembered ? placed : unplaced).push(id)
          cy.add({
            data: { id, recordID: repo.id, ...data },
            // Where it was left, or a spiral start, so layouts don't begin
            // from one point.
            position: remembered ?? {
              x: Math.cos(index * 2.4) * (90 + index * 12),
              y: Math.sin(index * 2.4) * (90 + index * 12),
            },
          })
        }
        cy.getElementById(id).toggleClass("pending", !repo.last_success)
      })
      connections.forEach((link) => {
        const id = `l:${link.id}`
        const source = `p:${link.source_repository_id}`
        const target = `p:${link.target_repository_id}`
        // Cytoscape throws on an edge whose end isn't on the canvas.
        if (
          cy.getElementById(id).length ||
          !cy.getElementById(source).length ||
          !cy.getElementById(target).length
        ) {
          return
        }
        cy.add({
          data: {
            id,
            recordID: link.id,
            source,
            target,
            label: CONNECTION_KINDS[link.kind]?.label ?? link.kind,
          },
        })
      })
    })
    // New applications go where they were left; with none saved, the map
    // is arranged, and saved ones keep their places while new ones go beside
    // them. New connections leave the map as it is.
    if (!added) return
    const fit = existing === 0 || fitted.current
    const remembered = cy
      .nodes()
      .filter((node) => !unplaced.includes(node.id()))
      .map((node) => node.id())
    if (unplaced.length === 0) {
      if (fit) fitView(cy)
      return
    }
    if (existing === 0 && placed.length === 0) {
      // Never arranged: arrange it, and save that for everyone.
      arrange(cy, layout, fit)
      latest.current.onSave?.({ layout, positions: positionsOf(cy.nodes()) })
    } else {
      placeBeside(cy, remembered, unplaced)
      if (fit) fitView(cy)
      latest.current.onSave?.({
        positions: positionsOf(
          cy.nodes().filter((node) => unplaced.includes(node.id()))
        ),
      })
    }
    // The layout is read when applications arrive, not when it changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applications, connections])

  // Follow places others saved since (the saved map read again), except
  // for an application being dragged.
  React.useEffect(() => {
    const cy = instance.current
    if (!cy) return
    cy.nodes().forEach((node) => {
      const point = saved.positions[node.data("recordID")]
      if (!point || node.grabbed()) return
      const { x, y } = node.position()
      if (Math.abs(x - point.x) > 1 || Math.abs(y - point.y) > 1)
        node.position(point)
    })
  }, [saved])

  React.useEffect(() => {
    const cy = instance.current
    if (!cy) return
    cy.batch(() => {
      cy.edges().toggleClass("no-label", !labels)
      cy.elements().removeClass("selected muted")
      if (selection) {
        const selected = cy.getElementById(
          `${selection.type === "application" ? "p" : "l"}:${selection.id}`
        )
        if (!selected.length) return
        selected.addClass("selected")
        const neighborhood =
          selection.type === "application"
            ? selected.closedNeighborhood()
            : selected.union(selected.connectedNodes())
        cy.elements().difference(neighborhood).addClass("muted")
      }
    })
  }, [selection, labels, applications, connections])

  function zoom(factor: number) {
    const cy = instance.current
    if (cy) {
      cy.zoom({
        level: cy.zoom() * factor,
        renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 },
      })
    }
  }

  return (
    <div className="relative size-full min-h-0">
      {/* Sized, not positioned: Cytoscape's own stylesheet makes its
          container position: relative. */}
      <div
        ref={container}
        className="size-full"
        role="img"
        aria-label={`System map: ${applications.length} applications and ${connections.length} connections. The list beside it shows the same applications and connections with a keyboard.`}
      />
      <div
        className="absolute top-3 right-3 flex flex-wrap items-center justify-end gap-1.5"
        aria-label="Map controls"
      >
        <Button
          size="xs"
          variant="outline"
          onClick={() => {
            fitted.current = true
            if (instance.current) fitView(instance.current)
          }}
        >
          Fit
        </Button>
        <Button
          size="icon-xs"
          variant="outline"
          aria-label="Zoom in"
          onClick={() => zoom(1.3)}
        >
          <Icon icon="plus" />
        </Button>
        <Button
          size="icon-xs"
          variant="outline"
          aria-label="Zoom out"
          onClick={() => zoom(1 / 1.3)}
        >
          <Icon icon={MinusSignIcon} />
        </Button>
        <Button
          size="icon-xs"
          variant="outline"
          aria-label="Arrange again"
          title="Arrange again"
          onClick={() => {
            if (!instance.current) return
            fitted.current = true
            arrange(instance.current, layout)
            onSave?.({
              layout,
              positions: positionsOf(instance.current.nodes()),
            })
          }}
        >
          <Icon icon="refresh" />
        </Button>
        <Select
          items={LAYOUTS}
          value={layout}
          onValueChange={(value) => {
            const next: Layout =
              value === "breadthfirst" || value === "circle" ? value : "cose"
            setLayout(next)
            if (!instance.current) return
            fitted.current = true
            arrange(instance.current, next)
            onSave?.({
              layout: next,
              positions: positionsOf(instance.current.nodes()),
            })
          }}
        >
          <SelectTrigger size="sm" aria-label="Map layout">
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false}>
            {LAYOUTS.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                {item.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Toggle
          size="sm"
          variant="outline"
          pressed={labels}
          onPressedChange={setLabels}
        >
          Labels
        </Toggle>
      </div>
      <span className="pointer-events-none absolute bottom-3 left-3 text-2xs text-subtle">
        Drag applications · Scroll to zoom · Double-click one to open its code
        graph
      </span>
    </div>
  )
}
