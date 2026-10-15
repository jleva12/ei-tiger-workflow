import * as React from "react"
import { useAuiState, type ToolCallMessagePartProps } from "@assistant-ui/react"
import { useQuery } from "@tanstack/react-query"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
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
 * a mark a pulse runs through while it goes, what it's doing, and its steps
 * as it reaches them (the one at work turning, the line into it flowing),
 * each with when it started and how long it took. Followed through the
 * runtime while the call waits, and after, while the run still goes.
 */

const TONE: Record<RunPhase, string> = {
  starting: "var(--wf-running)",
  running: "var(--wf-running)",
  waiting: "var(--wf-waiting)",
  succeeded: "var(--wf-done)",
  failed: "var(--wf-failed)",
  stopped: "var(--muted-foreground)",
}

const PHASE_LABEL: Record<RunPhase, string> = {
  starting: "Starting",
  running: "Running",
  waiting: "Waiting",
  succeeded: "Done",
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

/** The run's mark: a little graph a pulse runs through while it goes. */
function RunMark({ phase }: { phase: RunPhase }) {
  const ended = !isGoing(phase)
  return (
    <span
      className="relative grid size-8 shrink-0 place-items-center rounded-(--radius-item) border bg-background"
      aria-hidden="true"
    >
      <svg
        data-slot="workflow-run-mark"
        data-phase={phase}
        viewBox="0 0 24 24"
        className="size-6"
        style={{ "--wf-tone": TONE[phase] } as React.CSSProperties}
      >
        <path className="wf-edge" d="M5 7 L12 17 L19 7" />
        {(phase === "running" || phase === "starting") && (
          <path className="wf-pulse" d="M5 7 L12 17 L19 7" pathLength={40} />
        )}
        <circle className="wf-node" cx="5" cy="7" r="2.6" />
        <circle className="wf-node" cx="12" cy="17" r="2.6" />
        <circle className="wf-node" cx="19" cy="7" r="2.6" />
      </svg>
      {ended && phase !== "stopped" && (
        <span
          className={cn(
            "absolute -right-1 -bottom-1 grid size-3.5 place-items-center rounded-full text-background ring-2 ring-card",
            phase === "succeeded" ? "bg-signal-success" : "bg-signal-failed"
          )}
        >
          <Icon icon={phase === "succeeded" ? "check" : "close"} size={9} />
        </span>
      )}
    </span>
  )
}

/** A step's dot on the rail. */
function StepDot({ state }: { state: TimelineStep["state"] }) {
  return (
    <span
      data-slot="workflow-run-dot"
      data-state={state}
      className={cn(
        "grid size-3.5 shrink-0 place-items-center rounded-full",
        state === "done" && "bg-signal-success text-background",
        state === "failed" && "bg-signal-failed text-background",
        state === "waiting" && "bg-warning-surface text-notice-accent"
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
      {state === "done" && <Icon icon="check" size={9} />}
      {state === "failed" && <Icon icon="close" size={9} />}
      {state === "waiting" && <Icon icon="clock" size={10} />}
    </span>
  )
}

function StepRow({
  step,
  first,
  last,
  flowIn,
  flowOut,
  runStart,
  now,
}: {
  step: TimelineStep
  first: boolean
  last: boolean
  /** The run went on to this step and it's at work: the line into it flows. */
  flowIn: false | "running" | "waiting"
  flowOut: false | "running" | "waiting"
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
      className="grid grid-cols-[0.875rem_minmax(0,1fr)_auto] gap-x-2.5"
    >
      <span className="flex flex-col items-center" aria-hidden="true">
        <span
          className={cn("wf-line h-2 shrink-0", first && "invisible")}
          data-flow={flowIn || undefined}
        />
        <StepDot state={step.state} />
        <span
          className={cn("wf-line min-h-1.5 flex-1", last && "invisible")}
          data-flow={flowOut || undefined}
        />
      </span>
      <span className="flex min-w-0 items-baseline gap-1.5 pt-1 pb-1.5">
        <span
          className={cn(
            "truncate text-xs",
            working ? "font-medium text-foreground" : "text-foreground"
          )}
        >
          {step.name}
        </span>
        <span className="shrink-0 text-3xs text-subtle">
          {kindLabel(step.kind)}
        </span>
      </span>
      <span
        className="flex items-baseline gap-2 pt-1 pb-1.5 font-mono text-3xs text-muted-foreground tabular-nums"
        title={
          step.startedAt !== undefined
            ? `Started ${new Date(step.startedAt).toLocaleTimeString()}`
            : undefined
        }
      >
        {offset !== undefined && (
          <span className="text-subtle">{offsetText(offset)}</span>
        )}
        {took && (
          <span
            className={cn(
              "min-w-10 text-right",
              step.state === "running" && "text-signal-running",
              step.state === "waiting" && "text-notice-accent"
            )}
          >
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
  const follower = React.useContext(WorkflowRunsContext)
  const sessionId = useSessionId()
  const answer = workflowAnswerOf(result)
  const answered = answer !== undefined || status.type !== "running"

  const query = useQuery({
    queryKey: ["workflow-run", follower?.key, sessionId, toolCallId],
    queryFn: ({ signal }) => follower!.progress(sessionId!, toolCallId, signal),
    enabled: Boolean(follower && sessionId && toolCallId),
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

  // Open: its steps are the point; folding keeps a long conversation short.
  const [open, setOpen] = React.useState(true)
  const title =
    follower?.names[toolName] || progress?.workflow || humanize(toolName)
  const line = headline(phase, steps, progress, answer, took)
  const tone = { "--wf-tone": TONE[phase] } as React.CSSProperties
  const href = progress && follower?.runHref?.(progress.run_id)

  return (
    <section
      data-slot="workflow-run"
      data-phase={phase}
      aria-label={`Workflow ${title}`}
      className="my-1.5 w-full max-w-xl overflow-hidden rounded-(--radius-card) border bg-card"
      style={tone}
    >
      <header className="flex items-center gap-3 px-3 py-2.5">
        <RunMark phase={phase} />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex min-w-0 items-baseline gap-2">
            <span className="truncate text-sm font-medium text-foreground">
              {title}
            </span>
            <span
              className={cn(
                "shrink-0 text-2xs font-medium",
                phase === "succeeded" && "text-success-foreground",
                phase === "failed" && "text-danger-foreground",
                (phase === "running" || phase === "starting") &&
                  "text-signal-running",
                phase === "waiting" && "text-notice-accent",
                phase === "stopped" && "text-muted-foreground"
              )}
            >
              {PHASE_LABEL[phase]}
            </span>
          </div>
          <p
            className="truncate text-xs text-muted-foreground"
            aria-live="polite"
            title={line}
          >
            {line}
          </p>
        </div>
        {took !== undefined && (
          <span
            className="shrink-0 font-mono text-2xs text-muted-foreground tabular-nums"
            aria-label={`${going ? "Running for" : "Took"} ${formatDuration(took)}`}
          >
            {clock(took)}
          </span>
        )}
        {href && (
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Open the run's page"
            nativeButton={false}
            render={<a href={href} target="_blank" rel="noreferrer" />}
          >
            <Icon icon="external" />
          </Button>
        )}
        {steps.length > 0 && (
          <Button
            variant="ghost"
            size="icon-xs"
            aria-expanded={open}
            aria-label={open ? "Hide the steps" : "Show the steps"}
            onClick={() => setOpen(!open)}
          >
            <Icon icon={open ? "up" : "down"} />
          </Button>
        )}
      </header>
      <div
        data-slot="workflow-run-bar"
        data-phase={phase}
        data-going={going || undefined}
        role="progressbar"
        aria-label={PHASE_LABEL[phase]}
        aria-busy={going}
      />
      {open && steps.length > 0 && (
        <ol className="flex flex-col px-3 pt-2 pb-1.5" aria-label="Steps">
          {steps.map((step, index) => {
            const next = steps[index + 1]
            const working = (s: TimelineStep | undefined) =>
              s && (s.state === "running" || s.state === "waiting")
                ? s.state
                : false
            return (
              <StepRow
                key={step.id}
                step={step}
                first={index === 0}
                last={index === steps.length - 1}
                flowIn={index > 0 && going ? working(step) : false}
                flowOut={going ? working(next) : false}
                runStart={start}
                now={now}
              />
            )
          })}
        </ol>
      )}
    </section>
  )
}
