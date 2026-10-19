import { useState, useSyncExternalStore, type ReactNode } from "react"
import { useAuiState, type AssistantRuntime } from "@assistant-ui/react"
import { Button } from "@/components/ui/button"
import {
  historyErrorMessage,
  historyForRuntime,
  subscribeHistoryRegistry,
} from "./history-feedback"
import { HistoryContext, useHistoryFeedback } from "./history-context"

export function AssistantHistoryProvider({
  runtime,
  children,
}: {
  runtime: AssistantRuntime
  children: ReactNode
}) {
  const feedback = useSyncExternalStore(
    subscribeHistoryRegistry,
    () => historyForRuntime(runtime),
    () => historyForRuntime(runtime)
  )
  return (
    <HistoryContext.Provider value={{ runtime, feedback }}>
      {children}
    </HistoryContext.Provider>
  )
}

/** Available even in a chat page with its history sidebar hidden. */
export function AssistantHistoryStatus() {
  const { runtime, entry, state } = useHistoryFeedback()
  const listError = useAuiState((s) => s.threads.loadError)
  const listLoading = useAuiState((s) => s.threads.isLoading)
  const [retrying, setRetrying] = useState<unknown>()
  const [actionError, setActionError] = useState<{
    source: unknown
    message: string
  }>()
  // Async actions belong to the error they started from. A conversation switch
  // or a newer load failure must never inherit an older action's feedback.
  const source = entry?.error ? entry : (state.navigation ?? listError)
  const error =
    entry?.error ??
    state.navigation?.error ??
    (listError ? historyErrorMessage(listError, "list") : undefined)
  if (!runtime || !error) return null
  const busy = retrying === source || Boolean(entry?.loading) || listLoading
  const retry = async () => {
    setRetrying(source)
    setActionError(undefined)
    try {
      if (entry?.error) await runtime.threads.reloadMainThread()
      else if (state.navigation)
        await runtime.threads.switchToThread(state.navigation.id)
      else await runtime.threads.reload()
    } catch (error) {
      // Load/fetch wrappers retain the scoped error. Also cover failures of
      // the runtime action itself rather than leaving an unhandled promise.
      setActionError({
        source,
        message: historyErrorMessage(
          error,
          entry?.error ? "load" : state.navigation ? "open" : "list"
        ),
      })
    } finally {
      setRetrying((current: unknown) =>
        current === source ? undefined : current
      )
    }
  }
  const newConversation = async () => {
    try {
      await runtime.threads.switchToNewThread()
    } catch (error) {
      setActionError({ source, message: historyErrorMessage(error, "open") })
    }
  }
  return (
    <div
      role="alert"
      data-slot="assistant-history-error"
      className="mx-2 rounded-(--radius-card) border border-danger-border bg-danger-surface p-3 text-xs text-danger-foreground"
    >
      <p className="font-medium">
        {entry?.error
          ? "Conversation couldn't load"
          : state.navigation
            ? "Conversation couldn't open"
            : "Conversation list couldn't load"}
      </p>
      <p className="mt-1 leading-relaxed">
        {actionError && actionError.source === source
          ? actionError.message
          : error}
      </p>
      <div className="mt-3 flex gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => {
            void retry()
          }}
        >
          {busy ? "Retrying…" : "Retry"}
        </Button>
        {entry?.error && !entry.loaded && (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              void newConversation()
            }}
          >
            New conversation
          </Button>
        )}
      </div>
    </div>
  )
}
