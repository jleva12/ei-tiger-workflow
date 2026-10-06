import {
  toolIssues,
  type ChatAgentValidationContext,
} from "@/features/agents/lib/validate"
import type { BuilderIssue, IssueLevel } from "@/features/builder/lib/types"
import { checkField } from "@/features/steps/lib/expressions"
import type { StepData } from "@/features/steps/lib/model"
import type { Scope } from "@/features/steps/lib/scope"
import {
  fieldOfCode as stepFieldOf,
  settingsIssues as stepIssues,
  stronglyConnected,
  switchIssues,
} from "@/features/steps/lib/validate"
import type { AgentGraph } from "./document"
import { agentExpressionSettings } from "./fields"
import {
  adkName,
  AGENT_KINDS,
  isForgeKind,
  outputsOf,
  RESERVED_NAMES,
  subAgentsOf,
  walkSubAgents,
  type AgentConfigs,
  type AgentStep,
  type LlmSettings,
  type LlmTool,
  type SubAgent,
} from "./model"
import { agentScopesOf } from "./scope"

/*
 * What stands between an agent and ADK building and running it: errors
 * (ADK would refuse it, or it can't run) and warnings (it runs, but
 * probably not as meant). The builder shows them on the nodes, in their
 * settings, in the agent's details and in the top bar. Forge's steps are
 * checked as a workflow checks them.
 */

export type AgentValidationContext = {
  /** This agent's ID: a saved-agent node can't run it. */
  selfId?: string
  /**
   * The organization's agents: their names, and the agents each runs as
   * saved-agent nodes, to find ones that would run themselves.
   */
  agents?: Map<string, { name: string; uses: string[] }>
  /**
   * The organization's agents from the Agents page, for LLM agents that are
   * one or call one: their names, versions, and the input their latest
   * declares (its JSON Schema).
   */
  chatAgents?: Map<string, ChatAgentLookup>
  /** The organization's MCP servers and knowledge bases, for the tools. */
  mcpServers?: ChatAgentValidationContext["mcpServers"]
  knowledgeBases?: ChatAgentValidationContext["knowledgeBases"]
}

/** An agent from the Agents page, as a workflow's checks see it. */
export type ChatAgentLookup = {
  name: string
  /** Its latest published version; null before it's published. */
  published: number | null
  hasDraft: boolean
  /** The JSON Schema of the input it takes (its state_schema). */
  input: Record<string, unknown>
}

// ADK's {key} (not a {{ }} template): what an instruction held before.
const SINGLE_BRACES = /(?<![{])(\{([A-Za-z_][A-Za-z0-9_]*)\??\})(?![}])/
// A field with more than this many problems reports only these.
const PER_FIELD = 3

type Found = [code: string, level: IssueLevel, message: string, field?: string]

// The setting each of a node's own issues is about, by its code.
const FIELDS: Record<string, string> = {
  name: "name",
  instruction: "instruction",
  "instruction-braces": "instruction",
  "max-tokens": "max_output_tokens",
  team: "sub_agents",
  passes: "max_iterations",
  agent: "agent",
  message: "message",
}

/**
 * The setting an issue of a node is about, by the issue's code: an
 * expression's, a sub-agent's (`<its ID>:<code>`: `agents.<id>.<setting>`),
 * a merge's mode, or one of the node's own (Forge's steps' as a
 * workflow's). None for the node as a whole.
 */
function fieldOf(code: string): string | undefined {
  if (code.startsWith("field:")) return stepFieldOf(code)
  if (code.startsWith("merge-exclusive-")) return "mode"
  if (code.startsWith("name-")) return `agents.${code.slice(5)}.name`
  const at = code.indexOf(":")
  if (at > 0) {
    const rest = code.slice(at + 1)
    return `agents.${code.slice(0, at)}.${FIELDS[rest] ?? rest}`
  }
  return FIELDS[code] ?? stepFieldOf(code)
}

/** What's missing or wrong in an LLM agent's own settings, as a node or a sub-agent. */
function llmIssues(config: LlmSettings): Found[] {
  const out: Found[] = []
  if (!config.instruction.trim())
    out.push(["instruction", "error", "Tell the agent what to do."])
  // ADK's own {key} isn't filled in: the run's data comes in by {{ }}.
  const single = SINGLE_BRACES.exec(config.instruction)
  if (single) {
    out.push([
      "instruction-braces",
      "warning",
      `Its instruction has ${single[1]}, which goes to the model as it is: write {{ }} to put in the run's data, e.g. {{ state.${single[2]} }}.`,
    ])
  }
  if (config.max_output_tokens !== null && config.max_output_tokens < 1) {
    out.push(["max-tokens", "error", "It answers with one token or more."])
  }
  return out
}

/**
 * What's wrong in an LLM agent's tools: each one's settings, as the Agents
 * builder checks a tool node's, and two the model would call by one name.
 * Each issue names its setting: `<prefix>tools.<tool ID>.<setting>`.
 */
function toolsIssues(
  tools: LlmTool[],
  context: AgentValidationContext,
  prefix = ""
): Found[] {
  const out: Found[] = []
  const tool = {
    agents: context.chatAgents
      ? new Map(
          [...context.chatAgents].map(([id, a]) => [
            id,
            { name: a.name, uses: [] },
          ])
        )
      : undefined,
    workflows: context.agents,
    selfId: context.selfId,
    mcpServers: context.mcpServers,
    knowledgeBases: context.knowledgeBases,
  } satisfies ChatAgentValidationContext
  const named = new Map<string, string[]>()
  for (const each of tools) {
    const at = `${prefix}tools.${each.id}`
    const label = each.name.trim() || "A tool"
    for (const [code, level, message, field] of toolIssues(each, tool)) {
      out.push([
        `tool-${each.id}-${code}`,
        level,
        `${label}: ${message}`,
        `${at}.${field ?? "name"}`,
      ])
    }
    if (each.kind === "saved_agent" && context.chatAgents) {
      const found = context.chatAgents.get(each.config.agent)
      if (each.config.agent && found && found.published === null) {
        out.push([
          `tool-${each.id}-unpublished`,
          "error",
          `${label}: ${found.name} isn't published yet: an agent is called at its latest published version.`,
          `${at}.agent`,
        ])
      }
    }
    if (each.kind === "http_tool" || each.kind === "knowledge_base") {
      const name = adkName(each.name)
      if (name) named.set(name, [...(named.get(name) ?? []), each.id])
    }
  }
  for (const [name, ids] of named) {
    if (ids.length < 2) continue
    for (const id of ids) {
      out.push([
        `tool-${id}-same-name`,
        "error",
        `Two tools are called ${name}: the model tells them apart by name.`,
        `${prefix}tools.${id}.name`,
      ])
    }
  }
  return out
}

/** What's wrong in a sub-agent's settings, and in its own sub-agents. */
function subAgentIssues(
  agents: SubAgent[],
  context: AgentValidationContext,
  path: string[] = []
): Found[] {
  const out: Found[] = []
  for (const agent of agents) {
    const where = [...path, agent.name.trim() || "A sub-agent"]
    const found: Found[] =
      agent.kind === "llm"
        ? llmIssues(agent.config)
        : agent.config.sub_agents.length
          ? []
          : [["team", "error", "Add a sub-agent for it to run."]]
    if (agent.kind === "loop_agent" && agent.config.max_iterations < 1) {
      found.push(["passes", "error", "It runs one pass or more."])
    }
    for (const [code, level, message] of found) {
      out.push([
        `${agent.id}:${code}`,
        level,
        `${where.join(" › ")}: ${message}`,
      ])
    }
    if (agent.kind === "llm") {
      for (const [code, level, message, field] of toolsIssues(
        agent.config.tools,
        context,
        `agents.${agent.id}.`
      )) {
        out.push([
          `${agent.id}-${code}`,
          level,
          `${where.join(" › ")}: ${message}`,
          field,
        ])
      }
    }
    out.push(...subAgentIssues(agent.config.sub_agents, context, where))
  }
  return out
}

/** What's missing or wrong in an LLM node that is an agent from the Agents page. */
function agentSourceIssues(
  config: AgentConfigs["llm"],
  context: AgentValidationContext
): Found[] {
  if (!config.agent)
    return [["agent", "error", "Pick the agent from the Agents page it uses."]]
  const found = context.chatAgents?.get(config.agent)
  // Unknown until they load: a pick isn't called deleted meanwhile.
  if (!context.chatAgents) return []
  if (!found)
    return [["agent", "error", "That agent was deleted; pick another."]]
  const out: Found[] = []
  if (config.version === "draft") {
    if (!found.hasDraft)
      out.push([
        "version",
        "error",
        `${found.name} has no draft: it's published as it is. Pick a version.`,
        "version",
      ])
  } else if (found.published === null) {
    out.push([
      "version",
      "error",
      `${found.name} isn't published yet: publish it, or use its draft.`,
      "version",
    ])
  } else if (config.version !== null && config.version > found.published) {
    out.push([
      "version",
      "error",
      `${found.name} has no version ${config.version} yet.`,
      "version",
    ])
  }
  const declared = found.input.properties
  const fields =
    declared && typeof declared === "object" ? Object.keys(declared) : []
  const required = Array.isArray(found.input.required)
    ? (found.input.required as string[])
    : []
  for (const field of required) {
    if (!config.inputs[field]?.trim())
      out.push([
        `input-${field}`,
        "error",
        `${found.name} needs its input ${field}: say what it is.`,
        `inputs.${field}`,
      ])
  }
  for (const field of Object.keys(config.inputs)) {
    if (!fields.includes(field) && config.inputs[field].trim())
      out.push([
        `input-${field}`,
        "warning",
        `${found.name} doesn't take an input ${field} any more.`,
        `inputs.${field}`,
      ])
  }
  return out
}

/** What's missing or wrong in one node's own settings. */
function settingsIssues(
  step: AgentStep,
  context: AgentValidationContext
): Found[] {
  if (isForgeKind(step.kind)) return stepIssues(step as StepData)
  const out: Found[] = []
  switch (step.kind) {
    case "llm":
      // An agent from the Agents page brings its own instruction and tools.
      if (step.config.source === "agent")
        return agentSourceIssues(step.config, context)
      out.push(...llmIssues(step.config))
      out.push(...toolsIssues(step.config.tools, context))
      break
    case "sequential":
    case "parallel":
    case "loop_agent":
      if (!step.config.sub_agents.length) {
        out.push(["team", "error", "Add the sub-agents it runs."])
      }
      if (step.kind === "loop_agent" && step.config.max_iterations < 1) {
        out.push(["passes", "error", "It runs one pass or more."])
      }
      break
    case "saved": {
      const target = step.config.agent
      if (!target) out.push(["agent", "error", "Pick the workflow it runs."])
      else if (target === context.selfId)
        out.push(["agent", "error", "A workflow can't run itself."])
      else if (context.agents && !context.agents.has(target)) {
        out.push(["agent", "error", "That workflow was deleted; pick another."])
      } else if (
        context.agents &&
        context.selfId &&
        reaches(target, context.selfId, context.agents)
      ) {
        const name = context.agents.get(target)?.name ?? "That agent"
        out.push([
          "agent",
          "error",
          `${name} runs this workflow, so they'd run each other forever.`,
        ])
      }
      break
    }
    case "human_input":
      if (!step.config.message.trim())
        out.push([
          "message",
          "warning",
          "Tell the person what they're answering.",
        ])
      break
    default:
      break
  }
  out.push(...subAgentIssues(subAgentsOf(step), context))
  return out
}

/**
 * The ways out a merge "all" waits on that can't all come in one run:
 * pairs of its ways in that each need a different way out of one
 * branching step (Approved and Rejected, True and False…). ADK's join
 * waits for every way in, so it would never go on. A step a way in
 * needs is one whose way out every path from the start to it takes.
 */
function exclusiveWays(
  mergeId: string,
  start: string,
  byId: Map<string, { data: AgentStep }>,
  outgoing: Map<string, { output: string; target: string }[]>,
  incoming: Map<string, string[]>
): string[] {
  const sources = [...new Set(incoming.get(mergeId) ?? [])]
  // For each source, the (step, way out) pairs it needs.
  const needs = new Map<string, Map<string, string>>(
    sources.map((s) => [s, new Map()])
  )
  for (const [id, step] of byId) {
    // A loop's ways (each item, then done) both come in a run.
    if (step.data.kind === "loop") continue
    const ways = outputsOf(step.data).filter((o) => o.branch)
    if (ways.length < 2) continue
    for (const way of ways) {
      const reached = new Set<string>()
      const queue = [start]
      while (queue.length) {
        const at = queue.shift()!
        if (reached.has(at)) continue
        reached.add(at)
        for (const link of outgoing.get(at) ?? []) {
          if (at === id && link.output === way.id) continue
          queue.push(link.target)
        }
      }
      for (const source of sources) {
        if (!reached.has(source)) needs.get(source)!.set(id, way.id)
      }
    }
  }
  const clashes = new Set<string>()
  for (const a of sources) {
    for (const b of sources) {
      if (a >= b) continue
      for (const [step, way] of needs.get(a)!) {
        const other = needs.get(b)!.get(step)
        if (other !== undefined && other !== way) clashes.add(step)
      }
    }
  }
  return [...clashes]
}

/** Whether `from` runs `to`, directly or through the agents it runs. */
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

/** Every issue, errors first, in the order of the nodes. */
export function validateAgent(
  graph: AgentGraph,
  context: AgentValidationContext = {},
  scopeOf: (id: string) => Scope = agentScopesOf(graph)
): BuilderIssue[] {
  const issues: BuilderIssue[] = []
  const add = (
    level: IssueLevel,
    step: string | undefined,
    code: string,
    message: string,
    field = step ? fieldOf(code) : undefined
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
  const incoming = new Map<string, string[]>()
  for (const c of graph.connections) {
    if (!byId.has(c.source) || !byId.has(c.target)) continue
    outgoing.set(c.source, [...(outgoing.get(c.source) ?? []), c])
    incoming.set(c.target, [...(incoming.get(c.target) ?? []), c.source])
  }

  // One start, which leads somewhere.
  const starts = graph.steps.filter((s) => s.data.kind === "start")
  if (starts.length === 0)
    add("error", undefined, "start", "Add a start: every run begins there.")
  for (const extra of starts.slice(1)) {
    add(
      "error",
      extra.id,
      "start",
      "A workflow has one start; remove this one."
    )
  }
  if (starts[0] && !(outgoing.get(starts[0].id)?.length ?? 0)) {
    add(
      "error",
      starts[0].id,
      "start-out",
      "Connect the start to the first node."
    )
  }
  for (const start of starts) {
    if (incoming.get(start.id)?.length)
      add("error", start.id, "start-in", "Nothing can lead back to the start.")
  }

  // Names: ADK calls every node and sub-agent by its name, once each.
  // Who holds each name: the node, and the setting (its name, a sub-agent's).
  const holders = new Map<string, { step: string; sub?: string }[]>()
  const hold = (name: string, step: string, sub?: string) => {
    const key = adkName(name)
    if (key) holders.set(key, [...(holders.get(key) ?? []), { step, sub }])
  }
  for (const step of graph.steps) {
    if (step.data.kind === "start") continue
    const name = adkName(step.data.name)
    if (!name)
      add("error", step.id, "name", "Give it a name that starts with a letter.")
    else if (RESERVED_NAMES.has(name))
      add(
        "error",
        step.id,
        "name",
        `ADK keeps the name “${name}” for the person.`
      )
    hold(step.data.name, step.id)
    for (const { agent } of walkSubAgents(subAgentsOf(step.data))) {
      const sub = adkName(agent.name)
      if (!sub)
        add(
          "error",
          step.id,
          `name-${agent.id}`,
          `A sub-agent needs a name that starts with a letter.`
        )
      else if (RESERVED_NAMES.has(sub)) {
        add(
          "error",
          step.id,
          `name-${agent.id}`,
          `ADK keeps the name “${sub}” for the person.`
        )
      }
      hold(agent.name, step.id, agent.id)
    }
  }
  for (const [name, held] of holders) {
    if (held.length < 2) continue
    for (const { step, sub } of held) {
      add(
        "error",
        step,
        sub ? `same-${name}-${sub}` : `same-${name}`,
        `Two agents are called ${name}; every name must be different.`,
        sub ? `agents.${sub}.name` : "name"
      )
    }
  }

  // An LLM sub-agent's answer is kept in the state under its ID: once each.
  const keepers = new Map<string, string[]>()
  for (const step of graph.steps) {
    for (const { agent } of walkSubAgents(subAgentsOf(step.data))) {
      if (agent.kind !== "llm") continue
      keepers.set(agent.id, [...(keepers.get(agent.id) ?? []), step.id])
    }
  }
  for (const [key, steps] of keepers) {
    if (key === "input") {
      for (const step of new Set(steps)) {
        add(
          "error",
          step,
          "state-input",
          "A sub-agent's ID can't be input: the state keeps the run's input there."
        )
      }
    } else if (steps.length > 1) {
      for (const step of new Set(steps)) {
        add(
          "error",
          step,
          `state-${key}`,
          `Two sub-agents have the ID ${key}, so their answers would overwrite each other in the state.`
        )
      }
    }
  }

  // What a run can reach.
  const reached = new Set<string>()
  if (starts[0]) {
    const queue = [starts[0].id]
    while (queue.length) {
      const id = queue.shift()!
      if (reached.has(id)) continue
      reached.add(id)
      for (const c of outgoing.get(id) ?? []) queue.push(c.target)
    }
  }

  // Circles: ADK runs them again and again, so one needs a way out: a
  // branch (If, Switch, an approval's Rejected, a loop's Done…) that
  // leaves it.
  for (const component of stronglyConnected(
    graph.steps.map((s) => s.id),
    outgoing
  )) {
    const circular =
      component.length > 1 ||
      (outgoing.get(component[0]) ?? []).some((c) => c.target === component[0])
    if (!circular) continue
    const inside = new Set(component)
    const leaves = component.some((id) => {
      const step = byId.get(id)?.data
      const links = outgoing.get(id) ?? []
      return (
        step &&
        outputsOf(step).some(
          (o) =>
            o.branch &&
            !links.some((l) => l.output === o.id && inside.has(l.target))
        )
      )
    })
    if (leaves) continue
    const names = component.map((id) => byId.get(id)?.data.name ?? id)
    for (const id of component) {
      add(
        "warning",
        id,
        "cycle",
        `${names.join(" → ")} go round in a circle with no way out: it would never end.`
      )
    }
  }

  for (const step of graph.steps) {
    for (const [code, level, message, field] of [
      ...settingsIssues(step.data, context),
      ...(step.data.kind === "switch"
        ? switchIssues(step.data as StepData, scopeOf(step.id))
        : []),
    ] as Found[]) {
      add(level, step.id, code, message, field ?? fieldOf(code))
    }
    // What its expressions read: paths that lead nowhere, wrong types.
    for (const setting of agentExpressionSettings(step.data)) {
      if (!setting.text.trim()) continue
      const found = checkField(
        setting.text,
        setting.mode,
        scopeOf(step.id),
        setting.check
      ).filter((d) => !d.local)
      found
        .slice(0, PER_FIELD)
        .forEach((d, index) =>
          add(
            d.severity,
            step.id,
            `field:${setting.key}:${index}`,
            `${setting.label}: ${d.message}`
          )
        )
    }
    // ADK refuses to build a graph with a node its start doesn't reach.
    if (starts[0] && step.data.kind !== "start" && !reached.has(step.id)) {
      add(
        "error",
        step.id,
        "unreached",
        "No way from the start leads here: connect it, or remove it."
      )
    }
    // A logic step's branch that goes nowhere is a slip; an action's
    // success that goes nowhere is just where the run ends.
    const links = outgoing.get(step.id) ?? []
    const logic = AGENT_KINDS[step.data.kind].group === "logic"
    for (const output of logic ? outputsOf(step.data) : []) {
      if (!output.branch || output.fallback) continue
      if (output.id === "done" || links.some((l) => l.output === output.id))
        continue
      add(
        "warning",
        step.id,
        `open-${output.id}`,
        output.body
          ? "The loop's body is empty: connect Each item to the steps to repeat."
          : `The ${output.label} way goes nowhere; a run that takes it ends here.`
      )
    }
    if (step.data.kind === "merge") {
      const ways = new Set(incoming.get(step.id) ?? []).size
      if (ways < 2) {
        add(
          "warning",
          step.id,
          "merge",
          `A merge waits for two or more ways; ${ways === 0 ? "none comes" : "one comes"} into this one.`
        )
      } else if (step.data.config.mode === "all" && starts[0]) {
        for (const branching of exclusiveWays(
          step.id,
          starts[0].id,
          byId,
          outgoing,
          incoming
        )) {
          const name = byId.get(branching)?.data.name ?? branching
          add(
            "error",
            step.id,
            `merge-exclusive-${branching}`,
            `Its ways in come from different ways out of ${name}, and a run takes only one, so waiting for all would never go on. Wait for any instead.`
          )
        }
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

/** The label a node's kind goes by. */
export const kindLabel = (step: AgentStep) => AGENT_KINDS[step.kind].label
