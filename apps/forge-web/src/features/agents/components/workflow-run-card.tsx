import * as React from "react"
import { useAuiState, type ToolCallMessagePartProps } from "@assistant-ui/react"
import { useQuery } from "@tanstack/react-query"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { useDisclosureAnchor } from "@/components/assistant-ui/elements/use-disclosure-anchor"
import { cn } from "@/lib/utils"
import {
  isServedKind,
  kindInfo,
  nodeCatalogInstalled,
} from "@/features/adk-workflows/lib/catalog"
import type { AgentKind } from "@/features/adk-workflows/lib/model"
import { humanize } from "@/features/runs/lib/runs"
import {
  followEvery,
  isGoing,
  phaseOf,
  runEndOf,
  runStartOf,
  timelineOf,
  workflowAnswerOf,
  type RunPhase,
  type TimelineStep,
  type WorkflowCallAnswer,
  type WorkflowRunProgress,
} from "@/features/agents/lib/workflow-runs"
import { WorkflowRunsContext } from "@/features/agents/lib/workflow-run-context"
import { formatDuration } from "@/lib/format"

import "./workflow-run-card.css"

/*
 * A call of one of the agent's workflow tools, drawn as the run it starts:
 * what it's doing, and a quiet timeline of its steps as it reaches them,
 * each with when it started and how long it took. Followed through the
 * runtime while the call waits, and after, while the run still goes.
 */

const PHASE_LABEL: Record<RunPhase, string> = {
  starting: "Starting",
  running: "Running",
  waiting: "Waiting",
  succeeded: "Completed",
  failed: "Failed",
  stopped: "Stopped",
}

/** Now, every second while `ticking`. */
function useNow(ticking: boolean) {
  const [now, setNow] = React.useState(() => Date.now())
  React.useEffect(() => {
    if (!ticking) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [ticking])
  return now
}

/** "0:07", "2:41", "1:02:03": a run's clock. */
function clock(ms: number) {
  const seconds = Math.max(0, Math.round(ms / 1000))
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = String(seconds % 60).padStart(2, "0")
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`
}

/** When a step started, after the run did: "+0.4s", "+12.0s", "+1:05". */
const offsetText = (ms: number) =>
  ms < 60_000 ? `+${(ms / 1000).toFixed(1)}s` : `+${clock(ms)}`

/** The step's kind in words: as the workflow builder names it, when it's loaded. */
function kindLabel(kind: string) {
  if (nodeCatalogInstalled() && isServedKind(kind))
    return kindInfo(kind as AgentKind).label
  return kind === "llm" ? "LLM agent" : humanize(kind)
}

/** What the run is doing, in a line under its name. */
function headline(
  phase: RunPhase,
  steps: TimelineStep[],
  progress: WorkflowRunProgress | undefined,
  answer: WorkflowCallAnswer | undefined,
  tookMs: number | undefined
) {
  const at = (state: TimelineStep["state"]) =>
    steps.findLast((step) => step.state === state)
  switch (phase) {
    case "starting":
      return progress
        ? "Queued: a worker picks it up next"
        : "Starting the run…"
    case "running": {
      const step = at("running")
      if (!step) return "Running"
      const number = steps.indexOf(step) + 1
      return `Step ${number}: ${step.name}`
    }
    case "waiting": {
      const step = at("waiting")
      if (step) return `Waiting for someone at ${step.name}`
      return progress?.waiting_reason || "Waiting until it's time to go on"
    }
    case "succeeded": {
      const count = steps.length
      const took = tookMs !== undefined ? ` in ${formatDuration(tookMs)}` : ""
      return `Finished ${count} ${count === 1 ? "step" : "steps"}${took}`
    }
    case "failed": {
      const message = progress?.error?.message || answer?.error
      const step = at("failed")
      if (!progress && !answer?.runId)
        return `Couldn't start: ${message || "the workflow didn't run"}`
      const where = step ? ` at ${step.name}` : ""
      return message ? `Failed${where}: ${message}` : `Failed${where}`
    }
    case "stopped":
      return "Stopped before it finished"
  }
}

/** A step's dot on the rail. */
function StepDot({ state }: { state: TimelineStep["state"] }) {
  return (
    <span
      data-slot="workflow-run-dot"
      data-state={state}
      className={cn(
        "grid size-4 shrink-0 place-items-center rounded-full text-muted-foreground",
        state === "failed" && "bg-danger-surface text-danger-foreground",
        state === "waiting" && "bg-warning-surface text-warning-foreground"
      )}
      role="img"
      aria-label={
        state === "done"
          ? "Done"
          : state === "failed"
            ? "Failed"
            : state === "waiting"
              ? "Waiting"
              : "Running"
      }
    >
      {state === "done" && <Icon icon="check" className="size-3" />}
      {state === "failed" && <Icon icon="close" className="size-3" />}
      {state === "waiting" && <Icon icon="clock" className="size-3" />}
      {state === "running" && (
        <Icon icon="loading" className="wf-spinner size-3.5" />
      )}
    </span>
  )
}

function StepRow({
  step,
  first,
  last,
  runStart,
  now,
}: {
  step: TimelineStep
  first: boolean
  last: boolean
  runStart: number | undefined
  now: number
}) {
  const offset =
    step.startedAt !== undefined && runStart !== undefined
      ? Math.max(0, step.startedAt - runStart)
      : undefined
  const working = step.state === "running" || step.state === "waiting"
  const took =
    step.tookMs !== undefined
      ? formatDuration(step.tookMs)
      : working && step.startedAt !== undefined
        ? formatDuration(Math.max(0, now - step.startedAt))
        : working
          ? "now"
          : undefined
  return (
    <li
      data-slot="workflow-run-step"
      data-state={step.state}
      className="grid grid-cols-[1rem_minmax(0,1fr)_auto] gap-x-2.5"
    >
      <span className="flex flex-col items-center">
        <span
          className={cn("wf-line h-2 shrink-0", first && "invisible")}
          aria-hidden="true"
        />
        <StepDot state={step.state} />
        <span
          className={cn("wf-line min-h-2 flex-1", last && "invisible")}
          aria-hidden="true"
        />
      </span>
      <span className="wf-step-label flex min-w-0 flex-wrap items-baseline gap-x-2 pt-1.5 pb-2">
        <span
          className={cn(
            "min-w-0 text-xs wrap-anywhere",
            working ? "font-medium text-foreground" : "text-foreground"
          )}
        >
          {step.name}
        </span>
        <span className="text-2xs text-muted-foreground">
          {kindLabel(step.kind)}
        </span>
      </span>
      <span
        className="flex items-baseline gap-3 pt-1.5 pb-2 text-2xs text-muted-foreground tabular-nums"
        title={
          step.startedAt !== undefined
            ? `Started ${new Date(step.startedAt).toLocaleTimeString()}`
            : undefined
        }
      >
        {offset !== undefined && (
          <span className="wf-step-offset text-subtle">
            <span className="sr-only">Started after </span>
            {offsetText(offset)}
          </span>
        )}
        {took && (
          <span
            className={cn(
              "min-w-10 text-right",
              working && "font-medium text-foreground"
            )}
          >
            <span className="sr-only">Duration </span>
            {took}
          </span>
        )}
      </span>
    </li>
  )
}

/** The current conversation's ADK session: the runtime knows runs by it. */
const useSessionId = () =>
  useAuiState(
    (s) =>
      s.threads.threadItems.find((t) => t.id === s.threads.mainThreadId)
        ?.remoteId
  )

export function WorkflowRunCard({
  toolName,
  toolCallId,
  result,
  status,
}: ToolCallMessagePartProps) {
  const answered =
    workflowAnswerOf(result) !== undefined || status.type !== "running"
  return (
    <WorkflowRun
      name={toolName}
      callId={toolCallId}
      result={result}
      answered={answered}
    />
  )
}

/**
 * A workflow's run, followed through the runtime by the call that started
 * it: a workflow tool's call, or an intake agent's completing (`callId` from
 * what it handed back). Without a call it shows how it ended (a run that
 * couldn't start says why).
 */
export function WorkflowRun({
  name,
  title: named,
  callId,
  result,
  answered,
}: {
  /** The tool the model called it by; for its title. */
  name: string
  /** Its title, when it isn't a tool's. */
  title?: string
  /** The call that started it: what the runtime knows the run by. */
  callId?: string
  /** What the call answered: where the run got to. */
  result: unknown
  /** The call has answered: no longer waiting on it. */
  answered: boolean
}) {
  const follower = React.useContext(WorkflowRunsContext)
  const sessionId = useSessionId()
  const answer = workflowAnswerOf(result)

  const query = useQuery({
    queryKey: ["workflow-run", follower?.key, sessionId, callId],
    queryFn: ({ signal }) => follower!.progress(sessionId!, callId!, signal),
    enabled: Boolean(follower && sessionId && callId),
    refetchInterval: (q) => followEvery(q.state.data, answered),
    retry: false,
    refetchOnWindowFocus: false,
    meta: { silent: true },
  })
  // Once the call answers, read the run as it ended: the last look may
  // have been just before.
  const { refetch } = query
  const wasAnswered = React.useRef(answered)
  React.useEffect(() => {
    if (answered && !wasAnswered.current) void refetch()
    wasAnswered.current = answered
  }, [answered, refetch])

  const progress = query.data ?? undefined
  const phase = phaseOf(progress, answer)
  const going = isGoing(phase)
  const steps = React.useMemo(
    () => (progress ? timelineOf(progress) : []),
    [progress]
  )
  const now = useNow(going)
  const start = runStartOf(progress) ?? steps[0]?.startedAt
  const end =
    runEndOf(progress) ??
    (going
      ? now
      : steps.reduce<number | undefined>(
          (latest, step) =>
            step.startedAt !== undefined
              ? Math.max(latest ?? 0, step.startedAt + (step.tookMs ?? 0))
              : latest,
          undefined
        ))
  const took =
    start !== undefined && end !== undefined ? end - start : undefined

  // Keep reached steps visible by default, with the same disclosure as chat tools.
  const [open, setOpen] = React.useState(true)
  const cardRef = React.useRef<HTMLElement>(null)
  const stepsId = React.useId()
  const holdInPlace = useDisclosureAnchor(cardRef, 0)
  const hasSteps = steps.length > 0
  const Header = hasSteps ? "button" : "div"
  const title =
    named || follower?.names[name] || progress?.workflow || humanize(name)
  const line = headline(phase, steps, progress, answer, took)
  const href = progress && follower?.runHref?.(progress.run_id)

  return (
    <section
      ref={cardRef}
      data-slot="workflow-run"
      data-phase={phase}
      aria-label={`Workflow ${title}`}
      className="my-1.5 w-full min-w-0 rounded-(--radius-card) border bg-background"
    >
      <header className="flex items-center">
        <Header
          type={hasSteps ? "button" : undefined}
          data-slot="workflow-run-trigger"
          aria-expanded={hasSteps ? open : undefined}
          aria-controls={hasSteps ? stepsId : undefined}
          onClick={
            hasSteps
              ? () => {
                  holdInPlace()
                  setOpen((value) => !value)
                }
              : undefined
          }
          className={cn(
            "flex min-w-0 flex-1 items-center gap-3 rounded-(--radius-card) px-4 py-3 text-left",
            hasSteps &&
              "cursor-pointer transition-colors duration-150 hover:bg-muted/40"
          )}
        >
          <Icon
            icon="branch"
            className="size-4 shrink-0 text-muted-foreground"
          />
          <span className="flex min-w-0 flex-1 flex-col gap-1">
            <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              <span className="min-w-0 text-sm font-medium wrap-anywhere text-foreground">
                {title}
              </span>
              <span
                data-slot="workflow-run-status"
                className={cn(
                  "inline-flex shrink-0 items-center gap-1 rounded-(--radius-chip) bg-muted px-1.5 py-0.5 text-3xs font-medium text-muted-foreground",
                  phase === "failed" &&
                    "bg-danger-surface text-danger-foreground",
                  phase === "waiting" &&
                    "bg-warning-surface text-warning-foreground"
                )}
              >
                {phase === "succeeded" && (
                  <Icon icon="check" className="size-3" />
                )}
                {phase === "failed" && (
                  <Icon icon="warning" className="size-3" />
                )}
                {phase === "waiting" && (
                  <Icon icon="clock" className="size-3" />
                )}
                {(phase === "running" || phase === "starting") && (
                  <Icon icon="loading" className="wf-spinner size-3" />
                )}
                {PHASE_LABEL[phase]}
              </span>
            </span>
            <span
              className={cn(
                "text-xs wrap-anywhere text-muted-foreground",
                phase === "failed" && "text-danger-foreground"
              )}
              aria-live="polite"
            >
              {line}
            </span>
          </span>
          {going && took !== undefined && (
            <span
              className="shrink-0 text-2xs text-muted-foreground tabular-nums"
              aria-label={`Running for ${formatDuration(took)}`}
            >
              {clock(took)}
            </span>
          )}
          {hasSteps && (
            <Icon
              icon="down"
              className={cn(
                "wf-chevron size-3.5 shrink-0 text-muted-foreground",
                !open && "-rotate-90"
              )}
            />
          )}
        </Header>
        {href && (
          <Button
            variant="ghost"
            size="icon-xs"
            className="mr-3 shrink-0 text-muted-foreground"
            aria-label="Open the run's page"
            nativeButton={false}
            render={<a href={href} target="_blank" rel="noreferrer" />}
          >
            <Icon icon="external" />
          </Button>
        )}
      </header>
      {hasSteps && (
        <ol
          id={stepsId}
          hidden={!open}
          className="border-t px-4 py-2"
          aria-label={`${title} steps`}
        >
          {steps.map((step, index) => (
            <StepRow
              key={step.id}
              step={step}
              first={index === 0}
              last={index === steps.length - 1}
              runStart={start}
              now={now}
            />
          ))}
        </ol>
      )}
    </section>
  )
}
