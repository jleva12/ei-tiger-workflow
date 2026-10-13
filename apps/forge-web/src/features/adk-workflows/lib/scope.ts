import { resolveExpression } from "@/features/steps/lib/expressions"
import type { StepData } from "@/features/steps/lib/model"
import {
  outputTypeOf,
  scopesFor,
  type ScopeRules,
} from "@/features/steps/lib/scope"
import {
  fromJsonSchema,
  t,
  unionOf,
  withDescription,
  type DataType,
} from "@/features/steps/lib/types"
import { toGraph, type AgentDocument, type AgentGraph } from "./document"
import {
  AGENT_KINDS,
  isForgeKind,
  outputsOf,
  subAgentsOf,
  walkSubAgents,
  type AgentStep,
  type SubAgent,
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
    return t.unknown("The run's input (the workflow has no start).")
  const schema = declared(start.config.input_schema)
  return schema
    ? { ...schema, description: "The values the run started with." }
    : t.unknown(
        "The start declares no input fields, so input isn't checked. Declare them on the start."
      )
}

/** One of the files a run started with, as `state.files` lists it. */
const RUN_FILE = t.object(
  {
    name: t.string(
      "The file's name: its artifact's, which loads it (ctx.load_artifact)."
    ),
    media_type: t.string("Its media type, e.g. application/pdf."),
    size_bytes: t.number("Its size, in bytes."),
    type: t.string("Its type (pdf, word, png…); empty for another."),
    version: t.number("Its artifact's version: 0."),
  },
  "A file the run started with.",
  { required: ["name", "media_type", "size_bytes", "type", "version"] }
)

/**
 * What a Pi agent hands on (packages/python/adk-workflows, factories/pi.py):
 * its answer, held to its output schema when it declares one, its tool
 * calls, its usage and the folder it worked in.
 */
export function piOutputType(outputSchema: Record<string, unknown>): DataType {
  return t.object(
    {
      answer: withDescription(
        declared(outputSchema) ??
          t.string(
            "Pi's answer, as text: declare what it answers (its fields) to read parts of it."
          ),
        "Pi's answer: its last message."
      ),
      tool_calls: t.array(
        t.object(
          {
            tool: t.string(
              "The tool: read, grep, find, ls, bash, edit or write."
            ),
            args: t.object({}, "What pi called it with.", { open: true }),
            is_error: t.boolean("Whether it failed."),
          },
          "One of pi's tool calls.",
          { required: ["tool", "args", "is_error"] }
        ),
        "Pi's tool calls, in order."
      ),
      usage: t.object(
        {
          input: t.number("Input tokens, cached ones included.", true),
          cached: t.number(
            "Input tokens read from the provider's cache.",
            true
          ),
          output: t.number("Output tokens.", true),
          cost: unionOf([
            t.number("What it cost, in USD."),
            t.null("The model has no price."),
          ]),
        },
        "Its model calls' tokens and cost, added up.",
        { required: ["input", "cached", "output", "cost"] }
      ),
      folder: t.string("The folder it worked in: an absolute path."),
    },
    "What pi did.",
    { required: ["answer", "tool_calls", "usage", "folder"] }
  )
}

/**
 * The session's state: the run's input (kept as `input`), the files it
 * started with (`files`, when its start allows them), its folder on the
 * worker's file system (`workspace`, when its start allows one), and each LLM
 * sub-agent's answer under its ID (sub-agents aren't nodes, so this is
 * how the agents after them read them). A node's answer is
 * `steps.<id>.output`.
 */
export function stateType(graph: AgentGraph): DataType {
  const keys: Record<string, DataType> = {
    input: { ...inputType(graph), description: "The run's input." },
  }
  const start = graph.steps.find((s) => s.data.kind === "start")?.data
  if (start?.kind === "start" && start.config.allow_files)
    keys.files = t.array(
      RUN_FILE,
      "The files the run started with: artifacts of its session."
    )
  if (start?.kind === "start" && start.config.allow_file_system)
    keys.workspace = t.string(
      "The run's own folder on the worker's file system: an absolute path, the same for each of its attempts. Point agents at it (or a folder in it, e.g. {{ state.workspace }}/backend) to clone repositories and work on files."
    )
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

/**
 * What a sub-agent answers, as the team it's in hands it on: the answer of
 * the last LLM agent in it to answer. An LLM agent's is held to its output
 * schema (text without one), or is the answer of one it hands off to.
 */
export function answerOf(agent: SubAgent, depth = 0): DataType {
  if (depth > 8) return t.unknown()
  const name = agent.name.trim() || "A sub-agent"
  switch (agent.kind) {
    case "llm": {
      const own = declared(agent.config.output_schema) ?? t.string()
      const answers = [
        own,
        ...agent.config.sub_agents.map((sub) => answerOf(sub, depth + 1)),
      ]
      return withDescription(
        answers.length > 1 ? unionOf(answers) : own,
        `${name}'s answer.`
      )
    }
    case "sequential": {
      const last = agent.config.sub_agents.at(-1)
      return last ? answerOf(last, depth + 1) : t.unknown()
    }
    default:
      return unionOf(
        agent.config.sub_agents.map((sub) => answerOf(sub, depth + 1))
      )
  }
}

/** What typing a graph can look up besides it. */
export type TypeContext = {
  /** Another of the organization's workflows, by ID: what a saved-workflow node runs. */
  workflow?: (id: string) => AgentDocument | undefined
  /** The workflows being typed, outermost first: one met again isn't typed again. */
  within?: string[]
}

/** What another workflow takes and hands back, as a saved-workflow node runs it. */
export type SavedWorkflowTypes = {
  name: string
  /** What it starts with: its start's input fields. */
  input: DataType
  /** What it hands back: its ending's result. */
  result: DataType
}

/**
 * What a workflow hands back: the result of the step a run ends at (an
 * End's result, or what came into it when it has none; any other step's
 * output). With several places a run can end, any of theirs.
 */
export function resultType(
  graph: AgentGraph,
  types: Pick<
    ReturnType<typeof agentTypes>,
    "scopeOf" | "outputOf" | "previousOf"
  >
): DataType {
  const leads = new Set(graph.connections.map((c) => c.source))
  const endings = graph.steps.filter(
    (s) =>
      s.data.kind !== "start" &&
      !leads.has(s.id) &&
      // A loop body's ending ends an item, not the run.
      !types.scopeOf(s.id).enclosing?.size
  )
  const results = endings.map((s) => {
    if (s.data.kind === "end") {
      const said = s.data.config.result.trim()
        ? resolveExpression(s.data.config.result, types.scopeOf(s.id))
        : types.previousOf(s.id)
      return said ?? t.unknown()
    }
    if (
      s.data.kind === "if" ||
      s.data.kind === "switch" ||
      s.data.kind === "match"
    )
      return types.previousOf(s.id) ?? t.unknown()
    return types.outputOf(s.id)
  })
  return results.length ? unionOf(results) : t.unknown()
}

/** Every node's scope, and what each hands on, computed once for a graph. */
export function agentTypes(graph: AgentGraph, context: TypeContext = {}) {
  const within = context.within ?? []
  const saved = new Map<string, SavedWorkflowTypes | undefined>()
  /** What another workflow takes and hands back, worked out once per graph. */
  const savedTypes = (id: string): SavedWorkflowTypes | undefined => {
    if (saved.has(id)) return saved.get(id)
    saved.set(id, undefined)
    const doc = within.includes(id) ? undefined : context.workflow?.(id)
    if (!doc) return undefined
    const other = toGraph(doc)
    const types = agentTypes(other, { ...context, within: [...within, id] })
    const found = {
      name: doc.name || id,
      input: inputType(other),
      result: resultType(other, types),
    }
    saved.set(id, found)
    return found
  }

  /** What a node hands on, as far as its settings say. */
  const outputTypeOfNode = (
    step: AgentStep,
    known: { input: DataType; incoming: Map<string, DataType> }
  ): DataType => {
    if (isForgeKind(step.kind)) return outputTypeOf(step as StepData, known)
    switch (step.kind) {
      case "start":
        return known.input
      case "llm":
        return (
          declared(step.config.output_schema) ??
          t.string(
            "The agent's answer, as text: declare what it answers (its fields) to read parts of it."
          )
        )
      case "sequential": {
        const last = step.config.sub_agents.at(-1)
        return last
          ? withDescription(
              answerOf(last),
              `What its last sub-agent, ${last.name.trim() || last.id}, answered.`
            )
          : t.unknown("What its last sub-agent answered.")
      }
      case "loop_agent":
        return withDescription(
          unionOf(step.config.sub_agents.map((sub) => answerOf(sub))),
          "What the last of its sub-agents to answer answered."
        )
      case "parallel":
        return step.config.sub_agents.length
          ? t.object(
              Object.fromEntries(
                step.config.sub_agents.map((sub) => [sub.id, answerOf(sub)])
              ),
              "Each sub-agent's answer, by its ID."
            )
          : t.unknown("What its sub-agents answered.")
      case "saved": {
        const other = step.config.agent
          ? savedTypes(step.config.agent)
          : undefined
        return other
          ? withDescription(
              other.result,
              `What ${other.name} hands back, as it is now.`
            )
          : t.unknown("What the other workflow handed back.")
      }
      case "pi_agent":
        return piOutputType(step.config.output_schema)
      case "human_input":
        return (
          declared(step.config.response_schema) ??
          t.unknown("The person's answer.")
        )
    }
    return t.unknown()
  }

  const rules: ScopeRules<AgentStep> = {
    input: inputType(graph),
    outputTypeOf: outputTypeOfNode,
    outputsOf,
    markOf: (step) => AGENT_KINDS[step.kind],
    roots: [
      { name: "state", type: stateType(graph), detail: "The session's state" },
    ],
  }
  return { ...scopesFor(graph, rules), savedTypes }
}

/** Every node's scope, computed once for a graph. */
export const agentScopesOf = (graph: AgentGraph, context: TypeContext = {}) =>
  agentTypes(graph, context).scopeOf
