import type {
  AdkEvent,
  AdkStreamCallback,
} from "@assistant-ui/react-google-adk"

/** Forge's server-owned invocation envelope. */
export const TURN_METADATA_KEY = "forge:turn"
/** Browser-only response state. Never send this key back to an agent. */
export const CLIENT_TURN_STATE_KEY = "forge:turn:client"

type TurnStatus = "running" | "succeeded" | "failed" | "cancelled"
export type ClientTurnOutcome = {
  status: TurnStatus
  errorCode?: string
  errorMessage?: string
}
type ServerTurnOutcome = ClientTurnOutcome & { version: 1; runId: string }
type Failure = { errorCode: string; errorMessage: string }

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

export function clientTurnOutcome(
  value: unknown
): ClientTurnOutcome | undefined {
  if (
    !record(value) ||
    typeof value.status !== "string" ||
    !["running", "succeeded", "failed", "cancelled"].includes(
      String(value.status)
    )
  )
    return undefined
  return {
    status: value.status as TurnStatus,
    ...(typeof value.errorCode === "string" && { errorCode: value.errorCode }),
    ...(typeof value.errorMessage === "string" && {
      errorMessage: value.errorMessage,
    }),
  }
}

const serverTurnOutcome = (value: unknown): ServerTurnOutcome | undefined => {
  const outcome = clientTurnOutcome(value)
  return outcome &&
    record(value) &&
    value.version === 1 &&
    typeof value.runId === "string" &&
    /^[a-f0-9]{32}$/i.test(value.runId)
    ? { ...outcome, version: 1, runId: value.runId }
    : undefined
}

const filteredReasons = new Set([
  "SAFETY",
  "RECITATION",
  "BLOCKLIST",
  "PROHIBITED_CONTENT",
  "SPII",
  "LANGUAGE",
  "IMAGE_SAFETY",
  "IMAGE_RECITATION",
  "IMAGE_OTHER",
  "IMAGE_PROHIBITED_CONTENT",
])

const finishFailure = (reason: string | undefined): Failure | undefined => {
  if (reason === "MAX_TOKENS")
    return {
      errorCode: reason,
      errorMessage:
        "The reply reached the model's output limit before it finished. Try asking for a shorter answer or continue the reply.",
    }
  if (reason && filteredReasons.has(reason))
    return {
      errorCode: reason,
      errorMessage:
        "The model's safety filters stopped this reply. Try rephrasing your request.",
    }
  if (reason === "MALFORMED_FUNCTION_CALL" || reason === "UNEXPECTED_TOOL_CALL")
    return {
      errorCode: reason,
      errorMessage:
        "The model couldn't complete a tool call. Please try again.",
    }
  return undefined
}

const interrupted: Failure = {
  errorCode: "STREAM_INTERRUPTED",
  errorMessage:
    "The connection ended before the assistant finished replying. Please try again.",
}
const invalidOutcome: Failure = {
  errorCode: "INVALID_TURN_OUTCOME",
  errorMessage:
    "The assistant returned an incomplete response. Please try again.",
}

/** Read both shapes accepted by ADK, without leaving aliases that override a split error event. */
export function normalizeAdkEvent(event: AdkEvent): AdkEvent {
  const raw = event as AdkEvent & Record<string, unknown>
  const result = { ...raw }
  for (const [camel, snake] of [
    ["errorCode", "error_code"],
    ["errorMessage", "error_message"],
    ["finishReason", "finish_reason"],
    ["customMetadata", "custom_metadata"],
    ["invocationId", "invocation_id"],
  ] as const) {
    if (result[camel] === undefined && raw[snake] !== undefined)
      (result as Record<string, unknown>)[camel] = raw[snake]
    delete result[snake]
  }
  if (result.actions) {
    const actions = result.actions as AdkEvent["actions"] &
      Record<string, unknown>
    const state = actions.stateDelta ?? actions.state_delta
    const clean = { ...actions }
    delete clean.state_delta
    if (record(state)) {
      clean.stateDelta = { ...state }
      delete clean.stateDelta[CLIENT_TURN_STATE_KEY]
    }
    result.actions = clean
  }
  return result
}

/** The same error interpretation for live replies and persisted history. */
export function adkEventFailure(event: AdkEvent): Failure | undefined {
  if (event.errorCode || event.errorMessage)
    return {
      errorCode: event.errorCode || "AGENT_ERROR",
      errorMessage:
        event.errorMessage ||
        "The assistant couldn't finish this reply. Please try again.",
    }
  return finishFailure(event.finishReason)
}

/** The SDK skips content on error/interruption events, so replay that content first. */
export function adkEventsForDisplay(raw: AdkEvent): AdkEvent[] {
  const event = normalizeAdkEvent(raw)
  const failed = Boolean(event.errorCode || event.errorMessage)
  // A failure must not be swallowed by the SDK's earlier interruption branch.
  const terminal =
    failed && event.interrupted ? { ...event, interrupted: false } : event
  if ((failed || event.interrupted) && event.content?.parts?.length)
    return [
      {
        ...event,
        errorCode: undefined,
        errorMessage: undefined,
        interrupted: false,
      },
      { ...terminal, content: undefined },
    ]
  return [terminal]
}

const outcomeEvent = (outcome: ClientTurnOutcome): AdkEvent => ({
  id: crypto.randomUUID(),
  actions: { stateDelta: { [CLIENT_TURN_STATE_KEY]: outcome } },
})

/**
 * Keep invocation completion separate from an individual agent's final message.
 * Generic ADK servers may omit Forge's envelope; Forge connections require it.
 * The local outcome survives the SDK joining partial text and error messages.
 */
export function withTurnLifecycle(
  stream: AdkStreamCallback,
  { requireTurnOutcome = false }: { requireTurnOutcome?: boolean } = {}
): AdkStreamCallback {
  return async function* (messages, config) {
    // Closing a completed response must not look like the user's Stop signal
    // to the SDK: only this transport owns and aborts the child request.
    const request = new AbortController()
    const abortRequest = () => request.abort(config.abortSignal.reason)
    config.abortSignal.addEventListener("abort", abortRequest, { once: true })
    try {
      if (config.abortSignal.aborted) return
      yield outcomeEvent({ status: "running" })
      const stateDelta = { ...config.stateDelta }
      delete stateDelta[CLIENT_TURN_STATE_KEY]
      let runId: string | undefined
      let terminal: ServerTurnOutcome | undefined
      let failure: Failure | undefined
      let emittedError = false
      try {
        const events = await stream(messages, {
          ...config,
          stateDelta,
          abortSignal: request.signal,
        })
        for await (const raw of events) {
          if (config.abortSignal.aborted) return
          const event = normalizeAdkEvent(raw)
          const metadata = event.customMetadata
          if (metadata && Object.hasOwn(metadata, TURN_METADATA_KEY)) {
            const outcome = serverTurnOutcome(metadata[TURN_METADATA_KEY])
            if (
              !outcome ||
              (outcome.status === "running"
                ? runId !== undefined
                : runId !== outcome.runId)
            ) {
              failure = invalidOutcome
              request.abort()
              break
            }
            if (outcome.status === "running") {
              runId = outcome.runId
              // Envelopes have no conversation content or agent-owned state.
              continue
            }
            terminal = outcome
            // The outcome is authoritative. Do not wait for a peer/proxy to close
            // its socket; aborting the child also releases the fetch body reader.
            request.abort()
            break
          }
          if (event.errorCode || event.errorMessage) {
            failure = adkEventFailure(event)
            // The ADK accumulator ignores content on an error event. Preserve
            // final full text first: normal ADK semantics replace partial text.
            yield* adkEventsForDisplay(event)
            emittedError = true
          } else {
            failure = finishFailure(event.finishReason) ?? failure
            yield* adkEventsForDisplay(event)
          }
        }
        if (config.abortSignal.aborted) return
        if ((requireTurnOutcome || runId) && !terminal) failure ??= interrupted
      } catch (error) {
        // Stop/replacement is deliberate. A received terminal outcome already
        // establishes the result even if the transport fails while closing.
        if (config.abortSignal.aborted) return
        if (!terminal)
          failure ??= {
            errorCode: "REQUEST_FAILED",
            errorMessage: `The assistant couldn't complete the request: ${error instanceof Error ? error.message : String(error)}`,
          }
      }
      if (terminal?.status === "failed")
        failure = {
          errorCode: terminal.errorCode || failure?.errorCode || "AGENT_ERROR",
          errorMessage:
            terminal.errorMessage ||
            failure?.errorMessage ||
            "The assistant couldn't finish this reply. Please try again.",
        }
      if (failure) {
        const event = outcomeEvent({ status: "failed", ...failure })
        yield emittedError ? event : { ...event, ...failure }
      } else if (terminal?.status === "cancelled") {
        yield outcomeEvent({
          status: "cancelled",
          errorMessage:
            terminal.errorMessage ||
            "The reply was stopped before it finished. Please try again.",
        })
      } else {
        yield outcomeEvent({ status: "succeeded" })
      }
    } finally {
      config.abortSignal.removeEventListener("abort", abortRequest)
      request.abort()
    }
  }
}
