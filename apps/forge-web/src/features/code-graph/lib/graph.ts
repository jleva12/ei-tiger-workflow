/**
 * An organization's code repository as the code graph holds it, as the
 * admin API relays it from the code graph worker: versioned nodes
 * (declarations, files, external symbols) and the edges between them, read
 * one published generation at a time. This module is the model only: what
 * the reads answer, how a kind is drawn, and how the explorer merges and
 * filters what it has loaded. The reads themselves are in `./api`.
 */

/** A property value; exactly one field is set. */
export type Property = {
  string?: string
  int64?: number
  float64?: number
  bool?: boolean
  strings?: string[]
}

/** Where a node or edge is in the source it was read from. */
export type Anchor = {
  content_sha256: string
  lineage: string
  span: {
    start: { line: number; byte_offset: number }
    end: { line: number; byte_offset: number }
  }
}

export type GraphNode = {
  id: string
  kind: string
  name?: string
  qualified_name?: string
  source?: Anchor
  properties?: Record<string, Property>
}

export type GraphEdge = {
  id: string
  kind: string
  source_id: string
  target_id: string
  source?: Anchor
  properties?: Record<string, Property>
}

/** A node as of the generation read, and where it was introduced. */
export type NodeVersion = {
  fact: { node: GraphNode }
  gen_from: number
  commit_from: string
}

export type EdgeVersion = {
  fact: { edge: GraphEdge }
  gen_from: number
  commit_from: string
}

/** What's loaded: pages of the graph and the neighbourhoods expanded. */
export type GraphData = {
  branch?: string
  commit_sha?: string
  repository_id: string
  /** 0 when nothing is published yet. */
  generation: number
  nodes: NodeVersion[]
  edges: EdgeVersion[]
  next_cursor?: string
  truncated: boolean
}

export type Neighbors = {
  generation: number
  neighbors: { edge: EdgeVersion; node?: NodeVersion }[]
  next_cursor?: string
}

export type NodeSourceText = {
  text: string
  text_start_line: number
  text_end_line: number
  truncated: boolean
}

export type Selection = { type: "node" | "edge"; id: string } | null

/** The canvas stays readable up to these; focus on a node to go further. */
export const MAX_NODES = 300
export const MAX_EDGES = 600

/**
 * The family a node kind belongs to: its colour and shape on the canvas. A
 * code graph has more kinds than colours stay apart, so families group them
 * and each kind has a glyph of its own, the way an IDE's structure view
 * marks them.
 */
export type NodeFamily = "file" | "type" | "callable" | "value" | "other"

export const FAMILY_LABELS: Record<NodeFamily, string> = {
  file: "Files and modules",
  type: "Types",
  callable: "Functions and methods",
  value: "Fields and variables",
  other: "External and other",
}

/** A letter or two, or a small drawing where a letter wouldn't say it. */
export type Glyph = { text: string } | { icon: GlyphIcon }
export type GlyphIcon = "file" | "external"

/**
 * How a node of a kind is drawn: its family's colour and shape, the kind's
 * glyph, a size that follows its scope (files and types large, locals and
 * parameters small) and an outline for where it comes from: `derived`
 * members the compiler made (dashed), `unresolved` references (dotted).
 */
export type NodeLook = {
  family: NodeFamily
  glyph: Glyph
  /** Its diameter on the canvas at zoom 1. */
  size: number
  origin: "declared" | "derived" | "unresolved"
}

const look = (
  family: NodeFamily,
  glyph: string | Glyph,
  size: number,
  origin: NodeLook["origin"] = "declared"
): NodeLook => ({
  family,
  glyph: typeof glyph === "string" ? { text: glyph } : glyph,
  size,
  origin,
})

// A Map, not an object: kinds are data, and "constructor" or "toString" must
// not reach Object.prototype.
const LOOKS = new Map<string, NodeLook>([
  ["source_file", look("file", { icon: "file" }, 40)],
  ["module", look("file", "M", 38)],
  ["namespace", look("file", "N", 38)],
  ["package", look("file", "P", 38)],
  ["class", look("type", "C", 40)],
  ["interface", look("type", "I", 40)],
  ["enum", look("type", "E", 38)],
  ["record", look("type", "R", 38)],
  ["annotation_type", look("type", "@", 36)],
  ["type_alias", look("type", "T", 36)],
  ["type_parameter", look("type", "<>", 28)],
  ["constructed_type", look("type", "[]", 30)],
  ["method", look("callable", "m", 32)],
  ["constructor", look("callable", "c", 32)],
  ["function", look("callable", "ƒ", 32)],
  ["lambda", look("callable", "λ", 26)],
  ["initializer", look("callable", "{}", 26)],
  ["field", look("value", "f", 28)],
  ["enum_constant", look("value", "e", 28)],
  ["record_component", look("value", "r", 28)],
  ["variable", look("value", "v", 28)],
  ["local_variable", look("value", "v", 23)],
  ["pattern_variable", look("value", "v", 23)],
  ["parameter", look("value", "p", 23)],
  ["receiver_parameter", look("value", "p", 23)],
  ["external_symbol", look("other", { icon: "external" }, 28)],
  ["intrinsic", look("other", "i", 24)],
  ["type_use", look("other", "T", 23)],
  ["unresolved_reference", look("other", "?", 24, "unresolved")],
  ["code_chunk", look("other", "¶", 24)],
])

/**
 * How to draw a kind. `derived_method` and the like are drawn as their base
 * kind with a dashed outline; kinds the table doesn't know are "other",
 * marked with their first letter.
 */
export function lookOf(kind: string): NodeLook {
  const known = LOOKS.get(kind)
  if (known) return known
  const base = kind.startsWith("derived_")
    ? LOOKS.get(kind.slice("derived_".length))
    : undefined
  if (base) return { ...base, origin: "derived" }
  return look("other", (kind[0] ?? "?").toLowerCase(), 24)
}

export function familyOf(kind: string): NodeFamily {
  return lookOf(kind).family
}

/** The node kinds a graph can start from, for the kind picker. */
export const START_KINDS = [...LOOKS.keys()].sort()

/**
 * Adds a read to what's loaded, within MAX_NODES and MAX_EDGES; whatever
 * doesn't fit marks the graph truncated. Counts and filters describe the
 * facts actually loaded, parallel edges included; no placeholder nodes are
 * invented for absent endpoints, so an edge to a node not loaded is left
 * out. Reads from another repository or generation are refused.
 */
export function mergeGraph(current: GraphData, incoming: GraphData): GraphData {
  if (
    current.repository_id !== incoming.repository_id ||
    current.generation !== incoming.generation
  ) {
    throw new Error(
      "The graph changed while you were exploring it. Reload it first."
    )
  }
  const nodes = new Map(current.nodes.map((v) => [v.fact.node.id, v]))
  const edges = new Map(current.edges.map((v) => [v.fact.edge.id, v]))
  let truncated = current.truncated || incoming.truncated
  for (const v of incoming.nodes) {
    if (nodes.has(v.fact.node.id) || nodes.size < MAX_NODES) {
      nodes.set(v.fact.node.id, v)
    } else {
      truncated = true
    }
  }
  for (const v of incoming.edges) {
    const e = v.fact.edge
    if (!nodes.has(e.source_id) || !nodes.has(e.target_id)) {
      truncated = true
      continue
    }
    if (edges.has(e.id) || edges.size < MAX_EDGES) {
      edges.set(e.id, v)
    } else {
      truncated = true
    }
  }
  return {
    ...current,
    nodes: [...nodes.values()],
    edges: [...edges.values()],
    truncated,
  }
}

/** The loaded nodes and edges that pass the kind toggles and the text filter. */
export function visibleGraph(
  data: GraphData,
  hiddenNodes: Set<string>,
  hiddenEdges: Set<string>,
  filter: string
) {
  const query = filter.trim().toLowerCase()
  const nodes = data.nodes.filter(
    ({ fact: { node } }) =>
      !hiddenNodes.has(node.kind) &&
      (!query ||
        `${node.name ?? ""} ${node.qualified_name ?? ""} ${node.id}`
          .toLowerCase()
          .includes(query))
  )
  const ids = new Set(nodes.map((v) => v.fact.node.id))
  const edges = data.edges.filter(
    ({ fact: { edge } }) =>
      !hiddenEdges.has(edge.kind) &&
      ids.has(edge.source_id) &&
      ids.has(edge.target_id)
  )
  return { nodes, edges }
}

export function nodeLabel(node: GraphNode) {
  return node.name || node.qualified_name || node.id
}

export function propertyValue(value: Property) {
  return (
    value.string ??
    value.int64 ??
    value.float64 ??
    value.bool ??
    value.strings?.join(", ") ??
    ""
  )
}

/** How many of each kind are loaded, by kind name. */
export function typeCounts(kinds: string[]) {
  const counts = new Map<string, number>()
  for (const kind of kinds) counts.set(kind, (counts.get(kind) ?? 0) + 1)
  return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b))
}
