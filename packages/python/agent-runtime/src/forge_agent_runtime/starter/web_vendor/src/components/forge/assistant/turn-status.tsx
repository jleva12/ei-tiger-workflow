import { useAuiState } from "@assistant-ui/react"
import { useAdkSessionState } from "@assistant-ui/react-google-adk"

import { CLIENT_TURN_STATE_KEY, clientTurnOutcome } from "./turn-lifecycle"

/** A failed invocation remains visible even when the SDK merged it into partial text. */
export function AdkTurnStatus() {
  const state = useAdkSessionState()
  const running = useAuiState((s) => s.thread.isRunning)
  const outcome = clientTurnOutcome(state[CLIENT_TURN_STATE_KEY])
  if (
    running ||
    !outcome ||
    (outcome.status !== "failed" && outcome.status !== "cancelled")
  )
    return null
  return (
    <div
      role="alert"
      data-slot="assistant-turn-error"
      className="mx-2 rounded-(--radius-card) border border-danger-border bg-danger-surface p-3 text-xs text-danger-foreground"
    >
      <p className="font-medium">
        {outcome.status === "cancelled"
          ? "Reply stopped"
          : "Reply couldn't finish"}
      </p>
      <p className="mt-1 leading-relaxed">
        {outcome.errorMessage || "Please try again."}
      </p>
    </div>
  )
}
