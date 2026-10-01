import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type { SymbolKind, TaskStatus } from "@/components/forge/variants"
import { api } from "@/lib/api-instance"
import {
  backgroundTasks,
  isLiveStatus,
  useBackgroundTasks,
  type BackgroundTask,
  type BackgroundTaskPage,
} from "@/lib/background-tasks"
import { formatRelative } from "@/lib/format"
import { workflowPath } from "./api"

/*
 * Running an organization's workflow. A run takes the workflow as it's saved
 * when it starts (editing it later never changes a run in progress), acts as
 * the member who ran it (the workflows it starts run as them too), and is
 * one of the organization's background tasks (type `workflows`) in the API. The
 * console lists runs on the Workflows page, as its Workflow tasks, not
 * under Background tasks: that's where they're followed, retried, and where
 * their approvals are decided. Running takes `workflows:run`.
 */

/** A workflow run's background task type. */
export const WORKFLOW_TASK_TYPE = "workflows"

/** The Workflows page's tabs; Overview is the default. */
export type WorkflowsTab = "overview" | "tasks"

export const isWorkflowsTab = (value: unknown): value is WorkflowsTab =>
  value === "overview" || value === "tasks"

/** The job that will run it (the 202's body). */
export type RunStarted = {
  queue: string
  key: string
  workflow_id: string
  /** The revision it runs. */
  revision: number
}

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
 * Workflow tasks' status groups, in the order work moves, one per badge
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

/**
 * The organization's workflow runs, newest first, 100 at a time (`fetchNextPage`
 * for more): its background tasks of type `workflows`. Refreshes every 5 s
 * while one is in progress, else every 30 s.
 */
export const useOrganizationWorkflowRuns = (organizationId: string) =>
  useBackgroundTasks(
    organizationId,
    { task_type: [WORKFLOW_TASK_TYPE] },
    { errorTitle: "Couldn't refresh the workflow tasks" }
  )

/** A run still going: queued, running, or waiting for a time or a person. */
export const isOpenRun = (run: BackgroundTask) =>
  isLiveStatus(run.status) ||
  run.status === "AWAITING_VALIDATION" ||
  Boolean(run.status === "STOPPED" && run.waiting_until)

const runsKey = (organizationId: string, workflowId: string) => [
  ...backgroundTasks.scope({ organizationId }).keys.all,
  "workflow-runs",
  workflowId,
]

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

/**
 * A workflow's latest runs, newest first: its background tasks. Refreshes
 * every few seconds while one is still going. Under the organization's background
 * tasks' keys, so acting on one refreshes these too.
 */
export function useWorkflowRuns(
  organizationId: string,
  workflowId: string,
  { enabled = true, limit = 10 }: { enabled?: boolean; limit?: number } = {}
) {
  return useQuery({
    queryKey: [...runsKey(organizationId, workflowId), limit],
    queryFn: ({ signal }) =>
      api.get<BackgroundTaskPage>(`${workflowPath(organizationId, workflowId)}/runs`, {
        params: { limit },
        signal,
      }),
    enabled: enabled && Boolean(organizationId && workflowId),
    refetchInterval: (query) => runsPollMs(query.state),
    meta: { errorTitle: "Couldn't load the workflow's runs" },
  })
}

/**
 * Run the workflow as it's saved, with an input that fits its start step
 * (the API answers 422 with why when it doesn't). Errors are the caller's
 * to show: the run dialog shows them by the input.
 */
export function useRunWorkflow(organizationId: string, workflowId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (input: unknown) =>
      api.post<RunStarted>(`${workflowPath(organizationId, workflowId)}/runs`, { input }),
    meta: { silent: true },
    onSuccess: () => {
      // Seconds later a worker picks it up; poll until it shows.
      void client.invalidateQueries({ queryKey: runsKey(organizationId, workflowId) })
      window.setTimeout(
        () => void client.invalidateQueries({ queryKey: runsKey(organizationId, workflowId) }),
        OPEN_POLL_MS
      )
    },
  })
}
