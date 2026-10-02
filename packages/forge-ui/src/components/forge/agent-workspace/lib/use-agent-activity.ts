import { useMemo } from "react"
import { useAuiState } from "@assistant-ui/react"
import { activityForTurn } from "./activity"
import { eventIdOf, useTurnStats } from "./turn-stats"

export function useAgentActivity() {
  const messages = useAuiState((s) => s.thread.messages)
  const running = useAuiState((s) => s.thread.isRunning)
  const sessionId = useAuiState((s) => s.threadListItem.remoteId)
  const stats = useTurnStats((s) => s.stats)
  const activity = useMemo(
    () => activityForTurn(messages, running),
    [messages, running]
  )
  const turnId =
    stats && stats.sessionId === sessionId && activity.lastMessageId
      ? stats.eventTurn[eventIdOf(activity.lastMessageId)]
      : undefined
  const turn = !running && turnId ? stats?.turns[turnId] : undefined
  return { ...activity, running, sessionId, turn }
}
