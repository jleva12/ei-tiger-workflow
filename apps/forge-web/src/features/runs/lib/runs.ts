import * as React from "react"
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type QueryClient,
} from "@tanstack/react-query"

import type { ChipTone } from "@/components/forge/variants"
import { toast } from "@/components/ui/toast"
import { api } from "@/lib/api-instance"
import { formatDateTime, formatDuration } from "@/lib/format"
import { isTimestamp, parseTimestamp } from "@/lib/timestamps"
import {
  IDLE_POLL_MS,
  isLiveRun,
  isOpenRun,
  OPEN_POLL_MS,
  runsPollMs,
} from "@/features/runs/lib/display"
import { agentPath } from "@/features/adk-workflows/lib/api"
import {
  AGENT_FORMAT,
  type AgentDocument,
} from "@/features/adk-workflows/lib/document"
import {
  AGENT_KINDS,
  isAgentKind,
  type AgentKind,
} from "@/features/adk-workflows/lib/model"

/*
 * Running an organization's workflows. The admin API keeps each run
 * (beside its ADK session, in its MySQL) and a worker carries it on: from
 * queued to running, through what it waits for (a person's decision or
 * answer, a time), to succeeded or failed. A run takes the workflow as
 * it's saved when it starts and acts as the member who ran it. Its steps
 * are read from its ADK session (`/adk-runs/{run}/steps`), and what it
 * waits for is answered there: an approval decided, a person's answer
 * given. Running takes `agents:run`; deciding an approval takes what its
 * node asks of the approvers (`agents:approve` for the organization's
 * admins, `agents:run` for any member); retrying, resubmitting and
 * abandoning take `agents:manage_runs` (its admins).
 */

/* -------------------------------------------------------------------------- */
/* Contract                                                                   */
/* -------------------------------------------------------------------------- */

/**
 * Where a run is: queued (for a worker), running (on one), paused (for a
 * person: an approval or a question), waiting (for a time), or finished
 * (succeeded, failed or abandoned).
 */
export type AdkRunStatus =
  | "queued"
  | "running"
  | "paused"
  | "waiting"
  | "succeeded"
  | "failed"
  | "abandoned"

/** Who did something to a run; no ID for the timer (an approval nobody decided in time). */
export type AdkRunActor = { id: string | null; name: string }

/** Who an approval node asks to decide. */
export type Approvers = "org:admin" | "org:member"

/** What a paused run asks, as its node put it. */
export type AdkPauseDetails = {
  /** A decision (approve or reject), or a person's answer. */
  kind?: "approval" | "human_input"
  approvers?: Approvers
  /** When an approval's time is up: it's rejected then. */
  expires_at?: string
  /** A JSON Schema of the answer human input asks for. */
  response_schema?: Record<string, unknown>
  step?: string
  step_name?: string
  workflow_name?: string
  message?: string
  /** An LLM agent's tool a person allows before it's called: the tool, and what it would send. */
  confirmation?: boolean
  tool?: string
  args?: Record<string, unknown>
} & Record<string, unknown>

/** What a paused run waits at: an approval or a question. */
export type AdkRunPause = {
  /** Names it in a decision or an answer, so one never lands on a later one. */
  id: string
  kind: "approval" | "human_input"
  /** What to decide or answer, e.g. "Refund 40 EUR?". */
  reason: string
  details: AdkPauseDetails
  requested_at: string | null
  /** When an approval's time is up; null when it waits for good. */
  deadline: string | null
}

/**
 * Why a run failed (`failed`: a step failed it, its input didn't fit, or a
 * question was declined; `error`: a bug), or the hiccup it's retried after
 * (`transient`; `interrupted`: its worker went).
 */
export type AdkRunFailureCategory =
  "failed" | "error" | "transient" | "interrupted"

export type AdkRunFailure = {
  message: string
  category: AdkRunFailureCategory
  /** The step it failed at, when it was one. */
  step: string | null
  occurred_at: string
}

/** A run, as lists show it. */
export type AdkRun = {
  id: string
  organization_id: string
  /** The workflow it runs, and its name and revision when it started. */
  agent_id: string
  agent_name: string
  revision: number
  /** Which version ran: a published one, or "draft"; null for runs from before versions. */
  version: number | "draft" | null
  /** Its ADK session, where its steps are read from. */
  session_id: string
  status: AdkRunStatus
  /** 1, and one more for each retry after a failure, a hiccup or an interruption. */
  attempt: number
  requested_by: AdkRunActor
  /** The run this one resubmitted. */
  resubmit_of: string | null
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  /** A waiting run carries on by itself then. */
  waiting_until: string | null
  waiting_reason: string | null
  /** What a paused run waits at. */
  pause: AdkRunPause | null
  /** Why it failed, or the last hiccup it was retried after. */
  error: AdkRunFailure | null
}

/** One page of runs, newest first. */
export type AdkRunPage = { items: AdkRun[]; total: number }

/** What happened to a run, as its Activity lists it. */
export type AdkRunEventKind =
  | "created"
  | "started"
  | "resumed"
  | "note"
  | "paused"
  | "decided"
  | "answered"
  | "declined"
  | "timed_out"
  | "waiting"
  | "succeeded"
  | "failed"
  | "retried"
  | "recovered"
  | "abandoned"
  | (string & {})

export type AdkRunEvent = {
  id: number
  at: string
  kind: AdkRunEventKind
  message: string
  /** Who did it; null for the worker, the timer, the run itself. */
  actor: AdkRunActor | null
  attributes: Record<string, unknown> | null
}

/** What the run's status allows (the page checks who may). */
export type AdkRunActions = {
  retry: boolean
  resubmit: boolean
  abandon: boolean
  decide: boolean
  answer: boolean
}

/** A run with what only its page shows. */
/** A file a run started with: an artifact of its session. */
export type AdkRunFile = {
  /** Its name: the artifact's, which steps load it by. */
  name: string
  media_type: string
  size_bytes: number
  /** Its type (`pdf`, `word`, …); empty for another. */
  type: string
  version: number
}

export type AdkRunDetail = AdkRun & {
  input: unknown
  /** The files it started with. */
  files: AdkRunFile[]
  /** What the run handed on, once it ended with a result. */
  result: unknown
  /** The workflow as the run started with it. */
  document: unknown
  /** What started it, e.g. `{type: "manual", by}`. */
  trigger: Record<string, unknown> | null
  /** Its activity, oldest first. */
  events: AdkRunEvent[]
  actions: AdkRunActions
}

/** A decision on the approval a run waits at. */
export type ApprovalDecision = {
  approved: boolean
  /** Why, for the record; the run's later steps can read it. */
  comment?: string
}

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

/** The first 8 characters of a run's ID, as its page's reference. */
export const shortId = (id: string) => id.slice(0, 8)

/** Who may decide an approval, in words. */
export const APPROVERS: Record<Approvers, string> = {
  "org:admin": "The organization's admins",
  "org:member": "Any organization member",
}

/** Each failure category: its label, what it means, and its chip. */
export const FAILURE_CATEGORIES: Record<
  AdkRunFailureCategory,
  { label: string; description: string; tone: ChipTone }
> = {
  failed: {
    label: "The run failed",
    description:
      "A step failed it with no way to take, its input didn't fit, or a question was declined. Retrying carries it on from where it stopped.",
    tone: "danger",
  },
  error: {
    label: "Error",
    description:
      "An unexpected error running it, most likely a bug. It isn't retried automatically.",
    tone: "danger",
  },
  transient: {
    label: "Retrying automatically",
    description:
      "A temporary problem, such as a model overloaded, the network or a database failover. The worker retries it by itself.",
    tone: "warning",
  },
  interrupted: {
    label: "Interrupted",
    description:
      "Its worker stopped or went partway through. It carries on automatically.",
    tone: "warning",
  },
}

export const failureCategory = (category: string) =>
  FAILURE_CATEGORIES[category as AdkRunFailureCategory] ??
  FAILURE_CATEGORIES.error

const EVENT_TITLES: Record<string, string> = {
  created: "Started",
  started: "Picked up",
  resumed: "Carried on",
  note: "Progress",
  paused: "Waiting for a person",
  decided: "Decided",
  answered: "Answered",
  declined: "Declined",
  timed_out: "Timed out",
  waiting: "Waiting",
  succeeded: "Succeeded",
  failed: "Failed",
  retried: "Retried",
  recovered: "Recovered",
  abandoned: "Abandoned",
}

/** An activity entry's title. */
export const eventTitle = (event: Pick<AdkRunEvent, "kind">) =>
  EVENT_TITLES[event.kind] ?? humanize(event.kind)

/** Whether an activity entry is a failure, shown as one. */
export const isFailureEvent = (event: Pick<AdkRunEvent, "kind">) =>
  event.kind === "failed" || event.kind === "declined"

/**
 * An error callout's title: the runs' database being unavailable (the
 * admin's 503) reads as such, anything else as `fallback`.
 */
export const failureTitle = (
  error: { status?: number } | null | undefined,
  fallback: string
) => (error?.status === 503 ? "Workflow runs are unavailable" : fallback)

/* -------------------------------------------------------------------------- */
/* Where the page looks                                                       */
/* -------------------------------------------------------------------------- */

/** The workflows page's tabs; Overview is the default. */
export type AgentsTab = "overview" | "runs"

export const isAgentsTab = (value: unknown): value is AgentsTab =>
  value === "overview" || value === "runs"

/** A run's page's tabs; Overview is the default. */
export type AdkRunTab = "overview" | "steps" | "activity"

export const isAdkRunTab = (value: unknown): value is AdkRunTab =>
  value === "overview" || value === "steps" || value === "activity"

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

/** A node of the workflow, as a run went through it. */
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
  /** The tools its LLM agents called, in order; absent from older APIs. */
  calls?: AdkToolCall[]
}

/** A tool an LLM agent in a step called, and what it answered. */
export type AdkToolCall = {
  id: string
  /** The agent that called it (its ADK name). */
  agent: string | null
  name: string
  args: unknown
  /** done, failed (an error, or a person refused it), waiting (to be allowed) or running. */
  status: "done" | "failed" | "waiting" | "running"
  /** What it answered; a long answer as `{truncated}`. */
  response: unknown
  at: string | null
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

/** The workflow a run started with, when it's one the builder reads. */
export function runDocumentOf(document: unknown) {
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

/**
 * What a run waits for: a person's answer (`human_input`), else a decision
 * (`approval`, also what a pause the console doesn't know is taken for).
 */
export const pauseKindOf = (pause: Pick<AdkRunPause, "kind">) =>
  pause.kind === "human_input" ? "human_input" : "approval"

/** The JSON Schema a person's answer is held to; `{}` takes anything. */
export function responseSchemaOf(
  pause: Pick<AdkRunPause, "details">
): Record<string, unknown> {
  const schema = pause.details.response_schema
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

/** Runs per page of the Runs tab (the API's most). */
export const ADK_RUNS_PAGE = 100

const runsPath = (organizationId: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/adk-runs`
const runPath = (organizationId: string, runId: string) =>
  `${runsPath(organizationId)}/${encodeURIComponent(runId)}`

/**
 * Every query about the organization's runs is under its `all` key, so
 * acting on a run (`refreshAdkRuns`) refreshes them all.
 */
export const adkRunKeys = {
  all: (organizationId: string) => ["adk-runs", organizationId] as const,
  list: (
    organizationId: string,
    filters: { agentId?: string; limit?: number }
  ) => [...adkRunKeys.all(organizationId), "list", filters] as const,
  pages: (organizationId: string) =>
    [...adkRunKeys.all(organizationId), "pages"] as const,
  detail: (organizationId: string, runId: string) =>
    [...adkRunKeys.all(organizationId), "run", runId] as const,
  steps: (organizationId: string, runId: string) =>
    [...adkRunKeys.all(organizationId), "steps", runId] as const,
}

// A run acted on reaches a worker seconds later, so for a minute after one
// the run and the lists are read as if something were running.
const FOLLOW_MS = 60_000
const following = new Map<string, number>()
const follow = (key: string) => following.set(key, Date.now() + FOLLOW_MS)
const isFollowing = (key: string) => (following.get(key) ?? 0) > Date.now()

/** A run acted on in the last minute, which is read as if it were running. */
export const isFollowingAdkRun = (organizationId: string, runId: string) =>
  isFollowing(`${organizationId}/${runId}`)

/**
 * Rereads the organization's runs (every query under their key), and
 * follows them closely for a while: after starting or acting on one.
 */
export function refreshAdkRuns(
  client: QueryClient,
  organizationId: string,
  runId?: string
) {
  follow(organizationId)
  if (runId) follow(`${organizationId}/${runId}`)
  return client.invalidateQueries({ queryKey: adkRunKeys.all(organizationId) })
}

// Offset pages can overlap when runs start between them; keep the first.
function loadedRuns(data: InfiniteData<AdkRunPage, number>): AdkRunPage {
  const seen = new Set<string>()
  const items: AdkRun[] = []
  for (const page of data.pages) {
    for (const run of page.items) {
      if (seen.has(run.id)) continue
      seen.add(run.id)
      items.push(run)
    }
  }
  return { items, total: data.pages.at(-1)?.total ?? items.length }
}

/**
 * The organization's workflow runs, newest first, 100 at a time
 * (`fetchNextPage` for more). Refreshes every few seconds while one is
 * still going, else every 30 s. `data` is the loaded runs and how many there are.
 */
export function useOrganizationAdkRuns(organizationId: string) {
  return useInfiniteQuery({
    queryKey: adkRunKeys.pages(organizationId),
    queryFn: ({ pageParam, signal }) =>
      api.get<AdkRunPage>(runsPath(organizationId), {
        params: { limit: ADK_RUNS_PAGE, offset: pageParam },
        signal,
      }),
    initialPageParam: 0,
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((sum, page) => sum + page.items.length, 0)
      return last.items.length > 0 && loaded < last.total ? loaded : undefined
    },
    select: loadedRuns,
    enabled: Boolean(organizationId),
    refetchInterval: (query) =>
      query.state.status === "error"
        ? false
        : isFollowing(organizationId) ||
            query.state.data?.pages.some((page) => page.items.some(isOpenRun))
          ? OPEN_POLL_MS
          : IDLE_POLL_MS,
    meta: { errorTitle: "Couldn't refresh the workflow runs" },
  })
}

/**
 * A workflow's latest runs, newest first. Refreshes every few seconds
 * while one is still going, else every 30 s.
 */
export function useAdkWorkflowRuns(
  organizationId: string,
  agentId: string,
  { enabled = true, limit = 10 }: { enabled?: boolean; limit?: number } = {}
) {
  return useQuery({
    queryKey: adkRunKeys.list(organizationId, { agentId, limit }),
    queryFn: ({ signal }) =>
      api.get<AdkRunPage>(runsPath(organizationId), {
        params: { agent_id: agentId, limit },
        signal,
      }),
    enabled: enabled && Boolean(organizationId && agentId),
    refetchInterval: (query) =>
      isFollowing(organizationId) && query.state.status !== "error"
        ? OPEN_POLL_MS
        : runsPollMs(query.state),
    meta: { errorTitle: "Couldn't load the workflow's runs" },
  })
}

/**
 * One run, with its input, result, the workflow it ran and its
 * activity. Refreshes every few seconds while a worker has it (or it was
 * just acted on), every 30 s while it waits for a person or a time.
 */
export function useAdkRun(organizationId: string, runId: string | undefined) {
  return useQuery({
    queryKey: adkRunKeys.detail(organizationId, runId ?? ""),
    queryFn: ({ signal }) =>
      api.get<AdkRunDetail>(runPath(organizationId, runId!), { signal }),
    enabled: Boolean(organizationId && runId),
    refetchInterval: (query) => {
      if (query.state.status === "error") return false
      const run = query.state.data
      if (isLiveRun(run) || isFollowingAdkRun(organizationId, runId ?? ""))
        return OPEN_POLL_MS
      return run && isOpenRun(run) ? IDLE_POLL_MS : false
    },
    meta: { errorTitle: "Couldn't refresh the workflow run" },
  })
}

/** How often a run's steps refresh, given the run. */
export function stepsPollMs(
  run: Pick<AdkRun, "status"> | undefined,
  following: boolean
) {
  if (following || isLiveRun(run)) return OPEN_POLL_MS
  // Waiting for a person or a time: nothing moves until then.
  return run && isOpenRun(run) ? IDLE_POLL_MS : false
}

/**
 * A run's steps, from its ADK session: every node, how far the run got with
 * it, what it handed on or why it failed. Refreshes every few seconds while
 * the run goes, and once more whenever the run itself changes (its page's
 * own query), so its last step shows when it finishes.
 */
export function useAdkRunSteps(
  organizationId: string,
  run: Pick<AdkRun, "id" | "status" | "updated_at"> | undefined
) {
  const client = useQueryClient()
  const runId = run?.id ?? ""
  const query = useQuery({
    queryKey: adkRunKeys.steps(organizationId, runId),
    queryFn: ({ signal }) =>
      api.get<AdkRunSteps>(`${runPath(organizationId, runId)}/steps`, {
        signal,
      }),
    enabled: Boolean(organizationId && runId),
    refetchInterval: (query) =>
      query.state.status === "error"
        ? false
        : stepsPollMs(run, isFollowingAdkRun(organizationId, runId)),
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
        queryKey: adkRunKeys.steps(organizationId, runId),
      })
  }, [client, organizationId, runId, updated])

  return query
}

/* -------------------------------------------------------------------------- */
/* Actions                                                                    */
/* -------------------------------------------------------------------------- */

type MutationMeta = { meta?: { silent?: boolean; errorTitle?: string } }

/** What a run starts with: its input, and the files its start takes. */
export type RunStart = { input: unknown; files?: File[] }

/**
 * Run the workflow as it's saved, with an input that fits its start, and
 * files when it takes them (sent as a multipart form; the API saves each
 * as an artifact of the run's session). The API answers 422 with why when
 * they don't fit, or when the workflow doesn't build. The run is queued at
 * once; a worker takes it seconds later. Errors are the caller's to show:
 * the run dialog shows them by the input.
 */
export function useRunAdkWorkflow(
  organizationId: string,
  agentId: string,
  /** Which version runs: a published one, or the draft; the builder's when omitted. */
  version?: number | "draft"
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ input, files = [] }: RunStart) => {
      const path = `${agentPath(organizationId, agentId)}/runs`
      if (!files.length)
        return api.post<AdkRun>(path, {
          input,
          ...(version !== undefined ? { version } : {}),
        })
      const form = new FormData()
      form.append("input", JSON.stringify(input ?? null))
      if (version !== undefined) form.append("version", String(version))
      for (const file of files) form.append("files", file, file.name)
      return api.post<AdkRun, FormData>(path, form)
    },
    meta: { silent: true },
    onSuccess: (run) => void refreshAdkRuns(client, organizationId, run.id),
  })
}

/** A file a run started with, as it was sent. */
export function downloadAdkRunFile(
  organizationId: string,
  runId: string,
  name: string
) {
  return api.get<Blob>(
    `${runPath(organizationId, runId)}/files/${encodeURIComponent(name)}`,
    { responseType: "blob", silent: true }
  )
}

/**
 * Approve or reject what a workflow run waits for: it carries on down
 * the node's approved or rejected way, on a worker, within seconds. A
 * decision names the approval, so one someone else decided first, or a
 * question that isn't an approval (409), never lands on a later one.
 */
export function useDecideAdkRun(
  organizationId: string,
  runId: string,
  { meta }: MutationMeta = {}
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({
      requestId,
      approved,
      comment = "",
    }: ApprovalDecision & { requestId: string }) =>
      api.post<AdkRun>(`${runPath(organizationId, runId)}/decisions`, {
        request_id: requestId,
        approved,
        comment,
      }),
    meta: { errorTitle: "Couldn't record the decision", ...meta },
    onSuccess: async (_, { approved }) => {
      await refreshAdkRuns(client, organizationId, runId)
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

/** A person's answer to what a workflow run asks. */
export type AdkAnswer = { requestId: string; answer: unknown }

/**
 * Answer what a workflow run asks a person: the run carries on with
 * the answer, on a worker, within seconds. The API holds the answer to the
 * node's response schema and to 4,000 characters of JSON (422 with why when
 * it doesn't fit); 409 when it's no longer asked.
 */
export function useAnswerAdkRun(
  organizationId: string,
  runId: string,
  { meta }: MutationMeta = {}
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ requestId, answer }: AdkAnswer) =>
      api.post<AdkRun>(`${runPath(organizationId, runId)}/answers`, {
        request_id: requestId,
        answer,
      }),
    meta: { errorTitle: "Couldn't send the answer", ...meta },
    onSuccess: async () => {
      await refreshAdkRuns(client, organizationId, runId)
      toast.add({
        title: "Answer sent.",
        description: "The run carries on with it.",
        type: "success",
      })
    },
  })
}

/** A failed run, retried as its next attempt: it carries on from where it stopped. */
export function useRetryAdkRun(organizationId: string, runId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () =>
      api.post<AdkRun>(`${runPath(organizationId, runId)}/retry`),
    meta: { errorTitle: "Couldn't retry the run" },
    onSuccess: async (run) => {
      await refreshAdkRuns(client, organizationId, runId)
      toast.add({
        title: "Retrying.",
        description: `It carries on as attempt ${run.attempt}, from where it stopped.`,
        type: "success",
      })
    },
  })
}

/**
 * A finished run's payload, run again as a new run (another invocation of
 * the same ADK session). `onSuccess` gets the new run, e.g. to open it.
 */
export function useResubmitAdkRun(organizationId: string, runId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () =>
      api.post<AdkRun>(`${runPath(organizationId, runId)}/resubmit`),
    meta: { errorTitle: "Couldn't resubmit the run" },
    onSuccess: async (run) => {
      await refreshAdkRuns(client, organizationId, run.id)
      toast.add({
        title: "Resubmitted.",
        description: `It runs again as run ${shortId(run.id)}.`,
        type: "success",
      })
    },
  })
}

/** Give a run up: it never carries on. */
export function useAbandonAdkRun(
  organizationId: string,
  runId: string,
  { meta }: MutationMeta = {}
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () =>
      api.post<AdkRun>(`${runPath(organizationId, runId)}/abandon`),
    meta: { errorTitle: "Couldn't abandon the run", ...meta },
    onSuccess: async () => {
      await refreshAdkRuns(client, organizationId, runId)
      toast.add({ title: "Abandoned.", type: "success" })
    },
  })
}

/** Which version a run ran, as its page names it: "v3", "Draft · revision 17". */
export function versionLabel(
  run: Pick<AdkRun, "version" | "revision">
): string {
  if (typeof run.version === "number") return `v${run.version}`
  if (run.version === "draft") return `Draft · revision ${run.revision}`
  return `Revision ${run.revision}`
}

/** How a run came to be, from its trigger: by hand, over the API or A2A, from an agent or a workflow. */
export function triggerLabel(
  trigger: Record<string, unknown> | null | undefined
): string | undefined {
  if (!trigger) return undefined
  switch (trigger.type) {
    case "runtime":
      return trigger.protocol === "a2a" ? "Over A2A" : "Over the API"
    case "chat_agent":
      return "By a chat agent"
    case "workflow":
      return "By another workflow"
    case "manual":
      return "From the console"
    default:
      return undefined
  }
}
