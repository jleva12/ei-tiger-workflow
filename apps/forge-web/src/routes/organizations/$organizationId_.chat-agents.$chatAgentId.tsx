import { createFileRoute } from "@tanstack/react-router"

import { ChatAgentBuilderPage } from "@/features/agents/components/chat-agent-builder"

/**
 * One of an organization's chat agents, in its builder. Not nested in the
 * workspace's page (`$organizationId_`), because it draws its own sidebar
 * (the library of what can be attached) in place of the workspace's.
 */
export const Route = createFileRoute(
  "/organizations/$organizationId_/chat-agents/$chatAgentId"
)({
  // ?version=3 opens that published version, read-only.
  validateSearch: (search: Record<string, unknown>): { version?: number } => {
    const version = Number(search.version)
    return Number.isInteger(version) && version > 0 ? { version } : {}
  },
  component: ChatAgentRoute,
})

function ChatAgentRoute() {
  const { organizationId, chatAgentId } = Route.useParams()
  const { version } = Route.useSearch()
  return (
    <ChatAgentBuilderPage
      key={`${chatAgentId}@${version ?? "current"}`}
      organizationId={organizationId}
      agentId={chatAgentId}
      version={version}
    />
  )
}
