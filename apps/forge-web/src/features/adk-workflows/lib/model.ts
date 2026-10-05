import {
  FlowSquareIcon,
  DistributionIcon,
  LeftToRightListNumberIcon,
  PlayIcon,
  RepeatIcon,
  Robot01Icon,
  UserQuestion01Icon,
} from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { KindGroup, KindInfo, StepOutput } from "@/features/builder/lib/types"
import {
  outputsOf as stepOutputsOf,
  slugify,
  STEP_KINDS,
  uid,
  type StepConfigs,
  type StepData,
  type ThinkingLevel,
} from "@/features/steps/lib/model"

/*
 * An organization's agent: a Google ADK graph workflow (`google.adk.Workflow`)
 * of nodes joined by edges, run from its start. It combines two families of
 * node. Google ADK's agents: an LLM agent, or a Sequential, Parallel or Loop
 * agent with its sub-agents, another of the organization's agents nested
 * whole, and a pause for a person's answer. And Forge's workflow steps, with
 * the same kinds and settings as a `forge.workflow/v1` workflow's: an
 * approval, an HTTP request, a transform, a delay, If / Switch / Match, a
 * loop over items, a merge and an end. A node leaves by `next`, or a
 * branching step by one of its ways out. Sub-agents aren't drawn: they're
 * set up in their agent's settings, and can have sub-agents of their own.
 * The builder edits this model; `document.ts` writes it as the agent's JSON,
 * and the admin API builds every node into an ADK node (agent_graph).
 */

export const AGENTS_ICON: IconProp = FlowSquareIcon

export type ModelChoice = { provider: string; name: string }

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
 * Forge's steps a workflow takes as they are: their kinds, settings
 * and ways out come from steps/lib/model.ts.
 */
export const FORGE_KINDS = [
  "approval",
  "http",
  "transform",
  "delay",
  "if",
  "switch",
  "match",
  "loop",
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
  }
  llm: LlmSettings & {
    /** `single_turn`: it answers once; `task`: it may ask back until it's done. */
    mode: "single_turn" | "task"
  }
  sequential: TeamSettings
  parallel: TeamSettings
  loop_agent: LoopSettings
  saved: {
    /** Another of the organization's agents, by ID. */
    agent: string
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
/* Catalog                                                                    */
/* -------------------------------------------------------------------------- */

export const AGENT_GROUPS: KindGroup[] = [
  { id: "start", label: "Start" },
  { id: "agents", label: "Agents" },
  { id: "people", label: "People" },
  { id: "actions", label: "Actions" },
  { id: "logic", label: "Logic" },
  { id: "finish", label: "Finish" },
]

type AgentKindInfo<K extends AgentKind> = KindInfo & {
  defaults: () => AgentConfigs[K]
}

const FORGE_GROUPS: Record<ForgeKind, string> = {
  approval: "people",
  http: "actions",
  transform: "actions",
  delay: "actions",
  if: "logic",
  switch: "logic",
  match: "logic",
  loop: "logic",
  merge: "logic",
  end: "finish",
}

/** A Forge step kind as the library shows it: as the workflow builder does. */
function forgeKind<K extends ForgeKind>(kind: K): AgentKindInfo<K> {
  const { defaults, ...info } = STEP_KINDS[kind]
  return {
    ...info,
    group: FORGE_GROUPS[kind],
    defaults: defaults as () => AgentConfigs[K],
  }
}

const llmSettings = (): LlmSettings => ({
  description: "",
  instruction: "",
  model: { provider: "", name: "" },
  thinking_level: "",
  output_schema: {},
  include_contents: "default",
  disallow_transfer_to_parent: false,
  disallow_transfer_to_peers: false,
  max_output_tokens: null,
  sub_agents: [],
})

export const AGENT_KINDS: { [K in AgentKind]: AgentKindInfo<K> } = {
  start: {
    label: "Start",
    group: "start",
    icon: PlayIcon,
    summary: "Where every run starts: what the person or system sends in.",
    keywords: "start input begin entry",
    terminal: true,
    idPrefix: "start",
    defaults: () => ({ input_schema: {} }),
  },
  llm: {
    label: "LLM agent",
    group: "agents",
    icon: Robot01Icon,
    summary:
      "A model works on what comes in with your instruction, and can hand off to its sub-agents.",
    keywords: "llm ai model agent prompt instruction gemini delegate transfer",
    orb: 0,
    idPrefix: "agent",
    defaults: () => ({
      ...llmSettings(),
      mode: "single_turn",
    }),
  },
  sequential: {
    label: "Sequential agent",
    group: "agents",
    icon: LeftToRightListNumberIcon,
    summary:
      "Runs its sub-agents one after another, each seeing what the last did.",
    keywords: "sequence pipeline chain steps order team",
    idPrefix: "sequential",
    defaults: () => ({ description: "", sub_agents: [] }),
  },
  parallel: {
    label: "Parallel agent",
    group: "agents",
    icon: DistributionIcon,
    summary: "Runs its sub-agents at the same time, each on its own.",
    keywords: "parallel concurrent fan out together team",
    idPrefix: "parallel",
    defaults: () => ({ description: "", sub_agents: [] }),
  },
  loop_agent: {
    label: "Loop agent",
    group: "agents",
    icon: RepeatIcon,
    summary:
      "Runs its sub-agents in order, again and again, up to a number of passes.",
    keywords: "loop repeat iterate refine critique team agent",
    idPrefix: "loop_agent",
    defaults: () => ({ description: "", sub_agents: [], max_iterations: 3 }),
  },
  saved: {
    label: "Saved workflow",
    group: "agents",
    icon: AGENTS_ICON,
    summary: "Runs another of the organization's workflows here, whole.",
    keywords: "saved nested reuse subgraph workflow compose other agent",
    idPrefix: "saved",
    defaults: () => ({ agent: "" }),
  },
  approval: forgeKind("approval"),
  human_input: {
    label: "Human input",
    group: "people",
    icon: UserQuestion01Icon,
    summary: "Pauses for a person to answer; their answer is what it hands on.",
    keywords: "human person input ask question approve pause review",
    person: true,
    idPrefix: "human_input",
    defaults: () => ({ message: "", response_schema: {} }),
  },
  http: forgeKind("http"),
  transform: forgeKind("transform"),
  delay: forgeKind("delay"),
  if: forgeKind("if"),
  switch: forgeKind("switch"),
  match: forgeKind("match"),
  loop: forgeKind("loop"),
  merge: forgeKind("merge"),
  end: forgeKind("end"),
}

export const AGENT_KIND_LIST = Object.keys(AGENT_KINDS) as AgentKind[]

export const isAgentKind = (value: unknown): value is AgentKind =>
  typeof value === "string" && Object.hasOwn(AGENT_KINDS, value)

/** The kinds a sub-agent can be, in the order its menu lists them. */
export const SUB_AGENT_KINDS: SubAgentKind[] = [
  "llm",
  "sequential",
  "parallel",
  "loop_agent",
]

export const isSubAgentKind = (value: unknown): value is SubAgentKind =>
  typeof value === "string" && (SUB_AGENT_KINDS as string[]).includes(value)

/** A sub-agent's settings: its kind's, without what only a node has. */
export function subAgentDefaults<K extends SubAgentKind>(
  kind: K
): SubAgentConfigs[K] {
  if (kind === "llm") return llmSettings() as SubAgentConfigs[K]
  const defaults = AGENT_KINDS[kind].defaults() as SubAgentConfigs[K]
  return defaults
}

/* -------------------------------------------------------------------------- */
/* Outputs                                                                    */
/* -------------------------------------------------------------------------- */

const NEXT: StepOutput[] = [{ id: "next", label: "Next" }]

/**
 * A node's ways out, in the order the canvas lists them: a Forge step's are
 * a workflow step's (Success / Error, True / False, a loop's Each item and
 * Done, none for an End); an ADK agent's, `next`.
 */
export function outputsOf(step: AgentStep): StepOutput[] {
  return isForgeKind(step.kind) ? stepOutputsOf(step as StepData) : NEXT
}

/** The Forge steps a run can wait at: a person deciding, or time passing. */
export const PAUSING_KINDS = new Set<AgentKind>([
  "approval",
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
  const prefix = AGENT_KINDS[kind].idPrefix
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
  const info = AGENT_KINDS[kind]
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
  const base = kind === "llm" ? "Sub-agent" : AGENT_KINDS[kind].label
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
