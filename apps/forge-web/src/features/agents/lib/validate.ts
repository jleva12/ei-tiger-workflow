import { adkName, RESERVED_NAMES } from "@/features/adk-workflows/lib/model"
import type { BuilderIssue, IssueLevel } from "@/features/builder/lib/types"
import { checkField } from "@/features/steps/lib/expressions"
import { stronglyConnected } from "@/features/steps/lib/validate"
import type { ChatAgentGraph } from "./document"
import { chatScope, declaredState, stateNameProblem } from "./input"
import {
  AGENT_LIKE,
  HANDS_OFF,
  TOOLS,
  type ChatAgentSettings,
  type ChatStep,
} from "./model"

/*
 * What stands between a chat agent and ADK building it: errors (ADK would
 * refuse it) and warnings (it builds, but probably not as meant). ADK's
 * rules: an agent has one parent; names are ADK names, once each; a
 * sub-agent given a task hands off to no one.
 */

export type ChatAgentValidationContext = {
  /** This agent's ID: a saved-agent node can't pick it. */
  selfId?: string
  /** The organization's agents: their names, and the agents each uses. */
  agents?: Map<string, { name: string; uses: string[] }>
  /** The organization's workflows, by ID, for the workflow tools. */
  workflows?: Map<string, { name: string }>
  /**
   * The organization's MCP servers, by ID, for the MCP tools: whether
   * they're signed in to, and their tools' names when they've been checked.
   */
  mcpServers?: Map<
    string,
    { name: string; connected: boolean; tools: string[] | null }
  >
  /**
   * The organization's knowledge bases, by ID, for the knowledge base tools:
   * their names, and how many of their documents are searchable.
   */
  knowledgeBases?: Map<string, { name: string; ready: number }>
}

type Found = [code: string, level: IssueLevel, message: string, field?: string]

const URLISH = /^https?:\/\/\S+$/

/** Whether `from` uses `to`, itself or through the agents it uses. */
function reaches(
  from: string,
  to: string,
  agents: Map<string, { uses: string[] }>
) {
  const seen = new Set<string>()
  const queue = [from]
  while (queue.length) {
    const id = queue.shift()!
    if (id === to) return true
    if (seen.has(id)) continue
    seen.add(id)
    queue.push(...(agents.get(id)?.uses ?? []))
  }
  return false
}

/** What's missing or wrong in an LLM agent's own settings. */
function agentIssues(config: ChatAgentSettings): Found[] {
  const out: Found[] = []
  if (!config.instruction.trim()) {
    out.push([
      "instruction",
      "error",
      "Tell the agent what to do.",
      "instruction",
    ])
  }
  if (config.max_output_tokens !== null && config.max_output_tokens < 1) {
    out.push([
      "max-tokens",
      "error",
      "It answers with one token or more.",
      "max_output_tokens",
    ])
  }
  return out
}

/** What's missing or wrong in one node's own settings. */
function settingsIssues(
  step: ChatStep,
  context: ChatAgentValidationContext
): Found[] {
  const out: Found[] = []
  switch (step.kind) {
    case "agent":
    case "sub_agent":
      out.push(...agentIssues(step.config))
      break
    case "saved_agent": {
      const target = step.config.agent
      if (!target)
        out.push(["agent", "error", "Pick the agent it uses.", "agent"])
      else if (target === context.selfId) {
        out.push(["agent", "error", "An agent can't use itself.", "agent"])
      } else if (context.agents && !context.agents.has(target)) {
        out.push([
          "agent",
          "error",
          "That agent was deleted; pick another.",
          "agent",
        ])
      } else if (
        context.agents &&
        context.selfId &&
        reaches(target, context.selfId, context.agents)
      ) {
        const name = context.agents.get(target)?.name ?? "That agent"
        out.push([
          "agent",
          "error",
          `${name} uses this agent, so they'd call each other forever.`,
          "agent",
        ])
      }
      break
    }
    case "adk_workflow": {
      const target = step.config.workflow
      if (!target)
        out.push([
          "workflow",
          "error",
          "Pick the workflow it runs.",
          "workflow",
        ])
      else if (context.workflows && !context.workflows.has(target)) {
        out.push([
          "workflow",
          "error",
          "That workflow was deleted; pick another.",
          "workflow",
        ])
      }
      break
    }
    case "http_tool": {
      const c = step.config
      if (!adkName(step.name)) {
        out.push([
          "name",
          "error",
          "Its name is the tool's name: give it one that starts with a letter.",
          "name",
        ])
      }
      if (!c.url.trim()) out.push(["url", "error", "Give it a URL.", "url"])
      else if (!URLISH.test(c.url.trim())) {
        out.push([
          "url",
          "error",
          "A URL starts with https:// (or http://).",
          "url",
        ])
      }
      if (!c.description.trim()) {
        out.push([
          "description",
          "warning",
          "Say what it does: the model decides when to call it by this.",
          "description",
        ])
      }
      if (!c.headers.every((h) => h.name.trim())) {
        out.push(["headers", "error", "Every header needs a name.", "headers"])
      }
      break
    }
    case "openapi": {
      const c = step.config
      if (c.source === "url") {
        if (!c.url.trim())
          out.push(["url", "error", "Give the spec's URL.", "url"])
        else if (!URLISH.test(c.url.trim())) {
          out.push([
            "url",
            "error",
            "A URL starts with https:// (or http://).",
            "url",
          ])
        }
      } else if (!c.spec.trim()) {
        out.push([
          "spec",
          "error",
          "Paste the spec: OpenAPI 3, JSON or YAML.",
          "spec",
        ])
      } else if (c.spec.trim().startsWith("{")) {
        try {
          JSON.parse(c.spec)
        } catch {
          out.push([
            "spec",
            "error",
            "The spec starts like JSON but isn't valid JSON.",
            "spec",
          ])
        }
      }
      break
    }
    case "knowledge_base": {
      const c = step.config
      if (!c.knowledge_bases.length) {
        out.push([
          "knowledge_bases",
          "error",
          "Pick the knowledge bases it searches.",
          "knowledge_bases",
        ])
      }
      for (const id of c.knowledge_bases) {
        // Unknown until they load: a pick isn't called deleted meanwhile.
        if (!context.knowledgeBases) break
        const found = context.knowledgeBases.get(id)
        if (!found) {
          out.push([
            "knowledge_bases",
            "error",
            "The organization has no such knowledge base any more: pick another.",
            "knowledge_bases",
          ])
        } else if (found.ready === 0) {
          out.push([
            "knowledge_bases",
            "warning",
            `${found.name} has no searchable documents yet: upload some on the Knowledge bases page.`,
            "knowledge_bases",
          ])
        }
      }
      if (
        !Number.isInteger(c.max_results) ||
        c.max_results < 1 ||
        c.max_results > 20
      ) {
        out.push([
          "max_results",
          "error",
          "Answer between 1 and 20 passages a search.",
          "max_results",
        ])
      }
      break
    }
    case "mcp": {
      const c = step.config
      if (c.server) {
        out.push(...mcpServerIssues(c.server, c.tools, context))
        break
      }
      if (!c.url.trim())
        out.push(["url", "error", "Give the server's URL.", "url"])
      else if (!URLISH.test(c.url.trim())) {
        out.push([
          "url",
          "error",
          "A URL starts with https:// (or http://).",
          "url",
        ])
      }
      if (!c.headers.every((h) => h.name.trim())) {
        out.push(["headers", "error", "Every header needs a name.", "headers"])
      }
      break
    }
    default:
      break
  }
  return out
}

/**
 * What's missing or wrong in one tool's settings, wherever it is: a tool
 * node on the canvas, or one of a workflow's LLM agent's tools. Each issue
 * names the setting it's about.
 */
export function toolIssues(
  tool: Pick<ChatStep, "kind" | "name" | "config">,
  context: ChatAgentValidationContext
): [code: string, level: IssueLevel, message: string, field?: string][] {
  return settingsIssues(tool as ChatStep, context)
}

/** An MCP tool's issues with the organization's server it picks. */
function mcpServerIssues(
  id: string,
  tools: string,
  context: ChatAgentValidationContext
): Found[] {
  // Unknown until they load: a pick isn't called deleted meanwhile.
  if (!context.mcpServers) return []
  const server = context.mcpServers.get(id)
  if (!server)
    return [
      [
        "server",
        "error",
        "The organization has no such MCP server any more: pick another.",
        "server",
      ],
    ]
  const out: Found[] = []
  if (!server.connected) {
    out.push([
      "server",
      "warning",
      `Nobody has signed in to ${server.name} yet: sign in on the MCP servers page.`,
      "server",
    ])
  }
  const offered = server.tools ? new Set(server.tools) : undefined
  const missing = tools
    .split(",")
    .map((name) => name.trim())
    .filter((name) => name && offered && !offered.has(name))
  if (missing.length) {
    out.push([
      "tools",
      "warning",
      `${server.name} didn't list ${missing.join(", ")} when last checked.`,
      "tools",
    ])
  }
  return out
}

/** Every issue, errors first, in the order of the nodes. */
export function validateChatAgent(
  graph: ChatAgentGraph,
  context: ChatAgentValidationContext = {}
): BuilderIssue[] {
  const issues: BuilderIssue[] = []
  const add = (
    level: IssueLevel,
    step: string | undefined,
    code: string,
    message: string,
    field?: string
  ) =>
    issues.push({
      id: `${step ?? "agent"}:${code}`,
      level,
      step,
      field,
      message,
    })

  const byId = new Map(graph.steps.map((s) => [s.id, s]))
  const outgoing = new Map<string, { output: string; target: string }[]>()
  const incoming = new Map<string, { output: string; source: string }[]>()
  for (const c of graph.connections) {
    if (!byId.has(c.source) || !byId.has(c.target)) continue
    outgoing.set(c.source, [...(outgoing.get(c.source) ?? []), c])
    incoming.set(c.target, [...(incoming.get(c.target) ?? []), c])
  }
  const name = (id: string) => byId.get(id)?.data.name.trim() || id

  // The chat agent: one.
  const roots = graph.steps.filter((s) => s.data.kind === "agent")
  if (!roots.length) {
    add(
      "error",
      undefined,
      "root",
      "Add the chat agent: it's what people talk to."
    )
  }
  for (const extra of roots.slice(1)) {
    add(
      "error",
      extra.id,
      "root",
      "There's one chat agent; make this one a sub-agent, or remove it."
    )
  }

  // What the chat agent reaches: the rest does nothing.
  const reached = new Set<string>()
  const queue = roots.slice(0, 1).map((r) => r.id)
  while (queue.length) {
    const id = queue.shift()!
    if (reached.has(id)) continue
    reached.add(id)
    for (const link of outgoing.get(id) ?? []) queue.push(link.target)
  }

  // Names: ADK calls every agent by its name, once each.
  const holders = new Map<string, string[]>()
  for (const step of graph.steps) {
    if (!AGENT_LIKE.has(step.data.kind)) continue
    const key = adkName(step.data.name)
    if (!key)
      add(
        "error",
        step.id,
        "name",
        "Give it a name that starts with a letter.",
        "name"
      )
    else if (RESERVED_NAMES.has(key)) {
      add(
        "error",
        step.id,
        "name",
        `ADK keeps the name “${key}” for the person.`,
        "name"
      )
    } else holders.set(key, [...(holders.get(key) ?? []), step.id])
  }
  // A tool's name, once each: the model calls it by it.
  const tools = new Map<string, string[]>()
  for (const step of graph.steps) {
    if (step.data.kind !== "http_tool") continue
    const key = adkName(step.data.name)
    if (key) tools.set(key, [...(tools.get(key) ?? []), step.id])
  }
  for (const [key, held] of [...holders, ...tools]) {
    if (held.length < 2) continue
    for (const id of held) {
      add(
        "error",
        id,
        `same-${key}`,
        `Two are called ${key}; every name must be different.`,
        "name"
      )
    }
  }

  // Calling or handing off in a circle never ends.
  const agentLinks = new Map(
    [...outgoing].map(([id, links]) => [
      id,
      links.filter((l) =>
        AGENT_LIKE.has(byId.get(l.target)?.data.kind ?? "agent")
      ),
    ])
  )
  for (const component of stronglyConnected(
    graph.steps.map((s) => s.id),
    agentLinks
  )) {
    const circular =
      component.length > 1 ||
      (agentLinks.get(component[0]) ?? []).some(
        (l) => l.target === component[0]
      )
    if (!circular) continue
    const names = component.map(name)
    for (const id of component) {
      add(
        "error",
        id,
        "cycle",
        `${names.join(" → ")} call or hand off to each other in a circle: it would never end.`
      )
    }
  }

  // Every agent's instructions read the state the chat agent declares.
  const entry = graph.steps.find((s) => s.data.kind === "agent")?.data
  const stateSchema = entry?.kind === "agent" ? entry.config.state_schema : {}
  const scope = chatScope(stateSchema)

  for (const step of graph.steps) {
    const { data } = step
    for (const [code, level, message, field] of settingsIssues(data, context)) {
      add(level, step.id, code, message, field)
    }
    if (data.kind === "agent") {
      for (const [field] of declaredState(data.config.state_schema).fields) {
        const problem = stateNameProblem(field)
        if (problem)
          add("error", step.id, `state:${field}`, problem, "state_schema")
      }
    }
    if (data.kind === "agent" || data.kind === "sub_agent") {
      // The field says these itself; ":field:" keeps it from saying them twice.
      checkField(data.config.instruction, "template", scope, { textual: true })
        .filter((d) => !d.local)
        .forEach((d, n) =>
          add(
            d.severity,
            step.id,
            `instruction:field:${n}`,
            d.message,
            "instruction"
          )
        )
    }
    const into = incoming.get(step.id) ?? []
    const out = outgoing.get(step.id) ?? []

    if (data.kind !== "agent" && roots.length && !reached.has(step.id)) {
      add(
        "warning",
        step.id,
        "unreached",
        into.length
          ? "The chat agent doesn't reach it, so it's never used."
          : "Nothing uses it: connect an agent's Tools or Hands off to way to it."
      )
    }

    if (data.kind === "sub_agent") {
      const parents = new Set(
        into.filter((l) => l.output === HANDS_OFF).map((l) => l.source)
      )
      if (parents.size > 1) {
        add(
          "error",
          step.id,
          "parents",
          `ADK gives an agent one parent: ${[...parents].map(name).join(" and ")} both hand off to it. Keep one, or use a copy.`
        )
      }
      const handedTo = into.some((l) => l.output === HANDS_OFF)
      const called = into.some((l) => l.output === TOOLS)
      if ((handedTo || called) && !data.config.description.trim()) {
        add(
          "warning",
          step.id,
          "description",
          handedTo
            ? "Say what it's for: the agent hands off to it by this."
            : "Say what it does: the agent calls it by this.",
          "description"
        )
      }
      if (
        handedTo &&
        data.config.mode !== "chat" &&
        out.some((l) => l.output === HANDS_OFF)
      ) {
        add(
          "error",
          step.id,
          "task-leaf",
          "A sub-agent that does a task, or answers once, hands off to no one: remove its hand-offs, or let it take over the conversation.",
          "mode"
        )
      }
    }

    if (AGENT_LIKE.has(data.kind)) {
      const attached = out
        .filter((l) => l.output === TOOLS)
        .map((l) => byId.get(l.target)?.data)
      if (attached.filter((t) => t?.kind === "memory").length > 1) {
        add(
          "warning",
          step.id,
          "memories",
          "It has Memory twice: one is enough."
        )
      }
    }
  }

  const order = new Map(graph.steps.map((s, index) => [s.id, index]))
  return issues.sort(
    (a, b) =>
      (a.level === b.level ? 0 : a.level === "error" ? -1 : 1) ||
      (order.get(a.step ?? "") ?? -1) - (order.get(b.step ?? "") ?? -1)
  )
}
