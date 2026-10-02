import type { SymbolKind, TaskStatus } from "@/components/forge/variants"
import { isLiveStatus, type BackgroundTask } from "@/lib/background-tasks"
import { formatRelative } from "@/lib/format"

/*
 * How runs read in lists (an ADK workflow's Runs menu, the Runs tab): what
 * stands out about each, their status groups, and how often a list
 * refreshes while one is still going.
 */

/**
 * What stands out about a run beside its status: the decision or time it
 * waits for, or why it failed; undefined when nothing does.
 */
export function runNote(run: BackgroundTask) {
  if (run.status === "AWAITING_VALIDATION") return "Waiting for a decision"
  if (run.status === "STOPPED" && run.waiting_until)
    return `Carries on ${formatRelative(run.waiting_until)}`
  if (run.failure && run.status !== "COMPLETED") return run.failure.message
  return undefined
}

/** What a run is doing, in the Runs menu's words. */
export const runLine = (run: BackgroundTask) =>
  runNote(run) ?? `Started ${formatRelative(run.started_at ?? run.created_at)}`

export type RunGroup = {
  id: TaskStatus
  title: string
  symbol: SymbolKind
}

/**
 * Runs' status groups, in the order work moves, one per badge
 * status (`statusDisplay`), so a run's badge always matches its band.
 */
export const RUN_GROUPS: RunGroup[] = [
  { id: "pending", title: "Queued · Waiting", symbol: "backlog" },
  { id: "running", title: "Running", symbol: "progress" },
  { id: "review", title: "Awaiting approval", symbol: "review" },
  { id: "completed", title: "Completed", symbol: "completed" },
  { id: "failed", title: "Failed", symbol: "failed" },
  { id: "cancelled", title: "Stopped", symbol: "cancelled" },
]

/** A run still going: queued, running, or waiting for a time or a person. */
export const isOpenRun = (run: BackgroundTask) =>
  isLiveStatus(run.status) ||
  run.status === "AWAITING_VALIDATION" ||
  Boolean(run.status === "STOPPED" && run.waiting_until)

// Runs show up once a worker picks them up, seconds after they're started.
/** A list of runs refreshes this often while one of them is still going… */
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
  data?: { items: BackgroundTask[] }
}) =>
  state.status === "error"
    ? false
    : state.data?.items.some(isOpenRun)
      ? OPEN_POLL_MS
      : IDLE_POLL_MS
