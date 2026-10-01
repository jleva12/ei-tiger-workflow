import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type { ChipTone } from "@/components/forge/variants"
import { toast } from "@/components/ui/toast"
import { api } from "@/lib/api-instance"
import {
  backgroundTasks,
  humanize,
  isBackgroundTaskTab,
  isFollowingBackgroundTask,
  isLiveStatus,
  refreshBackgroundTask,
  useBackgroundTask,
  useBackgroundTasks,
  type ApprovalDecision,
  type Approvers,
  type BackgroundTask,
  type BackgroundTaskApproval,
  type BackgroundTaskDispatch,
  type BackgroundTaskPage,
  type BackgroundTaskTab,
} from "@/lib/background-tasks"
import { formatDateTime, formatDuration } from "@/lib/format"
import { isTimestamp, parseTimestamp } from "@/lib/timestamps"
import {
  IDLE_POLL_MS,
  isOpenRun,
  OPEN_POLL_MS,
  runsPollMs,
} from "@/lib/workflows/runs"
import { agentPath } from "./api"
import { AGENT_FORMAT, type AgentDocument } from "./document"
import { AGENT_KINDS, isAgentKind, type AgentKind } from "./model"

/*
 * Running an organization's ADK workflow. It's kept apart from Forge
 * workflows' runs: its own task type (`adk_workflows`), queue and routes.
 * A run takes the ADK workflow as it's saved when it starts, acts as the
 * member who ran it, and is one of the organization's background tasks in
 * the API all the same, so its page is a background task's page (on the
 * ADK workflows page, as its Runs). Its steps are read from its ADK session
 * (`/adk-runs/{task}/steps`), and what it waits for is answered there: an
 * approval decided, a person's answer given. Running takes `agents:run`;
 * deciding an approval takes what its node asks of the approvers
 * (`agents:approve` for the organization's admins, `agents:run` for any
 * member).
 */

/** An ADK workflow run's background task type. */
export const ADK_WORKFLOW_TASK_TYPE = "adk_workflows"

/** The ADK workflows page's tabs; Overview is the default. */
export type AgentsTab = "overview" | "runs"

export const isAgentsTab = (value: unknown): value is AgentsTab =>
  value === "overview" || value === "runs"

/** A run's page's tabs: a background task's, and its Steps. */
export type AdkRunTab = BackgroundTaskTab | "steps"

export const isAdkRunTab = (value: unknown): value is AdkRunTab =>
  value === "steps" || isBackgroundTaskTab(value)

/** The job that will run it (the 202's body). */
export type AdkRunStarted = {
  queue: string
  key: string
  agent_id: string
  /** The revision it runs. */
  revision: number
  /** The ADK session it runs in. */
  session_id: string
}

/* -------------------------------------------------------------------------- */
/* Steps                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * How far a run got with a node: done (it handed on its output, or took
 * its Error way), failed (it failed the run), waiting (for a person or a
 * time), running (started, not finished: a team or a loop partway), or not
 * reached (every node, before the worker starts the run's session).
 */
export type AdkStepStatus =
  "done" | "failed" | "waiting" | "running" | "not_reached"

/** A node of the ADK workflow, as a run went through it. */
export type AdkRunStep = {
  /** The node's ID in the document. */
  id: string
  /** Its name, as the run knew it. */
  name: string
  /** Its kind, e.g. `llm` or `approval`. */
  kind: string
  status: AdkStepStatus
  /**
   * What it handed on, once done, as later steps read it; a step inside a
   * loop's body, its last item's. Null when it took its Error way.
   */
  output: unknown
  /**
   * Why it failed (`{message, code}`), or the error a step that took its
   * Error way handed on (an HTTP request's `{message, status, body}`).
   */
  error: unknown
  started_at: string | null
  finished_at: string | null
}

/** A run's steps, read from its ADK session. */
export type AdkRunSteps = {
  session_id: string
  steps: AdkRunStep[]
}

const STEP_STATUS: Record<AdkStepStatus, { label: string; tone: ChipTone }> = {
  done: { label: "Done", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  waiting: { label: "Waiting", tone: "notice" },
  running: { label: "Running", tone: "warning" },
  not_reached: { label: "Not reached", tone: "outline" },
}

/** A step's status in words, and its chip; unknown ones read as they are. */
export const stepStatusOf = (status: string) =>
  STEP_STATUS[status as AdkStepStatus] ?? {
    label: humanize(status),
    tone: "neutral" as ChipTone,
  }

/** How long a step took, when it started and finished; else undefined. */
export function stepDurationMs(
  step: Pick<AdkRunStep, "started_at" | "finished_at">
) {
  if (!step.started_at || !step.finished_at) return undefined
  if (!isTimestamp(step.started_at) || !isTimestamp(step.finished_at))
    return undefined
  const ms =
    parseTimestamp(step.finished_at).getTime() -
    parseTimestamp(step.started_at).getTime()
  return ms >= 0 ? ms : undefined
}

/** When a step ran, in words: when it started, and how long it took or waits. */
export function stepTiming(
  step: Pick<AdkRunStep, "status" | "started_at" | "finished_at">
) {
  if (!step.started_at) return undefined
  const started = formatDateTime(step.started_at)
  if (step.status === "waiting") return `Waiting since ${started}`
  if (step.status === "running") return `Running since ${started}`
  const ms = stepDurationMs(step)
  return ms === undefined
    ? `Started ${started}`
    : `${started} · took ${formatDuration(ms)}`
}

/** A step's error, readably. */
export type StepError = {
  message: string
  /** Its code: ADK's error code, or an HTTP response's status. */
  code?: string
  /** What else it carries, e.g. an HTTP response's body. */
  more?: unknown
}

/**
 * A step's error as the page shows it: its message and code, and the rest
 * (an HTTP response's body) to look into; undefined when there's none.
 */
export function stepErrorOf(error: unknown): StepError | undefined {
  if (error === null || error === undefined || error === "") return undefined
  if (typeof error === "string") return { message: error }
  if (typeof error !== "object" || Array.isArray(error))
    return { message: JSON.stringify(error) }
  const { message, code, status, ...rest } = error as Record<string, unknown>
  const named =
    typeof code === "string" || typeof code === "number"
      ? String(code)
      : typeof status === "number" || typeof status === "string"
        ? `HTTP ${status}`
        : undefined
  const keys = Object.keys(rest)
  const found: StepError = {
    message:
      typeof message === "string" && message.trim()
        ? message
        : named
          ? `Failed with ${named}`
          : "It failed",
  }
  if (named) found.code = named
  if (keys.length)
    found.more = keys.length === 1 && keys[0] === "body" ? rest.body : rest
  return found
}

/**
 * The ADK workflow a run started with: the snapshot in its task's input,
 * when it carries one as `document`.
 */
export function runDocumentOf(payload: Record<string, unknown> | undefined) {
  const document = payload?.document
  if (!document || typeof document !== "object") return undefined
  const doc = document as Partial<AgentDocument>
  return doc.format === AGENT_FORMAT && Array.isArray(doc.nodes)
    ? (doc as AgentDocument)
    : undefined
}

/**
 * A step's kind as the builder knows it: its node's in the document when
 * there is one, else what the run said; undefined for a kind the builder
 * doesn't have.
 */
export function stepKindOf(
  step: Pick<AdkRunStep, "id" | "kind">,
  doc: Pick<AgentDocument, "nodes"> | undefined
): AgentKind | undefined {
  const kind = doc?.nodes.find((node) => node.id === step.id)?.kind ?? step.kind
  return isAgentKind(kind) ? kind : undefined
}

/** A step's name: the run's, else its node's, else its kind's. */
export function stepNameOf(
  step: Pick<AdkRunStep, "id" | "name" | "kind">,
  doc: Pick<AgentDocument, "nodes"> | undefined
) {
  if (step.name?.trim()) return step.name
  const node = doc?.nodes.find((found) => found.id === step.id)
  if (node?.name.trim()) return node.name
  const kind = stepKindOf(step, doc)
  return kind ? AGENT_KINDS[kind].label : step.id
}

/* -------------------------------------------------------------------------- */
/* Pauses                                                                     */
/* -------------------------------------------------------------------------- */

/** What a paused ADK workflow run asks, from its approval's details. */
export type AdkPauseDetails = {
  /** A decision (approve or reject), or a person's answer. */
  kind: "approval" | "human_input"
  approvers?: Approvers
  /** When an approval's time is up: it's rejected then. */
  expires_at?: string
  /** A JSON Schema of the answer human input asks for. */
  response_schema?: Record<string, unknown>
  step?: string
  step_name?: string
  workflow_name?: string
  message?: string
}

/**
 * What a run waits for: a person's answer (`human_input`), else a decision
 * (`approval`, also what a pause the console doesn't know is taken for).
 */
export const pauseKindOf = (
  approval: Pick<BackgroundTaskApproval, "details">
): AdkPauseDetails["kind"] =>
  approval.details.kind === "human_input" ? "human_input" : "approval"

/** The JSON Schema a person's answer is held to; `{}` takes anything. */
export function responseSchemaOf(
  approval: Pick<BackgroundTaskApproval, "details">
): Record<string, unknown> {
  const schema = approval.details.response_schema
  return schema && typeof schema === "object" && !Array.isArray(schema)
    ? (schema as Record<string, unknown>)
    : {}
}

/** The permission deciding an ADK approval takes; anything unknown is the admins'. */
export const adkApprovePermission = (approvers: unknown) =>
  approvers === "org:member" ? "agents:run" : "agents:approve"

/* -------------------------------------------------------------------------- */
/* Queries                                                                    */
/* -------------------------------------------------------------------------- */

const runPath = (organizationId: string, taskId: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/adk-runs/${encodeURIComponent(taskId)}`

// Under the organization's background tasks' keys, so acting on a task
// (and refreshBackgroundTask) refreshes these too.
const runsKey = (organizationId: string, agentId: string) => [
  ...backgroundTasks.scope({ organizationId }).keys.all,
  "adk-workflow-runs",
  agentId,
]
const stepsKey = (organizationId: string, taskId: string) => [
  ...backgroundTasks.scope({ organizationId }).keys.all,
  "adk-run-steps",
  taskId,
]

/**
 * The organization's ADK workflow runs, newest first, 100 at a time
 * (`fetchNextPage` for more): its background tasks of type `adk_workflows`.
 */
export const useOrganizationAdkRuns = (organizationId: string) =>
  useBackgroundTasks(
    organizationId,
    { task_type: [ADK_WORKFLOW_TASK_TYPE] },
    { errorTitle: "Couldn't refresh the ADK workflow runs" }
  )

/**
 * An ADK workflow's latest runs, newest first. Refreshes every few seconds
 * while one is still going, else every 30 s.
 */
export function useAdkWorkflowRuns(
  organizationId: string,
  agentId: string,
  { enabled = true, limit = 10 }: { enabled?: boolean; limit?: number } = {}
) {
  return useQuery({
    queryKey: [...runsKey(organizationId, agentId), limit],
    queryFn: ({ signal }) =>
      api.get<BackgroundTaskPage>(
        `${agentPath(organizationId, agentId)}/runs`,
        {
          params: { limit },
          signal,
        }
      ),
    enabled: enabled && Boolean(organizationId && agentId),
    refetchInterval: (query) => runsPollMs(query.state),
    meta: { errorTitle: "Couldn't load the ADK workflow's runs" },
  })
}

/**
 * Run the ADK workflow as it's saved, with an input that fits its start
 * (the API answers 422 with why when it doesn't, or when the ADK workflow
 * doesn't build). Errors are the caller's to show: the run dialog shows
 * them by the input.
 */
export function useRunAdkWorkflow(organizationId: string, agentId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (input: unknown) =>
      api.post<AdkRunStarted>(`${agentPath(organizationId, agentId)}/runs`, {
        input,
      }),
    meta: { silent: true },
    onSuccess: () => {
      // Seconds later a worker picks it up; poll until it shows.
      const queryKey = runsKey(organizationId, agentId)
      void client.invalidateQueries({ queryKey })
      window.setTimeout(
        () => void client.invalidateQueries({ queryKey }),
        OPEN_POLL_MS
      )
    },
  })
}

/** How often a run's steps refresh, given the run. */
export function stepsPollMs(
  run: Pick<BackgroundTask, "status" | "waiting_until"> | undefined,
  following: boolean
) {
  if (following || isLiveStatus(run?.status)) return OPEN_POLL_MS
  // Waiting for a person or a time: nothing moves until then.
  return run && isOpenRun(run as BackgroundTask) ? IDLE_POLL_MS : false
}

/**
 * A run's steps, from its ADK session: every node, how far the run got with
 * it, what it handed on or why it failed. Refreshes every few seconds while
 * the run goes, and once more whenever the run itself changes (the task
 * page's own query, shared), so its last step shows when it finishes.
 */
export function useAdkRunSteps(
  organizationId: string,
  taskId: string | undefined
) {
  const client = useQueryClient()
  const task = useBackgroundTask(organizationId, taskId)
  const run = task.data
  const query = useQuery({
    queryKey: stepsKey(organizationId, taskId ?? ""),
    queryFn: ({ signal }) =>
      api.get<AdkRunSteps>(`${runPath(organizationId, taskId!)}/steps`, {
        signal,
      }),
    enabled: Boolean(organizationId && taskId),
    refetchInterval: (query) =>
      query.state.status === "error"
        ? false
        : stepsPollMs(
            run,
            isFollowingBackgroundTask(organizationId, taskId ?? "")
          ),
    meta: { errorTitle: "Couldn't refresh the run's steps" },
  })

  // The run moved on (a step finished, it paused, it ended): read again.
  const updated = run?.updated_at
  const seen = React.useRef(updated)
  React.useEffect(() => {
    if (seen.current === updated) return
    const first = seen.current === undefined
    seen.current = updated
    if (!first)
      void client.invalidateQueries({
        queryKey: stepsKey(organizationId, taskId ?? ""),
      })
  }, [client, organizationId, taskId, updated])

  return query
}

/* -------------------------------------------------------------------------- */
/* Answers                                                                    */
/* -------------------------------------------------------------------------- */

type MutationMeta = { meta?: { silent?: boolean; errorTitle?: string } }

/**
 * Approve or reject what an ADK workflow run waits for: it carries on down
 * the node's approved or rejected way, on a worker, within seconds. A
 * decision names the approval, so one someone else decided first, or a
 * question that isn't an approval (409), never lands on a later one. Not
 * the task's own decisions: those refuse ADK workflow runs.
 */
export function useDecideAdkRun(
  organizationId: string,
  taskId: string,
  { meta }: MutationMeta = {}
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({
      requestId,
      approved,
      comment = "",
    }: ApprovalDecision & { requestId: string }) =>
      api.post<BackgroundTaskDispatch>(
        `${runPath(organizationId, taskId)}/decisions`,
        {
          request_id: requestId,
          approved,
          comment,
        }
      ),
    meta: { errorTitle: "Couldn't record the decision", ...meta },
    onSuccess: async (_, { approved }) => {
      await refreshBackgroundTask(client, organizationId, taskId)
      toast.add({
        title: approved ? "Approved." : "Rejected.",
        description: approved
          ? "The run carries on down its approved way."
          : "The run carries on down its rejected way.",
        type: "success",
      })
    },
  })
}

/** A person's answer to what an ADK workflow run asks. */
export type AdkAnswer = { requestId: string; answer: unknown }

/**
 * Answer what an ADK workflow run asks a person: the run carries on with
 * the answer, on a worker, within seconds. The API holds the answer to the
 * node's response schema and to 4,000 characters of JSON (422 with why when
 * it doesn't fit); 409 when it's no longer asked.
 */
export function useAnswerAdkRun(
  organizationId: string,
  taskId: string,
  { meta }: MutationMeta = {}
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ requestId, answer }: AdkAnswer) =>
      api.post<BackgroundTaskDispatch>(
        `${runPath(organizationId, taskId)}/answers`,
        {
          request_id: requestId,
          answer,
        }
      ),
    meta: { errorTitle: "Couldn't send the answer", ...meta },
    onSuccess: async () => {
      await refreshBackgroundTask(client, organizationId, taskId)
      toast.add({
        title: "Answer sent.",
        description: "The run carries on with it.",
        type: "success",
      })
    },
  })
}
