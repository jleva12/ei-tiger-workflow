import { extendedThinkingLevels } from "@/components/forge/assistant/index"
import type { LlmSettings, SubAgent } from "@/features/adk-workflows/lib/model"
import { modelIdOf } from "@/features/steps/lib/models"
import type { AgentLookups } from "./agent-store"

/* How agents are set up, in a line: under a node's name, and in sub-agent lists. */

export const plural = (n: number, word: string) =>
  `${n} ${n === 1 ? word : `${word}s`}`

/** The model an LLM agent runs on, and how long it thinks. */
export function modelLine(
  config: Pick<LlmSettings, "model" | "thinking_level">,
  lookups: Pick<AgentLookups, "models">
) {
  const named = modelIdOf(config.model)
  const model = named
    ? (lookups.models[named] ?? config.model.name.trim())
    : lookups.models[""]
      ? `Default · ${lookups.models[""]}`
      : "Default model"
  const level = extendedThinkingLevels.find(
    (l) => l.id === config.thinking_level
  )
  return [model, level && `${level.label} thinking`].filter(Boolean).join(" · ")
}

/** How a sub-agent is set up, in a line. */
export function subAgentLine(agent: SubAgent, lookups: AgentLookups) {
  switch (agent.kind) {
    case "llm":
      return modelLine(agent.config, lookups)
    case "sequential":
      return `${plural(agent.config.sub_agents.length, "sub-agent")}, in order`
    case "parallel":
      return `${plural(agent.config.sub_agents.length, "sub-agent")}, at once`
    case "loop_agent":
      return `${plural(agent.config.sub_agents.length, "sub-agent")}, up to ${agent.config.max_iterations} ${
        agent.config.max_iterations === 1 ? "pass" : "passes"
      }`
  }
}
