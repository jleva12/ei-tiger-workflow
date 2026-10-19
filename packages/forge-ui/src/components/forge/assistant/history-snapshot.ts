import {
  AdkEventAccumulator,
  type AdkEvent,
  type AdkMessage,
  type AdkThreadSnapshot,
} from "@assistant-ui/react-google-adk"

import {
  CLIENT_TURN_STATE_KEY,
  TURN_METADATA_KEY,
  adkEventFailure,
  adkEventsForDisplay,
  clientTurnOutcome,
  normalizeAdkEvent,
  type ClientTurnOutcome,
} from "./turn-lifecycle.ts"

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

/** Reject malformed history atomically; a partly replayed session looks believable but is wrong. */
function parseEvent(value: unknown, index: number): AdkEvent {
  const invalid = (field: string) => {
    throw new Error(`Invalid ADK session event at index ${index}: ${field}.`)
  }
  if (!record(value) || !Object.keys(value).length)
    return invalid("expected a non-empty object")
  const id = value.id
  if (
    id != null &&
    typeof id !== "string" &&
    !(typeof id === "number" && Number.isFinite(id))
  )
    return invalid('expected "id" to be a string or finite number')
  for (const key of [
    "author",
    "invocationId",
    "invocation_id",
    "errorCode",
    "error_code",
    "errorMessage",
    "error_message",
    "finishReason",
    "finish_reason",
  ])
    if (value[key] != null && typeof value[key] !== "string")
      return invalid(`expected "${key}" to be a string`)
  for (const key of ["partial", "interrupted", "turnComplete", "turn_complete"])
    if (value[key] != null && typeof value[key] !== "boolean")
      return invalid(`expected "${key}" to be a boolean`)
  for (const key of ["actions", "customMetadata", "custom_metadata"])
    if (value[key] != null && !record(value[key]))
      return invalid(`expected "${key}" to be an object`)
  if (record(value.actions))
    for (const key of [
      "stateDelta",
      "state_delta",
      "artifactDelta",
      "artifact_delta",
    ])
      if (value.actions[key] != null && !record(value.actions[key]))
        return invalid(`expected "actions.${key}" to be an object`)
  const content = value.content
  if (content != null) {
    if (!record(content)) return invalid('expected "content" to be an object')
    if (content.parts != null) {
      if (!Array.isArray(content.parts) || !content.parts.every(record))
        return invalid('expected "content.parts" to be an array of objects')
      for (const part of content.parts) {
        if (part.text != null && typeof part.text !== "string")
          return invalid('expected part "text" to be a string')
        for (const key of [
          "functionCall",
          "function_call",
          "functionResponse",
          "function_response",
          "inlineData",
          "inline_data",
          "fileData",
          "file_data",
        ])
          if (part[key] != null && !record(part[key]))
            return invalid(`expected part "${key}" to be an object`)
      }
    }
  }
  return normalizeAdkEvent({
    ...value,
    id: id == null ? `history-event-${index}` : String(id),
    ...(typeof value.error === "string" &&
      value.errorMessage == null &&
      value.error_message == null && { errorMessage: value.error }),
  } as AdkEvent)
}

/** The SDK joins adjacent AI/tool messages using the first AI status only. */
function preserveIncompleteStatuses(messages: AdkMessage[]): AdkMessage[] {
  const result = [...messages]
  let group: number[] = []
  const finish = () => {
    const messages = group.map((index) => result[index])
    const failed =
      messages.find(
        (message) =>
          message?.type === "ai" &&
          message.status?.type === "incomplete" &&
          ["error", "length", "content-filter"].includes(message.status.reason)
      ) ??
      messages.find(
        (message) =>
          message?.type === "ai" &&
          message.status?.type === "incomplete" &&
          message.status.reason === "cancelled"
      )
    if (failed?.type === "ai")
      for (const index of group) {
        const message = result[index]
        if (message?.type === "ai")
          result[index] = { ...message, status: failed.status }
      }
    group = []
  }
  for (const [index, message] of result.entries()) {
    if (message.type === "human") finish()
    else if (message.type === "ai") group.push(index)
  }
  finish()
  return result
}

/** Cleanup can append a control-only outcome after a newer turn has already run. */
function placeTerminalOutcomes(events: AdkEvent[]): AdkEvent[] {
  const lastInvocationEvent = new Map<string, number>()
  const after = new Map<number, AdkEvent[]>()
  const moved = new Set<number>()
  for (const [index, event] of events.entries()) {
    const metadata = event.customMetadata
    const terminal = clientTurnOutcome(metadata?.[TURN_METADATA_KEY])
    const hasActions = Object.values(event.actions ?? {}).some((value) =>
      record(value)
        ? Object.keys(value).length > 0
        : Array.isArray(value)
          ? value.length > 0
          : Boolean(value)
    )
    const controlOnly =
      terminal &&
      terminal.status !== "running" &&
      Object.keys(metadata ?? {}).every((key) => key === TURN_METADATA_KEY) &&
      !event.content?.parts?.length &&
      !event.errorCode &&
      !event.errorMessage &&
      !event.finishReason &&
      !event.interrupted &&
      !hasActions &&
      event.author !== "user"
    const id = event.invocationId
    if (controlOnly) {
      const prior = id ? lastInvocationEvent.get(id) : undefined
      if (prior !== undefined) {
        const outcomes = after.get(prior) ?? []
        outcomes.push(event)
        after.set(prior, outcomes)
        moved.add(index)
      }
      continue
    }
    if (id) lastInvocationEvent.set(id, index)
  }
  // Only relocate control envelopes with an exact, earlier invocation match.
  // Content/state events keep their original order; timestamps are never used.
  return events.flatMap((event, index) => [
    ...(moved.has(index) ? [] : [event]),
    ...(after.get(index) ?? []),
  ])
}

/** Reconstruct all ADK state, preserving failed response text and the last turn's outcome. */
export function snapshotOfAdkEvents(
  events: readonly unknown[]
): AdkThreadSnapshot {
  if (!Array.isArray(events))
    throw new Error("Invalid ADK session history: expected an array of events.")
  const parsed = placeTerminalOutcomes(events.map(parseEvent))
  const accumulator = new AdkEventAccumulator()
  let invocationId: string | undefined
  let outcome: ClientTurnOutcome = { status: "succeeded" }
  for (const event of parsed) {
    const incomingId = event.invocationId
    // User events also delimit turns in older histories lacking invocation IDs.
    if (
      event.author === "user" ||
      (incomingId && incomingId !== invocationId)
    ) {
      outcome = { status: "succeeded" }
      invocationId = incomingId
    }
    if (incomingId) invocationId = incomingId
    for (const displayed of adkEventsForDisplay(event))
      accumulator.processEvent(displayed)
    const failure = adkEventFailure(event)
    const terminal = clientTurnOutcome(
      event.customMetadata?.[TURN_METADATA_KEY]
    )
    if (failure) {
      outcome = { status: "failed", ...failure }
      // Contentless filter/length stops otherwise produce no assistant reply.
      if (!event.errorCode && !event.errorMessage)
        accumulator.processEvent({ id: `${event.id}:failure`, ...failure })
    } else if (outcome.status !== "failed") {
      if (terminal?.status === "failed") {
        outcome = terminal
        accumulator.processEvent({
          id: `${event.id}:failure`,
          errorCode: terminal.errorCode ?? "AGENT_ERROR",
          errorMessage:
            terminal.errorMessage ??
            "The assistant couldn't finish this reply. Please try again.",
        })
      } else if (terminal?.status === "cancelled" || event.interrupted) {
        const message =
          terminal?.errorMessage ||
          "The reply was stopped before it finished. Please try again."
        if (outcome.status !== "cancelled") {
          // Keep the stop explanation on this turn after a later turn clears
          // the latest-outcome banner. Finalize first so it cannot replace text.
          accumulator.processEvent({
            id: `${event.id}:stop`,
            interrupted: true,
          })
          accumulator.processEvent({
            id: `${event.id}:notice`,
            partial: true,
            content: { parts: [{ text: message }] },
          })
          accumulator.processEvent({
            id: `${event.id}:stopped`,
            interrupted: true,
          })
        }
        outcome = { ...terminal, status: "cancelled", errorMessage: message }
      }
    }
  }
  return {
    messages: preserveIncompleteStatuses(accumulator.getMessages()),
    longRunningToolIds: accumulator.getLongRunningToolIds(),
    toolConfirmations: accumulator.getToolConfirmations(),
    authRequests: accumulator.getAuthRequests(),
    escalated: accumulator.isEscalated(),
    messageMetadata: accumulator.getMessageMetadata(),
    stateDelta: {
      ...accumulator.getStateDelta(),
      [CLIENT_TURN_STATE_KEY]: outcome,
    },
    artifactDelta: accumulator.getArtifactDelta(),
    agentInfo: accumulator.getAgentInfo(),
  }
}

type Headers =
  | Record<string, string>
  | (() => Record<string, string> | Promise<Record<string, string>>)

async function requestHeaders(
  headers: Headers | undefined,
  signal?: AbortSignal
): Promise<Record<string, string>> {
  signal?.throwIfAborted()
  if (typeof headers !== "function") return headers ?? {}
  if (!signal) return headers()
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason)
    signal.addEventListener("abort", abort, { once: true })
    Promise.resolve()
      .then(headers)
      .then(resolve, reject)
      .finally(() => signal.removeEventListener("abort", abort))
  })
}

/** Session loader used by every live ADK chat; headers are read afresh per request. */
export function createAdkHistoryLoader({
  url,
  appName,
  userId,
  headers,
}: {
  url: string
  appName: string
  userId: string
  headers?: Headers
}) {
  const base = `${url.replace(/\/+$/, "")}/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions`
  return async (
    sessionId: string,
    options?: { signal?: AbortSignal }
  ): Promise<AdkThreadSnapshot> => {
    const signal = options?.signal
    const resolved = await requestHeaders(headers, signal)
    signal?.throwIfAborted()
    const response = await fetch(`${base}/${encodeURIComponent(sessionId)}`, {
      headers: resolved,
      signal,
    })
    if (!response.ok)
      throw new Error(`Failed to load conversation: ${response.status}`)
    const session: unknown = await response.json()
    signal?.throwIfAborted()
    if (
      !record(session) ||
      typeof session.id !== "string" ||
      !session.id ||
      session.id !== sessionId
    )
      throw new Error(
        'Invalid ADK session load response: expected the requested session "id".'
      )
    if (session.events !== undefined && !Array.isArray(session.events))
      throw new Error(
        'Invalid ADK session load response: expected "events" to be an array.'
      )
    return snapshotOfAdkEvents(session.events ?? [])
  }
}
