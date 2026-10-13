import type { QueryClient } from "@tanstack/react-query"

import { openApiOperations } from "@/features/credentials/lib/api"
import {
  briefTool,
  scopeReferences,
  type BriefAgent,
  type InstructionBrief,
} from "@/features/agents/lib/instruction-writer"
import {
  subAgentAt,
  type AgentStep,
  type LlmSettings,
  type SubAgent,
} from "@/features/adk-workflows/lib/model"
import type { AgentBuilderState } from "./agent-store"

/*
 * The instruction writer's brief of one of a workflow's LLM agents (a node,
 * or a sub-agent in one, at `path`), as the builder holds it: its tools,
 * the sub-agents it hands off to, the agent above it, and the run's data
 * its instruction can put in.
 */

const subAgentOf = (agent: SubAgent): BriefAgent => ({
  id: agent.id,
  name: agent.name,
  description: agent.config.description,
  kind: agent.kind,
})

export async function workflowInstructionBrief(
  state: Pick<AgentBuilderState, "meta" | "nodes" | "scopeOf">,
  id: string,
  path: string[],
  client: QueryClient
): Promise<InstructionBrief> {
  const step = state.nodes.find((n) => n.id === id)?.data as
    AgentStep | undefined
  const agent = step && path.length ? subAgentAt(step, path) : undefined
  if (!step || (path.length ? agent?.kind !== "llm" : step.kind !== "llm")) {
    throw new Error("That agent isn't in the workflow any more.")
  }
  const name = agent ? agent.name : step.name
  const config = (agent ? agent.config : step.config) as LlmSettings & {
    mode?: string
  }
  // The agent above it: a sub-agent's, or the node.
  const above =
    path.length > 1 ? subAgentAt(step, path.slice(0, -1)) : undefined
  const parent: BriefAgent | undefined = !path.length
    ? undefined
    : above
      ? subAgentOf(above)
      : {
          name: step.name,
          description:
            "description" in step.config ? String(step.config.description) : "",
          kind: step.kind,
        }
  const organizationId = state.meta.organization_id
  const tools = await Promise.all(
    config.tools.map(async (tool) =>
      briefTool(
        tool.kind,
        tool.name,
        tool.config,
        tool.kind === "openapi"
          ? await openApiOperations(
              client,
              organizationId,
              tool.config as Parameters<typeof openApiOperations>[2]
            )
          : undefined
      )
    )
  )
  const start = state.nodes.find((n) => n.data.kind === "start")?.data
  const outputSchema = config.output_schema
  return {
    where: "workflow",
    role: path.length ? "workflow_sub_agent" : "workflow_agent",
    name,
    description: config.description,
    container: state.meta.name,
    ...(!path.length && config.mode ? { mode: config.mode } : {}),
    ask_person: false,
    sees_only_given: config.include_contents === "none",
    ...(outputSchema && Object.keys(outputSchema).length
      ? { output_schema: outputSchema }
      : {}),
    files: start?.kind === "start" && start.config.allow_files,
    tools,
    hand_offs: config.sub_agents.map(subAgentOf),
    ...(parent ? { parent } : {}),
    references: scopeReferences(state.scopeOf(id)),
  }
}
