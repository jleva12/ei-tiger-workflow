import { createFileRoute } from "@tanstack/react-router"

import { ChatPageConfigPage } from "@/features/agents/components/chat-page-config"

/**
 * A chat agent's chat page, set up beside a live preview of it. Not nested
 * in the agent's builder (`$chatAgentId_`): a page of its own.
 */
export const Route = createFileRoute(
  "/organizations/$organizationId_/chat-agents/$chatAgentId_/chat-page"
)({
  component: ChatPageConfigRoute,
})

function ChatPageConfigRoute() {
  const { organizationId, chatAgentId } = Route.useParams()
  return (
    <ChatPageConfigPage
      key={chatAgentId}
      organizationId={organizationId}
      agentId={chatAgentId}
    />
  )
}
