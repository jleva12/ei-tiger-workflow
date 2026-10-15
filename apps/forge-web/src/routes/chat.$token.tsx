import { createFileRoute } from "@tanstack/react-router"

import { ChatPage } from "@/features/agents/components/chat-page"

/**
 * A chat agent shared as a chat page: the whole window, without the shell,
 * for anyone who has the link (`chat-page.ts`).
 */
export const Route = createFileRoute("/chat/$token")({
  staticData: { bare: true },
  component: ChatPageRoute,
})

function ChatPageRoute() {
  const { token } = Route.useParams()
  return <ChatPage token={token} />
}
