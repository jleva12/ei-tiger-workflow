import {
  expressionSettings,
  type ExpressionSetting,
} from "@/features/steps/lib/fields"
import type { StepData } from "@/features/steps/lib/model"
import { fromJsonSchema } from "@/features/steps/lib/types"
import { takesBody } from "@/features/agents/lib/http-template"
import {
  isForgeKind,
  subAgentsOf,
  walkSubAgents,
  type AgentStep,
  type LlmTool,
} from "./model"

/*
 * The settings of each kind of node that read the run's data, how each is
 * written, and what it must come to. The settings dialog's fields and the
 * agent's checks both come from here, so a field is completed and checked
 * the same way it's reported. Forge's steps have a workflow's.
 */

/**
 * A template: text with `{{ }}` parts. What a person reads is checked for
 * objects written out as JSON; a model reads JSON as well as text.
 */
const template = (
  key: string,
  label: string,
  text: string,
  { forModel = false } = {}
): ExpressionSetting => ({
  key,
  label,
  mode: "template",
  text,
  check: forModel ? {} : { textual: true },
})

/** The setting key of a sub-agent's instruction, by the sub-agent's ID. */
export const subAgentInstruction = (id: string) => `agents.${id}.instruction`

/**
 * An LLM agent's HTTP tools' URLs and bodies (one they send): templates
 * whose `{{ }}` reads the run's data as its instruction does. A body's
 * whole-value part goes in as it is, so it isn't held to text.
 */
function httpTools(
  tools: LlmTool[],
  prefix: string,
  agent?: string
): ExpressionSetting[] {
  return tools.flatMap((tool) => {
    if (tool.kind !== "http_tool") return []
    const label = `${agent ? `${agent}'s tool ` : ""}${tool.name.trim() || "An HTTP tool"}`
    const key = `${prefix}tools.${tool.id}`
    return [
      template(`${key}.url`, `${label}'s URL`, tool.config.url),
      ...(takesBody(tool.config.method)
        ? [
            template(`${key}.body`, `${label}'s body`, tool.config.body, {
              forModel: true,
            }),
          ]
        : []),
    ]
  })
}

/** Every LLM agent's instruction in the node, its own and its sub-agents', and their HTTP tools'. */
function instructions(step: AgentStep): ExpressionSetting[] {
  const out: ExpressionSetting[] = []
  if (step.kind === "llm") {
    out.push(
      template("instruction", "Instruction", step.config.instruction, {
        forModel: true,
      }),
      ...httpTools(step.config.tools, "")
    )
  }
  for (const { agent } of walkSubAgents(subAgentsOf(step))) {
    if (agent.kind !== "llm") continue
    const name = agent.name.trim() || "A sub-agent"
    out.push(
      template(
        subAgentInstruction(agent.id),
        `${name}'s instruction`,
        agent.config.instruction,
        { forModel: true }
      ),
      ...httpTools(agent.config.tools, `agents.${agent.id}.`, name)
    )
  }
  return out
}

/** The setting key of an input of an agent from the Agents page, by its field. */
export const agentInput = (field: string) => `inputs.${field}`

/** What a node's settings are checked against besides the run's data. */
export type SettingsLookups = {
  /**
   * The JSON Schema of the input an agent from the Agents page takes (its
   * state_schema), by its ID: each input field is held to its field's.
   */
  agentInput?: (agent: string) => Record<string, unknown> | undefined
  /**
   * The JSON Schema of what another of the organization's workflows starts
   * with (its start's input_schema), by its ID: a saved-workflow node's
   * inputs are held to its fields'.
   */
  workflowInput?: (workflow: string) => Record<string, unknown> | undefined
}

/** The type an agent's input field takes, from its input's JSON Schema. */
export function agentInputType(
  schema: Record<string, unknown> | undefined,
  field: string
) {
  const properties = schema?.properties
  const declared =
    properties && typeof properties === "object"
      ? (properties as Record<string, unknown>)[field]
      : undefined
  return declared === undefined ? undefined : fromJsonSchema(declared)
}

/** Input fields set from the run's data, each held to the type its input declares. */
const inputSettings = (
  inputs: Record<string, string>,
  schema: Record<string, unknown> | undefined
) =>
  Object.entries(inputs).map(([field, text]): ExpressionSetting => {
    const want = agentInputType(schema, field)
    return {
      key: agentInput(field),
      label: `Its input ${field}`,
      mode: "expression",
      text,
      check: want ? { want } : {},
    }
  })

/** The node's settings that hold expressions or templates. */
export function agentExpressionSettings(
  step: AgentStep,
  lookups: SettingsLookups = {}
): ExpressionSetting[] {
  if (isForgeKind(step.kind)) return expressionSettings(step as StepData)
  if (step.kind === "human_input") {
    return [template("message", "What they're asked", step.config.message)]
  }
  if (step.kind === "llm" && step.config.source === "agent") {
    // An agent from the Agents page: what it's sent, and its input fields,
    // each held to the type its input declares.
    const schema = step.config.agent
      ? lookups.agentInput?.(step.config.agent)
      : undefined
    return [
      template("message", "Its message", step.config.message, {
        forModel: true,
      }),
      ...inputSettings(step.config.inputs, schema),
    ]
  }
  if (step.kind === "saved") {
    // Another workflow's start's fields, when it's started with them.
    const schema = step.config.agent
      ? lookups.workflowInput?.(step.config.agent)
      : undefined
    return inputSettings(step.config.inputs, schema)
  }
  return instructions(step)
}

/** One setting's field spec, by key. */
export const agentExpressionSetting = (
  step: AgentStep,
  key: string,
  lookups: SettingsLookups = {}
) => agentExpressionSettings(step, lookups).find((s) => s.key === key)
