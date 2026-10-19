import { isLiveRun, isOpenRun } from "@/features/runs/lib/display"
import { reachedSteps, runProgress } from "@/features/runs/lib/progress"
import type {
  AdkRunStatus,
  AdkRunStep,
  AdkStepStatus,
} from "@/features/runs/lib/runs"
import { isTimestamp, parseTimestamp } from "@/lib/timestamps"
import type { StepRunState } from "@/features/builder/components/run-overlay"

/*
 * A chat agent's workflow tools, followed from its chat: a call of one starts
 * a run, which the chat shows step by step while the call waits (the
 * runtime's `…/sessions/{session}/workflow-runs/{call}`), and as it ended
 * once the call answers.
 */

/** A step of the run, as a chat sees it: not what it was handed or handed on. */
export type WorkflowRunStep = Pick<
  AdkRunStep,
  "id" | "name" | "kind" | "started_at" | "finished_at" | "handed_to"
> & { status: AdkStepStatus }

/** How a run a call started is going. */
export type WorkflowRunProgress = {
  run_id: string
  /** The workflow's name, as its builder names it. */
  workflow: string
  status: AdkRunStatus
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  /** What a run that waits for a time waits for. */
  waiting_reason: string | null
  error: { message: string } | null
  /** The steps it reached so far, and its start, in its document's order. */
  steps: WorkflowRunStep[]
}

/** What a workflow tool answered: the run, and where it had got to. */
export type WorkflowCallAnswer = {
  runId?: string
  status?: AdkRunStatus | "missing"
  /** Why the call failed, or the run did. */
  error?: string
}

type Headers = Record<string, string> | (() => Record<string, string>)

/** Reads how the runs a conversation's workflow calls started are going. */
export type WorkflowRunFollower = {
  /** Which runtime, agent and person: part of every query's key. */
  key: string
  /** The workflow tools by the names the model calls them, as the canvas names them. */
  names: Readonly<Record<string, string>>
  /** The run's page, for people who can open it (the builder's chat). */
  runHref?: (runId: string) => string
  /** The run a call started; null when it started none (yet). */
  progress: (
    sessionId: string,
    callId: string,
    signal?: AbortSignal
  ) => Promise<WorkflowRunProgress | null>
}

/** Follows a chat's workflow calls through the runtime its chat talks to. */
export function workflowRunFollower({
  url,
  appName,
  userId,
  headers,
  names,
  runHref,
}: {
  /** The run API: the chat's `adk.url`. */
  url: string
  appName: string
  userId: string
  headers?: Headers
  names: Readonly<Record<string, string>>
  runHref?: (runId: string) => string
}): WorkflowRunFollower {
  const base = `${url.replace(/\/$/, "")}/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions`
  return {
    key: `${url}|${appName}|${userId}`,
    names,
    ...(runHref && { runHref }),
    async progress(sessionId, callId, signal) {
      const response = await fetch(
        `${base}/${encodeURIComponent(sessionId)}/workflow-runs/${encodeURIComponent(callId)}`,
        {
          headers: typeof headers === "function" ? headers() : headers,
          signal,
        }
      )
      if (response.status === 404) return null
      if (!response.ok)
        throw new Error(`The run couldn't be read (${response.status})`)
      return (await response.json()) as WorkflowRunProgress
    },
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

const messageOf = (value: unknown) =>
  typeof value === "string"
    ? value
    : isRecord(value) && typeof value.message === "string"
      ? value.message
      : undefined

/**
 * What a workflow call answered, from its result as the chat has it (ADK's
 * response as JSON text or a value): undefined before it answers. Forge's
 * tools answer in an envelope, `{status: "success", payload}` or
 * `{status: "failed", reason}`: a call that couldn't start the run answers
 * with why only.
 */
export function workflowAnswerOf(
  result: unknown
): WorkflowCallAnswer | undefined {
  let value = result
  if (typeof result === "string") {
    try {
      value = JSON.parse(result)
    } catch {
      return result ? { error: result } : undefined
    }
  }
  if (!isRecord(value)) return undefined
  if (value.status === "failed" && typeof value.reason === "string")
    return { error: value.reason || "The workflow couldn't be run." }
  if (value.status === "success" && "payload" in value) value = value.payload
  if (!isRecord(value)) return undefined
  if (typeof value.run_id === "string" || typeof value.status === "string") {
    const answer: WorkflowCallAnswer = {}
    if (typeof value.run_id === "string") answer.runId = value.run_id
    if (typeof value.status === "string")
      answer.status = value.status as WorkflowCallAnswer["status"]
    const error = messageOf(value.error)
    if (error) answer.error = error
    return answer
  }
  const error = messageOf(value.error) ?? messageOf(value.result)
  return { error: error || "The workflow couldn't be run." }
}

/** A workflow an intake agent started when it completed, as it handed it back. */
export type StartedRun = {
  /** The workflow's name. */
  workflow: string
  /** The call that started it, which the runtime knows the run by; none when it couldn't start. */
  call_id?: string
  run_id?: string
  status?: string
  result?: unknown
  /** Why it couldn't start, or failed. */
  error?: unknown
}

/**
 * The workflows an intake agent says it started as it completed (the
 * `workflows` of its `finish_task` call, or of what it handed back); none
 * for anything else.
 */
export function startedRunsOf(result: unknown): StartedRun[] {
  let value = result
  if (typeof result === "string") {
    try {
      value = JSON.parse(result)
    } catch {
      return []
    }
  }
  const runs = isRecord(value) ? value.workflows : undefined
  if (!Array.isArray(runs)) return []
  return runs.filter(
    (run): run is StartedRun =>
      isRecord(run) &&
      typeof run.workflow === "string" &&
      (run.call_id === undefined || typeof run.call_id === "string")
  )
}

/** A run a submitted request started, with the request's intake. */
export type SubmittedRun = StartedRun & { intake: string }

/**
 * The workflows the requests an agent submitted started (`submit_requests`'
 * answer: each submitted request's `workflows`, from its intake's On
 * complete way); none for anything else.
 */
export function submittedRunsOf(result: unknown): SubmittedRun[] {
  let value = result
  if (typeof result === "string") {
    try {
      value = JSON.parse(result)
    } catch {
      return []
    }
  }
  const submitted = isRecord(value) ? value.submitted : undefined
  if (!Array.isArray(submitted)) return []
  return submitted.flatMap((request) =>
    isRecord(request)
      ? startedRunsOf(request).map((run) => ({
          ...run,
          intake: typeof request.intake === "string" ? request.intake : "",
        }))
      : []
  )
}

const time = (iso: string | null | undefined) =>
  iso && isTimestamp(iso) ? parseTimestamp(iso).getTime() : undefined

/** A step on the timeline: where it is, and when it started and ended. */
export type TimelineStep = {
  id: string
  name: string
  kind: string
  state: Exclude<StepRunState, "idle">
  /**
   * When it started, in ms. A step at work that hasn't recorded anything
   * yet (ADK records most steps once they hand on) started as the run went
   * on to it: when the step before finished, or the run started.
   */
  startedAt?: number
  /** How long it took, once it finished. */
  tookMs?: number
}

/**
 * The run's steps in the order it reached them, each with where it is: the
 * one at work running (or waiting for a person); done, failed. Before it's
 * recorded anything, its start is at work.
 */
export function timelineOf(progress: WorkflowRunProgress): TimelineStep[] {
  const data = { steps: progress.steps as AdkRunStep[], taken_edges: [] }
  const states = runProgress(progress, data)
  const steps = reachedSteps(data, states).flatMap((step): TimelineStep[] => {
    const state = states.steps.get(step.id)
    if (!state || state === "idle") return []
    const startedAt = time(step.started_at)
    const finishedAt = time(step.finished_at)
    return [
      {
        id: step.id,
        name: step.name || step.id,
        kind: step.kind,
        state,
        ...(startedAt !== undefined && { startedAt }),
        ...(startedAt !== undefined &&
          finishedAt !== undefined &&
          finishedAt >= startedAt && { tookMs: finishedAt - startedAt }),
      },
    ]
  })
  // The last transition: when the latest step to finish did.
  const handedOn = steps.reduce<number | undefined>(
    (latest, step) =>
      step.startedAt !== undefined && step.tookMs !== undefined
        ? Math.max(latest ?? 0, step.startedAt + step.tookMs)
        : latest,
    undefined
  )
  const went = handedOn ?? runStartOf(progress)
  return steps.map((step) =>
    step.startedAt === undefined &&
    (step.state === "running" || step.state === "waiting") &&
    went !== undefined
      ? { ...step, startedAt: went }
      : step
  )
}

/** When the run began: it started, or was asked for. */
export const runStartOf = (progress: WorkflowRunProgress | undefined) =>
  time(progress?.started_at) ?? time(progress?.created_at)

/** When the run ended, once it has. */
export const runEndOf = (progress: WorkflowRunProgress | undefined) =>
  progress && !isOpenRun(progress) ? time(progress.finished_at) : undefined

/** Where a run is, as the chat's card says it. */
export type RunPhase =
  "starting" | "running" | "waiting" | "succeeded" | "failed" | "stopped"

/**
 * Where the run is, from what was read of it and what the call answered:
 * the call's answer settles a run the chat couldn't read.
 */
export function phaseOf(
  progress: WorkflowRunProgress | undefined,
  answer: WorkflowCallAnswer | undefined
): RunPhase {
  const status = progress?.status ?? answer?.status
  switch (status) {
    case "running":
      return "running"
    case "paused":
    case "waiting":
      return "waiting"
    case "succeeded":
      return "succeeded"
    case "failed":
    case "missing":
      return "failed"
    case "abandoned":
      return "stopped"
    case "queued":
      return "starting"
    default:
      // The call answered without a run: it couldn't start one.
      return answer?.error ? "failed" : "starting"
  }
}

/** Whether the run may still change: worth asking about again. */
export const isGoing = (phase: RunPhase) =>
  phase === "starting" || phase === "running" || phase === "waiting"

/** How often to ask how it's going, in ms; false to stop. */
export function followEvery(
  progress: WorkflowRunProgress | null | undefined,
  answered: boolean
): number | false {
  // Waiting for its call: it's moving, or about to.
  if (!answered) return 1500
  // The call answered while the run still goes (it waits for a person, or
  // took longer than the call waits): less often, until it ends.
  if (progress && isOpenRun(progress)) return isLiveRun(progress) ? 2500 : 5000
  return false
}
