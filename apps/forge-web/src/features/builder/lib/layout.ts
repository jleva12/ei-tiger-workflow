import { LANE, labelWidth, TRACK } from "./routing"
import type { BaseStep, BuilderGraph, Point, StepOutput } from "./types"

/*
 * Tidy up: lays a graph out left to right in columns, one per step of
 * distance from where it starts. Each step's way in lines up with the way
 * out it follows, so a run of single steps draws one straight line and a
 * branch continues from its own row; branches fan out downwards in the
 * order of their rows (True above False, cases top to bottom).
 *
 * A connection that skips columns gets a slot in each column it passes,
 * so no step stands in its way; the steps in a column are ordered to cross
 * as few lines as they can; the room between two columns grows with the
 * lines that bend there; and a loop keeps clear room over itself and its
 * body for the line coming back. Connections that come back (a loop's
 * body returning to it) don't push anything right.
 */

export type Size = { width: number; height: number }

/** What the canvas measured: each step's size, and where its ways in and out sit. */
export type Measure = {
  size: (id: string) => Size | undefined
  /** A way in or out's distance from the top of its step. */
  port: (id: string, handle: string, type: "source" | "target") => number | undefined
}

/** The least room between two columns. */
const COLUMN_GAP = 96
/** Between two steps in a column. */
const ROW_GAP = 32
/** Between a step and a line passing it in its column. */
const LINE_GAP = 20
/** Kept clear over a loop and its body, for the line coming back. */
const LANE_ROOM = LANE + 10
export const DEFAULT_SIZE: Size = { width: 240, height: 64 }
// Where a header's way in and out sit when nothing was measured.
const HEADER_PORT = 28

/** A step, or a line passing through a column on its way to a step further on. */
type Item = {
  id: string
  step: boolean
  layer: number
  width: number
  height: number
  /** Its way in's distance from its top. */
  inPort: number
  y: number
  /** A passing line's connection: the steps it joins. */
  joins?: [string, string]
}

/** A connection, or the stretch of one between two neighbouring columns. */
type Link = {
  from: string
  /** The way out it leaves by. */
  out: string
  to: string
  /** That way out's place among its step's, 0 (the top row) to 1, to order by. */
  frac: number
}

/** What Tidy up needs to know of a builder's kinds of step. */
export type LayoutRules<S extends BaseStep> = {
  outputsOf: (step: S) => StepOutput[]
  /** Each loop's body, by the loop's ID; none for graphs without loops. */
  loopBodies: (graph: BuilderGraph<S>) => Map<string, Set<string>>
  /** Where every run starts (the start step): laid out first. */
  isRoot: (step: S) => boolean
}

export function tidyGraph<S extends BaseStep>(
  graph: BuilderGraph<S>,
  measure: Measure,
  { outputsOf, loopBodies, isRoot }: LayoutRules<S>
): Map<string, Point> {
  const ids = graph.steps.map((s) => s.id)
  const byId = new Map(graph.steps.map((s) => [s.id, s]))
  const size = (id: string) => measure.size(id) ?? DEFAULT_SIZE
  const port = (id: string, handle: string, type: "source" | "target") =>
    measure.port(id, handle, type) ?? HEADER_PORT

  // Each step's ways out, in the order of its outputs.
  const outs = new Map<string, { target: string; output: string; frac: number }[]>()
  for (const step of graph.steps) {
    const order = outputsOf(step.data).map((o) => o.id)
    outs.set(
      step.id,
      graph.connections
        .filter((c) => c.source === step.id && byId.has(c.target))
        .sort((a, b) => order.indexOf(a.output) - order.indexOf(b.output))
        .map((c) => ({
          target: c.target,
          output: c.output,
          frac: Math.max(0, order.indexOf(c.output)) / Math.max(1, order.length),
        }))
    )
  }

  // Depth-first from the entry point, then from anything it doesn't reach:
  // the connections that come back are back edges, and each step's first
  // way in is the one it lines up with.
  const roots = [...graph.steps.filter((s) => isRoot(s.data)).map((s) => s.id), ...ids]
  const state = new Map<string, "open" | "done">()
  const back = new Set<string>()
  const parent = new Map<string, { source: string; output: string }>()
  const visit = (id: string) => {
    state.set(id, "open")
    for (const { target, output } of outs.get(id) ?? []) {
      const seen = state.get(target)
      if (seen === "open") back.add(`${id}:${output}->${target}`)
      else if (!seen) {
        parent.set(target, { source: id, output })
        visit(target)
      }
    }
    state.set(id, "done")
  }
  for (const root of roots) if (!state.has(root)) visit(root)
  const forward = (source: string, output: string, target: string) =>
    !back.has(`${source}:${output}->${target}`)

  // Columns: the longest way in, ignoring back edges.
  const preds = new Map<string, string[]>(ids.map((id) => [id, []]))
  for (const [source, links] of outs) {
    for (const { target, output } of links) {
      if (forward(source, output, target)) preds.get(target)!.push(source)
    }
  }
  const layerOf = new Map<string, number>()
  const columnOf = (id: string): number => {
    const known = layerOf.get(id)
    if (known !== undefined) return known
    layerOf.set(id, 0)
    let value = 0
    for (const p of preds.get(id) ?? []) value = Math.max(value, columnOf(p) + 1)
    layerOf.set(id, value)
    return value
  }
  ids.forEach(columnOf)

  // Items: the steps, and a slot for each connection in each column it skips.
  const items = new Map<string, Item>()
  for (const id of ids) {
    const { width, height } = size(id)
    items.set(id, {
      id,
      step: true,
      layer: layerOf.get(id)!,
      width,
      height,
      inPort: port(id, "in", "target"),
      y: 0,
    })
  }
  const links: Link[] = []
  const linksFrom = new Map<string, Link[]>()
  /** The link each item lines up with. */
  const alignWith = new Map<string, Link>()
  const addLink = (link: Link) => {
    links.push(link)
    const list = linksFrom.get(link.from)
    if (list) list.push(link)
    else linksFrom.set(link.from, [link])
  }
  for (const [source, list] of outs) {
    for (const { target, output, frac } of list) {
      if (!forward(source, output, target)) continue
      let from = source
      let out = output
      let linkFrac = frac
      for (let layer = layerOf.get(source)! + 1; layer < layerOf.get(target)!; layer += 1) {
        const id = `${source}:${output}->${target}@${layer}`
        items.set(id, {
          id,
          step: false,
          layer,
          width: 0,
          height: 0,
          inPort: 0,
          y: 0,
          joins: [source, target],
        })
        const link = { from, out, to: id, frac: linkFrac }
        addLink(link)
        alignWith.set(id, link)
        from = id
        out = "next"
        linkFrac = 0.5
      }
      const link = { from, out, to: target, frac: linkFrac }
      addLink(link)
      const first = parent.get(target)
      if (first && first.source === source && first.output === output) alignWith.set(target, link)
    }
  }

  // Loops keep room over themselves and their body: which items are inside each.
  const blocks = [...loopBodies(graph)].map(([loop, body]) => {
    const members = new Set<string>([loop, ...body])
    // A line passing between two steps of the loop is inside it too.
    for (const item of items.values()) {
      if (item.joins?.every((id) => members.has(id))) members.add(item.id)
    }
    return members
  })

  // First order: the order a walk from the entry point meets them, taking
  // each step's ways out in order.
  const layers: string[][] = []
  const placed = new Set<string>()
  const place = (id: string) => {
    placed.add(id)
    ;(layers[items.get(id)!.layer] ??= []).push(id)
  }
  const walk = (id: string) => {
    for (const link of linksFrom.get(id) ?? []) {
      if (placed.has(link.to)) continue
      place(link.to)
      walk(link.to)
    }
  }
  for (const root of roots) {
    if (placed.has(root)) continue
    place(root)
    walk(root)
  }
  for (let i = 0; i < layers.length; i += 1) layers[i] ??= []
  const ordered = reorder(layers, links, items)

  // Rows: each item's way in level with the way out it follows, pushed
  // down as far as the items above it in its column need.
  const outPort = (id: string, output: string) =>
    items.get(id)!.step ? port(id, output, "source") : 0
  const desired = (id: string) => {
    const link = alignWith.get(id)
    if (!link) return undefined
    const from = items.get(link.from)!
    return from.y + outPort(link.from, link.out) - items.get(id)!.inPort
  }
  const gapBetween = (above: Item, below: Item) => {
    const base =
      above.step && below.step ? ROW_GAP : above.step || below.step ? LINE_GAP : TRACK + 2
    const opens = blocks.some((members) => members.has(below.id) && !members.has(above.id))
    return base + (opens ? LANE_ROOM : 0)
  }
  let lowest = 0
  ordered.forEach((members, layer) => {
    let previous: Item | undefined
    for (const id of members) {
      const item = items.get(id)!
      const want = desired(id) ?? (layer === 0 ? 0 : lowest)
      item.y = previous
        ? Math.max(want, previous.y + previous.height + gapBetween(previous, item))
        : want
      previous = item
      lowest = Math.max(lowest, item.y + item.height + ROW_GAP)
    }
  })

  // Where an item had to move down off its line, move what leads to it
  // down with it, back to where a line already bends, when there's room.
  const below = (item: Item) => {
    const column = ordered[item.layer]
    const next = column[column.indexOf(item.id) + 1]
    return next ? items.get(next) : undefined
  }
  const bent = (id: string) => {
    const want = desired(id)
    return want === undefined || items.get(id)!.y > want + 0.5
  }
  for (let round = 0; round < ids.length; round += 1) {
    let moved = false
    for (const layer of [...ordered].reverse()) {
      for (const id of layer) {
        const want = desired(id)
        const item = items.get(id)!
        if (want === undefined || item.y <= want + 0.5) continue
        const shift = item.y - want
        const chain: Item[] = []
        let link = alignWith.get(id)
        let ok = true
        while (link) {
          const from = items.get(link.from)!
          if ((linksFrom.get(from.id) ?? []).length !== 1) {
            ok = false
            break
          }
          const next = below(from)
          if (next && next.y < from.y + shift + from.height + gapBetween(from, next)) {
            ok = false
            break
          }
          chain.push(from)
          if (bent(from.id)) break
          link = alignWith.get(from.id)
        }
        if (!ok || !chain.length) continue
        for (const from of chain) from.y += shift
        moved = true
      }
    }
    if (!moved) break
  }

  // Columns: after the widest step of every column before, with room for
  // the lines that bend between them, and for the label on a loop's way
  // into its body.
  const labelRoom = new Map<string, number>()
  for (const step of graph.steps) {
    const each = outputsOf(step.data).find((o) => o.body)
    if (each) labelRoom.set(step.id, labelWidth(each.label) + 56)
  }
  const xs: number[] = []
  let x = 0
  ordered.forEach((members, layer) => {
    xs[layer] = x
    const widest = Math.max(0, ...members.map((id) => items.get(id)!.width))
    const bends = new Set<string>()
    for (const link of links) {
      const from = items.get(link.from)!
      const to = items.get(link.to)!
      if (from.layer !== layer) continue
      if (Math.abs(from.y + outPort(from.id, link.out) - (to.y + to.inPort)) > 1) bends.add(link.to)
    }
    const room = Math.max(0, ...members.map((id) => labelRoom.get(id) ?? 0))
    x += widest + Math.max(COLUMN_GAP, 60 + bends.size * TRACK, room)
  })

  const positions = new Map<string, Point>()
  for (const id of ids) {
    const item = items.get(id)!
    positions.set(id, { x: xs[item.layer], y: Math.round(item.y) })
  }
  return positions
}

/** How many links between neighbouring columns cross, in this order. */
function crossingsOf(layers: string[][], links: Link[], items: Map<string, Item>) {
  const index = new Map<string, number>()
  for (const layer of layers) layer.forEach((id, i) => index.set(id, i))
  let count = 0
  const byLayer = new Map<number, { a: number; b: number }[]>()
  for (const link of links) {
    const layer = items.get(link.from)!.layer
    const list = byLayer.get(layer) ?? []
    list.push({ a: index.get(link.from)! + link.frac, b: index.get(link.to)! })
    byLayer.set(layer, list)
  }
  for (const list of byLayer.values()) {
    for (let i = 0; i < list.length; i += 1) {
      for (let j = i + 1; j < list.length; j += 1) {
        if ((list[i].a - list[j].a) * (list[i].b - list[j].b) < 0) count += 1
      }
    }
  }
  return count
}

/**
 * Reorders each column by where its lines come from and go (barycentres),
 * sweeping right then left, and keeps the order that crosses least; ties
 * keep the walk's order.
 */
function reorder(layers: string[][], links: Link[], items: Map<string, Item>) {
  let best = layers.map((l) => [...l])
  let bestCount = crossingsOf(best, links, items)
  const current = best.map((l) => [...l])
  const into = new Map<string, Link[]>()
  const outOf = new Map<string, Link[]>()
  for (const link of links) {
    into.set(link.to, [...(into.get(link.to) ?? []), link])
    outOf.set(link.from, [...(outOf.get(link.from) ?? []), link])
  }
  for (let round = 0; round < 4 && bestCount > 0; round += 1) {
    for (const down of [true, false]) {
      const index = new Map<string, number>()
      for (const layer of current) layer.forEach((id, i) => index.set(id, i))
      const range = down ? [...current.keys()].slice(1) : [...current.keys()].slice(0, -1).reverse()
      for (const l of range) {
        const keyed = current[l].map((id, i) => {
          const near = down
            ? (into.get(id) ?? []).map((k) => index.get(k.from)! + k.frac)
            : (outOf.get(id) ?? []).map((k) => index.get(k.to)!)
          return {
            id,
            key: near.length ? near.reduce((a, b) => a + b, 0) / near.length : i,
            i,
          }
        })
        keyed.sort((a, b) => a.key - b.key || a.i - b.i)
        current[l] = keyed.map((k) => k.id)
        current[l].forEach((id, i) => index.set(id, i))
      }
      const count = crossingsOf(current, links, items)
      if (count < bestCount) {
        bestCount = count
        best = current.map((l) => [...l])
      }
    }
  }
  return best
}
