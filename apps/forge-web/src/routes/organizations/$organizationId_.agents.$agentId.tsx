import { createFileRoute } from "@tanstack/react-router"

import { AgentBuilderPage } from "@/features/adk-workflows/components/agent-builder"

/**
 * One of an organization's agents, in its builder. Not nested in the workspace's
 * page (`$organizationId_`), because it draws its own sidebar (the node library) in
 * place of the workspace's. Only the organization's members get in.
 */
export const Route = createFileRoute("/organizations/$organizationId_/agents/$agentId")({
  component: AgentRoute,
})

function AgentRoute() {
  const { organizationId, agentId } = Route.useParams()
  return <AgentBuilderPage key={agentId} organizationId={organizationId} agentId={agentId} />
}
