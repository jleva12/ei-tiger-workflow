import * as React from "react"

/** A tool the agent may use, as the chat API lists it. */
export type AgentTool = {
  /** What a run's `tools` state lists. */
  name: string
  description: string
}

// The tools switched off, so the next visit starts from them. Stored as the
// ones switched off, so a tool the server adds later starts on.
const DISABLED_KEY = "assistant.disabledTools"

function readDisabled(): Set<string> {
  try {
    const saved: unknown = JSON.parse(
      localStorage.getItem(DISABLED_KEY) ?? "[]"
    )
    return new Set(Array.isArray(saved) ? saved.map(String) : [])
  } catch {
    return new Set()
  }
}

function saveDisabled(disabled: Set<string>) {
  try {
    localStorage.setItem(DISABLED_KEY, JSON.stringify([...disabled]))
  } catch {
    // Storage may be unavailable (private mode); the choice lasts the visit.
  }
}

/**
 * The tools the agent at `adkUrl` lets a conversation choose, and which the
 * person has switched on. `enabledNames` is what the next run sends as its
 * `tools` state: undefined until the list arrives, which leaves the agent
 * all of them. Only while `enabled`.
 */
export function useAgentTools(
  adkUrl: string | undefined,
  appName: string,
  enabled: boolean
) {
  const [tools, setTools] = React.useState<AgentTool[]>([])
  const [disabled, setDisabled] = React.useState(readDisabled)

  React.useEffect(() => {
    if (!enabled || !adkUrl) return
    const controller = new AbortController()
    fetch(`${adkUrl}/apps/${encodeURIComponent(appName)}/tools`, {
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((body: AgentTool[] | null) => {
        if (body) setTools(body)
      })
      // Without the list the agent keeps all its tools.
      .catch(() => {})
    return () => controller.abort()
  }, [adkUrl, appName, enabled])

  const setToolEnabled = React.useCallback((name: string, on: boolean) => {
    setDisabled((previous) => {
      const next = new Set(previous)
      if (on) next.delete(name)
      else next.add(name)
      saveDisabled(next)
      return next
    })
  }, [])

  const enabledNames = React.useMemo(
    () =>
      tools.length > 0
        ? tools.map((tool) => tool.name).filter((name) => !disabled.has(name))
        : undefined,
    [tools, disabled]
  )

  return { tools, enabledNames, setToolEnabled }
}
