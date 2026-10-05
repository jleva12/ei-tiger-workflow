import * as React from "react"

import { AssistantModal, useAdkAssistant } from "@/components/forge/assistant"
import { agentState } from "./agent-state"

/** The agent's server: this origin when it serves the UI; Vite sends /api there in development. */
const API: string = import.meta.env.VITE_AGENT_API || "/api"
const PERSON = "agent.person"

type Agent = { id: string; name: string; description: string }

/** Who's chatting: an ID kept in this browser, so their conversations come back. */
function personId() {
  try {
    let id = localStorage.getItem(PERSON)
    if (!id) {
      id = crypto.randomUUID()
      localStorage.setItem(PERSON, id)
    }
    return id
  } catch {
    return "guest"
  }
}

/**
 * The agent, as a floating assistant in the page's bottom-right corner. It
 * asks the server which agent it runs (GET /api/agent), then talks to it over
 * ADK's run API, sending `agentState` with every message.
 */
export function AgentAssistant() {
  const [agent, setAgent] = React.useState<Agent | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  React.useEffect(() => {
    fetch(`${API}/agent`)
      .then((response) =>
        response.ok
          ? (response.json() as Promise<Agent>)
          : Promise.reject(new Error(`${response.status} ${response.statusText}`))
      )
      .then(setAgent, (caught: unknown) =>
        setError(caught instanceof Error ? caught.message : String(caught))
      )
  }, [])
  if (error) {
    return (
      <p className="fixed end-4 bottom-4 max-w-80 rounded-lg border bg-background p-3 text-xs text-muted-foreground shadow-sm">
        The agent's server isn't answering ({error}). Start it with{" "}
        <code className="font-mono">uv run main.py</code>.
      </p>
    )
  }
  return agent ? <Assistant agent={agent} /> : null
}

function Assistant({ agent }: { agent: Agent }) {
  const [userId] = React.useState(personId)
  const { runtime, artifacts } = useAdkAssistant({
    adk: { url: API, appName: agent.id, userId },
    runState: () => ({ ...agentState }),
  })
  return (
    <AssistantModal
      runtime={runtime}
      artifacts={artifacts}
      title={agent.name}
      welcome={{
        title: `Talk to ${agent.name}`,
        description: agent.description || undefined,
      }}
    />
  )
}
