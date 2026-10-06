import { extendedThinkingLevels } from "@/components/forge/assistant/index"
import type {
  LlmSettings,
  LlmTool,
  SubAgent,
} from "@/features/adk-workflows/lib/model"
import type { ToolLookups } from "@/features/agents/components/tool-fields"
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

/** What one of an LLM agent's tools calls, in a line. */
export function toolLine(tool: LlmTool, lookups: ToolLookups): string {
  switch (tool.kind) {
    case "mcp":
      return tool.config.server
        ? (lookups.mcpServers[tool.config.server]?.name ?? "A deleted server")
        : tool.config.url.trim() || "No server yet"
    case "knowledge_base":
      return tool.config.knowledge_bases.length
        ? tool.config.knowledge_bases
            .map((kb) => lookups.knowledgeBases[kb]?.name ?? "a deleted one")
            .join(", ")
        : "No knowledge base yet"
    case "http_tool":
      return `${tool.config.method} ${tool.config.url.trim() || "…"}`
    case "openapi":
      return tool.config.source === "inline"
        ? "A spec pasted in"
        : tool.config.url.trim() || "No spec yet"
    case "saved_agent":
      return tool.config.agent
        ? (lookups.agents[tool.config.agent] ?? "A deleted agent")
        : "No agent yet"
    case "adk_workflow":
      return tool.config.workflow
        ? (lookups.workflows[tool.config.workflow] ?? "A deleted workflow")
        : "No workflow yet"
  }
}
