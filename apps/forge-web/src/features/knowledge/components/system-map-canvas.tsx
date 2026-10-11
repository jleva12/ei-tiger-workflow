import * as React from "react"
import { MinusSignIcon } from "@hugeicons/core-free-icons"
import cytoscape, {
  type Core,
  type LayoutOptions,
  type NodeCollection,
  type NodeSingular,
  type StylesheetJson,
} from "cytoscape"
import fcose from "cytoscape-fcose"

import { Icon } from "@/components/forge/icon"
import { resolveIcon } from "@/components/forge/icons"
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
  COMPONENT_KINDS,
  CONNECTION_KINDS,
  endOf,
  type Component,
  type ComponentKind,
  type Connection,
  type Group,
  type MapEnd,
  type MapLayout,
  type MapPoint,
  type SystemMapLayout,
  type SystemMapUpdate,
} from "../lib/system"

/**
 * What the map has selected: an application, a component, a group, a
 * connection, or nothing.
 */
export type MapSelection = {
  type: "application" | "component" | "group" | "connection"
  id: string
} | null

/** What the map answers those beside it. */
export type SystemMapCanvasHandle = {
  /**
   * Where an application or a component should go as it joins a group
   * (below what's in it already, or where the empty group is), or leaves
   * its own (null: just outside it); undefined to leave it where it is.
   */
  placeFor(end: MapEnd, groupId: string | null): MapPoint | undefined
}

type Layout = MapLayout

// The force layout that keeps groups together and apart from the rest:
// Cytoscape's own lets nodes outside a group drift into its box.
cytoscape.use(fcose)

const LAYOUTS = [
  { value: "cose", label: "Force" },
  { value: "breadthfirst", label: "Layered" },
  { value: "circle", label: "Circle" },
]

// The four project colours, when their tokens can't be read.
const PROJECT_FALLBACK = ["#b5abd2", "#9abbb0", "#d7b38e", "#9badc9"]

const PREFIX = {
  application: "p",
  component: "c",
  group: "g",
  connection: "l",
} as const

/** The canvas's ID of something on the map. */
const elementId = (type: keyof typeof PREFIX, id: string) =>
  `${PREFIX[type]}:${id}`

const NODE_HEIGHT = 30
const COMPONENT_HEIGHT = 34
const NODE_PADDING = 28
// A component's icon, left of its name, and the room it takes.
const ICON_SIZE = 15
const ICON_ROOM = 22
const FONT_SIZE = 12
const FIT_PADDING = 40
// Fitting a few applications stops here rather than blowing them up.
const MOST_FITTED_ZOOM = 1.25
// What an empty group measures, before anything is in it.
const EMPTY_GROUP = { width: 180, height: 90 }

/**
 * Each component kind's shape: a database a barrel, a queue a tag, storage
 * a bucket, an external system cut off, a client round; with the room
 * beyond its name the shape's ends take.
 */
const SHAPES: Record<ComponentKind, { shape: string; extra: number }> = {
  database: { shape: "barrel", extra: 10 },
  queue: { shape: "round-tag", extra: 14 },
  cache: { shape: "round-hexagon", extra: 18 },
  storage: { shape: "bottom-round-rectangle", extra: 4 },
  external: { shape: "cut-rectangle", extra: 12 },
  service: { shape: "round-rectangle", extra: 0 },
  client: { shape: "ellipse", extra: 26 },
}

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
  // Groups are drawn around what's in them: the layered and circle
  // layouts place only the rest.
  const placed = cy.elements().not(":parent")
  if (mode === "breadthfirst") {
    placed
      .layout({
        ...common,
        name: "breadthfirst",
        directed: true,
        spacingFactor: 1.4,
      })
      .run()
  } else if (mode === "circle") {
    placed.layout({ ...common, name: "circle", spacingFactor: 1.3 }).run()
  } else {
    // Pills are wide: strong repulsion keeps what isn't connected apart;
    // what's in a group stays close, and the rest stays out of its box.
    cy.layout({
      ...common,
      name: "fcose",
      quality: "proof",
      randomize: true,
      nodeDimensionsIncludeLabels: true,
      packComponents: true,
      nodeRepulsion: () => 20_000,
      idealEdgeLength: () => 130,
      edgeElasticity: () => 0.45,
      nestingFactor: 0.1,
      gravity: 0.25,
      gravityCompound: 1,
      gravityRangeCompound: 1.5,
      nodeSeparation: 90,
      numIter: 2500,
      tile: true,
      tilingPaddingVertical: 24,
      tilingPaddingHorizontal: 24,
    } as LayoutOptions).run()
  }
  if (fit) fitView(cy)
}

/** Where each node is now, by the ID of what it is. */
function positionsOf(nodes: NodeCollection) {
  const positions: Record<string, MapPoint> = {}
  nodes.forEach((node) => {
    const { x, y } = node.position()
    positions[node.data("recordID")] = { x: Math.round(x), y: Math.round(y) }
  })
  return positions
}

/**
 * Places nodes the saved map doesn't have in a column to the right of
 * those it does, so a new one never lands on top of another.
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

/** Where a node goes in a group: below what's in it, or where it's empty. */
function placeIn(group: NodeSingular, node: NodeSingular | undefined) {
  const others = node ? group.children().not(node) : group.children()
  if (!others.length) {
    const { x, y } = group.position()
    return { x: Math.round(x), y: Math.round(y) }
  }
  const box = others.boundingBox({ includeLabels: false })
  const width = node?.width() ?? 120
  return {
    x: Math.round(box.x1 + width / 2),
    y: Math.round(box.y2 + 56 + NODE_HEIGHT / 2),
  }
}

/** How wide a node is: its name in the canvas's font, padded. */
function widthFor(label: string, font: string, extra = 0) {
  const context = document.createElement("canvas").getContext("2d")
  const text = context
    ? ((context.font = `500 ${FONT_SIZE}px ${font}`),
      Math.ceil(context.measureText(label).width))
    : label.length * 7
  return text + NODE_PADDING + extra
}

/** A glyph as an SVG image Cytoscape draws, stroked in a colour. */
function iconUrl(kind: ComponentKind, color: string) {
  const parts = resolveIcon(COMPONENT_KINDS[kind].icon).map(([tag, attrs]) => {
    const written = Object.entries(attrs)
      .filter(([name]) => name !== "key")
      .map(([name, value]) => {
        const attribute = name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)
        const shown = String(value).replace("currentColor", color)
        return `${attribute}="${shown}"`
      })
      .join(" ")
    return `<${tag} ${written}/>`
  })
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none">${parts.join("")}</svg>`
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
}

/**
 * The map's stylesheet, from the theme's tokens: applications as pills
 * tinted with their colour (dashed while they aren't in the code graph);
 * components in their kind's shape with its icon, untinted (an external
 * system dashed, outside the organization); groups as boxes drawn around
 * what's in them, named above; connections as arrows labelled with how
 * they connect.
 */
function styles(font: string): StylesheetJson {
  const colors = PROJECT_FALLBACK.map((fallback, index) =>
    tokenColor(`--project-${index + 1}`, fallback)
  )
  const panel = tokenColor("--card", "#ffffff")
  const ink = tokenColor("--foreground", "#1f2328")
  const muted = tokenColor("--muted-foreground", "#6a727e")
  const surface = tokenColor("--muted", "#f5f6f8")
  const line = tokenColor("--border", "#e6e8eb")
  const kinds = Object.keys(COMPONENT_KINDS) as ComponentKind[]
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
      selector: 'node[type = "component"]',
      style: {
        height: COMPONENT_HEIGHT,
        "background-color": surface,
        "background-opacity": 1,
        "border-color": muted,
        "border-opacity": 0.55,
        "background-width": ICON_SIZE,
        "background-height": ICON_SIZE,
        "background-position-x": NODE_PADDING / 2 - 2,
        "background-position-y": "50%",
        "background-clip": "none",
        "background-fit": "none",
        "text-margin-x": ICON_ROOM / 2,
      },
    },
    ...kinds.map((kind) => ({
      selector: `node[kind = "${kind}"]`,
      style: {
        shape: SHAPES[kind].shape as "round-rectangle",
        "background-image": iconUrl(kind, muted),
        "background-position-x": NODE_PADDING / 2 - 2 + SHAPES[kind].extra / 2,
        "text-margin-x": (ICON_ROOM + SHAPES[kind].extra / 2) / 2,
      },
    })),
    {
      selector: 'node[kind = "external"]',
      style: { "border-style": "dashed" },
    },
    {
      selector: 'node[type = "group"]',
      style: {
        shape: "round-rectangle",
        "corner-radius": "10",
        "background-color": surface,
        "background-opacity": 0.6,
        "border-color": line,
        "border-width": 1.5,
        "border-opacity": 1,
        padding: "22px",
        "text-valign": "top",
        "text-halign": "center",
        "text-margin-y": -6,
        "font-size": 11,
        "font-weight": 600,
        color: muted,
      },
    },
    {
      selector: 'node[type = "group"]:childless',
      style: { width: EMPTY_GROUP.width, height: EMPTY_GROUP.height },
    },
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
        "border-opacity": 1,
        "border-style": "solid",
      },
    },
    { selector: 'node[type = "group"].selected', style: { color: ink } },
    { selector: ".muted", style: { opacity: 0.2 } },
    { selector: "edge.no-label", style: { label: "" } },
  ]
}

/**
 * A system as a pannable, zoomable map: one pill per application (code
 * repository), tinted with its colour in the list; one shape per component
 * (a database, a queue, an external system…) with its kind's icon; a box
 * around each group's applications and components, named above it; and an
 * arrow per connection from one to another, labelled with how. Selecting
 * something dims everything it isn't connected to; double-clicking an
 * application opens its code graph. It opens as people left it (`saved`,
 * the same for everyone): where things were dragged, what's in which
 * group, and the layout picked. Those who may change it save each drag and
 * arrangement (`onSave`); for anyone else, moving things only lasts until
 * they leave. Places and groups others save meanwhile are followed when the
 * saved map is read again.
 */
export function SystemMapCanvas({
  ref,
  saved,
  onSave,
  applications,
  components,
  groups,
  connections,
  selection,
  onSelect,
  onOpen,
}: {
  ref?: React.Ref<SystemMapCanvasHandle>
  /** The map as people left it. */
  saved: SystemMapLayout
  /** Saves new places, or a layout; absent for viewers. */
  onSave?: (update: SystemMapUpdate) => void
  /** The knowledge base's applications, in the list's order (their colours follow it). */
  applications: CodeRepository[]
  /** Its components. */
  components: Component[]
  /** Its groups; what's in each is `saved.groups`. */
  groups: Group[]
  /** Connections between its applications and components. */
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
  // What the canvas placed beside the rest as it arrived, until someone
  // moves it.
  const autoPlaced = React.useRef(new Set<string>())
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

  React.useImperativeHandle(
    ref,
    () => ({
      placeFor(end, groupId) {
        const cy = instance.current
        if (!cy) return undefined
        const node = cy.getElementById(elementId(end.type, end.id))
        if (groupId) {
          const group = cy.getElementById(elementId("group", groupId))
          return group.length
            ? placeIn(group, node.length ? node : undefined)
            : undefined
        }
        const parent = node.parent()
        if (!node.length || !parent.length) return undefined
        const box = parent.boundingBox({})
        return {
          x: Math.round(box.x2 + 60 + node.width() / 2),
          y: Math.round(node.position().y),
        }
      },
    }),
    []
  )

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
        type: e.target.data("type"),
        id: e.target.data("recordID"),
      })
    )
    cy.on("tap", "edge", (e) =>
      handlers.current.onSelect({
        type: "connection",
        id: e.target.data("recordID"),
      })
    )
    cy.on("dbltap", 'node[type = "application"]', (e) =>
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
    // Save where something was dropped: that one only (a group, with
    // what's in it), so people moving different ones don't undo each other.
    cy.on("dragfree", "node", (e) => {
      const moved = e.target.union(e.target.descendants())
      moved.forEach((node: NodeSingular) => {
        autoPlaced.current.delete(node.id())
      })
      latest.current.onSave?.({ positions: positionsOf(moved) })
    })
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
    const { positions, groups: membership } = latest.current.saved
    const known = new Set(groups.map((group) => group.id))
    // The group something is in, when the map has that group.
    const parentOf = (id: string) => {
      const group = membership[id]
      return group && known.has(group) ? elementId("group", group) : undefined
    }
    const ids = new Set([
      ...applications.map((repo) => elementId("application", repo.id)),
      ...components.map((part) => elementId("component", part.id)),
      ...groups.map((group) => elementId("group", group.id)),
      ...connections.map((link) => elementId("connection", link.id)),
    ])
    const existing = cy.nodes().length
    let added = false
    const placed: string[] = []
    const unplaced: string[] = []
    // Placed by the canvas, not by anyone, now in a group: where in it.
    const regrouped: [string, MapPoint][] = []
    // A spiral start, so layouts don't begin from one point.
    const spiral = (index: number) => ({
      x: Math.cos(index * 2.4) * (90 + index * 12),
      y: Math.sin(index * 2.4) * (90 + index * 12),
    })
    const nodes = [
      ...applications.map((repo, index) => ({
        id: elementId("application", repo.id),
        recordID: repo.id,
        data: {
          type: "application",
          label: repo.name,
          width: widthFor(repo.name, font),
          color: index % PROJECT_FALLBACK.length,
        },
        pending: !repo.last_success,
      })),
      ...components.map((part) => ({
        id: elementId("component", part.id),
        recordID: part.id,
        data: {
          type: "component",
          kind: part.kind,
          label: part.name,
          width: widthFor(
            part.name,
            font,
            ICON_ROOM + (SHAPES[part.kind]?.extra ?? 0)
          ),
        },
        pending: false,
      })),
    ]
    cy.batch(() => {
      // Groups first: what's in one needs it on the canvas.
      groups.forEach((group, index) => {
        const id = elementId("group", group.id)
        const data = { type: "group", label: group.name }
        const found = cy.getElementById(id)
        if (found.length) {
          found.data(data)
          return
        }
        added = true
        const remembered = positions[group.id]
        ;(remembered ? placed : unplaced).push(id)
        cy.add({
          data: { id, recordID: group.id, ...data },
          position: remembered ?? spiral(nodes.length + index),
        })
      })
      nodes.forEach(({ id, recordID, data, pending }, index) => {
        const parent = parentOf(recordID)
        const found = cy.getElementById(id)
        if (found.length) {
          // Renamed, moved in the list, or into another group.
          found.data(data)
          const current = found.parent().first()
          if ((current.length ? current.id() : undefined) !== parent) {
            // Placed beside the rest as it arrived, its group known only
            // now (a component added into one): it goes into it. Where is
            // read before it's in it, which changes an empty group's place.
            if (parent && autoPlaced.current.has(id)) {
              regrouped.push([id, placeIn(cy.getElementById(parent), found)])
            }
            found.move({ parent: parent ?? null })
          }
        } else {
          added = true
          const remembered = positions[recordID]
          ;(remembered ? placed : unplaced).push(id)
          const into = parent ? cy.getElementById(parent) : undefined
          cy.add({
            data: { id, recordID, parent, ...data },
            // Where it was left, in its group, or a spiral start.
            position:
              remembered ??
              (into?.length ? placeIn(into, undefined) : spiral(index)),
          })
        }
        cy.getElementById(id).toggleClass("pending", pending)
      })
      // What's gone: a group's members were moved out of it above, unless
      // they're going too, so removing it takes nothing else.
      cy.elements()
        .filter((element) => !ids.has(element.id()))
        .remove()
      connections.forEach((link) => {
        const id = elementId("connection", link.id)
        const from = endOf(link, "source")
        const to = endOf(link, "target")
        const source = elementId(from.type, from.id)
        const target = elementId(to.type, to.id)
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
    for (const [id, point] of regrouped) {
      cy.getElementById(id).position(point)
      autoPlaced.current.delete(id)
    }
    if (regrouped.length) {
      const moved = new Set(regrouped.map(([id]) => id))
      latest.current.onSave?.({
        positions: positionsOf(
          cy.nodes().filter((node) => moved.has(node.id()))
        ),
      })
    }
    // New things go where they were left; with nothing saved, the map is
    // arranged, and saved things keep their places while new ones go into
    // their group, or beside the rest. New connections leave the map as it
    // is.
    if (!added) return
    const fit = existing === 0 || fitted.current
    if (unplaced.length === 0) {
      if (fit) fitView(cy)
      return
    }
    if (existing === 0 && placed.length === 0) {
      // Never arranged: arrange it, and save that for everyone.
      arrange(cy, layout, fit)
      latest.current.onSave?.({ layout, positions: positionsOf(cy.nodes()) })
      return
    }
    // Those in a group were placed in it as they were added.
    const beside = unplaced.filter(
      (id) => !cy.getElementById(id).parent().length
    )
    for (const id of beside) autoPlaced.current.add(id)
    const remembered = cy
      .nodes()
      .filter((node) => !unplaced.includes(node.id()))
      .map((node) => node.id())
    if (beside.length) placeBeside(cy, remembered, beside)
    if (fit) fitView(cy)
    latest.current.onSave?.({
      positions: positionsOf(
        cy.nodes().filter((node) => unplaced.includes(node.id()))
      ),
    })
    // The layout is read when things arrive, not when it changes; the
    // saved map's groups are read with them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applications, components, groups, connections, saved.groups])

  // Follow places others saved since (the saved map read again), except
  // for a node being dragged, and groups drawn around what's in them.
  React.useEffect(() => {
    const cy = instance.current
    if (!cy) return
    cy.nodes().forEach((node) => {
      const point = saved.positions[node.data("recordID")]
      if (!point || node.grabbed() || node.isParent()) return
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
          elementId(selection.type, selection.id)
        )
        if (!selected.length) return
        selected.addClass("selected")
        let shown =
          selection.type === "connection"
            ? selected.union(selected.connectedNodes())
            : selection.type === "group"
              ? selected
                  .union(selected.descendants())
                  .union(selected.descendants().closedNeighborhood())
              : selected.closedNeighborhood()
        // A group fades what's in it: keep those around what's shown.
        shown = shown.union(shown.nodes().ancestors())
        cy.elements().difference(shown).addClass("muted")
      }
    })
  }, [
    selection,
    labels,
    applications,
    components,
    groups,
    connections,
    saved.groups,
  ])

  // Bring what's selected into view, when it's off the canvas.
  React.useEffect(() => {
    const cy = instance.current
    if (!cy || !selection) return
    const selected = cy.getElementById(elementId(selection.type, selection.id))
    if (!selected.length) return
    const box = selected.renderedBoundingBox({})
    if (
      box.x2 < 0 ||
      box.y2 < 0 ||
      box.x1 > cy.width() ||
      box.y1 > cy.height()
    ) {
      cy.center(selected)
    }
  }, [selection])

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
        aria-label={`System map: ${applications.length} applications, ${components.length} components, ${groups.length} groups and ${connections.length} connections. The list beside it shows the same with a keyboard.`}
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
        Drag to move · Scroll to zoom · Double-click an application to open its
        code graph
      </span>
    </div>
  )
}
