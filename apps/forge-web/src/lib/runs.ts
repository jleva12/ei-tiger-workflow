import type { SymbolKind, TaskStatus } from "@/components/forge/variants"
import { formatRelative } from "@/lib/format"
import type { AdkRun, AdkRunStatus } from "@/lib/agents/runs"

/*
 * How ADK workflow runs read in lists (an ADK workflow's Runs menu, the
 * Runs tab) and on their page: each status's badge and label, what stands
 * out about a run, the lists' status groups, and how often a run or a list
 * refreshes while one is still going.
 */

/** Each status on the Forge status badges. */
const STATUS: Record<AdkRunStatus, { status: TaskStatus; label: string }> = {
  queued: { status: "pending", label: "Queued" },
  running: { status: "running", label: "Running" },
  paused: { status: "review", label: "Awaiting approval" },
  waiting: { status: "pending", label: "Waiting" },
  succeeded: { status: "completed", label: "Succeeded" },
  failed: { status: "failed", label: "Failed" },
  abandoned: { status: "cancelled", label: "Abandoned" },
}

/** A run's badge and label; a run waiting for an answer says so. */
export function runStatusDisplay(run: Pick<AdkRun, "status" | "pause">) {
  const shown = STATUS[run.status] ?? { status: "pending", label: "Unknown" }
  return run.status === "paused" && run.pause?.kind === "human_input"
    ? { ...shown, label: "Awaiting an answer" }
    : shown
}

/** Waiting for a worker or on one: queued or running. */
export const isLiveRun = (run: Pick<AdkRun, "status"> | undefined) =>
  run?.status === "queued" || run?.status === "running"

/** A run still going: queued, running, or waiting for a person or a time. */
export const isOpenRun = (run: Pick<AdkRun, "status">) =>
  run.status === "queued" ||
  run.status === "running" ||
  run.status === "paused" ||
  run.status === "waiting"

/**
 * What stands out about a run beside its status: the decision, answer or
 * time it waits for, the hiccup it's retried after, or why it failed;
 * undefined when nothing does.
 */
export function runNote(run: Pick<AdkRun, "status" | "pause" | "waiting_until" | "error">) {
  if (run.status === "paused")
    return run.pause?.kind === "human_input" ? "Waiting for an answer" : "Waiting for a decision"
  if (run.status === "waiting" && run.waiting_until)
    return `Carries on ${formatRelative(run.waiting_until)}`
  if (run.error && run.status === "failed") return run.error.message
  if (run.error && isLiveRun(run)) return `Retrying: ${run.error.message}`
  return undefined
}

/** What a run is doing, in the Runs menu's words. */
export const runLine = (
  run: Pick<AdkRun, "status" | "pause" | "waiting_until" | "error" | "started_at" | "created_at">
) => runNote(run) ?? `Started ${formatRelative(run.started_at ?? run.created_at)}`

export type RunGroup = {
  id: TaskStatus
  title: string
  symbol: SymbolKind
}

/**
 * Runs' status groups, in the order work moves, one per badge status
 * (`runStatusDisplay`), so a run's badge always matches its band.
 */
export const RUN_GROUPS: RunGroup[] = [
  { id: "pending", title: "Queued · Waiting", symbol: "backlog" },
  { id: "running", title: "Running", symbol: "progress" },
  { id: "review", title: "Waiting for a person", symbol: "review" },
  { id: "completed", title: "Succeeded", symbol: "completed" },
  { id: "failed", title: "Failed", symbol: "failed" },
  { id: "cancelled", title: "Abandoned", symbol: "cancelled" },
]

// Runs show up as soon as they're started; a worker takes them seconds later.
/** A run, or a list of runs, refreshes this often while one is still going… */
export const OPEN_POLL_MS = 4_000
/** …and this often otherwise. */
export const IDLE_POLL_MS = 30_000

/**
 * How often a list of runs refreshes: every few seconds while one is still
 * going, else every 30 s; not once it failed (one error notice, then the
 * window regaining focus tries again).
 */
export const runsPollMs = (state: { status: string; data?: { items: Pick<AdkRun, "status">[] } }) =>
  state.status === "error" ? false : state.data?.items.some(isOpenRun) ? OPEN_POLL_MS : IDLE_POLL_MS
