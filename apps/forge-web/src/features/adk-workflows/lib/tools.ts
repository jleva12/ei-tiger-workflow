import { chatKindInfo, servedChatKinds } from "@/features/agents/lib/catalog"
import type { ChatConfigs } from "@/features/agents/lib/model"
import { uid } from "@/features/steps/lib/model"
import {
  isLlmToolKind,
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
 * than drawn on a canvas, called and described as the Agents canvas's node
 * catalog has them (agents/lib/catalog.ts). Kept apart from model.ts, which
 * the Agents builder's model imports.
 */

/** A tool's ID: unique among its agent's tools, and an issue's key. */
export const TOOL_ID = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/

/** The kinds of tool an LLM agent is offered, in the order the catalog lists them. */
export const workflowToolKinds = (): LlmToolKind[] =>
  servedChatKinds().filter(isLlmToolKind)

export const toolDefaults = <K extends LlmToolKind>(kind: K): ChatConfigs[K] =>
  chatKindInfo(kind).defaults()

/** A new tool of a kind, named apart from `taken`. */
export function newTool<K extends LlmToolKind>(
  kind: K,
  taken: Iterable<string>
): LlmTool<K> {
  return {
    id: uid("tool"),
    kind,
    name: uniqueName(chatKindInfo(kind).label, taken),
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
