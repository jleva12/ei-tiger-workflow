import {
  AiNetworkIcon,
  ApiIcon,
  Brain01Icon,
  ChatBotIcon,
  Globe02Icon,
  McpServerIcon,
  Robot01Icon,
} from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import { adkName, type ModelChoice } from "@/lib/agents/model"
import type { KindGroup, KindInfo, StepOutput } from "@/lib/builder/types"
import type {
  HeaderRow,
  HttpMethod,
  ThinkingLevel,
} from "@/lib/workflows/model"

/*
 * A chat agent, as the Agents page builds it: one Google ADK `LlmAgent`
 * people talk to, and what's attached to it on the canvas. It has two
 * ways out: its tools (what it can call: memory, HTTP endpoints, an
 * OpenAPI spec, an MCP server, an ADK workflow, another agent), and
 * the agents it hands off to (sub-agents it transfers the conversation
 * to, or gives a task). A sub-agent has the same two ways out, so a
 * specialist can have tools of its own.
 */

export const CHAT_AGENTS_ICON: IconProp = ChatBotIcon

/** What an LLM agent is set up with: the chat agent's, and a sub-agent's. */
export type ChatAgentSettings = {
  /** What it's for: what agents read when handing off to it, or calling it. */
  description: string
  /** What it's told to do: its system instruction. */
  instruction: string
  /** One of the models Forge offers; both empty for the default. */
  model: ModelChoice
  /** How long the model thinks: the nearest level it offers; empty for its own. */
  thinking_level: ThinkingLevel | ""
  /** The most tokens it answers with; null for the model's limit. */
  max_output_tokens: number | null
}

/** How a sub-agent is handed to, when it's on the Hands off to way. */
export type HandOff = "chat" | "task" | "single_turn"

export type ChatConfigs = {
  agent: ChatAgentSettings
  sub_agent: ChatAgentSettings & {
    /**
     * chat: it takes over the conversation; task: it does one task, asking
     * the person if it must, and hands back; single_turn: it answers once.
     */
    mode: HandOff
    /** It doesn't hand the conversation back to the agent that handed it over. */
    disallow_transfer_to_parent: boolean
    /** It doesn't hand the conversation to the agent's other sub-agents. */
    disallow_transfer_to_peers: boolean
    /** `none`: it sees only what it's given, not the conversation so far. */
    include_contents: "default" | "none"
  }
  /** Another of the organization's agents, by ID, used whole. */
  saved_agent: { agent: string }
  /** One of the organization's ADK workflows, by ID, called as a tool. */
  adk_workflow: { workflow: string }
  memory: {
    /** on_demand: it looks things up when it decides to; every_turn: what's relevant comes with each message. */
    mode: "on_demand" | "every_turn"
  }
  http_tool: {
    /** What it does: the model reads this to decide when to call it. */
    description: string
    method: HttpMethod
    /** Where it calls; `{name}` puts in an argument of that name. */
    url: string
    headers: HeaderRow[]
    /** A JSON Schema of the arguments the model fills in. */
    parameters: Record<string, unknown>
    /** A person confirms each call before it's made. */
    confirm: boolean
  }
  openapi: {
    /** Where the spec comes from: fetched from `url`, or pasted into `spec`. */
    source: "url" | "inline"
    url: string
    /** An OpenAPI 3 spec, JSON or YAML. */
    spec: string
    /** The operation IDs it may call, comma-separated; empty for all. */
    operations: string
    confirm: boolean
  }
  mcp: {
    transport: "streamable_http" | "sse"
    url: string
    headers: HeaderRow[]
    /** The server's tools it may call, comma-separated; empty for all. */
    tools: string
    confirm: boolean
  }
}

export type ChatKind = keyof ChatConfigs

/** A node of a chat agent's canvas: the canvas node's data. */
export type ChatStep<K extends ChatKind = ChatKind> = {
  [Kind in K]: { kind: Kind; name: string; config: ChatConfigs[Kind] }
}[K]

/* -------------------------------------------------------------------------- */
/* Ways out                                                                   */
/* -------------------------------------------------------------------------- */

/** An agent's ways out: what it can call, and who it hands off to. */
export const TOOLS = "tools"
export const HANDS_OFF = "agents"

// Named ways of their own: each a row on the agent's card.
const AGENT_OUTPUTS: StepOutput[] = [
  { id: TOOLS, label: "Tools", branch: true },
  { id: HANDS_OFF, label: "Hands off to", branch: true },
]

/** The agents: the chat agent and its sub-agents. */
export const AGENT_LIKE = new Set<ChatKind>(["agent", "sub_agent"])

/** What can be handed off to: an agent. */
const HANDED_TO = new Set<ChatKind>(["sub_agent", "saved_agent"])

export function outputsOf(step: ChatStep): StepOutput[] {
  return AGENT_LIKE.has(step.kind) ? AGENT_OUTPUTS : []
}

/** Whether an agent's way out takes a kind: only agents are handed off to. */
export function accepts(step: ChatStep, output: string, kind: ChatKind) {
  if (!AGENT_LIKE.has(step.kind) || kind === "agent") return false
  return output === HANDS_OFF ? HANDED_TO.has(kind) : output === TOOLS
}

/* -------------------------------------------------------------------------- */
/* Catalog                                                                    */
/* -------------------------------------------------------------------------- */

export const CHAT_GROUPS: KindGroup[] = [
  { id: "agent", label: "The agent" },
  { id: "agents", label: "Agents" },
  { id: "tools", label: "Tools" },
]

type ChatKindInfo<K extends ChatKind> = KindInfo & {
  defaults: () => ChatConfigs[K]
}

const agentSettings = (): ChatAgentSettings => ({
  description: "",
  instruction: "",
  model: { provider: "", name: "" },
  thinking_level: "",
  max_output_tokens: null,
})

export const CHAT_KINDS: { [K in ChatKind]: ChatKindInfo<K> } = {
  agent: {
    label: "Chat agent",
    group: "agent",
    icon: ChatBotIcon,
    summary:
      "The agent people chat with: what it's told, its model, what it can call and who it hands off to.",
    keywords: "chat agent llm root assistant bot",
    orb: 0,
    idPrefix: "agent",
    defaults: agentSettings,
  },
  sub_agent: {
    label: "Sub-agent",
    group: "agents",
    icon: Robot01Icon,
    summary:
      "Another LLM agent: it takes over when handed off to, does a task and hands back, or is a tool the agent calls.",
    keywords: "sub agent specialist delegate transfer hand off llm",
    orb: 2,
    idPrefix: "sub",
    defaults: () => ({
      ...agentSettings(),
      mode: "chat",
      disallow_transfer_to_parent: false,
      disallow_transfer_to_peers: false,
      include_contents: "default",
    }),
  },
  saved_agent: {
    label: "Saved agent",
    group: "agents",
    icon: ChatBotIcon,
    summary:
      "Another of the organization's agents, whole: handed off to, or called as a tool.",
    keywords: "saved agent reuse other existing",
    idPrefix: "saved",
    defaults: () => ({ agent: "" }),
  },
  memory: {
    label: "Memory",
    group: "tools",
    icon: Brain01Icon,
    summary:
      "Recalls what was said in earlier conversations with the same person.",
    keywords: "memory remember recall history past conversations",
    idPrefix: "memory",
    defaults: () => ({ mode: "on_demand" }),
  },
  http_tool: {
    label: "HTTP tool",
    group: "tools",
    icon: Globe02Icon,
    summary: "Calls an HTTP endpoint with arguments the model fills in.",
    keywords: "http api rest request endpoint function tool webhook",
    idPrefix: "http",
    defaults: () => ({
      description: "",
      method: "GET",
      url: "",
      headers: [],
      parameters: {},
      confirm: false,
    }),
  },
  openapi: {
    label: "OpenAPI",
    group: "tools",
    icon: ApiIcon,
    summary: "The operations of an OpenAPI spec, each a tool.",
    keywords: "openapi swagger spec api rest operations",
    idPrefix: "openapi",
    defaults: () => ({
      source: "url",
      url: "",
      spec: "",
      operations: "",
      confirm: false,
    }),
  },
  mcp: {
    label: "MCP server",
    group: "tools",
    icon: McpServerIcon,
    summary: "The tools of a remote MCP server, over streamable HTTP or SSE.",
    keywords: "mcp model context protocol server tools remote",
    idPrefix: "mcp",
    defaults: () => ({
      transport: "streamable_http",
      url: "",
      headers: [],
      tools: "",
      confirm: false,
    }),
  },
  adk_workflow: {
    label: "ADK workflow",
    group: "tools",
    icon: AiNetworkIcon,
    summary:
      "One of the organization's ADK workflows, as a tool: the agent runs it with its input and gets its result.",
    keywords: "adk workflow graph tool run process",
    idPrefix: "workflow",
    defaults: () => ({ workflow: "" }),
  },
}

export const CHAT_KIND_LIST = Object.keys(CHAT_KINDS) as ChatKind[]

export const isChatKind = (value: unknown): value is ChatKind =>
  typeof value === "string" && Object.hasOwn(CHAT_KINDS, value)

/* -------------------------------------------------------------------------- */
/* Nodes                                                                      */
/* -------------------------------------------------------------------------- */

/** A node's ID: made by the builder, unique in the agent and never reused. */
export const NODE_ID_PATTERN = /^[a-z][a-z0-9_]{0,47}$/

/** A name taken, as ADK would call it (the same for two names that differ by case). */
function uniqueName(base: string, taken: Iterable<string>) {
  const used = new Set([...taken].map((n) => adkName(n) || n))
  const key = (name: string) => adkName(name) || name
  if (!used.has(key(base))) return base
  for (let n = 2; ; n += 1) {
    const name = `${base} ${n}`
    if (!used.has(key(name))) return name
  }
}

const ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

/** A new node's ID: its kind's prefix and a random suffix (`http_k3f9x`). */
export function nextNodeId(kind: ChatKind, taken: Iterable<string>) {
  const used = new Set(taken)
  if (kind === "agent" && !used.has("agent")) return "agent"
  for (;;) {
    const random = crypto.getRandomValues(new Uint8Array(5))
    const id = `${CHAT_KINDS[kind].idPrefix}_${Array.from(random, (b) => ID_ALPHABET[b % ID_ALPHABET.length]).join("")}`
    if (!used.has(id)) return id
  }
}

/** A new node of a kind, named apart from `steps`. */
export function newNode<K extends ChatKind>(
  kind: K,
  steps: ChatStep[] = [],
  name?: string
): ChatStep<K> {
  const info = CHAT_KINDS[kind]
  return {
    kind,
    name: uniqueName(
      name ?? info.label,
      steps.map((s) => s.name)
    ),
    config: info.defaults(),
  } as ChatStep<K>
}

/** A copy of a node, named apart from `steps`. */
export function copyNode(step: ChatStep, steps: ChatStep[]): ChatStep {
  return {
    ...structuredClone(step),
    name: uniqueName(
      `${step.name} copy`,
      steps.map((s) => s.name)
    ),
  }
}
