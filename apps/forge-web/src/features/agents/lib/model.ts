import { AiBrain01Icon } from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import { adkName, type ModelChoice } from "@/features/adk-workflows/lib/model"
import type { StepOutput } from "@/features/builder/lib/types"
import type {
  HeaderRow,
  HttpMethod,
  ThinkingLevel,
} from "@/features/steps/lib/model"
import {
  chatKindInfo,
  chatWaysOut,
  isAgentLike,
  isHandedTo,
  isServedChatKind,
} from "./catalog"

/*
 * A chat agent, as the Agents page builds it: one Google ADK `LlmAgent`
 * people talk to, and what's attached to it on the canvas. It has two
 * ways out: its tools (what it can call: memory, knowledge bases, HTTP
 * endpoints, an OpenAPI spec, an MCP server, a workflow, another agent, code
 * execution), and
 * the agents it hands off to (sub-agents it transfers the conversation
 * to, or gives a task). A sub-agent has the same two ways out, so a
 * specialist can have tools of its own. What each kind is called, its ways
 * out, what can be handed off to and what a new one starts with are the
 * admin API's node catalog's (`catalog.ts`).
 */

export const CHAT_AGENTS_ICON: IconProp = AiBrain01Icon

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
  /**
   * It may stop mid-turn to ask the person something (ADK's
   * adk_request_input), and the chat shows a form for the answer.
   */
  ask_person: boolean
}

/** How a sub-agent is handed to, when it's on the Hands off to way. */
export type HandOff = "chat" | "task" | "single_turn"

export type ChatConfigs = {
  agent: ChatAgentSettings & {
    /**
     * A JSON Schema of the state the chat sends in stateDelta besides the
     * model picker's (agents/lib/input.ts): what its instructions read as
     * `state.<field>`. `{}` declares none.
     */
    state_schema: Record<string, unknown>
  }
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
  /**
   * One of the organization's workflows, by ID, called as a tool, at a
   * version: a published one, its draft, or null for its latest published.
   */
  adk_workflow: { workflow: string; version: number | "draft" | null }
  memory: {
    /** on_demand: it looks things up when it decides to; every_turn: what's relevant comes with each message. */
    mode: "on_demand" | "every_turn"
  }
  knowledge_base: {
    /**
     * The organization's knowledge bases it searches, by ID (the Knowledge
     * bases page): their documents, parsed, chunked and embedded.
     */
    knowledge_bases: string[]
    /** What the model is told it finds there; empty to describe the knowledge bases. */
    description: string
    /** The most passages one search answers (1–20). */
    max_results: number
  }
  http_tool: {
    /** What it does: the model reads this to decide when to call it. */
    description: string
    method: HttpMethod
    /**
     * Where it calls, a template: `{name}` puts in the model's argument of
     * that name, `{{ expression }}` one of the run's values.
     */
    url: string
    /**
     * Query parameters, added to the URL's own: each name and value a
     * template like its URL, filled in and then percent-encoded whole. A
     * value that's one placeholder coming to nothing leaves it out.
     */
    query: HeaderRow[]
    headers: HeaderRow[]
    /**
     * Its JSON body (POST, PUT, PATCH), a template like its URL: a
     * placeholder that's a whole value goes in as it is. Empty for none.
     */
    body: string
    /**
     * A JSON Schema of the arguments: their types and descriptions. The
     * model is offered those the URL and body name (lib/http-template.ts).
     */
    parameters: Record<string, unknown>
    /**
     * One of the organization's credentials, by ID (the Credentials page):
     * how it signs its requests, sent only to the URLs it's for. Empty for none.
     */
    credential: string
    /** A person confirms each call before it's made. */
    confirm: boolean
  }
  openapi: {
    /** Where the spec comes from: fetched from `url`, or pasted into `spec`. */
    source: "url" | "inline"
    url: string
    /** An OpenAPI 3 spec, JSON or YAML. */
    spec: string
    /**
     * The operations it may call, comma-separated, by their tool names (or
     * operation IDs); empty for all.
     */
    operations: string
    /** One of the organization's credentials, by ID: how its calls sign in. Empty for none. */
    credential: string
    confirm: boolean
  }
  mcp: {
    /**
     * One of the organization's MCP servers, by ID (the MCP servers page):
     * its URL, headers and sign-in come from there. Empty for a server set
     * up here, by `transport`, `url` and `headers`.
     */
    server: string
    transport: "streamable_http" | "sse"
    url: string
    headers: HeaderRow[]
    /** The server's tools it may call, comma-separated; empty for all. */
    tools: string
    confirm: boolean
  }
  /**
   * Python the model writes, run where the runtime runs code (for now ADK's
   * UnsafeLocalCodeExecutor, when the deployment turns it on): it answers
   * with what the code printed and its errors.
   */
  code_execution: {
    /** A person confirms each run, seeing the code, before it runs. */
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

/** Whether a kind is one a chat agent can have now: served, and set up here. */
export const isChatKind = isServedChatKind

export { isAgentLike }

/** A node's ways out, as its kind's are served: an agent's two, none for the rest. */
export function outputsOf(step: ChatStep): StepOutput[] {
  return chatWaysOut(step.kind)
}

/** Whether an agent's way out takes a kind: only agents are handed off to. */
export function accepts(step: ChatStep, output: string, kind: ChatKind) {
  if (!isAgentLike(step.kind) || kind === "agent") return false
  return output === HANDS_OFF ? isHandedTo(kind) : output === TOOLS
}

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
    const id = `${chatKindInfo(kind).idPrefix}_${Array.from(random, (b) => ID_ALPHABET[b % ID_ALPHABET.length]).join("")}`
    if (!used.has(id)) return id
  }
}

/** A new node of a kind, named apart from `steps`. */
export function newNode<K extends ChatKind>(
  kind: K,
  steps: ChatStep[] = [],
  name?: string
): ChatStep<K> {
  const info = chatKindInfo(kind)
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
