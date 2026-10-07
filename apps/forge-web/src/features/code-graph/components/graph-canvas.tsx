import * as React from "react"
import { MinusSignIcon } from "@hugeicons/core-free-icons"
import cytoscape, { type Core, type StylesheetJson } from "cytoscape"

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
import {
  familyOf,
  lookOf,
  nodeLabel,
  type EdgeVersion,
  type NodeFamily,
  type NodeVersion,
  type Selection,
} from "../lib/graph"
import { CANVAS_SHAPES, FAMILIES, GLYPH_SCALE, glyphImage } from "./node-look"
import { tokenColor } from "./token-color"

type Layout = "concentric" | "cose"

const LAYOUTS = [
  { value: "concentric", label: "Radial" },
  { value: "cose", label: "Force" },
]

// Each family's colour when its token can't be read.
const FALLBACK: Record<NodeFamily, string> = {
  file: "#2a78d6",
  type: "#eb6834",
  callable: "#1baf7a",
  value: "#4a3aa7",
  other: "#9a9ca3",
}

function arrange(cy: Core, mode: Layout, fit = true) {
  if (mode === "concentric") {
    cy.layout({
      name: "concentric",
      animate: false,
      fit,
      padding: 32,
      minNodeSpacing: 35,
      concentric: (node) => node.degree(false),
      levelWidth: () => 3,
    }).run()
  } else {
    cy.layout({
      name: "cose",
      animate: false,
      randomize: false,
      fit,
      padding: 32,
      nodeRepulsion: () => 5000,
      idealEdgeLength: () => 75,
      numIter: 1000,
    }).run()
  }
}

/**
 * The canvas's stylesheet, from the theme's tokens: each family's colour and
 * shape, then each kind's size and glyph (for the kinds on the canvas, so
 * kinds no table knows still get theirs). Rebuilt when the theme or the
 * kinds change.
 */
function styles(kinds: Iterable<string>): StylesheetJson {
  const family = Object.fromEntries(
    FAMILIES.map((name) => [
      name,
      {
        color: tokenColor(`--graph-${name}`, FALLBACK[name]),
        ink: tokenColor(`--graph-${name}-ink`, "#ffffff"),
      },
    ])
  ) as Record<NodeFamily, { color: string; ink: string }>
  const surface = tokenColor("--background", "#ffffff")
  const ink = tokenColor("--foreground", "#1f2328")
  const muted = tokenColor("--muted-foreground", "#6a727e")
  const line = tokenColor("--border", "#d5d9de")
  const font = getComputedStyle(document.body).fontFamily
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        width: 28,
        height: 28,
        // A ring of the page's colour keeps touching nodes apart.
        "border-width": 2,
        "border-color": surface,
        color: ink,
        "font-family": font,
        "font-size": 12,
        "text-valign": "bottom",
        "text-margin-y": 6,
        "text-wrap": "ellipsis",
        "text-max-width": "140px",
        "min-zoomed-font-size": 7,
        "background-position-x": "50%",
        "background-position-y": "50%",
        "background-clip": "none",
        "background-image-containment": "over",
      },
    },
    ...FAMILIES.map((name) => ({
      selector: `node[family = '${name}']`,
      style: {
        "background-color": family[name].color,
        shape: CANVAS_SHAPES[name],
        "background-width": `${GLYPH_SCALE[name] * 100}%`,
        "background-height": `${GLYPH_SCALE[name] * 100}%`,
      },
    })),
    ...[...kinds].map((kind) => {
      const look = lookOf(kind)
      const { color, ink: glyphInk } = family[look.family]
      const hollow = look.origin !== "declared"
      return {
        selector: `node[kind = ${JSON.stringify(kind)}]`,
        style: {
          // Files are a little wider than tall, like a page on its side.
          width: look.family === "file" ? look.size * 1.12 : look.size,
          height: look.family === "file" ? look.size * 0.88 : look.size,
          "background-image": glyphImage(look.glyph, hollow ? color : glyphInk),
          ...(hollow && {
            "background-color": surface,
            "border-color": color,
            "border-style": look.origin === "derived" ? "dashed" : "dotted",
          }),
        },
      }
    }),
    {
      selector: "edge",
      style: {
        width: 1.2,
        "line-color": line,
        "target-arrow-color": muted,
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.8,
        "curve-style": "bezier",
        label: "data(kind)",
        "font-size": 10,
        "font-family": font,
        color: muted,
        "text-rotation": "autorotate",
        "text-background-color": surface,
        "text-background-opacity": 1,
        "text-background-padding": "2px",
        "min-zoomed-font-size": 7,
      },
    },
    { selector: "edge[kind = 'contains']", style: { "line-style": "dashed" } },
    {
      selector: "edge.selected",
      style: { "line-color": ink, "target-arrow-color": ink, width: 2.5 },
    },
    {
      selector: "node.selected",
      style: {
        "border-width": 3,
        "border-color": ink,
        "border-style": "solid",
      },
    },
    { selector: ".muted", style: { opacity: 0.18 } },
    { selector: ".filtered", style: { display: "none" } },
    { selector: ".no-label", style: { label: "" } },
  ]
}

/**
 * The loaded graph as a pannable, zoomable network: nodes coloured and
 * shaped by family, marked with their kind's glyph and sized by scope,
 * labelled by name; edges labelled by kind. Selecting a
 * node dims everything outside its neighbourhood; double-clicking expands
 * it. Filtering hides elements without moving the rest.
 */
export function GraphCanvas({
  nodes,
  edges,
  visibleNodes,
  visibleEdges,
  selection,
  onSelect,
  onExpand,
}: {
  nodes: NodeVersion[]
  edges: EdgeVersion[]
  visibleNodes: NodeVersion[]
  visibleEdges: EdgeVersion[]
  selection: Selection
  onSelect: (selection: Selection) => void
  onExpand: (id: string) => void
}) {
  const container = React.useRef<HTMLDivElement>(null)
  const instance = React.useRef<Core | null>(null)
  const handlers = React.useRef({ onSelect, onExpand })
  // Whether the view is the fitted one, not one the user moved.
  const fitted = React.useRef(true)
  // The kinds the stylesheet has a size and glyph for.
  const styled = React.useRef(new Set<string>())
  const [labels, setLabels] = React.useState(true)
  const [layout, setLayout] = React.useState<Layout>("concentric")

  React.useEffect(() => {
    handlers.current = { onSelect, onExpand }
  }, [onSelect, onExpand])

  React.useEffect(() => {
    if (!container.current) return
    const cy = cytoscape({
      container: container.current,
      elements: [],
      style: styles(styled.current),
      minZoom: 0.12,
      maxZoom: 3.5,
      boxSelectionEnabled: false,
      autounselectify: true,
    })
    instance.current = cy
    cy.on("tap", "node", (e) =>
      handlers.current.onSelect({ type: "node", id: e.target.data("recordID") })
    )
    cy.on("tap", "edge", (e) =>
      handlers.current.onSelect({ type: "edge", id: e.target.data("recordID") })
    )
    cy.on("dbltap", "node", (e) =>
      handlers.current.onExpand(e.target.data("recordID"))
    )
    cy.on("tap", (e) => {
      if (e.target === cy) handlers.current.onSelect(null)
    })
    // Until the user pans, zooms or drags, the graph keeps fitting the
    // canvas as it resizes (the window, or the inspector beside it).
    fitted.current = true
    cy.on("dragpan scrollzoom pinchzoom grabon", () => {
      fitted.current = false
    })
    const resize = new ResizeObserver(() => {
      cy.resize()
      if (fitted.current) cy.fit(cy.elements(":visible"), 32)
    })
    resize.observe(container.current)
    // The theme provider switches the root's light/dark class.
    const theme = new MutationObserver(() => cy.style(styles(styled.current)))
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
    const ids = new Set([
      ...nodes.map((v) => `n:${v.fact.node.id}`),
      ...edges.map((v) => `e:${v.fact.edge.id}`),
    ])
    // New kinds need their size and glyph before they're laid out.
    const unseen = nodes.filter((v) => !styled.current.has(v.fact.node.kind))
    if (unseen.length) {
      for (const v of unseen) styled.current.add(v.fact.node.kind)
      cy.style(styles(styled.current))
    }
    const existing = cy.nodes().length
    let added = false
    cy.batch(() => {
      cy.elements()
        .filter((e) => !ids.has(e.id()))
        .remove()
      nodes.forEach(({ fact: { node } }, index) => {
        const id = `n:${node.id}`
        if (cy.getElementById(id).length) return
        added = true
        cy.add({
          data: {
            id,
            recordID: node.id,
            label: nodeLabel(node),
            kind: node.kind,
            family: familyOf(node.kind),
          },
          // A spiral start, so layouts don't begin from one point.
          position: {
            x: Math.cos(index * 2.4) * (80 + index * 4),
            y: Math.sin(index * 2.4) * (80 + index * 4),
          },
        })
      })
      edges.forEach(({ fact: { edge } }) => {
        if (!cy.getElementById(`e:${edge.id}`).length) {
          cy.add({
            data: {
              id: `e:${edge.id}`,
              recordID: edge.id,
              source: `n:${edge.source_id}`,
              target: `n:${edge.target_id}`,
              kind: edge.kind,
            },
          })
        }
      })
    })
    if (added) arrange(cy, layout, existing === 0)
  }, [nodes, edges, layout])

  React.useEffect(() => {
    const cy = instance.current
    if (!cy) return
    const ids = new Set([
      ...visibleNodes.map((v) => `n:${v.fact.node.id}`),
      ...visibleEdges.map((v) => `e:${v.fact.edge.id}`),
    ])
    cy.batch(() => {
      cy.elements().forEach((e) => {
        e.toggleClass("filtered", !ids.has(e.id()))
      })
      cy.edges().toggleClass("no-label", !labels)
      cy.elements().removeClass("selected muted")
      if (selection) {
        const selected = cy.getElementById(
          `${selection.type === "node" ? "n" : "e"}:${selection.id}`
        )
        selected.addClass("selected")
        const neighborhood =
          selection.type === "node"
            ? selected.closedNeighborhood()
            : selected.union(selected.connectedNodes())
        cy.elements().difference(neighborhood).addClass("muted")
      }
    })
  }, [visibleNodes, visibleEdges, selection, labels, nodes, edges])

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
        aria-label={`Code graph: ${visibleNodes.length} nodes and ${visibleEdges.length} relationships. The node list beside it explores the same graph with a keyboard.`}
      />
      <div
        className="absolute top-3 right-3 flex flex-wrap items-center justify-end gap-1.5"
        aria-label="Graph controls"
      >
        <Button
          size="xs"
          variant="outline"
          onClick={() => {
            fitted.current = true
            instance.current?.fit(instance.current.elements(":visible"), 32)
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
          }}
        >
          <Icon icon="refresh" />
        </Button>
        <Select
          items={LAYOUTS}
          value={layout}
          onValueChange={(value) => {
            const next: Layout = value === "cose" ? "cose" : "concentric"
            setLayout(next)
            if (!instance.current) return
            fitted.current = true
            arrange(instance.current, next)
          }}
        >
          <SelectTrigger size="sm" aria-label="Graph layout">
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
          Edge labels
        </Toggle>
      </div>
      <span className="pointer-events-none absolute bottom-3 left-3 text-2xs text-subtle">
        Drag nodes · Scroll to zoom · Double-click a node to expand it
      </span>
    </div>
  )
}
