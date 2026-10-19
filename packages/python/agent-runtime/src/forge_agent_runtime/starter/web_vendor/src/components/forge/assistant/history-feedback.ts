import type {
  AssistantRuntime,
  RemoteThreadListAdapter,
} from "@assistant-ui/react"
import type { UseAdkRuntimeOptions } from "@assistant-ui/react-google-adk"

export type HistoryLoader = NonNullable<UseAdkRuntimeOptions["load"]>
export type HistoryEntry = {
  loading: boolean
  loaded: boolean
  error?: string
}
export type HistoryFeedbackState = {
  entries: ReadonlyMap<string, HistoryEntry>
  navigation?: { id: string; error: string }
}
const EMPTY: HistoryFeedbackState = { entries: new Map() }

/** Give people an actionable explanation without rendering raw server responses. */
export function historyErrorMessage(
  error: unknown,
  operation: "list" | "load" | "open"
) {
  const status =
    typeof error === "object" && error !== null && "status" in error
      ? Number(error.status)
      : Number(
          error instanceof Error
            ? error.message.match(/:\s*(\d{3})\b/)?.[1]
            : NaN
        )
  if (status === 401)
    return "Your session has expired. Sign in again, then retry."
  if (status === 403)
    return "You don't have permission to view this conversation history."
  if (status === 404 && operation !== "list")
    return "This conversation is no longer available. You can start a new conversation."
  return operation === "list"
    ? "Your conversations couldn't be loaded. Check your connection and retry."
    : operation === "open"
      ? "That conversation couldn't be opened. Check your connection and retry."
      : "This conversation couldn't be loaded. Check your connection and retry before sending another message."
}

/** Per-connection feedback, kept outside agent state and scoped to each conversation. */
export function createHistoryFeedback() {
  let state = EMPTY
  const listeners = new Set<() => void>()
  const requests = new Map<string, symbol>()
  let navigation = 0
  let selected: string | undefined
  const publish = (next: HistoryFeedbackState) => {
    state = next
    for (const listener of listeners) listener()
  }
  const entry = (id: string, value: HistoryEntry | undefined) => {
    const entries = new Map(state.entries)
    if (value) entries.set(id, value)
    else entries.delete(id)
    publish({ ...state, entries })
  }
  return {
    subscribe(listener: () => void) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    getSnapshot: () => state,
    select(id: string | undefined) {
      if (selected === id) return
      selected = id
      navigation++
      if (state.navigation) publish({ ...state, navigation: undefined })
    },
    assertReady(id: string | undefined) {
      const current = id ? state.entries.get(id) : undefined
      if (current && !current.loaded)
        throw new Error(
          "Load this conversation successfully before sending another message. Retry loading it or start a new conversation."
        )
    },
    wrapLoad(load: HistoryLoader): HistoryLoader {
      return async (id, options) => {
        if (options?.signal?.aborted)
          throw (
            options.signal.reason ?? new DOMException("Aborted", "AbortError")
          )
        const previous = state.entries.get(id)
        const token = Symbol(id)
        requests.set(id, token)
        entry(id, {
          ...previous,
          loaded: previous?.loaded ?? false,
          loading: true,
        })
        try {
          const snapshot = await load(id, options)
          if (!options?.signal?.aborted && requests.get(id) === token)
            entry(id, { loaded: true, loading: false })
          return snapshot
        } catch (error) {
          if (!options?.signal?.aborted && requests.get(id) === token)
            entry(id, {
              loaded: previous?.loaded ?? false,
              loading: false,
              error: historyErrorMessage(error, "load"),
            })
          throw error
        } finally {
          if (requests.get(id) === token) {
            requests.delete(id)
            if (options?.signal?.aborted)
              entry(
                id,
                previous?.loaded || previous?.error
                  ? { ...previous, loading: false }
                  : undefined
              )
          }
        }
      }
    },
    wrapAdapter(adapter: RemoteThreadListAdapter): RemoteThreadListAdapter {
      return {
        ...adapter,
        async fetch(id) {
          const token = ++navigation
          if (state.navigation) publish({ ...state, navigation: undefined })
          try {
            return await adapter.fetch(id)
          } catch (error) {
            if (token === navigation)
              publish({
                ...state,
                navigation: { id, error: historyErrorMessage(error, "open") },
              })
            throw error
          }
        },
      }
    },
  }
}
export type HistoryFeedback = ReturnType<typeof createHistoryFeedback>

// The hook and provider share feedback by runtime, so every existing screen,
// modal and custom layout receives it without an additional required prop.
const registered = new WeakMap<AssistantRuntime, HistoryFeedback>()
const registryListeners = new Set<() => void>()
export const historyForRuntime = (runtime: AssistantRuntime) =>
  registered.get(runtime)
export function subscribeHistoryRegistry(listener: () => void) {
  registryListeners.add(listener)
  return () => {
    registryListeners.delete(listener)
  }
}
export function registerHistoryFeedback(
  runtime: AssistantRuntime,
  feedback: HistoryFeedback
) {
  registered.set(runtime, feedback)
  for (const listener of registryListeners) listener()
  const select = () => {
    const current = runtime.threads.mainItem.getState()
    feedback.select(current.externalId ?? current.remoteId)
  }
  select()
  const unsubscribe = runtime.threads.subscribe(select)
  return () => {
    unsubscribe()
    if (registered.get(runtime) === feedback) {
      registered.delete(runtime)
      for (const listener of registryListeners) listener()
    }
  }
}
