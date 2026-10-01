import * as React from "react"
import { useStore as useFlowStore, type InternalNode, type ReactFlowState } from "@xyflow/react"

import type { ConnectionMeaning } from "@/lib/builder/connections"
import { routeEdges, type Box, type EdgeRoute, type RouteRequest } from "@/lib/builder/routing"
import { edgeId, INPUT, type Point } from "@/lib/builder/types"
import { graphOf, useBuilder, type FlowNode } from "./store"

/*
 * How the canvas draws each connection: what it means (its colour and
 * label) and, once both its steps are measured, the way it runs. Every
 * connection is routed together (see lib/builder/routing), again
 * whenever a step moves or changes size.
 */

export type EdgeLook = ConnectionMeaning & { route?: EdgeRoute }

export const EdgeLooksContext = React.createContext<Map<string, EdgeLook>>(new Map())

/** How the canvas draws this connection. */
export const useEdgeLook = (id: string) => React.useContext(EdgeLooksContext).get(id)

// Where every step stands, how big it is and where its ways out sit, as
// one string: the routes are worked out again only when it changes, not
// when the canvas pans or zooms.
function geometryOf(state: ReactFlowState) {
  let key = ""
  for (const node of state.nodeLookup.values()) {
    const { x, y } = node.internals.positionAbsolute
    const ports = node.internals.handleBounds?.source?.map((h) => h.y).join("/") ?? ""
    key += `${node.id}:${x},${y},${node.measured.width},${node.measured.height},${ports};`
  }
  return key
}

let measuring: CanvasRenderingContext2D | null | undefined
const measured = new Map<string, number>()

/**
 * Measures line labels in their type (10px medium at the default text
 * size, so it follows the text-size preference): a label's width is its
 * words, its padding and border, and room for an icon.
 */
function labelMeasure() {
  measuring ??= document.createElement("canvas").getContext("2d")
  const root = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16
  const font = `500 ${root * 0.625}px ${getComputedStyle(document.body).fontFamily}`
  return (text: string, icon: boolean) => {
    const key = `${font}|${text}`
    let width = measured.get(key)
    if (width === undefined && measuring) {
      measuring.font = font
      width = Math.ceil(measuring.measureText(text).width)
      measured.set(key, width)
    }
    return width === undefined ? undefined : width + 14 + (icon ? 13 : 0)
  }
}

/** A way out's outer edge (on the right), or a way in's (on the left). */
function portOf(
  node: InternalNode<FlowNode> | undefined,
  handle: string | null | undefined,
  type: "source" | "target"
): Point | undefined {
  const bounds = node?.internals.handleBounds?.[type]?.find((h) => h.id === handle)
  if (!node || !bounds) return undefined
  const { x, y } = node.internals.positionAbsolute
  return {
    x: x + bounds.x + (type === "source" ? bounds.width : 0),
    y: y + bounds.y + bounds.height / 2,
  }
}

export function useEdgeLooks(): Map<string, EdgeLook> {
  const geometry = useFlowStore(geometryOf)
  const lookup = useFlowStore((s) => s.nodeLookup) as Map<string, InternalNode<FlowNode>>
  const adapter = useBuilder((s) => s.adapter)
  const nodes = useBuilder((s) => s.nodes)
  const edges = useBuilder((s) => s.edges)

  const { meanings, bodies } = React.useMemo(() => {
    const graph = graphOf(nodes, edges)
    return {
      meanings: adapter.meaningsOf(graph, (c) => edgeId(c.source, c.output, c.target)),
      bodies: adapter.loopBodies(graph),
    }
  }, [adapter, nodes, edges])

  return React.useMemo(() => {
    // `geometry` stands for the measurements read from `lookup` here.
    void geometry
    const boxes = new Map<string, Box>()
    for (const node of lookup.values()) {
      const { width, height } = node.measured
      if (!width || !height) continue
      const { x, y } = node.internals.positionAbsolute
      boxes.set(node.id, { x, y, width, height })
    }
    const requests: RouteRequest[] = []
    const labelled = new Set<string>()
    const widthOf = labelMeasure()
    for (const edge of edges) {
      const meaning = meanings.get(edge.id)
      const from = portOf(lookup.get(edge.source), edge.sourceHandle, "source")
      const to = portOf(lookup.get(edge.target), INPUT, "target")
      if (!meaning || !from || !to) continue
      let label: RouteRequest["label"]
      if (meaning.role === "return") {
        // One "Next item" over each loop, however many ways come back.
        if (meaning.loop && !labelled.has(meaning.loop)) {
          label = {
            text: meaning.label!,
            icon: true,
            always: true,
            width: widthOf(meaning.label!, true),
          }
          labelled.add(meaning.loop)
        }
      } else if (meaning.label) {
        label = {
          text: meaning.label,
          always: meaning.role === "each",
          width: widthOf(meaning.label, false),
        }
      }
      requests.push({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        from,
        to,
        loop: meaning.role === "return" ? meaning.loop : undefined,
        bundle: meaning.tone,
        label,
      })
    }
    const routes = routeEdges(boxes, requests, bodies)
    const looks = new Map<string, EdgeLook>()
    for (const [id, meaning] of meanings) looks.set(id, { ...meaning, route: routes.get(id) })
    return looks
  }, [geometry, lookup, edges, meanings, bodies])
}
