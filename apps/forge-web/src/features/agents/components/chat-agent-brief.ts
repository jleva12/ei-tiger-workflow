import type { QueryClient } from "@tanstack/react-query"

import { openApiOperations } from "@/features/credentials/lib/api"
import { chatScope } from "@/features/agents/lib/input"
import {
  briefTool,
  scopeReferences,
  type BriefAgent,
  type InstructionBrief,
} from "@/features/agents/lib/instruction-writer"
import { HANDS_OFF, TOOLS, type ChatStep } from "@/features/agents/lib/model"
import type { ChatBuilderState } from "./chat-agent-store"

/*
 * The instruction writer's brief of one of a chat agent's agents (the agent
 * people talk to, or a sub-agent), from the canvas as it is: what's on its
 * Tools and Hands off to ways, who hands off to it, and what its
 * instruction can put in.
 */

function agentOf(step: ChatStep, mode?: string): BriefAgent {
  switch (step.kind) {
    case "agent":
      return {
        name: step.name,
        description: step.config.description,
        kind: "agent",
      }
    case "sub_agent":
      return {
        name: step.name,
        description: step.config.description,
        kind: "sub_agent",
        mode: mode ?? step.config.mode,
      }
    case "intake_agent":
      return {
        name: step.name,
        description: step.config.description,
        kind: "intake_agent",
        mode: "task",
      }
    case "saved_agent":
      return {
        name: step.name,
        description: "",
        kind: "saved_agent",
        agent: step.config.agent,
        ...(mode ? { mode } : {}),
      }
    default:
      return { name: step.name, description: "", kind: step.kind }
  }
}

export async function chatInstructionBrief(
  state: Pick<ChatBuilderState, "meta" | "nodes" | "edges">,
  id: string,
  client: QueryClient
): Promise<InstructionBrief> {
  const steps = new Map(state.nodes.map((n) => [n.id, n.data]))
  const step = steps.get(id)
  if (!step || (step.kind !== "agent" && step.kind !== "sub_agent")) {
    throw new Error("That agent isn't on the canvas any more.")
  }
  const out = (way: string) =>
    state.edges
      .filter((e) => e.source === id && e.sourceHandle === way)
      .map((e) => steps.get(e.target))
      .filter((s): s is ChatStep => s !== undefined)
  const organizationId = state.meta.organization_id
  const tools = await Promise.all(
    out(TOOLS).map(async (tool) => {
      if (tool.kind === "saved_agent") {
        return {
          kind: tool.kind,
          name: tool.name,
          config: { agent: tool.config.agent },
        }
      }
      const operations =
        tool.kind === "openapi"
          ? await openApiOperations(client, organizationId, tool.config)
          : undefined
      return briefTool(tool.kind, tool.name, tool.config, operations)
    })
  )
  const into = state.edges.find(
    (e) =>
      e.target === id &&
      (e.sourceHandle === HANDS_OFF || e.sourceHandle === TOOLS)
  )
  const parent = into && steps.get(into.source)
  const root = state.nodes.find((n) => n.data.kind === "agent")?.data
  const stateSchema = root?.kind === "agent" ? root.config.state_schema : {}
  const settings = step.config
  return {
    where: "chat",
    role: step.kind === "agent" ? "chat_agent" : "sub_agent",
    name: step.name,
    description: settings.description,
    container: state.meta.name,
    ...(step.kind === "sub_agent"
      ? {
          mode: into?.sourceHandle === TOOLS ? "tool" : step.config.mode,
        }
      : {}),
    ask_person: settings.ask_person,
    sees_only_given:
      step.kind === "sub_agent" && step.config.include_contents === "none",
    files: false,
    tools,
    hand_offs: out(HANDS_OFF).map((agent) => agentOf(agent)),
    ...(parent ? { parent: agentOf(parent) } : {}),
    references: scopeReferences(chatScope(stateSchema)),
  }
}
