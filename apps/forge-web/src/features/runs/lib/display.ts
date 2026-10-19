import type { SymbolKind, TaskStatus } from "@/components/forge/variants"
import { formatRelative } from "@/lib/format"
import {
  waitsOnlyForRuns,
  type AdkRun,
  type AdkRunDetail,
  type AdkRunStatus,
} from "@/features/runs/lib/runs"

/*
 * How workflow runs read in lists (a workflow's Runs menu, the
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

/**
 * A run's badge and label; a run waiting for an answer, for a run it started
 * (nobody's to decide: it's waiting, not for review), or being stopped, says so.
 */
export function runStatusDisplay(
  run: Pick<AdkRun, "status" | "pause"> &
    Partial<Pick<AdkRun, "cancel_requested_at" | "gates">>
) {
  const shown = STATUS[run.status] ?? { status: "pending", label: "Unknown" }
  if (waitsOnlyForRuns(run))
    return { status: "pending" as TaskStatus, label: "Waiting for a run" }
  if (run.status === "paused" && run.pause?.kind === "human_input")
    return { ...shown, label: "Awaiting an answer" }
  if (run.status === "running" && run.cancel_requested_at)
    return { ...shown, label: "Stopping" }
  return shown
}

/**
 * Whether a run is an A2A task's: started over A2A (its task's ID is its
 * own), not resubmitted from one. Its task ended for good when it did, as A2A
 * has it, so it isn't retried: it's resubmitted, as a run of its own.
 */
export const isA2aTask = (run: Pick<AdkRunDetail, "trigger" | "resubmit_of">) =>
  run.trigger?.type === "runtime" &&
  run.trigger.protocol === "a2a" &&
  !run.resubmit_of

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
 * time it waits for, its being stopped, the hiccup it's retried after, or
 * why it failed; undefined when nothing does.
 */
export function runNote(
  run: Pick<AdkRun, "status" | "pause" | "waiting_until" | "error"> &
    Partial<Pick<AdkRun, "cancel_requested_at" | "gates">>
) {
  if (run.status === "running" && run.cancel_requested_at)
    return "Abandoned: its worker stops it"
  if (waitsOnlyForRuns(run)) {
    const child = run.pause?.details.child_run_id
    return child
      ? `Waiting for run ${child.slice(0, 8)} to end`
      : "Waiting for a run it started"
  }
  if (run.status === "paused")
    return run.pause?.kind === "human_input"
      ? "Waiting for an answer"
      : "Waiting for a decision"
  if (run.status === "waiting" && run.waiting_until)
    return `Carries on ${formatRelative(run.waiting_until)}`
  if (run.error && run.status === "failed") return run.error.message
  if (run.error && isLiveRun(run)) return `Retrying: ${run.error.message}`
  return undefined
}

/** What a run is doing, in the Runs menu's words. */
export const runLine = (
  run: Pick<
    AdkRun,
    "status" | "pause" | "waiting_until" | "error" | "started_at" | "created_at"
  > &
    Partial<Pick<AdkRun, "cancel_requested_at" | "gates">>
) =>
  runNote(run) ?? `Started ${formatRelative(run.started_at ?? run.created_at)}`

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
export const runsPollMs = (state: {
  status: string
  data?: { items: Pick<AdkRun, "status">[] }
}) =>
  state.status === "error"
    ? false
    : state.data?.items.some(isOpenRun)
      ? OPEN_POLL_MS
      : IDLE_POLL_MS
