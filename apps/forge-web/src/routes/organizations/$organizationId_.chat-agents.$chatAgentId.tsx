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
  component: ChatAgentRoute,
})

function ChatAgentRoute() {
  const { organizationId, chatAgentId } = Route.useParams()
  return (
    <ChatAgentBuilderPage
      key={chatAgentId}
      organizationId={organizationId}
      agentId={chatAgentId}
    />
  )
}
