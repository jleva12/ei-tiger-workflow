import type { ImportResult } from "@/components/builder/import-dialog"
import { edgeId, type BuilderGraph, type Point } from "@/lib/builder/types"
import { readList } from "@/lib/steps/document"
import {
  DELAY_UNITS,
  HTTP_METHODS,
  THINKING_LEVELS,
  uid,
} from "@/lib/steps/model"
import {
  AGENT_KINDS,
  isAgentKind,
  isSubAgentKind,
  newNode,
  NODE_ID_PATTERN,
  outputsOf,
  SUB_AGENT_ID,
  subAgentDefaults,
  type AgentConfigs,
  type AgentKind,
  type AgentStep,
  type SubAgent,
} from "./model"

/*
 * An agent as JSON: the ADK graph the admin API builds, and where the
 * builder drew each node. `nodes` are the nodes, each with its kind, its
 * settings (sub-agents inside them) and the outputs it can leave by;
 * `edges` join an output of one node to another node. `layout` is for the
 * builder only. The builder saves agents in this format, exports it and
 * imports it; `schema.ts` describes it as a JSON Schema.
 */

export const AGENT_FORMAT = "forge.agent/v1"

export type AgentNode<K extends AgentKind = AgentKind> = {
  [Kind in K]: {
    id: string
    kind: Kind
    name: string
    config: AgentConfigs[Kind]
    /** The IDs of its outputs, in order; edges leave by these. */
    outputs: string[]
  }
}[K]

export type AgentEdge = {
  id: string
  source: string
  /** Which of the source node's outputs it leaves by. */
  source_output: string
  target: string
}

export type AgentDocument = {
  format: typeof AGENT_FORMAT
  id: string
  name: string
  description: string
  organization_id: string
  nodes: AgentNode[]
  edges: AgentEdge[]
  /** Where the builder draws each node, by ID. */
  layout: Record<string, Point>
  created_at: string
  updated_at: string
}

export type AgentGraph = BuilderGraph<AgentStep>

export function newAgentId() {
  const random = crypto.getRandomValues(new Uint32Array(2))
  return `ag_${Array.from(random, (n) => n.toString(36))
    .join("")
    .slice(0, 10)}`
}

/** The document for a graph. */
export function toDocument(
  meta: Pick<
    AgentDocument,
    "id" | "name" | "description" | "organization_id" | "created_at"
  > & {
    updated_at?: string
  },
  graph: AgentGraph
): AgentDocument {
  return {
    format: AGENT_FORMAT,
    id: meta.id,
    name: meta.name,
    description: meta.description,
    organization_id: meta.organization_id,
    nodes: graph.steps.map(
      (s) =>
        ({
          id: s.id,
          kind: s.data.kind,
          name: s.data.name,
          config: s.data.config,
          outputs: outputsOf(s.data).map((o) => o.id),
        }) as AgentNode
    ),
    edges: graph.connections.map((c) => ({
      id: edgeId(c.source, c.output, c.target),
      source: c.source,
      source_output: c.output,
      target: c.target,
    })),
    layout: Object.fromEntries(
      graph.steps.map((s) => [
        s.id,
        { x: Math.round(s.position.x), y: Math.round(s.position.y) },
      ])
    ),
    created_at: meta.created_at,
    updated_at: meta.updated_at ?? new Date().toISOString(),
  }
}

/** The graph of a document the builder can open. */
export function toGraph(doc: AgentDocument): AgentGraph {
  return {
    steps: doc.nodes.map((node, index) => ({
      id: node.id,
      data: {
        kind: node.kind,
        name: node.name,
        config: node.config,
      } as AgentStep,
      position: doc.layout[node.id] ?? { x: index * 320, y: 0 },
    })),
    connections: doc.edges.map((e) => ({
      source: e.source,
      output: e.source_output,
      target: e.target,
    })),
  }
}

/** A new, empty agent: just its start. */
export function newAgent(organizationId: string, name: string): AgentDocument {
  const now = new Date().toISOString()
  return toDocument(
    {
      id: newAgentId(),
      name,
      description: "",
      organization_id: organizationId,
      created_at: now,
      updated_at: now,
    },
    {
      steps: [
        { id: "start", data: newNode("start"), position: { x: 0, y: 0 } },
      ],
      connections: [],
    }
  )
}

/** A copy of an agent under a new ID. */
export function copyAgent(doc: AgentDocument, name: string): AgentDocument {
  const now = new Date().toISOString()
  return {
    ...structuredClone(doc),
    id: newAgentId(),
    name,
    created_at: now,
    updated_at: now,
  }
}

/** The IDs of the organization's agents a document runs as saved-agent nodes. */
export const usesOf = (doc: Pick<AgentDocument, "nodes">) =>
  doc.nodes.flatMap((n) =>
    n.kind === "saved" && n.config.agent ? [n.config.agent] : []
  )

/* -------------------------------------------------------------------------- */
/* Reading a document                                                         */
/* -------------------------------------------------------------------------- */

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

// Settings that take one of a few values, whatever the kind.
const ENUMS: Record<string, readonly string[]> = {
  include_contents: ["default", "none"],
  // Empty: the model's own.
  thinking_level: ["", ...THINKING_LEVELS],
}
// Settings that take one of a few values that depend on the kind: an LLM
// agent's mode isn't a merge's.
const KIND_ENUMS: Record<string, Record<string, readonly string[]>> = {
  llm: { mode: ["single_turn", "task"] },
  merge: { mode: ["all", "any"] },
  http: { method: HTTP_METHODS },
  delay: { unit: DELAY_UNITS.map((u) => u.value) },
  approval: { approvers: ["org:admin", "org:member"] },
  end: { outcome: ["succeeded", "failed"] },
}
// Lists of rows, read as a workflow's are.
const LISTS = new Set(["headers", "cases", "arms"])
// Settings that hold a JSON Schema of their own.
const SCHEMAS = new Set(["input_schema", "output_schema", "response_schema"])
// Settings that are a number, or null for the model's own.
const OPTIONAL_NUMBERS = new Set(["max_output_tokens"])
// An LLM agent's settings since retired (its answer is steps.<id>.output,
// or a sub-agent's state.<id>; an instruction's data comes in by {{ }}):
// dropped without a word.
const RETIRED = new Set(["output_key", "temperature", "input_schema"])

/**
 * Settings from whatever a document holds: the defaults, with every value
 * of the right type taken from `raw`. What doesn't fit is dropped and noted.
 */
function readSettings(
  kind: string,
  defaults: Record<string, unknown>,
  raw: unknown,
  at: string,
  notes: string[],
  depth: number
): Record<string, unknown> {
  const enums = { ...ENUMS, ...KIND_ENUMS[kind] }
  if (raw === undefined) return defaults
  if (!isRecord(raw)) {
    notes.push(`${at} isn't an object; it has its defaults.`)
    return defaults
  }
  const config: Record<string, unknown> = {}
  for (const [key, fallback] of Object.entries(defaults)) {
    const value = raw[key]
    const path = `${at}.${key}`
    if (value === undefined) {
      config[key] = fallback
    } else if (SCHEMAS.has(key)) {
      if (isRecord(value)) config[key] = value
      else {
        notes.push(`${path} should be a JSON Schema object.`)
        config[key] = fallback
      }
    } else if (key in enums) {
      if (typeof value === "string" && enums[key].includes(value))
        config[key] = value
      else {
        notes.push(
          `${path} should be one of ${enums[key].map((v) => v || "empty").join(", ")}.`
        )
        config[key] = fallback
      }
    } else if (OPTIONAL_NUMBERS.has(key)) {
      if (
        value === null ||
        (typeof value === "number" && Number.isFinite(value))
      )
        config[key] = value
      else {
        notes.push(`${path} should be a number, or null.`)
        config[key] = fallback
      }
    } else if (key === "sub_agents") {
      config[key] = readSubAgents(value, path, notes, depth + 1)
    } else if (LISTS.has(key)) {
      config[key] = readList(key, value, path, notes) ?? fallback
    } else if (key === "model") {
      const model = { provider: "", name: "" }
      if (isRecord(value)) {
        if (typeof value.provider === "string") model.provider = value.provider
        if (typeof value.name === "string") model.name = value.name
      } else notes.push(`${path} isn't an object.`)
      config[key] = model
    } else if (
      typeof value === typeof fallback &&
      (typeof value !== "number" || Number.isFinite(value))
    ) {
      config[key] = value
    } else {
      notes.push(`${path} should be a ${typeof fallback}.`)
      config[key] = fallback
    }
  }
  for (const key of Object.keys(raw)) {
    if (!(key in defaults) && !RETIRED.has(key))
      notes.push(`${at}.${key} isn't a setting; dropped.`)
  }
  return config
}

// Deeper than this, sub-agents are dropped: a document can't nest forever.
const MOST_DEPTH = 8

function readSubAgents(
  value: unknown,
  path: string,
  notes: string[],
  depth: number
): SubAgent[] {
  if (!Array.isArray(value)) {
    notes.push(`${path} should be a list.`)
    return []
  }
  if (depth > MOST_DEPTH) {
    notes.push(`${path} nests too deep; dropped.`)
    return []
  }
  const ids = new Set<string>()
  return value.flatMap((item, index) => {
    const at = `${path}[${index}]`
    if (!isRecord(item)) {
      notes.push(`${at} isn't an object; dropped.`)
      return []
    }
    // A Loop agent was `loop` before workflows' loop joined the kinds.
    const kind = item.kind === "loop" ? "loop_agent" : item.kind
    if (!isSubAgentKind(kind)) {
      notes.push(
        `${at}.kind "${String(item.kind)}" isn't a sub-agent Forge knows; dropped.`
      )
      return []
    }
    let id =
      typeof item.id === "string" && SUB_AGENT_ID.test(item.id)
        ? item.id
        : uid("sub")
    if (typeof item.id === "string" && item.id && id !== item.id)
      notes.push(`${at}.id should be letters, digits and _; it has a new one.`)
    if (ids.has(id)) id = uid("sub")
    ids.add(id)
    const name =
      typeof item.name === "string" && item.name.trim()
        ? item.name
        : AGENT_KINDS[kind].label
    const config = readSettings(
      kind,
      subAgentDefaults(kind) as Record<string, unknown>,
      item.config,
      `${at}.config`,
      notes,
      depth
    )
    return [{ id, kind, name, config } as SubAgent]
  })
}

/**
 * A node as saved before the agent kinds and workflow steps were combined:
 * a function is a transform, a router a switch, a join a merge that waits
 * for all, and a Loop agent (`loop` then) a `loop_agent`.
 */
function earlierKind(kind: unknown, config: unknown): [unknown, unknown] {
  const was = isRecord(config) ? config : {}
  switch (kind) {
    case "function":
      return ["transform", { expression: was.expression, output_schema: {} }]
    case "router":
      return ["switch", { value: was.route_on, cases: was.routes }]
    case "join":
      return ["merge", { mode: "all" }]
    case "loop":
      return "sub_agents" in was ? ["loop_agent", config] : [kind, config]
    default:
      return [kind, config]
  }
}

/**
 * A stored agent as the builder holds it: read as an import is, so one
 * saved before a change to the format (a kind since renamed) reads as
 * today's.
 */
export function storedAgent(
  doc: AgentDocument,
  organizationId: string
): AgentDocument {
  const read = parseAgent(doc, { organizationId })
  return read.ok ? read.doc : doc
}

/** A document from JSON text (or parsed JSON), made to fit the format. */
export function parseAgent(
  input: string | unknown,
  { organizationId }: { organizationId: string }
): ImportResult<AgentDocument> {
  let raw: unknown = input
  if (typeof input === "string") {
    try {
      raw = JSON.parse(input)
    } catch (error) {
      return {
        ok: false,
        error: `This isn't valid JSON: ${error instanceof Error ? error.message : String(error)}`,
      }
    }
  }
  if (!isRecord(raw))
    return {
      ok: false,
      error: "An ADK workflow is a JSON object, with nodes and edges.",
    }
  if (raw.format !== AGENT_FORMAT) {
    return {
      ok: false,
      error: `This isn't a Forge ADK workflow: its "format" should be "${AGENT_FORMAT}".`,
    }
  }
  if (!Array.isArray(raw.nodes))
    return { ok: false, error: `An ADK workflow's "nodes" is a list of its nodes.` }

  const notes: string[] = []
  const nodes: AgentNode[] = []
  const byId = new Map<string, AgentNode>()
  raw.nodes.forEach((item, index) => {
    const at = `nodes[${index}]`
    if (!isRecord(item)) return notes.push(`${at} isn't an object; dropped.`)
    const { id } = item
    const [kind, rawConfig] = earlierKind(item.kind, item.config)
    if (typeof id !== "string" || !NODE_ID_PATTERN.test(id)) {
      return notes.push(
        `${at}.id should be lowercase letters, digits and _, starting with a letter; dropped.`
      )
    }
    if (byId.has(id))
      return notes.push(
        `${at}.id "${id}" is used twice; the second was dropped.`
      )
    if (!isAgentKind(kind))
      return notes.push(
        `${at}.kind "${String(kind)}" isn't a node Forge knows; dropped.`
      )
    const data = {
      kind,
      name:
        kind !== "start" && typeof item.name === "string" && item.name.trim()
          ? item.name
          : AGENT_KINDS[kind].label,
      config: readSettings(
        kind,
        AGENT_KINDS[kind].defaults() as Record<string, unknown>,
        rawConfig,
        `${at}.config`,
        notes,
        0
      ),
    } as AgentStep
    const node = {
      id,
      ...data,
      outputs: outputsOf(data).map((o) => o.id),
    } as AgentNode
    nodes.push(node)
    byId.set(id, node)
  })

  const edges: AgentEdge[] = []
  const seen = new Set<string>()
  const rawEdges = Array.isArray(raw.edges) ? raw.edges : []
  if (raw.edges !== undefined && !Array.isArray(raw.edges)) {
    notes.push(`"edges" isn't a list; the ADK workflow has no connections.`)
  }
  rawEdges.forEach((item, index) => {
    const at = `edges[${index}]`
    if (!isRecord(item)) return notes.push(`${at} isn't an object; dropped.`)
    const from =
      typeof item.source === "string" ? byId.get(item.source) : undefined
    if (!from) return notes.push(`${at}.source isn't a node; dropped.`)
    const target =
      typeof item.target === "string" ? byId.get(item.target) : undefined
    if (!target) return notes.push(`${at}.target isn't a node; dropped.`)
    if (target.kind === "start")
      return notes.push(`${at} leads into the start; dropped.`)
    const output = item.source_output
    if (typeof output !== "string" || !from.outputs.includes(output)) {
      return notes.push(
        `${at}.source_output "${String(output)}" isn't an output of ${from.id}; dropped.`
      )
    }
    const id = edgeId(from.id, output, target.id)
    if (seen.has(id)) return
    seen.add(id)
    edges.push({
      id,
      source: from.id,
      source_output: output,
      target: target.id,
    })
  })

  const layout: Record<string, Point> = {}
  let needsLayout = false
  const rawLayout = isRecord(raw.layout) ? raw.layout : {}
  for (const node of nodes) {
    const point = rawLayout[node.id]
    if (
      isRecord(point) &&
      typeof point.x === "number" &&
      typeof point.y === "number" &&
      Number.isFinite(point.x) &&
      Number.isFinite(point.y)
    ) {
      layout[node.id] = { x: point.x, y: point.y }
    } else needsLayout = true
  }

  const now = new Date().toISOString()
  const text = (value: unknown, fallback: string) =>
    typeof value === "string" ? value : fallback
  return {
    ok: true,
    notes,
    needsLayout,
    doc: {
      format: AGENT_FORMAT,
      id: typeof raw.id === "string" && raw.id ? raw.id : newAgentId(),
      name: text(raw.name, "").trim() || "Imported ADK workflow",
      description: text(raw.description, ""),
      organization_id: organizationId,
      nodes,
      edges,
      layout,
      created_at: text(raw.created_at, now),
      updated_at: text(raw.updated_at, now),
    },
  }
}
