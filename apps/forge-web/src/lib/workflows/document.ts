import { edgeId, type Point } from "@/lib/builder/types"
import {
  isStepKind,
  newStep,
  outputsOf,
  STEP_ID_PATTERN,
  STEP_KINDS,
  THINKING_LEVELS,
  uid,
  type StepConfigs,
  type StepData,
  type StepKind,
} from "./model"

/*
 * A workflow as JSON: the graph a runner executes, and where the builder
 * drew each step. `nodes` are the steps, each with its kind, settings and
 * the outputs it can leave by; `edges` join an output of one step to
 * another step; `entry` names the step every run starts at. `layout` is
 * for the builder only: a runner ignores it. The builder saves workflows
 * in this format, exports it and imports it, so all three read the same.
 * `schema.ts` describes it as a JSON Schema.
 */

export const WORKFLOW_FORMAT = "forge.workflow/v1"

export type WorkflowNode<K extends StepKind = StepKind> = {
  [Kind in K]: {
    id: string
    kind: Kind
    name: string
    config: StepConfigs[Kind]
    /** The IDs of its outputs, in order; connections leave by these. */
    outputs: string[]
  }
}[K]

export type WorkflowEdge = {
  id: string
  source: string
  /** Which of the source step's outputs it leaves by. */
  source_output: string
  target: string
}

export { edgeId, type Point } from "@/lib/builder/types"

export type WorkflowDocument = {
  format: typeof WORKFLOW_FORMAT
  id: string
  name: string
  description: string
  organization_id: string
  /** The entry point step's ID; null without one. */
  entry: string | null
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  /** Where the builder draws each step, by ID. */
  layout: Record<string, Point>
  created_at: string
  updated_at: string
}

/** The step and connection lists the builder edits. */
export type WorkflowGraph = {
  steps: { id: string; data: StepData; position: Point }[]
  connections: { source: string; output: string; target: string }[]
}


export function newWorkflowId() {
  const random = crypto.getRandomValues(new Uint32Array(2))
  return `wf_${Array.from(random, (n) => n.toString(36)).join("").slice(0, 10)}`
}

/** The document for a graph. */
export function toDocument(
  meta: Pick<
    WorkflowDocument,
    "id" | "name" | "description" | "organization_id" | "created_at"
  > & { updated_at?: string },
  graph: WorkflowGraph
): WorkflowDocument {
  const entry = graph.steps.find((s) => s.data.kind === "entry")
  return {
    format: WORKFLOW_FORMAT,
    id: meta.id,
    name: meta.name,
    description: meta.description,
    organization_id: meta.organization_id,
    entry: entry?.id ?? null,
    nodes: graph.steps.map(
      (s) =>
        ({
          id: s.id,
          kind: s.data.kind,
          name: s.data.name,
          config: s.data.config,
          outputs: outputsOf(s.data).map((o) => o.id),
        }) as WorkflowNode
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

/**
 * The graph of a document the builder can open (one `parseWorkflow` passed,
 * or the API stored): its steps with any settings their kinds gained since
 * it was saved.
 */
export function toGraph(doc: WorkflowDocument): WorkflowGraph {
  return {
    steps: doc.nodes.map((node, index) => ({
      id: node.id,
      data: { kind: node.kind, name: node.name, config: withAddedSettings(node) } as StepData,
      position: doc.layout[node.id] ?? { x: index * 320, y: 0 },
    })),
    connections: doc.edges.map((e) => ({
      source: e.source,
      output: e.source_output,
      target: e.target,
    })),
  }
}

/** A new workflow: its entry point and nothing else yet. */
export function newWorkflow(organizationId: string, name: string): WorkflowDocument {
  const now = new Date().toISOString()
  return toDocument(
    {
      id: newWorkflowId(),
      name,
      description: "",
      organization_id: organizationId,
      created_at: now,
      updated_at: now,
    },
    {
      steps: [
        { id: "start", data: newStep("entry", "Start"), position: { x: 0, y: 0 } },
      ],
      connections: [],
    }
  )
}

/** A copy of a workflow under a new ID. */
export function copyWorkflow(doc: WorkflowDocument, name: string): WorkflowDocument {
  const now = new Date().toISOString()
  return { ...structuredClone(doc), id: newWorkflowId(), name, created_at: now, updated_at: now }
}

/* -------------------------------------------------------------------------- */
/* Reading a document                                                         */
/* -------------------------------------------------------------------------- */

export type ParseResult =
  | {
      ok: true
      doc: WorkflowDocument
      /** What was dropped or filled in to make it fit. */
      notes: string[]
      /** Steps had no position, so the builder should tidy them. */
      needsLayout: boolean
    }
  | { ok: false; error: string }

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

// Settings that take one of a few values.
const ENUMS: Record<string, readonly string[]> = {
  trigger: ["manual", "event", "schedule"],
  output: ["text", "json"],
  approvers: ["org:admin", "org:member"],
  method: ["GET", "POST", "PUT", "PATCH", "DELETE"],
  unit: ["seconds", "minutes", "hours", "days"],
  mode: ["all", "any"],
  outcome: ["succeeded", "failed"],
  // Empty: the model's own.
  thinking_level: ["", ...THINKING_LEVELS],
}

// Settings that hold a whole JSON value of their own: a JSON Schema, by kind.
const FREE_FORM = new Set(["entry.input_schema", "http.output_schema", "transform.output_schema"])

// Settings a kind gained after workflows were stored: a document saved
// before them opens with their defaults (the admin API fills them in when
// one is saved: ADDED_SETTINGS in its workflows.py).
const ADDED_SETTINGS: Partial<Record<StepKind, string[]>> = {
  agent: ["thinking_level"],
  http: ["output_schema"],
}

function withAddedSettings(node: WorkflowNode): StepConfigs[StepKind] {
  const missing = (ADDED_SETTINGS[node.kind] ?? []).filter((key) => !(key in node.config))
  if (missing.length === 0) return node.config
  const defaults = STEP_KINDS[node.kind].defaults() as Record<string, unknown>
  return { ...node.config, ...Object.fromEntries(missing.map((key) => [key, defaults[key]])) }
}

// Ways out a kind once had under another name: connections from them move.
// None at the moment (the worker's document.py keeps the same list).
const RENAMED_OUTPUTS: Partial<Record<StepKind, Record<string, string>>> = {}

// What each item of a list setting looks like.
const ITEMS: Record<string, Record<string, string> | "string"> = {
  headers: { id: "", name: "", value: "" },
  cases: { id: "", value: "" },
  arms: { id: "", label: "", condition: "" },
  event_types: "string",
}

/**
 * A kind's settings from whatever a document holds: the defaults, with
 * every value of the right type taken from `raw`. What doesn't fit is
 * dropped and noted.
 */
function readConfig(
  kind: StepKind,
  raw: unknown,
  at: string,
  notes: string[]
): StepConfigs[StepKind] {
  const defaults = STEP_KINDS[kind].defaults() as Record<string, unknown>
  if (raw === undefined) return defaults as StepConfigs[StepKind]
  // An entry point once named one event type, as `event_type`.
  if (
    kind === "entry" &&
    isRecord(raw) &&
    typeof raw.event_type === "string" &&
    raw.event_types === undefined
  ) {
    const { event_type: key, ...rest } = raw
    raw = { ...rest, event_types: key ? [key] : [] }
  }
  if (!isRecord(raw)) {
    notes.push(`${at}.config isn't an object; it has its defaults.`)
    return defaults as StepConfigs[StepKind]
  }
  const config: Record<string, unknown> = {}
  for (const [key, fallback] of Object.entries(defaults)) {
    const value = raw[key]
    const path = `${at}.config.${key}`
    if (value === undefined) {
      config[key] = fallback
    } else if (FREE_FORM.has(`${kind}.${key}`)) {
      // A JSON Schema: kept as it is, whatever it holds.
      if (isRecord(value)) config[key] = value
      else notes.push(`${path} should be an object.`)
    } else if (key in ENUMS) {
      if (typeof value === "string" && ENUMS[key].includes(value)) {
        config[key] = value
      } else {
        const values = ENUMS[key].map((each) => each || "empty")
        notes.push(`${path} should be one of ${values.join(", ")}.`)
        config[key] = fallback
      }
    } else if (Array.isArray(fallback)) {
      config[key] = readList(key, value, path, notes) ?? fallback
    } else if (isRecord(fallback)) {
      // The agent's model: provider and name.
      const nested: Record<string, unknown> = { ...fallback }
      if (isRecord(value)) {
        for (const field of Object.keys(fallback)) {
          if (typeof value[field] === typeof fallback[field]) {
            nested[field] = value[field]
          }
        }
      } else notes.push(`${path} isn't an object.`)
      config[key] = nested
    } else if (typeof value === typeof fallback) {
      config[key] =
        typeof value === "number" && !Number.isFinite(value) ? fallback : value
    } else {
      notes.push(`${path} should be a ${typeof fallback}.`)
      config[key] = fallback
    }
  }
  for (const key of Object.keys(raw)) {
    if (!(key in defaults)) notes.push(`${at}.config.${key} isn't a setting of ${kind}; dropped.`)
  }
  return config as StepConfigs[StepKind]
}

/** A list setting (a switch's cases, a match's rules, headers…), each item made to fit. */
export function readList(key: string, value: unknown, path: string, notes: string[]) {
  const shape = ITEMS[key]
  if (!Array.isArray(value) || !shape) {
    notes.push(`${path} should be a list.`)
    return undefined
  }
  if (shape === "string") {
    return [...new Set(value.filter((item): item is string => typeof item === "string"))]
  }
  const ids = new Set<string>()
  return value.flatMap((item, index) => {
    if (!isRecord(item)) {
      notes.push(`${path}[${index}] isn't an object; dropped.`)
      return []
    }
    const row: Record<string, string> = {}
    for (const field of Object.keys(shape)) {
      row[field] = typeof item[field] === "string" ? (item[field] as string) : ""
    }
    if (!row.id || ids.has(row.id)) row.id = uid(key.replace(/s$/, ""))
    ids.add(row.id)
    return [row]
  })
}

/** A document from JSON text (or parsed JSON), made to fit the format. */
export function parseWorkflow(
  input: string | unknown,
  { organizationId }: { organizationId: string }
): ParseResult {
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
  if (!isRecord(raw)) {
    return { ok: false, error: "A workflow is a JSON object, with nodes and edges." }
  }
  if (raw.format !== WORKFLOW_FORMAT) {
    return {
      ok: false,
      error: `This isn't a Forge workflow: its "format" should be "${WORKFLOW_FORMAT}".`,
    }
  }
  if (!Array.isArray(raw.nodes)) {
    return { ok: false, error: `A workflow's "nodes" is a list of its steps.` }
  }
  const notes: string[] = []
  const nodes: WorkflowNode[] = []
  const byId = new Map<string, WorkflowNode>()
  raw.nodes.forEach((item, index) => {
    const at = `nodes[${index}]`
    if (!isRecord(item)) {
      notes.push(`${at} isn't an object; dropped.`)
      return
    }
    const { id, kind } = item
    if (typeof id !== "string" || !STEP_ID_PATTERN.test(id)) {
      notes.push(`${at}.id should be lowercase letters, digits and _, starting with a letter; dropped.`)
      return
    }
    if (byId.has(id)) {
      notes.push(`${at}.id "${id}" is used twice; the second was dropped.`)
      return
    }
    if (!isStepKind(kind)) {
      notes.push(`${at}.kind "${String(kind)}" isn't a step Forge knows; dropped.`)
      return
    }
    const data = {
      kind,
      // The start step has no name of its own.
      name:
        kind !== "entry" && typeof item.name === "string" && item.name.trim()
          ? item.name
          : STEP_KINDS[kind].label,
      config: readConfig(kind, item.config, at, notes),
    } as StepData
    const node = {
      id,
      ...data,
      outputs: outputsOf(data).map((o) => o.id),
    } as WorkflowNode
    nodes.push(node)
    byId.set(id, node)
  })

  const edges: WorkflowEdge[] = []
  const seen = new Set<string>()
  const rawEdges = Array.isArray(raw.edges) ? raw.edges : []
  if (raw.edges !== undefined && !Array.isArray(raw.edges)) {
    notes.push(`"edges" isn't a list; the workflow has no connections.`)
  }
  rawEdges.forEach((item, index) => {
    const at = `edges[${index}]`
    if (!isRecord(item)) return notes.push(`${at} isn't an object; dropped.`)
    const { source, target } = item
    let output = item.source_output
    const from = typeof source === "string" ? byId.get(source) : undefined
    if (!from) return notes.push(`${at}.source isn't a step; dropped.`)
    if (typeof target !== "string" || !byId.has(target)) {
      return notes.push(`${at}.target isn't a step; dropped.`)
    }
    if (typeof output === "string") output = RENAMED_OUTPUTS[from.kind]?.[output] ?? output
    if (typeof output !== "string" || !from.outputs.includes(output)) {
      return notes.push(
        `${at}.source_output "${String(output)}" isn't an output of ${from.id}; dropped.`
      )
    }
    const id = edgeId(from.id, output, target)
    if (seen.has(id)) return
    seen.add(id)
    edges.push({ id, source: from.id, source_output: output, target })
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

  const entries = nodes.filter((n) => n.kind === "entry")
  const now = new Date().toISOString()
  const text = (value: unknown, fallback: string) =>
    typeof value === "string" ? value : fallback
  return {
    ok: true,
    notes,
    needsLayout,
    doc: {
      format: WORKFLOW_FORMAT,
      id: typeof raw.id === "string" && raw.id ? raw.id : newWorkflowId(),
      name: text(raw.name, "").trim() || "Imported workflow",
      description: text(raw.description, ""),
      organization_id: organizationId,
      entry: entries[0]?.id ?? null,
      nodes,
      edges,
      layout,
      created_at: text(raw.created_at, now),
      updated_at: text(raw.updated_at, now),
    },
  }
}
