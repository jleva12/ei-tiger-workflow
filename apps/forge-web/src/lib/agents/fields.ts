import {
  expressionSettings,
  type ExpressionSetting,
} from "@/lib/steps/fields"
import type { StepData } from "@/lib/steps/model"
import {
  isForgeKind,
  subAgentsOf,
  walkSubAgents,
  type AgentStep,
} from "./model"

/*
 * The settings of each kind of node that read the run's data, how each is
 * written, and what it must come to. The settings dialog's fields and the
 * agent's checks both come from here, so a field is completed and checked
 * the same way it's reported. Forge's steps have a workflow's.
 */

const template = (
  key: string,
  label: string,
  text: string
): ExpressionSetting => ({
  key,
  label,
  mode: "template",
  text,
  check: { textual: true },
})

/** The setting key of a sub-agent's instruction, by the sub-agent's ID. */
export const subAgentInstruction = (id: string) => `agents.${id}.instruction`

/** Every LLM agent's instruction in the node: its own, and its sub-agents'. */
function instructions(step: AgentStep): ExpressionSetting[] {
  const out: ExpressionSetting[] = []
  if (step.kind === "llm") {
    out.push(template("instruction", "Instruction", step.config.instruction))
  }
  for (const { agent } of walkSubAgents(subAgentsOf(step))) {
    if (agent.kind !== "llm") continue
    out.push(
      template(
        subAgentInstruction(agent.id),
        `${agent.name.trim() || "A sub-agent"}'s instruction`,
        agent.config.instruction
      )
    )
  }
  return out
}

/** The node's settings that hold expressions or templates. */
export function agentExpressionSettings(step: AgentStep): ExpressionSetting[] {
  if (isForgeKind(step.kind)) return expressionSettings(step as StepData)
  if (step.kind === "human_input") {
    return [template("message", "What they're asked", step.config.message)]
  }
  return instructions(step)
}

/** One setting's field spec, by key. */
export const agentExpressionSetting = (step: AgentStep, key: string) =>
  agentExpressionSettings(step).find((s) => s.key === key)
