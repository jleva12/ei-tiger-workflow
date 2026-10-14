import * as React from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"

import type { ChipTone } from "@/components/forge/variants"
import { api } from "@/lib/api-instance"
import { isTimestamp, parseTimestamp } from "@/lib/timestamps"
import {
  adkRunKeys,
  isFollowingAdkRun,
  stepsPollMs,
  type AdkRun,
} from "@/features/runs/lib/runs"

/*
 * A run's trace, for the organization's admins (`agents:manage_runs`):
 * every event of the run in its ADK session (`/adk-runs/{run}/trace`), each
 * with what it said and did, and the session's state before and after the
 * run. The state at any event is the state before, with each event's
 * `state_delta` up to it applied in turn.
 */

/* -------------------------------------------------------------------------- */
/* Contract                                                                   */
/* -------------------------------------------------------------------------- */

/** A function an event called: a tool, or ADK's own (a question, a confirmation). */
export type TraceCall = {
  id: string | null
  name: string | null
  args: unknown
}

/** An answer an event carries to a call. */
export type TraceResponse = {
  id: string | null
  name: string | null
  response: unknown
}

/** A file a message carries: its type and size, never its bytes. */
export type TraceFile = {
  name: string | null
  media_type: string | null
  size_bytes?: number
  uri?: string | null
}

/** A model's token usage on an answer. */
export type TraceUsage = {
  prompt_tokens: number | null
  output_tokens: number | null
  thoughts_tokens: number | null
  cached_tokens: number | null
  tool_prompt_tokens: number | null
  total_tokens: number | null
}

/** One event of a run, as its ADK session has it. */
export type TraceEvent = {
  /** Its place among the run's events, from 1. */
  seq: number
  id: string
  /** The ADK invocation (a turn) it's part of. */
  invocation_id: string
  at: string
  /** Who wrote it: `user`, the workflow, an agent. */
  author: string
  role: string | null
  branch: string | null
  /** Its ADK node path, e.g. `steps@1/shape@1`. */
  path: string
  /** The document's step it's at (the innermost), and those it's inside, outermost first. */
  step: string | null
  steps: string[]
  /** It's the step's own event (not a sub-agent's inside it). */
  own: boolean
  text: string | null
  thought: string | null
  calls: TraceCall[]
  responses: TraceResponse[]
  files: TraceFile[]
  output: unknown
  /** It handed something on (`output` may be null). */
  has_output: boolean
  output_for: string[]
  /** What it set in the session's state. */
  state_delta: Record<string, unknown>
  /** The artifacts it saved: name to version. */
  artifact_delta: Record<string, number>
  /** The way a logic step took. */
  route: unknown
  transfer_to_agent: string | null
  escalate: boolean
  error: { code: string | null; message: string | null } | null
  usage: TraceUsage | null
  model: string | null
  /** What Forge rode on it: an Error way's error, a cut-off tool call. */
  metadata: Record<string, unknown> | null
  partial: boolean
  interrupted: boolean
  long_running: string[]
}

export type AdkRunTrace = {
  events: TraceEvent[]
  /** The session's state as the first listed event found it. */
  state_before: Record<string, unknown>
  /** It once the run's events applied. */
  state_after: Record<string, unknown>
  /** The session's state now. */
  state: Record<string, unknown>
  /** A later run in the same session changed the state since. */
  later_changes: boolean
  /** How many of the run's earliest events aren't listed. */
  omitted: number
  invocation_id: string | null
  session_id: string
  earlier_sessions: string[]
  /** What the worker keeps of the run: its session, invocation, journals. */
  worker: Record<string, unknown>
}

/* -------------------------------------------------------------------------- */
/* What an event is                                                           */
/* -------------------------------------------------------------------------- */

/** What an event did, as the trace filters and tags it. */
export type TraceKind =
  | "sent"
  | "message"
  | "thought"
  | "call"
  | "response"
  | "pause"
  | "output"
  | "state"
  | "artifact"
  | "route"
  | "error"

export const TRACE_KINDS: Record<TraceKind, { label: string; tone: ChipTone }> =
  {
    sent: { label: "Called with", tone: "outline" },
    message: { label: "Message", tone: "neutral" },
    thought: { label: "Thought", tone: "outline" },
    call: { label: "Tool call", tone: "notice" },
    response: { label: "Tool answer", tone: "notice" },
    pause: { label: "Pause", tone: "warning" },
    output: { label: "Output", tone: "success" },
    state: { label: "State", tone: "warning" },
    artifact: { label: "Artifact", tone: "neutral" },
    route: { label: "Route", tone: "outline" },
    error: { label: "Error", tone: "danger" },
  }

// ADK's own calls: a question for a person, or a tool waiting to be allowed.
const PAUSE_CALLS = new Set(["adk_request_input", "adk_request_confirmation"])

const present = (value: unknown) =>
  value !== null &&
  value !== undefined &&
  !(Array.isArray(value) && value.length === 0)

/**
 * Where a step that works out what it sends (a saved workflow's inputs, an
 * agent's message, an HTTP request) records it, on an event of its own.
 */
export const CALLED_WITH_KEY = "forge_called_with"

/** What a step recorded it sent, on this event; undefined when it's not that event. */
export const calledWith = (event: Pick<TraceEvent, "metadata">) =>
  event.metadata && CALLED_WITH_KEY in event.metadata
    ? event.metadata[CALLED_WITH_KEY]
    : undefined

/** What an event did, in the order the trace tags it. */
export function eventKinds(event: TraceEvent): TraceKind[] {
  const kinds: TraceKind[] = []
  if (event.error) kinds.push("error")
  if (calledWith(event) !== undefined) kinds.push("sent")
  if (event.text) kinds.push("message")
  if (event.thought) kinds.push("thought")
  if (event.calls.some((call) => PAUSE_CALLS.has(call.name ?? "")))
    kinds.push("pause")
  if (event.calls.some((call) => !PAUSE_CALLS.has(call.name ?? "")))
    kinds.push("call")
  if (event.responses.length) kinds.push("response")
  if (event.has_output) kinds.push("output")
  if (Object.keys(event.state_delta).length) kinds.push("state")
  if (Object.keys(event.artifact_delta).length) kinds.push("artifact")
  if (present(event.route) || event.transfer_to_agent || event.escalate)
    kinds.push("route")
  return kinds
}

/** An event in a line: what it says or did first. */
export function eventSummary(event: TraceEvent): string {
  if (event.error) return event.error.message || event.error.code || "It failed"
  if (event.text) return event.text
  const call = event.calls[0]
  if (call) return `Calls ${call.name ?? "a function"}`
  const response = event.responses[0]
  if (response) return `Answers ${response.name ?? "a call"}`
  if (event.has_output) return `Hands on ${preview(event.output)}`
  const sent = calledWith(event)
  if (sent !== undefined) return `Called with ${preview(sent)}`
  const keys = Object.keys(event.state_delta)
  if (keys.length) return `Sets ${keys.join(", ")}`
  if (present(event.route)) return `Takes ${preview(event.route)}`
  if (event.transfer_to_agent) return `Hands off to ${event.transfer_to_agent}`
  if (event.thought) return event.thought
  return "No content"
}

/** A value in a few words: a string as it is, else short JSON. */
export function preview(value: unknown, length = 80): string {
  const text =
    typeof value === "string" ? value : (JSON.stringify(value) ?? "undefined")
  return text.length > length ? `${text.slice(0, length - 1)}…` : text
}

/** Whether a state key is one Forge keeps for itself (`forge:`). */
export const isPrivateKey = (key: string) => key.startsWith("forge:")

/* -------------------------------------------------------------------------- */
/* Filtering                                                                  */
/* -------------------------------------------------------------------------- */

export type TraceFilter = {
  /** Only events at or inside this step. */
  step?: string
  /** Only events that did one of these. */
  kinds?: TraceKind[]
  /** Text in what it said, called, answered, handed on or set. */
  query?: string
}

/** Whether an event is one the filter keeps. */
export function matchesTrace(event: TraceEvent, filter: TraceFilter): boolean {
  if (filter.step && !event.steps.includes(filter.step)) return false
  if (filter.kinds?.length) {
    const kinds = eventKinds(event)
    if (!filter.kinds.some((kind) => kinds.includes(kind))) return false
  }
  const query = filter.query?.trim().toLowerCase()
  if (query) {
    const haystack = [
      event.author,
      event.path,
      event.text,
      event.thought,
      JSON.stringify(event.calls),
      JSON.stringify(event.responses),
      event.has_output ? JSON.stringify(event.output) : "",
      JSON.stringify(event.state_delta),
      event.error?.message,
    ]
      .filter(Boolean)
      .join("\n")
      .toLowerCase()
    if (!haystack.includes(query)) return false
  }
  return true
}

/* -------------------------------------------------------------------------- */
/* State                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * The session's state once the events up to `seq` applied (none: 0), from
 * the state before the run.
 */
export function stateAt(
  trace: Pick<AdkRunTrace, "events" | "state_before">,
  seq: number
): Record<string, unknown> {
  const state = { ...trace.state_before }
  for (const event of trace.events) {
    if (event.seq > seq) break
    Object.assign(state, event.state_delta)
  }
  return state
}

/** A change to a state key: the event that made it, and the value before and after. */
export type StateChange = {
  key: string
  seq: number
  at: string
  author: string
  step: string | null
  /** Whether the key had a value before (else it was added). */
  existed: boolean
  before: unknown
  after: unknown
}

/** Every change the run's events made to the state, in order. */
export function stateChanges(
  trace: Pick<AdkRunTrace, "events" | "state_before">
): StateChange[] {
  const state: Record<string, unknown> = { ...trace.state_before }
  const changes: StateChange[] = []
  for (const event of trace.events) {
    for (const [key, after] of Object.entries(event.state_delta)) {
      changes.push({
        key,
        seq: event.seq,
        at: event.at,
        author: event.author,
        step: event.step,
        existed: key in state,
        before: state[key],
        after,
      })
      state[key] = after
    }
  }
  return changes
}

/** The state's keys: those it had before, then those the run added, each with how often the run changed it. */
export function stateKeys(
  trace: Pick<AdkRunTrace, "events" | "state_before" | "state_after">
): { key: string; changes: number; added: boolean }[] {
  const counts = new Map<string, number>()
  for (const change of stateChanges(trace))
    counts.set(change.key, (counts.get(change.key) ?? 0) + 1)
  const keys = new Set([
    ...Object.keys(trace.state_before),
    ...Object.keys(trace.state_after),
  ])
  return [...keys].map((key) => ({
    key,
    changes: counts.get(key) ?? 0,
    added: !(key in trace.state_before),
  }))
}

/* -------------------------------------------------------------------------- */
/* Totals                                                                     */
/* -------------------------------------------------------------------------- */

export type TraceTotals = {
  events: number
  stateChanges: number
  calls: number
  errors: number
  modelTurns: number
  promptTokens: number
  outputTokens: number
  totalTokens: number
}

/** What the run's events add up to. */
export function traceTotals(events: TraceEvent[]): TraceTotals {
  const totals: TraceTotals = {
    events: events.length,
    stateChanges: 0,
    calls: 0,
    errors: 0,
    modelTurns: 0,
    promptTokens: 0,
    outputTokens: 0,
    totalTokens: 0,
  }
  for (const event of events) {
    totals.stateChanges += Object.keys(event.state_delta).length
    totals.calls += event.calls.filter(
      (call) => !PAUSE_CALLS.has(call.name ?? "")
    ).length
    if (event.error) totals.errors += 1
    if (event.usage) {
      totals.modelTurns += 1
      totals.promptTokens += event.usage.prompt_tokens ?? 0
      totals.outputTokens += event.usage.output_tokens ?? 0
      totals.totalTokens += event.usage.total_tokens ?? 0
    }
  }
  return totals
}

/** Milliseconds from one event to the next; undefined when either time doesn't read. */
export function gapMs(
  from: string | undefined,
  to: string
): number | undefined {
  if (!from || !isTimestamp(from) || !isTimestamp(to)) return undefined
  return parseTimestamp(to).getTime() - parseTimestamp(from).getTime()
}

/* -------------------------------------------------------------------------- */
/* Query                                                                      */
/* -------------------------------------------------------------------------- */

const tracePath = (organizationId: string, runId: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/adk-runs/${encodeURIComponent(runId)}/trace`

const traceKey = (organizationId: string, runId: string, session?: string) =>
  [...adkRunKeys.all(organizationId), "trace", runId, session ?? ""] as const

/**
 * A run's trace, from its ADK session (or `sessionId`, one it ran in
 * before). Refreshes as its steps do: every few seconds while the run goes,
 * and once more whenever the run itself changes.
 */
export function useAdkRunTrace(
  organizationId: string,
  run: Pick<AdkRun, "id" | "status" | "updated_at"> | undefined,
  { enabled = true, sessionId }: { enabled?: boolean; sessionId?: string } = {}
) {
  const client = useQueryClient()
  const runId = run?.id ?? ""
  const query = useQuery({
    queryKey: traceKey(organizationId, runId, sessionId),
    queryFn: ({ signal }) =>
      api.get<AdkRunTrace>(tracePath(organizationId, runId), {
        params: sessionId ? { session_id: sessionId } : undefined,
        signal,
      }),
    enabled: enabled && Boolean(organizationId && runId),
    refetchInterval: (query) =>
      query.state.status === "error" || sessionId
        ? false
        : stepsPollMs(run, isFollowingAdkRun(organizationId, runId)),
    meta: { errorTitle: "Couldn't refresh the run's trace" },
  })

  // The run moved on: read again.
  const updated = run?.updated_at
  const seen = React.useRef(updated)
  React.useEffect(() => {
    if (seen.current === updated) return
    const first = seen.current === undefined
    seen.current = updated
    if (!first)
      void client.invalidateQueries({
        queryKey: traceKey(organizationId, runId, sessionId),
      })
  }, [client, organizationId, runId, sessionId, updated])

  return query
}
