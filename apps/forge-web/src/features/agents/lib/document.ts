import type { ImportResult } from "@/features/builder/components/import-dialog"
import { edgeId, type BuilderGraph, type Point } from "@/features/builder/lib/types"
import { readList } from "@/features/steps/lib/document"
import { HTTP_METHODS, THINKING_LEVELS } from "@/features/steps/lib/model"
import {
  accepts,
  CHAT_KINDS,
  isChatKind,
  newNode,
  NODE_ID_PATTERN,
  outputsOf,
  type ChatConfigs,
  type ChatKind,
  type ChatStep,
} from "./model"

/*
 * A chat agent as JSON: the agent, what's attached to it, and where the
 * builder drew each. `nodes` are the agent, its sub-agents and tools, each
 * with its kind and settings; `edges` join an agent's way out (`tools`,
 * `agents`) to what it calls or hands off to. `layout` is for the builder
 * only. The Agents page keeps chat agents in this format, exports it and
 * imports it.
 */

export const CHAT_AGENT_FORMAT = "forge.chat_agent/v1"

export type ChatAgentNode<K extends ChatKind = ChatKind> = {
  [Kind in K]: {
    id: string
    kind: Kind
    name: string
    config: ChatConfigs[Kind]
    /** The IDs of its ways out, in order; edges leave by these. */
    outputs: string[]
  }
}[K]

export type ChatAgentEdge = {
  id: string
  source: string
  /** Which of the source's ways out it leaves by: `tools` or `agents`. */
  source_output: string
  target: string
}

export type ChatAgentDocument = {
  format: typeof CHAT_AGENT_FORMAT
  id: string
  name: string
  description: string
  organization_id: string
  nodes: ChatAgentNode[]
  edges: ChatAgentEdge[]
  /** Where the builder draws each node, by ID. */
  layout: Record<string, Point>
  created_at: string
  updated_at: string
}

export type ChatAgentGraph = BuilderGraph<ChatStep>

export function newChatAgentId() {
  const random = crypto.getRandomValues(new Uint32Array(2))
  return `ca_${Array.from(random, (n) => n.toString(36))
    .join("")
    .slice(0, 10)}`
}

/** The document for a graph. */
export function toDocument(
  meta: Pick<
    ChatAgentDocument,
    "id" | "name" | "description" | "organization_id" | "created_at"
  > & { updated_at?: string },
  graph: ChatAgentGraph
): ChatAgentDocument {
  return {
    format: CHAT_AGENT_FORMAT,
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
        }) as ChatAgentNode
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
export function toGraph(doc: ChatAgentDocument): ChatAgentGraph {
  return {
    steps: doc.nodes.map((node, index) => ({
      id: node.id,
      data: {
        kind: node.kind,
        name: node.name,
        config: node.config,
      } as ChatStep,
      position: doc.layout[node.id] ?? { x: index * 320, y: 0 },
    })),
    connections: doc.edges.map((e) => ({
      source: e.source,
      output: e.source_output,
      target: e.target,
    })),
  }
}

/** A new chat agent: just the agent, named as the document is. */
export function newChatAgent(
  organizationId: string,
  name: string
): ChatAgentDocument {
  const now = new Date().toISOString()
  return toDocument(
    {
      id: newChatAgentId(),
      name,
      description: "",
      organization_id: organizationId,
      created_at: now,
      updated_at: now,
    },
    {
      steps: [
        {
          id: "agent",
          data: newNode("agent", [], name),
          position: { x: 0, y: 0 },
        },
      ],
      connections: [],
    }
  )
}

/** A copy of a chat agent under a new ID. */
export function copyChatAgent(
  doc: ChatAgentDocument,
  name: string
): ChatAgentDocument {
  const now = new Date().toISOString()
  const copy = structuredClone(doc)
  return {
    ...copy,
    id: newChatAgentId(),
    name,
    // The chat agent is its agent: the name goes to both.
    nodes: copy.nodes.map((n) => (n.kind === "agent" ? { ...n, name } : n)),
    created_at: now,
    updated_at: now,
  }
}

/** The IDs of the organization's other agents a chat agent uses (saved agents). */
export const usesOf = (doc: Pick<ChatAgentDocument, "nodes">) =>
  doc.nodes.flatMap((n) =>
    n.kind === "saved_agent" && n.config.agent ? [n.config.agent] : []
  )

/* -------------------------------------------------------------------------- */
/* Reading a document                                                         */
/* -------------------------------------------------------------------------- */

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

// Settings that take one of a few values: by kind, then any kind's.
const KIND_ENUMS: Partial<Record<ChatKind, Record<string, readonly string[]>>> =
  {
    sub_agent: { mode: ["chat", "task", "single_turn"] },
    memory: { mode: ["on_demand", "every_turn"] },
    http_tool: { method: HTTP_METHODS },
    openapi: { source: ["url", "inline"] },
    mcp: { transport: ["streamable_http", "sse"] },
  }
const ENUMS: Record<string, readonly string[]> = {
  include_contents: ["default", "none"],
  thinking_level: ["", ...THINKING_LEVELS],
}

/**
 * Settings from whatever a document holds: the defaults, with every value
 * of the right type taken from `raw`. What doesn't fit is dropped and noted.
 */
function readSettings(
  kind: ChatKind,
  raw: unknown,
  at: string,
  notes: string[]
): Record<string, unknown> {
  const defaults = CHAT_KINDS[kind].defaults() as Record<string, unknown>
  if (raw === undefined) return defaults
  if (!isRecord(raw)) {
    notes.push(`${at} isn't an object; it has its defaults.`)
    return defaults
  }
  const enums = { ...ENUMS, ...KIND_ENUMS[kind] }
  const config: Record<string, unknown> = {}
  for (const [key, fallback] of Object.entries(defaults)) {
    const value = raw[key]
    const path = `${at}.${key}`
    config[key] = fallback
    if (value === undefined) continue
    if (key in enums) {
      if (typeof value === "string" && enums[key].includes(value))
        config[key] = value
      else
        notes.push(
          `${path} should be one of ${enums[key].map((v) => v || "empty").join(", ")}.`
        )
    } else if (key === "headers") {
      config[key] = readList(key, value, path, notes) ?? fallback
    } else if (key === "parameters") {
      if (isRecord(value)) config[key] = value
      else notes.push(`${path} should be a JSON Schema object.`)
    } else if (key === "max_output_tokens") {
      if (
        value === null ||
        (typeof value === "number" && Number.isFinite(value))
      )
        config[key] = value
      else notes.push(`${path} should be a number, or null.`)
    } else if (key === "model") {
      const model = { provider: "", name: "" }
      if (isRecord(value)) {
        if (typeof value.provider === "string") model.provider = value.provider
        if (typeof value.name === "string") model.name = value.name
      } else notes.push(`${path} isn't an object.`)
      config[key] = model
    } else if (typeof value === typeof fallback) {
      config[key] = value
    } else {
      notes.push(`${path} should be a ${typeof fallback}.`)
    }
  }
  for (const key of Object.keys(raw)) {
    if (!(key in defaults)) notes.push(`${at}.${key} isn't a setting; dropped.`)
  }
  return config
}

/** A document from JSON text (or parsed JSON), made to fit the format. */
export function parseChatAgent(
  input: string | unknown,
  { organizationId }: { organizationId: string }
): ImportResult<ChatAgentDocument> {
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
    return {
      ok: false,
      error: "An agent is a JSON object, with nodes and edges.",
    }
  }
  if (raw.format !== CHAT_AGENT_FORMAT) {
    return {
      ok: false,
      error: `This isn't a Forge agent: its "format" should be "${CHAT_AGENT_FORMAT}".`,
    }
  }
  if (!Array.isArray(raw.nodes)) {
    return { ok: false, error: `An agent's "nodes" is a list of its nodes.` }
  }

  const notes: string[] = []
  const nodes: ChatAgentNode[] = []
  const byId = new Map<string, ChatAgentNode>()
  raw.nodes.forEach((item, index) => {
    const at = `nodes[${index}]`
    if (!isRecord(item)) return notes.push(`${at} isn't an object; dropped.`)
    const { id, kind } = item
    if (typeof id !== "string" || !NODE_ID_PATTERN.test(id)) {
      return notes.push(
        `${at}.id should be lowercase letters, digits and _, starting with a letter; dropped.`
      )
    }
    if (byId.has(id))
      return notes.push(
        `${at}.id "${id}" is used twice; the second was dropped.`
      )
    if (!isChatKind(kind)) {
      return notes.push(
        `${at}.kind "${String(kind)}" isn't something Forge's agents know; dropped.`
      )
    }
    if (kind === "agent" && nodes.some((n) => n.kind === "agent")) {
      return notes.push(
        `${at} is a second chat agent; an agent has one, so it was dropped.`
      )
    }
    const data = {
      kind,
      name:
        typeof item.name === "string" && item.name.trim()
          ? item.name
          : CHAT_KINDS[kind].label,
      config: readSettings(kind, item.config, `${at}.config`, notes),
    } as ChatStep
    const node = {
      id,
      ...data,
      outputs: outputsOf(data).map((o) => o.id),
    } as ChatAgentNode
    nodes.push(node)
    byId.set(id, node)
  })

  const edges: ChatAgentEdge[] = []
  const seen = new Set<string>()
  if (raw.edges !== undefined && !Array.isArray(raw.edges)) {
    notes.push(`"edges" isn't a list; nothing is connected.`)
  }
  const rawEdges = Array.isArray(raw.edges) ? raw.edges : []
  rawEdges.forEach((item, index) => {
    const at = `edges[${index}]`
    if (!isRecord(item)) return notes.push(`${at} isn't an object; dropped.`)
    const from =
      typeof item.source === "string" ? byId.get(item.source) : undefined
    if (!from) return notes.push(`${at}.source isn't a node; dropped.`)
    const target =
      typeof item.target === "string" ? byId.get(item.target) : undefined
    if (!target) return notes.push(`${at}.target isn't a node; dropped.`)
    const output = item.source_output
    if (typeof output !== "string" || !from.outputs.includes(output)) {
      return notes.push(
        `${at}.source_output "${String(output)}" isn't a way out of ${from.id}; dropped.`
      )
    }
    if (!accepts(from as ChatStep, output, target.kind)) {
      return notes.push(
        `${at}: ${from.id} can't lead to ${target.id} that way; dropped.`
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
      format: CHAT_AGENT_FORMAT,
      id: typeof raw.id === "string" && raw.id ? raw.id : newChatAgentId(),
      name: text(raw.name, "").trim() || "Imported agent",
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

/** A kept chat agent as the builder holds it: read as an import is. */
export function storedChatAgent(
  doc: ChatAgentDocument,
  organizationId: string
): ChatAgentDocument {
  const read = parseChatAgent(doc, { organizationId })
  return read.ok ? read.doc : doc
}
