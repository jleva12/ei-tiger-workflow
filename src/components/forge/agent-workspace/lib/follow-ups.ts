import * as React from "react"
import { useAuiState } from "@assistant-ui/react"
import { useAdkLongRunningToolIds } from "@assistant-ui/react-google-adk"
import { create } from "zustand"

type FollowUpsState = {
  /** By reply: its suggestions, or "loading" while they're written. */
  byReply: Record<string, string[] | "loading">
  set: (replyId: string, value: string[] | "loading") => void
}

const useFollowUpsStore = create<FollowUpsState>((set) => ({
  byReply: {},
  set: (replyId, value) =>
    set((state) => ({ byReply: { ...state.byReply, [replyId]: value } })),
}))

/**
 * Follow-up questions for the conversation's last reply, from the chat API
 * (`POST …/sessions/{id}/follow-ups`): asked once the reply is done, unless
 * the agent is waiting on the person (an approval or a question), and kept
 * per reply for the visit. Empty until they arrive.
 */
export function useFollowUps(
  adkUrl: string | undefined,
  appName: string,
  userId: string
): string[] {
  const sessionId = useAuiState((s) => s.threadListItem.remoteId)
  const running = useAuiState((s) => s.thread.isRunning)
  const lastReply = useAuiState((s) => {
    const last = s.thread.messages.at(-1)
    return last?.role === "assistant" ? last.id : undefined
  })
  const waiting = useAdkLongRunningToolIds().length > 0
  const cached = useFollowUpsStore((state) =>
    lastReply ? state.byReply[lastReply] : undefined
  )
  const set = useFollowUpsStore((state) => state.set)

  React.useEffect(() => {
    if (!adkUrl || !sessionId || !lastReply || running || waiting) return
    if (useFollowUpsStore.getState().byReply[lastReply] !== undefined) return
    set(lastReply, "loading")
    const url = `${adkUrl}/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(sessionId)}/follow-ups`
    fetch(url, { method: "POST" })
      .then((response) => (response.ok ? response.json() : { suggestions: [] }))
      .then((body: { suggestions?: string[] }) =>
        set(lastReply, body.suggestions ?? [])
      )
      .catch(() => set(lastReply, []))
  }, [adkUrl, appName, userId, sessionId, lastReply, running, waiting, set])

  return !running && !waiting && Array.isArray(cached) ? cached : []
}
