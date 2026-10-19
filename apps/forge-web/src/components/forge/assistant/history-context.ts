import { createContext, useContext, useSyncExternalStore } from "react"
import { useAuiState, type AssistantRuntime } from "@assistant-ui/react"
import type { HistoryFeedback, HistoryFeedbackState } from "./history-feedback"

export const HistoryContext = createContext<
  { runtime: AssistantRuntime; feedback?: HistoryFeedback } | undefined
>(undefined)
const EMPTY: HistoryFeedbackState = { entries: new Map() }
const emptySnapshot = () => EMPTY
const noSubscription = () => () => {}

export function useHistoryFeedback() {
  const context = useContext(HistoryContext)
  const feedback = context?.feedback
  const state = useSyncExternalStore(
    feedback?.subscribe ?? noSubscription,
    feedback?.getSnapshot ?? emptySnapshot,
    feedback?.getSnapshot ?? emptySnapshot
  )
  const id = useAuiState((s) => {
    const current = s.threads.threadItems.find(
      (item) => item.id === s.threads.mainThreadId
    )
    return current?.externalId ?? current?.remoteId
  })
  return { ...context, state, entry: id ? state.entries.get(id) : undefined }
}

export function useHistoryBlocked() {
  const { entry } = useHistoryFeedback()
  return Boolean(entry?.error && !entry.loaded)
}
