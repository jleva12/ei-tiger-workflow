import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type QueryClient,
} from "@tanstack/react-query"

import type { IconProp } from "@/components/forge/icons"
import type { ChipTone, TaskStatus } from "@/components/forge/variants"
import { toast } from "@/components/ui/toast"
import { AGENTS_ICON } from "@/lib/agents/model"
import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"

/*
 * An organization's background tasks: the jobs the async worker runs for it
 * (ADK workflow runs), from the admin API's
 * `/organizations/{organizationId}/background-tasks`. Each task is one tracked
 * run history with one or more attempts (runs). Reading takes
 * `organizations:read` in the organization; resubmitting, restarting and
 * abandoning take `background_tasks:manage` (its admins). Times are ISO-8601
 * UTC strings; IDs are opaque. The ADK workflows page lists them as its Runs
 * (lib/agents/runs), which decides their approvals.
 */

/* -------------------------------------------------------------------------- */
/* Contract                                                                   */
/* -------------------------------------------------------------------------- */

/** A task's (or an attempt's, or a step's) status, as the worker reports it. */
export type BackgroundTaskStatus =
  | "PENDING"
  | "RUNNING"
  | "STOPPING"
  | "PAUSING"
  | "PAUSED"
  | "AWAITING_VALIDATION"
  | "STOPPED"
  | "COMPLETED"
  | "FAILED"
  | "ABANDONED"
  | "UNKNOWN"

/** The job's own result, once it finished. */
export type BackgroundTaskOutcome = "ok" | "skipped" | "superseded" | "failed"

/**
 * Why an attempt failed: `transient` is retried automatically (network, rate
 * limit, database failover); `permanent` can't succeed with this input (a
 * bad file, no access); `interrupted` means its worker died or stopped
 * mid-run (restarted automatically); `error` is a bug (not retried).
 */
export type FailureCategory =
  "transient" | "permanent" | "interrupted" | "error"

/** A task's latest attempt's last failure, as the list shows it. */
export type BackgroundTaskFailure = {
  /** The exception's type, e.g. `forge_tasks.errors.StorageError`. */
  type: string
  message: string
  category: FailureCategory
  occurred_at: string
}

export type BackgroundTask = {
  /** The task (instance) ID. */
  id: string
  /** E.g. `workflows.run`. */
  job_name: string
  /** E.g. `workflows`. */
  task_type: string
  /** E.g. `run`. */
  kind: string
  /** A label from the task package, such as the workflow's name; or null. */
  description: string | null
  status: BackgroundTaskStatus
  outcome: BackgroundTaskOutcome | null
  /** How many attempts (runs) so far. */
  attempts: number
  created_at: string
  updated_at: string
  /** The latest attempt's; null before one starts. */
  started_at: string | null
  ended_at: string | null
  duration_ms: number | null
  failure: BackgroundTaskFailure | null
  /**
   * A stopped task that waits for a time (a workflow's delay, or its next
   * look at a workflow it started) and carries on by itself then; null otherwise.
   */
  waiting_until: string | null
  /** What it waits for, in words. */
  waiting_reason: string | null
  /** It waits for someone to approve or reject a step (AWAITING_VALIDATION). */
  awaiting_approval: boolean
}

/** One page of an organization's tasks, newest first (`created_at` descending). */
export type BackgroundTaskPage = {
  items: BackgroundTask[]
  /** Every matching task, for paging. */
  total: number
}

/** Who asked for something, or did it. */
export type BackgroundTaskActor = {
  kind: "HUMAN" | "SERVICE" | "SYSTEM"
  id: string
  display_name: string
}

export type BackgroundTaskDelivery = {
  queue: string
  key: string
  enqueued_at: string
}

/** A task the job asked for when it finished. */
export type BackgroundTaskFollowup = {
  task_type: string
  kind: string
  payload: Record<string, unknown>
}

/** The job's result, once it completed. */
export type BackgroundTaskResult = {
  status: BackgroundTaskOutcome
  detail: Record<string, unknown>
  followups: BackgroundTaskFollowup[]
  /** Null, or what the job reported. */
  error: unknown
}

/** A failure of one attempt, in full. */
export type BackgroundTaskRunFailure = BackgroundTaskFailure & {
  retryable: boolean
  permanent: boolean
  /** The step it failed in. */
  step: string | null
  stack_trace: string | null
  cause_chain: string[]
}

export type BackgroundTaskStep = {
  name: string
  status: BackgroundTaskStatus
  attempt: number
  started_at: string | null
  ended_at: string | null
  duration_ms: number | null
}

/** One attempt of a task. */
export type BackgroundTaskRun = {
  id: string
  attempt: number
  status: BackgroundTaskStatus
  exit_code: string
  exit_description: string
  created_at: string
  started_at: string | null
  ended_at: string | null
  duration_ms: number | null
  /** The attempt (run ID) this one restarted, or null. */
  restart_of: string | null
  requested_by: BackgroundTaskActor | null
  failures: BackgroundTaskRunFailure[]
  steps: BackgroundTaskStep[]
}

/**
 * What an audit event records. Abandoning is a `STATUS_CHANGED` to
 * `ABANDONED`; a `RESTARTED` event's actor is who restarted the task.
 */
export type BackgroundTaskEventType =
  | "RUN_CREATED"
  | "RUN_STARTED"
  | "STATUS_CHANGED"
  | "STEP_STARTED"
  | "STEP_COMPLETED"
  | "STEP_FAILED"
  | "RETRY_ATTEMPT"
  | "PAUSED"
  | "RESUMED"
  | "RESTARTED"
  | "STOPPED"
  | "COMPLETED"
  | "FAILED"
  | "RECOVERED"
  | "IDEMPOTENT_SKIP"
  | "ANNOTATION"
  // …and any the worker adds.
  | (string & {})

/** An entry of a task's audit trail. */
export type BackgroundTaskEvent = {
  sequence: number
  run_id: string
  at: string
  type: BackgroundTaskEventType
  /** Who or what did it; for `RESTARTED`, who restarted the task. */
  actor: BackgroundTaskActor | null
  from_status: BackgroundTaskStatus | null
  to_status: BackgroundTaskStatus | null
  step: string | null
  message: string | null
}

/** What the task's state allows. */
export type BackgroundTaskActions = {
  resubmit: boolean
  restart: boolean
  abandon: boolean
  /** Its approval can be decided. */
  decide: boolean
}

/** Who a workflow's approval step asks to decide. */
export type Approvers = "org:admin" | "org:member"

/** The decision a paused task waits for: a workflow's approval step. */
export type BackgroundTaskApproval = {
  /** Names this approval in a decision, so it never lands on a later one. */
  id: string
  /** What to decide, e.g. "Order PR-1042?". */
  reason: string
  details: {
    approvers?: Approvers
    step?: string
    step_name?: string
    workflow_id?: string
    workflow_name?: string
  } & Record<string, unknown>
  requested_at: string | null
  deadline: string | null
}

export type BackgroundTaskDetail = BackgroundTask & {
  /** The task's input. */
  payload: Record<string, unknown>
  labels: Record<string, string>
  delivery: BackgroundTaskDelivery | null
  requested_by: BackgroundTaskActor | null
  correlation_id: string | null
  result: BackgroundTaskResult | null
  /** Attempts, newest first. */
  runs: BackgroundTaskRun[]
  /** The audit trail, oldest first. */
  events: BackgroundTaskEvent[]
  actions: BackgroundTaskActions
  /** The approval it waits for; null unless it's awaiting one. */
  approval: BackgroundTaskApproval | null
}

/** Where a resubmitted or restarted task was queued (the 202's body). */
export type BackgroundTaskDispatch = { queue: string; key: string }

/** Who may decide an approval, in words. */
export const APPROVERS: Record<Approvers, string> = {
  "org:admin": "The organization's admins",
  "org:member": "Any organization member",
}

/** What the list asks for. Arrays repeat the parameter. */
export type BackgroundTaskFilters = {
  task_type?: string[]
  /** Every type but these. */
  exclude_task_type?: string[]
  status?: BackgroundTaskStatus[]
}

type BackgroundTaskParams = BackgroundTaskFilters & {
  limit?: number
  offset?: number
}

/* -------------------------------------------------------------------------- */
/* Statuses                                                                   */
/* -------------------------------------------------------------------------- */

const LIVE: readonly BackgroundTaskStatus[] = [
  "PENDING",
  "RUNNING",
  "STOPPING",
  "PAUSING",
]

/** Waiting for a worker or on one: pending, running, stopping or pausing. */
export const isLiveStatus = (status: BackgroundTaskStatus | undefined) =>
  status !== undefined && LIVE.includes(status)

/** Each status as the console shows it, on the Forge status badges. */
export const BACKGROUND_STATUS: Record<
  BackgroundTaskStatus,
  { status: TaskStatus; label: string }
> = {
  PENDING: { status: "pending", label: "Queued" },
  RUNNING: { status: "running", label: "Running" },
  STOPPING: { status: "running", label: "Stopping" },
  PAUSING: { status: "running", label: "Pausing" },
  PAUSED: { status: "review", label: "Paused" },
  AWAITING_VALIDATION: { status: "review", label: "Awaiting approval" },
  STOPPED: { status: "cancelled", label: "Stopped" },
  COMPLETED: { status: "completed", label: "Completed" },
  FAILED: { status: "failed", label: "Failed" },
  ABANDONED: { status: "cancelled", label: "Abandoned" },
  UNKNOWN: { status: "pending", label: "Unknown" },
}

const OUTCOME_LABELS: Partial<Record<BackgroundTaskOutcome, string>> = {
  skipped: "Skipped",
  superseded: "Superseded",
}

/**
 * A status's badge and label. A completed job that had nothing to do says
 * so (Skipped, Superseded) rather than Completed; a stopped one that waits
 * for a time to carry on says Waiting.
 */
export function statusDisplay(
  status: BackgroundTaskStatus,
  outcome?: BackgroundTaskOutcome | null,
  waitingUntil?: string | null
) {
  if (status === "STOPPED" && waitingUntil)
    return { status: "pending" as TaskStatus, label: "Waiting" }
  const shown = BACKGROUND_STATUS[status] ?? BACKGROUND_STATUS.UNKNOWN
  const label =
    status === "COMPLETED" && outcome ? OUTCOME_LABELS[outcome] : undefined
  return label ? { ...shown, label } : shown
}

/** The job's own result, in words. */
export const OUTCOME_NAMES: Record<BackgroundTaskOutcome, string> = {
  ok: "Done",
  skipped: "Skipped",
  superseded: "Superseded",
  failed: "Failed",
}

/** Each failure category: its label, what it means, and its chip. */
export const FAILURE_CATEGORIES: Record<
  FailureCategory,
  { label: string; description: string; tone: ChipTone }
> = {
  transient: {
    label: "Retrying automatically",
    description:
      "A temporary problem, such as the network, a rate limit or a database failover. The worker retries it by itself.",
    tone: "warning",
  },
  permanent: {
    label: "Can't succeed as submitted",
    description:
      "Its input can't work as it is, for example a record Forge can't read or a service it can't reach. Trying again with the same input won't help.",
    tone: "danger",
  },
  interrupted: {
    label: "Interrupted",
    description:
      "Its worker stopped or died partway through. It restarts automatically.",
    tone: "warning",
  },
  error: {
    label: "Error",
    description:
      "An unexpected error in the job, most likely a bug. It isn't retried automatically.",
    tone: "danger",
  },
}

export const failureCategory = (category: FailureCategory) =>
  FAILURE_CATEGORIES[category] ?? FAILURE_CATEGORIES.error

/* -------------------------------------------------------------------------- */
/* Words                                                                      */
/* -------------------------------------------------------------------------- */

/** "sync_repo" → "Sync repo". */
export function humanize(value: string) {
  const words = value
    .replace(/[_.-]+/g, " ")
    .trim()
    .toLowerCase()
  return words ? words[0].toUpperCase() + words.slice(1) : value
}

const TASK_TYPES: Record<string, { label: string; icon: IconProp }> = {
  adk_workflows: { label: "ADK workflow", icon: AGENTS_ICON },
}

/** A task type's name and icon; ones the console doesn't know by name read as they are. */
export const taskTypeOf = (taskType: string) =>
  TASK_TYPES[taskType] ?? { label: humanize(taskType), icon: "layers" }

const KINDS: Record<string, string> = {
  run: "Run",
}

/** What the job does, in words. */
export const kindLabel = (kind: string) => KINDS[kind] ?? humanize(kind)

/** The task's name: its description, else its job's. */
export const taskTitle = (
  task: Pick<BackgroundTask, "description" | "job_name">
) => task.description?.trim() || task.job_name

/** The first 8 characters of an ID, as the page's reference. */
export const shortId = (id: string) => id.slice(0, 8)

const EVENT_TYPES: Record<string, string> = {
  RUN_CREATED: "Attempt created",
  RUN_STARTED: "Attempt started",
  STATUS_CHANGED: "Status changed",
  STEP_STARTED: "Step started",
  STEP_COMPLETED: "Step completed",
  STEP_FAILED: "Step failed",
  RETRY_ATTEMPT: "Retrying",
  PAUSED: "Paused",
  RESUMED: "Resumed",
  RESTARTED: "Restarted",
  STOPPED: "Stopped",
  COMPLETED: "Completed",
  FAILED: "Failed",
  RECOVERED: "Recovered",
  IDEMPOTENT_SKIP: "Skipped, already done",
  ANNOTATION: "Note",
}

/** What an audit event records, in words; unknown types read as they are. */
export function eventTitle(
  event: Pick<BackgroundTaskEvent, "type" | "to_status">
) {
  if (event.type === "STATUS_CHANGED" && event.to_status === "ABANDONED")
    return "Abandoned"
  return EVENT_TYPES[event.type] ?? humanize(event.type)
}

/** An audit event that records something going wrong. */
export const isFailureEvent = (
  event: Pick<BackgroundTaskEvent, "type" | "to_status">
) =>
  event.type === "FAILED" ||
  event.type === "STEP_FAILED" ||
  event.to_status === "FAILED" ||
  event.to_status === "ABANDONED"

/**
 * The event that restarted an attempt, whose actor is who asked: a run
 * keeps its original requester. The `RESTARTED` event on the attempt or the
 * one it restarted that's closest in time to the attempt's creation.
 */
export function restartEventOf(
  run: Pick<BackgroundTaskRun, "id" | "restart_of" | "created_at">,
  events: readonly BackgroundTaskEvent[]
) {
  if (!run.restart_of) return undefined
  const created = Date.parse(run.created_at)
  let best: BackgroundTaskEvent | undefined
  let bestGap = Infinity
  for (const event of events) {
    if (event.type !== "RESTARTED") continue
    if (event.run_id !== run.id && event.run_id !== run.restart_of) continue
    const gap = Math.abs(Date.parse(event.at) - created)
    if (gap < bestGap) {
      best = event
      bestGap = gap
    }
  }
  return best
}

/**
 * Who an actor is: a person by their name (`person` looks up a Forge user
 * ID), a service by its name, the worker itself as System.
 */
export function actorLabel(
  actor: BackgroundTaskActor | null | undefined,
  person?: (id: string) => string | undefined
) {
  if (!actor) return undefined
  if (actor.display_name) return actor.display_name
  switch (actor.kind) {
    case "HUMAN":
      return person?.(actor.id) ?? actor.id
    case "SYSTEM":
      return "System"
    default:
      return actor.id
  }
}

/**
 * An error callout's title: background tasks being unavailable or not set
 * up (the admin's 503) reads as such, anything else as `fallback`.
 */
export const failureTitle = (
  error: { status?: number } | null | undefined,
  fallback: string
) => (error?.status === 503 ? "Background tasks are unavailable" : fallback)

/* -------------------------------------------------------------------------- */
/* Where the page looks                                                       */
/* -------------------------------------------------------------------------- */

/** A background task page's tabs; Overview is the default. */
export type BackgroundTaskTab = "overview" | "attempts" | "activity"

export const isBackgroundTaskTab = (
  value: unknown
): value is BackgroundTaskTab =>
  value === "overview" || value === "attempts" || value === "activity"

/* -------------------------------------------------------------------------- */
/* Resource                                                                   */
/* -------------------------------------------------------------------------- */

/**
 * An organization's background tasks. The worker creates them; the console lists
 * them, reads one, and resubmits, restarts or abandons it (below). Every
 * query here is under the organization's `keys.all`, so an action refreshes them all.
 */
export const backgroundTasks = createNestedResource<
  BackgroundTask,
  { organizationId: string },
  { list: BackgroundTaskPage; params: BackgroundTaskParams }
>({
  api,
  key: "background-tasks",
  path: ({ organizationId }) =>
    `/organizations/${organizationId}/background-tasks`,
  label: "background task",
  mapItems: (page, map) => ({ ...page, items: map(page.items) }),
})

const taskPath = (organizationId: string, taskId: string) =>
  `/organizations/${organizationId}/background-tasks/${encodeURIComponent(taskId)}`

// FastAPI reads a list from a repeated key: status=FAILED&status=STOPPED.
const repeated = { indexes: null } as const

/* -------------------------------------------------------------------------- */
/* Polling                                                                    */
/* -------------------------------------------------------------------------- */

/** Tasks per page of the list (the API's most). */
export const BACKGROUND_TASKS_PAGE = 100
/** The list refreshes this often while a task in it is in progress… */
export const ACTIVE_LIST_POLL_MS = 5_000
/** …and this often otherwise. */
export const IDLE_LIST_POLL_MS = 30_000
/** A task's page refreshes this often while it's in progress. */
export const LIVE_TASK_POLL_MS = 3_000

// Once a refresh fails, stop: one error notice, then Refresh (or the window
// regaining focus) tries again.
const unlessFailed = (status: string, interval: number | false) =>
  status === "error" ? false : interval

// An action reaches the worker a few seconds before the task changes (a
// resubmitted one only appears once a worker picks it up), so for a minute
// after one the list and the task are read as if something were running.
const FOLLOW_MS = 60_000
const following = new Map<string, number>()
const follow = (key: string) => following.set(key, Date.now() + FOLLOW_MS)
const isFollowing = (key: string) => (following.get(key) ?? 0) > Date.now()
const listFollowKey = (organizationId: string) => organizationId
const taskFollowKey = (organizationId: string, taskId: string) =>
  `${organizationId}/${taskId}`

/** A task acted on in the last minute, which is read as if it were running. */
export const isFollowingBackgroundTask = (
  organizationId: string,
  taskId: string
) => isFollowing(taskFollowKey(organizationId, taskId))

/* -------------------------------------------------------------------------- */
/* Queries                                                                    */
/* -------------------------------------------------------------------------- */

type LoadedTasks = { items: BackgroundTask[]; total: number }

// Offset pages can overlap when tasks arrive between them; keep the first.
function loadedTasks(
  data: InfiniteData<BackgroundTaskPage, number>
): LoadedTasks {
  const seen = new Set<string>()
  const items: BackgroundTask[] = []
  for (const page of data.pages) {
    for (const task of page.items) {
      if (seen.has(task.id)) continue
      seen.add(task.id)
      items.push(task)
    }
  }
  return { items, total: data.pages.at(-1)?.total ?? items.length }
}

/**
 * The organization's background tasks, newest first, 100 at a time
 * (`fetchNextPage` for more). Refreshes every 5 s while one of them is in
 * progress, else every 30 s. `data` is the loaded tasks and how many match.
 */
export function useBackgroundTasks(
  organizationId: string,
  filters: BackgroundTaskFilters = {},
  {
    enabled = true,
    errorTitle = "Couldn't refresh the background tasks",
  }: { enabled?: boolean; errorTitle?: string } = {}
) {
  const scoped = backgroundTasks.scope({ organizationId })
  return useInfiniteQuery({
    queryKey: [...scoped.keys.infiniteLists(), filters],
    queryFn: ({ pageParam, signal }) =>
      api.get<BackgroundTaskPage>(
        `/organizations/${organizationId}/background-tasks`,
        {
          params: {
            ...filters,
            limit: BACKGROUND_TASKS_PAGE,
            offset: pageParam,
          } satisfies BackgroundTaskParams,
          paramsSerializer: repeated,
          signal,
        }
      ),
    initialPageParam: 0,
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((sum, page) => sum + page.items.length, 0)
      return last.items.length > 0 && loaded < last.total ? loaded : undefined
    },
    select: loadedTasks,
    enabled: enabled && Boolean(organizationId),
    refetchInterval: (query) =>
      unlessFailed(
        query.state.status,
        isFollowing(listFollowKey(organizationId)) ||
          query.state.data?.pages.some((page) =>
            page.items.some((task) => isLiveStatus(task.status))
          )
          ? ACTIVE_LIST_POLL_MS
          : IDLE_LIST_POLL_MS
      ),
    meta: { errorTitle },
  })
}

/**
 * One task with its input, result, attempts (newest first), audit trail and
 * what its state allows. Refreshes every 3 s while it's in progress.
 */
export function useBackgroundTask(
  organizationId: string,
  taskId: string | undefined
) {
  const scoped = backgroundTasks.scope({ organizationId })
  return useQuery({
    queryKey: scoped.keys.detail(taskId ?? ""),
    queryFn: ({ signal }) =>
      api.get<BackgroundTaskDetail>(taskPath(organizationId, taskId!), {
        signal,
      }),
    enabled: Boolean(organizationId && taskId),
    refetchInterval: (query) =>
      unlessFailed(
        query.state.status,
        isLiveStatus(query.state.data?.status) ||
          isFollowing(taskFollowKey(organizationId, taskId ?? ""))
          ? LIVE_TASK_POLL_MS
          : false
      ),
    meta: { errorTitle: "Couldn't refresh the background task" },
  })
}

/* -------------------------------------------------------------------------- */
/* Actions                                                                    */
/* -------------------------------------------------------------------------- */

type MutationMeta = { meta?: { silent?: boolean; errorTitle?: string } }

/**
 * Rereads the organization's list and tasks (and every query under their
 * keys), and follows them closely for a while: after acting on a task.
 */
export function refreshBackgroundTask(
  client: QueryClient,
  organizationId: string,
  taskId: string
) {
  follow(listFollowKey(organizationId))
  follow(taskFollowKey(organizationId, taskId))
  return client.invalidateQueries({
    queryKey: backgroundTasks.scope({ organizationId }).keys.all,
  })
}

/**
 * Run the same job again with the same input, as a new task. It shows in
 * the list once a worker picks it up, usually within seconds.
 */
export function useResubmitBackgroundTask(
  organizationId: string,
  taskId: string,
  { meta }: MutationMeta = {}
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () =>
      api.post<BackgroundTaskDispatch>(
        `${taskPath(organizationId, taskId)}/resubmit`
      ),
    meta: { errorTitle: "Couldn't resubmit the task", ...meta },
    onSuccess: async () => {
      await refreshBackgroundTask(client, organizationId, taskId)
      toast.add({
        title: "Resubmitted.",
        description:
          "It runs again as a new task, which shows in the list once a worker picks it up.",
        type: "success",
      })
    },
  })
}

/**
 * Retry a failed or stopped task as its next attempt; it skips what it
 * already finished.
 */
export function useRestartBackgroundTask(
  organizationId: string,
  taskId: string,
  { meta }: MutationMeta = {}
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () =>
      api.post<BackgroundTaskDispatch>(
        `${taskPath(organizationId, taskId)}/restart`
      ),
    meta: { errorTitle: "Couldn't retry the task", ...meta },
    onSuccess: async () => {
      await refreshBackgroundTask(client, organizationId, taskId)
      toast.add({
        title: "Retry queued.",
        description:
          "Its next attempt starts once a worker picks it up, skipping the steps it already finished.",
        type: "success",
      })
    },
  })
}

/**
 * Give up on a failed or stopped task: no more automatic or manual retries.
 * Its page shows the answer straight away.
 */
export function useAbandonBackgroundTask(
  organizationId: string,
  taskId: string,
  { meta }: MutationMeta = {}
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () =>
      api.post<BackgroundTaskDetail>(
        `${taskPath(organizationId, taskId)}/abandon`
      ),
    meta: { errorTitle: "Couldn't abandon the task", ...meta },
    onSuccess: async (detail) => {
      client.setQueryData(
        backgroundTasks.scope({ organizationId }).keys.detail(taskId),
        detail
      )
      await client.invalidateQueries({
        queryKey: backgroundTasks.scope({ organizationId }).keys.all,
      })
      toast.add({
        title: "Task abandoned.",
        description: "It won't be retried. You can still resubmit it.",
        type: "success",
      })
    },
  })
}

/** A decision on the approval a workflow run waits at. */
export type ApprovalDecision = {
  approved: boolean
  /** Why, for the record; the run's later steps can read it. */
  comment?: string
}
