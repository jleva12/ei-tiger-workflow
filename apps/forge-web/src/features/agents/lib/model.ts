import { AiBrain01Icon } from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import { adkName, type ModelChoice } from "@/features/adk-workflows/lib/model"
import type { StepOutput } from "@/features/builder/lib/types"
import type {
  HeaderRow,
  HttpMethod,
  ThinkingLevel,
} from "@/features/steps/lib/model"
import type { AccessRule } from "./access"
import {
  chatKindInfo,
  chatWaysOut,
  isAgentLike,
  isHandedTo,
  isServedChatKind,
  isTool,
} from "./catalog"

/*
 * A chat agent, as the Agents page builds it: one Google ADK `LlmAgent`
 * people talk to, and what's attached to it on the canvas. It has two
 * ways out: its tools (what it can call: memory, knowledge bases, HTTP
 * endpoints, an OpenAPI spec, an MCP server, a workflow, another agent, code
 * execution), and
 * the agents it hands off to (sub-agents it transfers the conversation
 * to, or gives a task, and intake agents, which collect a record you design
 * and hand it back). A sub-agent has the same two ways out, so a
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

/** What an intake field's value is (forge_agent_runtime.intake). */
export type IntakeFieldType =
  | "text"
  | "number"
  | "integer"
  | "boolean"
  | "date"
  | "email"
  | "choice"
  | "choices"

/** One field of an intake agent's record. */
export type IntakeField = {
  /** The builder's, for telling fields apart while their keys change. */
  id: string
  /** What the record and the model call it: lower-case letters, digits and _. */
  key: string
  /** What people call it; the key when empty. */
  label: string
  type: IntakeFieldType
  /** A choice's options. */
  options: string[]
  /** What it is, and how to ask for it. */
  description: string
  /** Why it's asked: what the agent says when the person asks. */
  why: string
  /** The intake isn't done without it (when it applies). */
  required: boolean
  /** A JSONata condition on the record: it applies only when this holds. Empty: always. */
  when: string
  /** The model may work it out from what the person said; the person confirms it. */
  infer: boolean
  /** The person confirms its value before the intake finishes. */
  confirm: boolean
  /** The agent handing off may fill it in, from what the person already said. */
  prefill: boolean
  /** Its section's ID; empty for none. */
  section: string
}

/** Fields asked together, unlocked on a condition of their own. */
export type IntakeSection = {
  id: string
  title: string
  /** A JSONata condition on the record: its fields apply only when this holds. Empty: always. */
  when: string
}

/**
 * Who may use an agent: the rule on its node, checked for whoever the call
 * is for as it runs (`access.ts`); null lets in anyone who may call the agent.
 */
export type Guarded = { access: AccessRule | null }

export type ChatConfigs = {
  agent: ChatAgentSettings &
    Guarded & {
      /**
       * A JSON Schema of the state the chat sends in stateDelta besides the
       * model picker's (agents/lib/input.ts): what its instructions read as
       * `state.<field>`. `{}` declares none.
       */
      state_schema: Record<string, unknown>
    }
  sub_agent: ChatAgentSettings &
    Guarded & {
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
  /**
   * An agent that walks the person through an intake: it's handed off to,
   * collects the record designed here in whatever order the person gives
   * it, and hands it back. Used as a tool instead, the agent using it asks
   * the questions and files the person's requests to it
   * (forge_agent_runtime.requests).
   */
  intake_agent: ChatAgentSettings &
    Guarded & {
      fields: IntakeField[]
      sections: IntakeSection[]
      /** It reads back a summary and waits for the person's yes before it finishes. */
      confirm_summary: boolean
      /** The person may stop it: it hands back as cancelled. */
      allow_cancel: boolean
      /** It may hand back as escalated: for a person, or what it can't record. */
      allow_escalate: boolean
    }
  /** Another of the organization's agents, by ID, used whole. */
  saved_agent: Guarded & { agent: string }
  /**
   * One of the organization's workflows, by ID, called as a tool, at a
   * version: a published one, its draft, or null for its latest published.
   */
  adk_workflow: {
    workflow: string
    version: number | "draft" | null
    /**
     * Seconds the agent waits for the run before it's told it's still
     * running (with its ID); at most the deployment's cap.
     */
    wait_seconds: number
  }
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
   * Python the model writes, run where the deployment's sandbox mode says
   * (FORGE_AGENT_SANDBOX_MODE: a new OpenShell sandbox for each call, or ADK's
   * UnsafeLocalCodeExecutor locally): it answers with what the code printed
   * and its errors.
   */
  code_execution: {
    /** A person confirms each run, seeing the code, before it runs. */
    confirm: boolean
  }
  /**
   * Skills from the organization's vault (the Skills page): the model is
   * told each one's name and description, and loads its instructions and
   * files when a task calls for it (ADK's SkillToolset). An agent's Skills
   * nodes are one set of skills.
   */
  skills: {
    /** The organization's skills, by ID. */
    skills: string[]
    /**
     * The model may run the skills' scripts, where the runtime runs code;
     * otherwise it reads a script and follows it itself.
     */
    run_scripts: boolean
  }
  /**
   * Answers the chat draws (Generative UI): the agent gets the `present`
   * tool, whose call is a composition of the components picked here
   * (`components/forge/generative-ui/catalog.ts`), drawn beside its text; what the person
   * clicks or fills in comes back as their next message.
   */
  rich_answers: {
    /** The components it may use; only these go into its model calls. */
    components: string[]
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
/** An intake agent's way out to the workflows it starts when it completes. */
export const ON_COMPLETE = "on_complete"

/** Whether a kind is one a chat agent can have now: served, and set up here. */
export const isChatKind = isServedChatKind

export { isAgentLike }

/** A node's ways out, as its kind's are served: an agent's two, none for the rest. */
export function outputsOf(step: ChatStep): StepOutput[] {
  return chatWaysOut(step.kind)
}

/**
 * Whether an agent's way out takes a kind: a way it has (an intake agent
 * has tools, but hands off to no one), where only agents are handed off to,
 * an intake agent doesn't use another, and only workflows start when an
 * intake completes.
 */
export function accepts(step: ChatStep, output: string, kind: ChatKind) {
  if (kind === "agent" || !chatWaysOut(step.kind).some((o) => o.id === output))
    return false
  if (output === ON_COMPLETE) return kind === "adk_workflow"
  if (step.kind === "intake_agent" && kind === "intake_agent") return false
  return output === HANDS_OFF
    ? isHandedTo(kind)
    : output === TOOLS && isTool(kind)
}

/** Whether a kind is an agent of its own: the agents, sub-agents, intake agents and saved agents. */
export function isAgent(kind: ChatKind) {
  return isAgentLike(kind) || isHandedTo(kind)
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
