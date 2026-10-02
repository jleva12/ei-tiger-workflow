import { useEffect, useSyncExternalStore } from "react"
import { useAuiState } from "@assistant-ui/react"

// When each reasoning block started and how long it took, by message and
// first part, for as long as the page is open: switching conversations and
// back keeps them. Replies loaded from history weren't timed here.
const reasoningTimes = new Map<string, { start: number; seconds?: number }>()
const timeListeners = new Set<() => void>()
const subscribeToTimes = (listener: () => void) => {
  timeListeners.add(listener)
  return () => {
    timeListeners.delete(listener)
  }
}
// A question asked longer ago than this didn't start the thinking, e.g. a
// run resumed after an approval.
const MAX_WAIT_MS = 5 * 60_000

/**
 * How long a reasoning block of the current message took, in whole seconds
 * (at least 1): until its last thought streamed, from the question for a
 * reply's first thoughts (the model thinks before its first thought
 * arrives), or from its first thought for later ones (after tool calls).
 * Undefined while it runs, and for blocks this page didn't see stream.
 *
 * @param firstIndex The index of the block's first part in the message.
 * @param running Whether it's still streaming.
 */
export function useReasoningDuration(firstIndex: number, running: boolean) {
  const key = useAuiState((s) => `${s.message.id}:${firstIndex}`)
  const asked = useAuiState((s) => {
    if (firstIndex !== 0) return undefined
    for (let index = s.message.index - 1; index >= 0; index--) {
      const message = s.thread.messages[index]
      if (message?.role === "user") return message.createdAt.getTime()
    }
    return undefined
  })

  useEffect(() => {
    const time = reasoningTimes.get(key)
    if (running) {
      if (!time) {
        const now = Date.now()
        const recent = asked !== undefined && now - asked < MAX_WAIT_MS
        reasoningTimes.set(key, { start: recent ? asked : now })
      }
      return
    }
    if (time && time.seconds === undefined) {
      time.seconds = Math.max(1, Math.round((Date.now() - time.start) / 1000))
      timeListeners.forEach((listener) => listener())
    }
  }, [key, running, asked])

  return useSyncExternalStore(
    subscribeToTimes,
    () => reasoningTimes.get(key)?.seconds
  )
}
