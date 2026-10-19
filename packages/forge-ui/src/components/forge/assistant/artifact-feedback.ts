import type { AdkArtifactData } from "@assistant-ui/react-google-adk"

type ArtifactApi = {
  list(sessionId: string): Promise<string[]>
  load(sessionId: string, name: string): Promise<AdkArtifactData>
}
export type ArtifactFeedbackState = {
  names: readonly string[]
  loading: boolean
  pending?: string
  error?: { action: "list" | "download"; name?: string; message: string }
}

/** Keep asynchronous file failures visible and prevent stale session downloads. */
export function createArtifactFeedback({
  api,
  sessionId,
  onOpenArtifact,
  download,
}: {
  api?: ArtifactApi
  sessionId?: string
  onOpenArtifact?: (name: string) => void | Promise<void>
  download: (name: string, data: AdkArtifactData) => void
}) {
  let state: ArtifactFeedbackState = { names: [], loading: false }
  let request = 0
  const listeners = new Set<() => void>()
  const publish = (next: ArtifactFeedbackState) => {
    state = next
    for (const listener of listeners) listener()
  }
  const refresh = async () => {
    if (!api || !sessionId || state.loading || state.pending) return
    const token = ++request
    publish({ ...state, loading: true, error: undefined })
    try {
      const names = await api.list(sessionId)
      if (
        !Array.isArray(names) ||
        !names.every((name) => typeof name === "string" && name)
      )
        throw new Error("Invalid artifact list")
      if (token === request)
        publish({ names: [...new Set(names)], loading: false })
    } catch {
      if (token === request)
        publish({
          ...state,
          loading: false,
          error: {
            action: "list",
            message:
              "Saved files couldn't be loaded. Check your connection and retry.",
          },
        })
    }
  }
  const open = async (name: string) => {
    if (
      state.loading ||
      state.pending ||
      (!onOpenArtifact && (!api || !sessionId))
    )
      return
    const token = ++request
    publish({ ...state, pending: name, error: undefined })
    try {
      if (onOpenArtifact) await onOpenArtifact(name)
      else if (api && sessionId) {
        const data = await api.load(sessionId, name)
        if (token !== request) return
        download(name, data)
      }
      if (token === request) publish({ ...state, pending: undefined })
    } catch {
      if (token === request)
        publish({
          ...state,
          pending: undefined,
          error: {
            action: "download",
            name,
            message: `“${name}” couldn't be opened. Check your connection and retry.`,
          },
        })
    }
  }
  return {
    getSnapshot: () => state,
    subscribe(listener: () => void) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    refresh,
    open,
    async retry() {
      const error = state.error
      if (error?.action === "list") await refresh()
      else if (error?.name) await open(error.name)
    },
    cancelPending() {
      request++
    },
  }
}
