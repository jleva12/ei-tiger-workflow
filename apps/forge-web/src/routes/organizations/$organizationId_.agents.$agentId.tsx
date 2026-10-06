import { createFileRoute } from "@tanstack/react-router"

import { AgentBuilderPage } from "@/features/adk-workflows/components/agent-builder"

/**
 * One of an organization's agents, in its builder. Not nested in the workspace's
 * page (`$organizationId_`), because it draws its own sidebar (the node library) in
 * place of the workspace's. Only the organization's members get in.
 */
export const Route = createFileRoute("/organizations/$organizationId_/agents/$agentId")({
  // A published version to open read-only: ?version=3.
  validateSearch: (search: Record<string, unknown>): { version?: number } => {
    const version = Number(search.version)
    return Number.isInteger(version) && version > 0 ? { version } : {}
  },
  component: AgentRoute,
})

function AgentRoute() {
  const { organizationId, agentId } = Route.useParams()
  const { version } = Route.useSearch()
  return (
    <AgentBuilderPage
      key={`${agentId}@${version ?? ""}`}
      organizationId={organizationId}
      agentId={agentId}
      version={version}
    />
  )
}
