import * as React from "react"
import { createFileRoute, useNavigate } from "@tanstack/react-router"

import { AssistantWindowPage } from "@/features/assistant/components/assistant-window-page"

/**
 * The assistant in its own window, which the panel's pop-out button and
 * the launcher open (`openAssistantWindow`). It's the whole window, without
 * the shell. `thread` is the conversation to open on.
 */
export const Route = createFileRoute("/assistant")({
  staticData: { bare: true },
  validateSearch: (search: Record<string, unknown>): { thread?: string } => ({
    thread:
      typeof search.thread === "string" && search.thread
        ? search.thread
        : undefined,
  }),
  component: AssistantWindowRoute,
})

function AssistantWindowRoute() {
  const { thread } = Route.useSearch()
  const navigate = useNavigate()
  // Once it's open, the URL stops naming it, so a reload doesn't go back.
  const threadShown = React.useCallback(
    () =>
      void navigate({
        to: "/assistant",
        search: (prev) => ({ ...prev, thread: undefined }),
        replace: true,
      }),
    [navigate]
  )
  return <AssistantWindowPage thread={thread} onThreadShown={threadShown} />
}
