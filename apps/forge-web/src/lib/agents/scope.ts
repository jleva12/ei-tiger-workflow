import type { StepData } from "@/lib/workflows/model"
import {
  outputTypeOf,
  scopesFor,
  type Scope,
  type ScopeRules,
} from "@/lib/workflows/scope"
import { fromJsonSchema, t, type DataType } from "@/lib/workflows/types"
import type { AgentGraph } from "./document"
import {
  AGENT_KINDS,
  isForgeKind,
  outputsOf,
  subAgentsOf,
  walkSubAgents,
  type AgentStep,
} from "./model"

/*
 * What a node's expressions can read when the graph runs, and the type
 * of each: as in a workflow (Forge's convention, which the runner keeps
 * for every node), the run's `input`, `previous` (what the node before
 * handed on), `steps.<id>.output` (and `.error`), a loop's item and
 * index, and besides, `state`: the session's state, where agents keep
 * their answers under their output keys. Forge's steps hand on what they
 * do in a workflow; the agent kinds are typed here.
 */

const declared = (schema: Record<string, unknown>) =>
  Object.keys(schema).length ? fromJsonSchema(schema) : undefined

/** The values a run starts with: the start node's input fields. */
export function inputType(graph: AgentGraph): DataType {
  const start = graph.steps.find((s) => s.data.kind === "start")?.data
  if (start?.kind !== "start")
    return t.unknown("The run's input (the ADK workflow has no start).")
  const schema = declared(start.config.input_schema)
  return schema
    ? { ...schema, description: "The values the run started with." }
    : t.unknown(
        "The start declares no input fields, so input isn't checked. Declare them on the start."
      )
}

/**
 * The session's state: the run's input (kept as `input`), and each LLM
 * sub-agent's answer under its ID (sub-agents aren't nodes, so this is
 * how the agents after them read them). A node's answer is
 * `steps.<id>.output`.
 */
export function stateType(graph: AgentGraph): DataType {
  const keys: Record<string, DataType> = {
    input: { ...inputType(graph), description: "The run's input." },
  }
  for (const step of graph.steps) {
    for (const { agent } of walkSubAgents(subAgentsOf(step.data))) {
      if (agent.kind !== "llm") continue
      keys[agent.id] = {
        ...(declared(agent.config.output_schema) ?? t.string()),
        description: `${agent.name.trim() || "A sub-agent"}'s answer (in ${step.data.name.trim() || "its agent"}).`,
      }
    }
  }
  return t.object(
    keys,
    "The session's state: the run's input, and each sub-agent's answer under its ID.",
    { open: true }
  )
}

/** What an agent node hands on, as far as its settings say. */
function agentOutputType(
  step: AgentStep,
  context: { input: DataType; incoming: Map<string, DataType> }
): DataType {
  if (isForgeKind(step.kind)) return outputTypeOf(step as StepData, context)
  switch (step.kind) {
    case "start":
      return context.input
    case "llm":
      return (
        declared(step.config.output_schema) ?? t.string("The agent's answer.")
      )
    case "sequential":
    case "loop_agent":
      return t.unknown("What its last sub-agent answered.")
    case "parallel":
      return t.unknown("What its sub-agents answered.")
    case "saved":
      return t.unknown("What the other ADK workflow handed on.")
    case "human_input":
      return (
        declared(step.config.response_schema) ??
        t.unknown("The person's answer.")
      )
  }
  return t.unknown()
}

/** Every node's scope, and what each hands on, computed once for a graph. */
export function agentTypes(graph: AgentGraph): {
  scopeOf: (id: string) => Scope
  outputOf: (id: string) => DataType
} {
  const rules: ScopeRules<AgentStep> = {
    input: inputType(graph),
    outputTypeOf: agentOutputType,
    outputsOf,
    markOf: (step) => AGENT_KINDS[step.kind],
    roots: [
      { name: "state", type: stateType(graph), detail: "The session's state" },
    ],
  }
  return scopesFor(graph, rules)
}

/** Every node's scope, computed once for a graph. */
export const agentScopesOf = (graph: AgentGraph) => agentTypes(graph).scopeOf
