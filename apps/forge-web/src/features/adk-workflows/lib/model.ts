import { FlowSquareIcon } from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
// Types only: the Agents builder's model imports this one's values.
import type { ChatConfigs } from "@/features/agents/lib/model"
import type { StepOutput } from "@/features/builder/lib/types"
import {
  slugify,
  uid,
  type StepConfigs,
  type ThinkingLevel,
} from "@/features/steps/lib/model"
import { isWorkflowTool } from "@/features/agents/lib/catalog"
import { isServedKind, kindInfo, waysOutOf } from "./catalog"
import type { FileTypeId } from "./files"

/*
 * An organization's agent: a Google ADK graph workflow (`google.adk.Workflow`)
 * of nodes joined by edges, run from its start. It combines two families of
 * node. Google ADK's agents: an LLM agent, or a Sequential, Parallel or Loop
 * agent with its sub-agents, another of the organization's agents nested
 * whole, and a pause for a person's answer. And Forge's workflow steps, with
 * the same kinds and settings as a `forge.workflow/v1` workflow's: an
 * approval, an HTTP request, a transform, a filter, a delay, If / Switch /
 * Match, a loop or a for-each over items, a merge and an end. A node leaves by `next`, or a
 * branching step by one of its ways out. Sub-agents aren't drawn: they're
 * set up in their agent's settings, and can have sub-agents of their own.
 * The builder edits this model; `document.ts` writes it as the agent's JSON,
 * and the admin API builds every node into an ADK node (agent_graph). What
 * each kind is called, its library group, its ways out and what a new one
 * starts with are the admin API's node catalog's (`catalog.ts`).
 */

export const AGENTS_ICON: IconProp = FlowSquareIcon

export type ModelChoice = { provider: string; name: string }

/**
 * Pi's built-in tools (pi.dev, packages/python/pi-agent's TOOLS), in the
 * order a Pi agent's settings list them: reading and searching, then
 * running commands and changing files.
 */
export const PI_TOOLS = [
  "read",
  "grep",
  "find",
  "ls",
  "bash",
  "edit",
  "write",
] as const

export type PiTool = (typeof PI_TOOLS)[number]

/** The tools that change nothing: a Pi agent's to start with. */
export const PI_READ_ONLY_TOOLS: PiTool[] = ["read", "grep", "find", "ls"]

/**
 * The tools an LLM agent may have, set up in its settings: the Agents
 * builder's tool kinds, with their settings (`ChatConfigs`). An agent from
 * the Agents page is used as a tool (`saved_agent`), a workflow too. These
 * are the ones its settings are written for (the format's); which of them
 * it's offered, in what order and called what, is the Agents canvas's node
 * catalog's (`workflow_tool`).
 */
export const LLM_TOOL_KINDS = [
  "mcp",
  "knowledge_base",
  "http_tool",
  "openapi",
  "saved_agent",
  "adk_workflow",
  "code_execution",
  "skills",
] as const satisfies readonly (keyof ChatConfigs)[]

export type LlmToolKind = (typeof LLM_TOOL_KINDS)[number]

/** Whether a kind is a tool an LLM agent may have now: served for workflows, and set up here. */
export const isLlmToolKind = (value: unknown): value is LlmToolKind =>
  (LLM_TOOL_KINDS as readonly unknown[]).includes(value) &&
  isWorkflowTool(value as string)

/** One of an LLM agent's tools. */
export type LlmTool<K extends LlmToolKind = LlmToolKind> = {
  [Kind in K]: {
    /** Made by the builder, unique among the agent's tools. */
    id: string
    kind: Kind
    /** What it's called: an HTTP tool's name for the model. */
    name: string
    config: ChatConfigs[Kind]
  }
}[K]

/** Which version of an agent from the Agents page: null for its latest published. */
export type AgentVersion = number | "draft" | null

/** What an LLM agent is set up with, as a node or as a sub-agent. */
export type LlmSettings = {
  /** What it's for: what its parent and peers read when handing off to it. */
  description: string
  /**
   * What it's told to do: a template, where `{{ }}` puts in the run's data
   * (input, steps.<id>.output, state…) as it runs.
   */
  instruction: string
  /** One of the models Forge offers; both empty for the default. */
  model: ModelChoice
  /** How long the model thinks: the nearest level it offers; empty for its own. */
  thinking_level: ThinkingLevel | ""
  /** A JSON Schema its answer is held to; `{}` leaves it free text. */
  output_schema: Record<string, unknown>
  /** `none`: it sees only what it's given, not the conversation so far. */
  include_contents: "default" | "none"
  disallow_transfer_to_parent: boolean
  disallow_transfer_to_peers: boolean
  /** The most tokens it answers with; null for the model's limit. */
  max_output_tokens: number | null
  /** The agents it may hand off to. */
  sub_agents: SubAgent[]
  /** What it can call: MCP servers, knowledge bases, HTTP endpoints, APIs, agents, workflows, code. */
  tools: LlmTool[]
}

/** A Sequential or Parallel agent: its sub-agents, in order. */
export type TeamSettings = {
  description: string
  sub_agents: SubAgent[]
}

/** A Loop agent: its sub-agents, run in order again and again. */
export type LoopSettings = TeamSettings & {
  /** It stops after this many passes. */
  max_iterations: number
}

export type SubAgentConfigs = {
  llm: LlmSettings
  sequential: TeamSettings
  parallel: TeamSettings
  loop_agent: LoopSettings
}

export type SubAgentKind = keyof SubAgentConfigs

/** A sub-agent, set up inside its agent's settings. */
export type SubAgent<K extends SubAgentKind = SubAgentKind> = {
  [Kind in K]: {
    id: string
    kind: Kind
    name: string
    config: SubAgentConfigs[Kind]
  }
}[K]

/**
 * Forge's steps a workflow takes as they are: their kinds and settings
 * come from steps/lib/model.ts, and their settings fields from step-fields.
 */
export const FORGE_KINDS = [
  "approval",
  "decision_board",
  "http",
  "bash",
  "transform",
  "filter",
  "delay",
  "git_clone",
  "git_branch",
  "git_push",
  "github_pull_request",
  "if",
  "switch",
  "match",
  "loop",
  "for_each",
  "merge",
  "end",
] as const satisfies readonly (keyof StepConfigs)[]

export type ForgeKind = (typeof FORGE_KINDS)[number]

export const isForgeKind = (kind: string): kind is ForgeKind =>
  (FORGE_KINDS as readonly string[]).includes(kind)

export type AgentConfigs = {
  start: {
    /** A JSON Schema of what a run starts with; `{}` takes anything. */
    input_schema: Record<string, unknown>
    /** Whether a run may start with files: artifacts of its session. */
    allow_files: boolean
    /** The types of files it takes (`files.ts`); none takes any type. */
    file_types: FileTypeId[]
    /**
     * Whether a run gets a folder of its own on the worker's file system,
     * whose path its steps read as `state.workspace`.
     */
    allow_file_system: boolean
    /**
     * Whether a run ADK can't carry on where it stopped starts over in a new
     * session, its steps running again; off, it fails and can be resubmitted.
     */
    start_over: boolean
  }
  llm: LlmSettings & {
    /** `single_turn`: it answers once; `task`: it may ask back until it's done. */
    mode: "single_turn" | "task"
    /**
     * `inline`: set up here, with the settings above; `agent`: an agent from
     * the Agents page, used whole (its own instruction, model and tools).
     */
    source: "inline" | "agent"
    /** The agent from the Agents page, by ID (`ca_…`), when its source is `agent`. */
    agent: string
    /** Which of its versions: a published one, its draft, or null for its latest published. */
    version: AgentVersion
    /** What it's sent: a template, `{{ }}` putting in the run's data; empty for what it's handed. */
    message: string
    /** The fields of its input schema, each a JSONata expression over the run's data. */
    inputs: Record<string, string>
  }
  sequential: TeamSettings
  parallel: TeamSettings
  loop_agent: LoopSettings
  saved: {
    /** Another of the organization's agents, by ID. */
    agent: string
    /** Which of its versions runs: a published one, its draft, or null for its latest published. */
    version: AgentVersion
    /**
     * The fields of its start's input, each a JSONata expression over the
     * run's data: with any, they're its input instead of what comes in.
     */
    inputs: Record<string, string>
  }
  pi_agent: {
    /**
     * Where pi works: a template naming a folder in the run's folder
     * (`state.workspace`), relative to it or starting with it; empty for
     * the run's folder itself. Made when it isn't there.
     */
    folder: string
    /** What pi is asked: a template; empty for what the node is handed. */
    prompt: string
    /** Added to pi's own system prompt: a template. */
    instruction: string
    /** Pi's built-in tools it may use; none for none. */
    tools: PiTool[]
    /** One of the models Forge offers; both empty for the default. */
    model: ModelChoice
    /** How long the model thinks; empty for pi's default. */
    thinking_level: ThinkingLevel | ""
    /** A JSON Schema its answer is held to; `{}` leaves it text. */
    output_schema: Record<string, unknown>
    /** Whether pi reads AGENTS.md and CLAUDE.md in its folder and above. */
    context_files: boolean
    /** It's stopped after this many seconds. */
    timeout_seconds: number
  }
  human_input: {
    /** What the person is asked; `{{ }}` puts in the run's data. */
    message: string
    /** A JSON Schema of their answer; `{}` takes anything. */
    response_schema: Record<string, unknown>
  }
} & Pick<StepConfigs, ForgeKind>

export type AgentKind = keyof AgentConfigs

/** A node as the builder holds it: the canvas node's data. */
export type AgentStep<K extends AgentKind = AgentKind> = {
  [Kind in K]: { kind: Kind; name: string; config: AgentConfigs[Kind] }
}[K]

/* -------------------------------------------------------------------------- */
/* Kinds                                                                      */
/* -------------------------------------------------------------------------- */

/** Whether a kind is one a workflow can have now: served, and set up here. */
export const isAgentKind = isServedKind

/** The kinds a sub-agent can be, in the order its menu lists them. */
export const SUB_AGENT_KINDS: SubAgentKind[] = [
  "llm",
  "sequential",
  "parallel",
  "loop_agent",
]

export const isSubAgentKind = (value: unknown): value is SubAgentKind =>
  typeof value === "string" && (SUB_AGENT_KINDS as string[]).includes(value)

/** What only an LLM node has, not an LLM sub-agent. */
const LLM_NODE_ONLY = [
  "mode",
  "source",
  "agent",
  "version",
  "message",
  "inputs",
] as const satisfies readonly Exclude<
  keyof AgentConfigs["llm"],
  keyof LlmSettings
>[]

/** A sub-agent's settings: its kind's, without what only a node has. */
export function subAgentDefaults<K extends SubAgentKind>(
  kind: K
): SubAgentConfigs[K] {
  if (kind !== "llm")
    return kindInfo(kind).defaults() as unknown as SubAgentConfigs[K]
  const settings: Partial<AgentConfigs["llm"]> = kindInfo("llm").defaults()
  for (const key of LLM_NODE_ONLY) delete settings[key]
  return settings as SubAgentConfigs[K]
}

/* -------------------------------------------------------------------------- */
/* Outputs                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * A node's ways out, in the order the canvas lists them, as its kind's are
 * served: Success / Error, True / False, a case or rule each and the way
 * for the rest, a loop's Each item and Done, none for an End; `next` for
 * the rest.
 */
export function outputsOf(step: AgentStep): StepOutput[] {
  return waysOutOf(step.kind, step.config)
}

/** The Forge steps a run can wait at: a person deciding, or time passing. */
export const PAUSING_KINDS = new Set<AgentKind>([
  "approval",
  "decision_board",
  "human_input",
  "delay",
])

/* -------------------------------------------------------------------------- */
/* Names and IDs                                                              */
/* -------------------------------------------------------------------------- */

/** A node's ID: what edges call it. */
export const NODE_ID_PATTERN = /^[a-z][a-z0-9_]{0,47}$/

/**
 * What a sub-agent's ID is: a state key (an LLM sub-agent's answer is kept
 * under it), so a path can read it as `state.<id>`.
 */
export const SUB_AGENT_ID = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/

/**
 * A node's or sub-agent's ADK name: its name as a Python identifier
 * (`Billing specialist` → `billing_specialist`). Empty when its name has no
 * letter to start one with.
 */
export const adkName = (name: string) => slugify(name)

/** ADK keeps this name for the person. */
export const RESERVED_NAMES = new Set(["user"])

/** Every agent in a sub-agent list, depth first, with the names above it. */
export function* walkSubAgents(
  agents: SubAgent[],
  path: string[] = []
): Generator<{ agent: SubAgent; path: string[] }> {
  for (const agent of agents) {
    yield { agent, path: [...path, agent.id] }
    yield* walkSubAgents(agent.config.sub_agents, [...path, agent.id])
  }
}

/** A node's sub-agents, when its kind has them. */
export const subAgentsOf = (step: AgentStep): SubAgent[] =>
  "sub_agents" in step.config ? step.config.sub_agents : []

// The nodes that are ADK agents themselves.
const AGENT_NODE_KINDS = new Set<AgentKind>([
  "llm",
  "sequential",
  "parallel",
  "loop_agent",
])

/** How many ADK agents the nodes hold: the agent nodes, and every sub-agent in them. */
export const countAgents = (steps: AgentStep[]) =>
  steps.reduce(
    (sum, step) =>
      sum +
      (AGENT_NODE_KINDS.has(step.kind) ? 1 : 0) +
      [...walkSubAgents(subAgentsOf(step))].length,
    0
  )

/** Every name in use: the nodes' and their sub-agents'. */
export function namesIn(steps: AgentStep[]): string[] {
  const names: string[] = []
  for (const step of steps) {
    if (step.kind !== "start") names.push(step.name)
    for (const { agent } of walkSubAgents(subAgentsOf(step)))
      names.push(agent.name)
  }
  return names
}

/** `base`, or `base 2`, `base 3`…: the first whose ADK name isn't taken. */
export function uniqueName(base: string, taken: Iterable<string>) {
  const used = new Set([...taken].map(adkName))
  if (!used.has(adkName(base))) return base
  for (let n = 2; ; n += 1) {
    const name = `${base} ${n}`
    if (!used.has(adkName(name))) return name
  }
}

const ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

/**
 * A new node's ID, made by the builder: its kind and a random suffix
 * (`agent_k3f9x`), unlike any ID the agent has. The start is `start`.
 */
export function nextNodeId(kind: AgentKind, taken: Iterable<string>) {
  const used = new Set(taken)
  if (kind === "start" && !used.has("start")) return "start"
  const prefix = kindInfo(kind).idPrefix
  for (;;) {
    const random = crypto.getRandomValues(new Uint8Array(5))
    const id = `${prefix}_${Array.from(random, (b) => ID_ALPHABET[b % ID_ALPHABET.length]).join("")}`
    if (!used.has(id)) return id
  }
}

/** A new node of a kind, named apart from `steps`. */
export function newNode<K extends AgentKind>(
  kind: K,
  steps: AgentStep[] = [],
  name?: string
): AgentStep<K> {
  const info = kindInfo(kind)
  return {
    kind,
    name:
      kind === "start"
        ? info.label
        : uniqueName(name ?? info.label, namesIn(steps)),
    config: info.defaults(),
  } as AgentStep<K>
}

/** A new sub-agent of a kind, named apart from `taken`. */
export function newSubAgent<K extends SubAgentKind>(
  kind: K,
  taken: Iterable<string>
): SubAgent<K> {
  const base = kind === "llm" ? "Sub-agent" : kindInfo(kind).label
  return {
    id: uid("sub"),
    kind,
    name: uniqueName(base, taken),
    config: subAgentDefaults(kind),
  } as SubAgent<K>
}

/** Copies of sub-agents with new IDs and names apart from `taken` (which grows). */
function copySubAgents(agents: SubAgent[], taken: string[]): SubAgent[] {
  return agents.map((agent) => {
    const name = uniqueName(`${agent.name} copy`, taken)
    taken.push(name)
    return {
      ...agent,
      id: uid("sub"),
      name,
      config: {
        ...agent.config,
        sub_agents: copySubAgents(agent.config.sub_agents, taken),
      },
    } as SubAgent
  })
}

/** A copy of a node, its sub-agents with it, every name apart from `steps`. */
export function copyNode(step: AgentStep, steps: AgentStep[]): AgentStep {
  const copy = structuredClone(step)
  const taken = namesIn(steps)
  if (copy.kind !== "start") {
    copy.name = uniqueName(`${step.name} copy`, taken)
    taken.push(copy.name)
  }
  if ("sub_agents" in copy.config) {
    const config = copy.config as { sub_agents: SubAgent[] }
    return {
      ...copy,
      config: {
        ...config,
        sub_agents: copySubAgents(config.sub_agents, taken),
      },
    } as AgentStep
  }
  return copy
}

/* -------------------------------------------------------------------------- */
/* Sub-agents by path                                                         */
/* -------------------------------------------------------------------------- */

/** The sub-agent a path of IDs leads to, from a node's sub-agents. */
export function subAgentAt(
  step: AgentStep,
  path: string[]
): SubAgent | undefined {
  let list = subAgentsOf(step)
  let found: SubAgent | undefined
  for (const id of path) {
    found = list.find((a) => a.id === id)
    if (!found) return undefined
    list = found.config.sub_agents
  }
  return found
}

function updateIn(
  agents: SubAgent[],
  path: string[],
  change: (agent: SubAgent) => SubAgent
): SubAgent[] {
  const [id, ...rest] = path
  return agents.map((agent) => {
    if (agent.id !== id) return agent
    if (!rest.length) return change(agent)
    return {
      ...agent,
      config: {
        ...agent.config,
        sub_agents: updateIn(agent.config.sub_agents, rest, change),
      },
    } as SubAgent
  })
}

/** The node with the sub-agent at `path` changed. */
export function updateSubAgent(
  step: AgentStep,
  path: string[],
  change: (agent: SubAgent) => SubAgent
): AgentStep {
  if (!("sub_agents" in step.config)) return step
  const config = step.config as { sub_agents: SubAgent[] }
  return {
    ...step,
    config: {
      ...config,
      sub_agents: updateIn(config.sub_agents, path, change),
    },
  } as AgentStep
}
