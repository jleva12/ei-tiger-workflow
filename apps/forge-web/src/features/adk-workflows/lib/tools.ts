import { CHAT_KINDS, type ChatConfigs } from "@/features/agents/lib/model"
import { uid } from "@/features/steps/lib/model"
import {
  LLM_TOOL_KINDS,
  subAgentAt,
  uniqueName,
  updateSubAgent,
  type AgentStep,
  type LlmTool,
  type LlmToolKind,
  type SubAgent,
} from "./model"

/*
 * An LLM agent's tools, as its settings list them: the Agents builder's tool
 * kinds and settings (agents/lib/model.ts), set up inside the agent rather
 * than drawn on a canvas. Kept apart from model.ts, which the Agents
 * builder's model imports.
 */

/** A tool's ID: unique among its agent's tools, and an issue's key. */
export const TOOL_ID = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/

/** How each kind is offered when a tool is added. */
export const TOOL_KINDS: Record<
  LlmToolKind,
  {
    label: string
    summary: string
    icon: (typeof CHAT_KINDS)[LlmToolKind]["icon"]
  }
> = {
  mcp: {
    label: "MCP server",
    summary:
      "The tools of one of the organization's MCP servers, or one by URL.",
    icon: CHAT_KINDS.mcp.icon,
  },
  knowledge_base: {
    label: "Knowledge base",
    summary: "Searches the organization's knowledge bases for passages.",
    icon: CHAT_KINDS.knowledge_base.icon,
  },
  http_tool: {
    label: "HTTP tool",
    summary: "Calls an HTTP endpoint with arguments the model fills in.",
    icon: CHAT_KINDS.http_tool.icon,
  },
  openapi: {
    label: "OpenAPI",
    summary: "The operations of an OpenAPI spec, each a tool.",
    icon: CHAT_KINDS.openapi.icon,
  },
  saved_agent: {
    label: "Agent",
    summary: "An agent from the Agents page, called as a tool.",
    icon: CHAT_KINDS.saved_agent.icon,
  },
  adk_workflow: {
    label: "Workflow",
    summary:
      "Runs another of the organization's workflows and reads its result.",
    icon: CHAT_KINDS.adk_workflow.icon,
  },
}

export const toolDefaults = <K extends LlmToolKind>(kind: K): ChatConfigs[K] =>
  CHAT_KINDS[kind].defaults() as ChatConfigs[K]

/** A new tool of a kind, named apart from `taken`. */
export function newTool<K extends LlmToolKind>(
  kind: K,
  taken: Iterable<string>
): LlmTool<K> {
  return {
    id: uid("tool"),
    kind,
    name: uniqueName(TOOL_KINDS[kind].label, taken),
    config: toolDefaults(kind),
  } as LlmTool<K>
}

/**
 * The tools of the LLM agent at `path` in a node: the node's own (an empty
 * path), or a sub-agent's; undefined for anything else.
 */
export function toolsAt(
  step: AgentStep,
  path: string[]
): LlmTool[] | undefined {
  if (!path.length) return step.kind === "llm" ? step.config.tools : undefined
  const agent = subAgentAt(step, path)
  return agent?.kind === "llm" ? agent.config.tools : undefined
}

/** The node with the tools of the LLM agent at `path` changed. */
export function updateTools(
  step: AgentStep,
  path: string[],
  change: (tools: LlmTool[]) => LlmTool[]
): AgentStep {
  if (!path.length) {
    return step.kind === "llm"
      ? {
          ...step,
          config: { ...step.config, tools: change(step.config.tools) },
        }
      : step
  }
  return updateSubAgent(step, path, (agent) =>
    agent.kind === "llm"
      ? ({
          ...agent,
          config: { ...agent.config, tools: change(agent.config.tools) },
        } as SubAgent)
      : agent
  )
}

export { LLM_TOOL_KINDS }
