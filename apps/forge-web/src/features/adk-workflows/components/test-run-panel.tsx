import * as React from "react"
import { Link } from "@tanstack/react-router"
import { PlayIcon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { KindGlyph } from "@/features/builder/components/glyph"
import {
  RunOverlayContext,
  type StepRunState,
} from "@/features/builder/components/run-overlay"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { StatusBadge } from "@/components/forge/status"
import { LiveLabel } from "@/components/forge/task-page"
import { JsonView } from "@/features/json/components/json-view"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { AgentDocument } from "@/features/adk-workflows/lib/document"
import { kindInfo } from "@/features/adk-workflows/lib/catalog"
import { StatusNotice } from "@/features/runs/components/adk-run-page"
import { runStatusDisplay } from "@/features/runs/lib/display"
import { reachedSteps, runProgress } from "@/features/runs/lib/progress"
import {
  calledWithTitle,
  eventTitle,
  failureTitle,
  isFailureEvent,
  shortId,
  stepDurationMs,
  stepErrorOf,
  stepKindOf,
  stepNameOf,
  useAdkRun,
  useAdkRunSessionFiles,
  useAdkRunSteps,
  versionLabel,
  type AdkRunDetail,
  type AdkRunStep,
} from "@/features/runs/lib/runs"
import {
  RunFileActions,
  RunFileIcon,
  RunFileName,
  RunFileViewer,
} from "@/features/runs/components/run-files"
import {
  inputFileRef,
  runFileKey,
  savedFileRef,
  useRunFileDownload,
  type RunFileRef,
} from "@/features/runs/lib/run-files"
import { formatBytes, formatDuration } from "@/lib/format"
import { isTimestamp, parseTimestamp } from "@/lib/timestamps"

/*
 * A test run, followed from its workflow's builder: the run's steps and
 * events in a panel beside the canvas, and its progress drawn over the
 * canvas (the step at work spinning, the lines it went along). Both read the
 * same queries, refreshed every second and a half while it goes.
 */

const time = (iso: string | null | undefined) =>
  iso && isTimestamp(iso) ? parseTimestamp(iso).getTime() : undefined

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

/** The run's progress over the canvas it was started from. */
export function TestRunOverlay({
  organizationId,
  runId,
  children,
}: {
  organizationId: string
  /** The run followed; none draws nothing over the canvas. */
  runId: string | undefined
  children: React.ReactNode
}) {
  const detail = useAdkRun(organizationId, runId, { close: true })
  const steps = useAdkRunSteps(
    organizationId,
    runId ? detail.data : undefined,
    {
      close: true,
    }
  )
  const progress = React.useMemo(
    () => (runId && detail.data ? runProgress(detail.data, steps.data) : null),
    [runId, detail.data, steps.data]
  )
  return (
    <RunOverlayContext.Provider value={progress}>
      {children}
    </RunOverlayContext.Provider>
  )
}

const STATE_ICON: Record<
  Exclude<StepRunState, "idle">,
  { label: string; className: string }
> = {
  running: { label: "Running", className: "text-signal-running" },
  done: { label: "Done", className: "text-signal-success" },
  failed: { label: "Failed", className: "text-signal-failed" },
  waiting: { label: "Waiting", className: "text-notice-accent" },
}

function StateIcon({ state }: { state: StepRunState }) {
  if (state === "idle") return <span className="size-4 shrink-0" />
  const shown = STATE_ICON[state]
  return (
    <span
      role="img"
      aria-label={shown.label}
      className={cn("grid size-4 shrink-0 place-items-center", shown.className)}
    >
      {state === "running" ? (
        <Spinner className="size-3.5" />
      ) : (
        <Icon
          icon={
            state === "done" ? "check" : state === "failed" ? "failed" : "clock"
          }
          size={14}
        />
      )}
    </span>
  )
}

function StepRow({
  step,
  state,
  doc,
  now,
  selected,
  onShow,
}: {
  step: AdkRunStep
  state: StepRunState
  doc: AgentDocument | undefined
  now: number
  selected: boolean
  onShow: () => void
}) {
  const kind = stepKindOf(step, doc)
  const info = kind && kindInfo(kind)
  const took = stepDurationMs(step)
  const since = time(step.started_at)
  const timing =
    took !== undefined
      ? formatDuration(took)
      : state === "running" || state === "waiting"
        ? since !== undefined
          ? formatDuration(Math.max(0, now - since))
          : "now"
        : undefined
  return (
    <li>
      <button
        type="button"
        onClick={onShow}
        aria-current={selected || undefined}
        className={cn(
          "flex w-full min-w-0 items-center gap-2.5 rounded-(--radius-item) px-2 py-1.5 text-left hover:bg-accent/60",
          selected && "bg-accent hover:bg-accent"
        )}
      >
        <StateIcon state={state} />
        {info && <KindGlyph info={info} size="sm" />}
        <span className="min-w-0 flex-1 truncate text-xs text-foreground">
          {stepNameOf(step, doc)}
        </span>
        {(step.calls?.length ?? 0) > 0 && (
          <span className="shrink-0 text-3xs text-muted-foreground">
            {step.calls!.length} {step.calls!.length === 1 ? "tool" : "tools"}
          </span>
        )}
        {timing && (
          <span className="shrink-0 font-mono text-3xs text-muted-foreground tabular-nums">
            {timing}
          </span>
        )}
      </button>
      {selected && <StepDetail step={step} kind={kind} doc={doc} />}
    </li>
  )
}

/** A value a step had, under a small heading. */
function Detail({ title, value }: { title: string; value: unknown }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-3xs font-medium text-muted-foreground">
        {title}
      </span>
      <JsonView value={value} className="max-h-60 text-3xs" />
    </div>
  )
}

/**
 * What a selected step did, to debug it from the panel: why it failed (and
 * what the error carries), what it was handed, what it worked out and sent,
 * the tools its agents called (and what went wrong for each that failed),
 * and what it handed on.
 */
function StepDetail({
  step,
  kind,
  doc,
}: {
  step: AdkRunStep
  kind: string | undefined
  doc: AgentDocument | undefined
}) {
  const error = stepErrorOf(step.error)
  const has = (value: unknown) => value !== null && value !== undefined
  const calls = step.calls ?? []
  if (
    !error &&
    !has(step.input) &&
    !has(step.called_with) &&
    !has(step.output) &&
    calls.length === 0
  )
    return null
  return (
    <div className="mt-1 mb-2 flex min-w-0 flex-col gap-2 pl-8">
      {error && (
        <p className="text-2xs wrap-anywhere text-destructive">
          {step.status === "failed"
            ? error.message
            : `Took its Error way: ${error.message}`}
          {error.code && (
            <code className="ml-1.5 text-3xs text-muted-foreground">
              {error.code}
            </code>
          )}
        </p>
      )}
      {error && has(error.more) && (
        <Detail title="Error details" value={error.more} />
      )}
      {has(step.input) && (
        <Detail
          title={
            step.input_from
              ? `Input, from ${stepNameOf({ id: step.input_from, name: "", kind: "" }, doc)}`
              : "Input"
          }
          value={step.input}
        />
      )}
      {has(step.called_with) && (
        <Detail title={calledWithTitle(kind)} value={step.called_with} />
      )}
      {calls.length > 0 && (
        <div className="flex min-w-0 flex-col gap-1">
          <span className="text-3xs font-medium text-muted-foreground">
            {calls.length === 1 ? "1 tool call" : `${calls.length} tool calls`}
          </span>
          {calls.map((call) => (
            <div key={call.id} className="flex min-w-0 flex-col gap-0.5">
              <span className="flex min-w-0 items-center gap-1.5 text-2xs">
                <code className="min-w-0 truncate text-foreground">
                  {call.name}
                </code>
                <span
                  className={cn(
                    "shrink-0",
                    call.status === "failed"
                      ? "text-destructive"
                      : "text-muted-foreground"
                  )}
                >
                  {CALL_STATUS[call.status] ?? call.status}
                </span>
              </span>
              {call.error && (
                <p className="text-3xs wrap-anywhere text-destructive">
                  {call.error}
                </p>
              )}
              <JsonView
                value={{ sent: call.args, answered: call.response }}
                className="max-h-40 text-3xs"
              />
            </div>
          ))}
        </div>
      )}
      {has(step.output) && <Detail title="Output" value={step.output} />}
    </div>
  )
}

const CALL_STATUS: Record<string, string> = {
  done: "answered",
  failed: "failed",
  waiting: "waits to be allowed",
  running: "calling",
}

/**
 * The run's files: what it started with, and what its agents saved (each
 * one's latest), as they come; each previews in the file viewer, or
 * downloads.
 */
function Files({
  organizationId,
  run,
  doc,
}: {
  organizationId: string
  run: AdkRunDetail
  doc: AgentDocument | undefined
}) {
  const sessionFiles = useAdkRunSessionFiles(organizationId, run)
  const { download, fetching } = useRunFileDownload(organizationId, run.id)
  const [previewing, setPreviewing] = React.useState<RunFileRef>()
  const files = React.useMemo(() => {
    const saved = (sessionFiles.data?.files ?? []).flatMap((file) => {
      const latest = file.origin === "run" ? file.versions.at(-1) : undefined
      return latest
        ? [{ ref: savedFileRef(file, latest, doc), saved: true }]
        : []
    })
    return [
      ...(run.files ?? []).map((file) => ({
        ref: inputFileRef(file),
        saved: false,
      })),
      ...saved,
    ]
  }, [sessionFiles.data, run.files, doc])
  if (!files.length) return null
  return (
    <section className="flex flex-col gap-1.5">
      <h3 className="text-xs font-[550] text-foreground">Files</h3>
      <ul className="flex flex-col gap-0.5">
        {files.map(({ ref, saved }) => (
          <li
            key={runFileKey(ref)}
            className="flex min-w-0 items-center gap-2 rounded-(--radius-item) px-2 py-1 hover:bg-accent/60"
          >
            <RunFileIcon name={ref.name} size={14} />
            <span className="flex min-w-0 flex-1 flex-col">
              <RunFileName
                file={ref}
                onPreview={() => setPreviewing(ref)}
                className="text-xs"
              />
              <span className="truncate text-3xs text-muted-foreground">
                {[
                  ref.size ? formatBytes(ref.size) : null,
                  saved ? ref.detail || "saved by the run" : "started with",
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </span>
            <RunFileActions
              file={ref}
              fetching={fetching === runFileKey(ref)}
              onPreview={() => setPreviewing(ref)}
              onDownload={() => void download(ref)}
            />
          </li>
        ))}
      </ul>
      <RunFileViewer
        organizationId={organizationId}
        runId={run.id}
        files={files.map(({ ref }) => ref)}
        open={previewing}
        onOpen={setPreviewing}
        onClose={() => setPreviewing(undefined)}
      />
    </section>
  )
}

function Activity({ run }: { run: AdkRunDetail }) {
  const start = time(run.created_at) ?? 0
  if (!run.events.length) return null
  return (
    <ol className="flex flex-col gap-1.5">
      {run.events.map((event) => {
        const at = time(event.at)
        return (
          <li key={event.id} className="flex min-w-0 gap-2 text-2xs">
            <span className="w-11 shrink-0 text-right font-mono text-subtle tabular-nums">
              {at !== undefined
                ? `+${formatDuration(Math.max(0, at - start))}`
                : ""}
            </span>
            <span
              className={cn(
                "min-w-0 flex-1 wrap-anywhere",
                isFailureEvent(event)
                  ? "text-destructive"
                  : "text-muted-foreground"
              )}
            >
              <span className="font-medium text-foreground">
                {eventTitle(event)}
              </span>
              {event.message ? ` · ${event.message}` : ""}
              {event.actor?.name ? ` · by ${event.actor.name}` : ""}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

/**
 * A test run beside the canvas it was started from: where it stands (and
 * what it waits for, decided or answered here), each step it reached, in
 * order, the one at work spinning, each opening on the canvas, its events,
 * and its result. Run again starts another; the full page has everything.
 */
export function TestRunPanel({
  organizationId,
  runId,
  doc,
  selectedStep,
  onShowStep,
  onRunAgain,
  onClose,
}: {
  organizationId: string
  runId: string
  /** The workflow on the canvas, for its steps' names and kinds. */
  doc: AgentDocument | undefined
  /** The step selected on the canvas. */
  selectedStep: string | undefined
  /** Shows a step on the canvas. */
  onShowStep: (id: string) => void
  /** Opens the run dialog again; none when running isn't possible now. */
  onRunAgain?: () => void
  onClose: () => void
}) {
  const detail = useAdkRun(organizationId, runId, { close: true })
  const run = detail.data
  const steps = useAdkRunSteps(organizationId, run, { close: true })
  const progress = React.useMemo(
    () => runProgress(run, steps.data),
    [run, steps.data]
  )
  const reached = React.useMemo(
    () => reachedSteps(steps.data, progress),
    [steps.data, progress]
  )
  const notReached = (steps.data?.steps.length ?? 0) - reached.length
  const now = useNow(progress.live)
  const shown = run && runStatusDisplay(run)
  const began = time(run?.created_at)
  const elapsed =
    run?.duration_ms ??
    (began !== undefined && run && !run.finished_at ? now - began : null)
  const hasResult = run?.result !== null && run?.result !== undefined

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-col gap-2 border-b px-4 pt-3.5 pb-3">
        <div className="flex items-center gap-2">
          <Icon icon={PlayIcon} size={15} className="text-muted-foreground" />
          <h2 className="text-sm font-[550]">Test run</h2>
          <code className="text-3xs text-subtle">{shortId(runId)}</code>
          <span className="ml-auto flex items-center gap-1">
            <Tooltip>
              <TooltipTrigger
                render={
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Open the run's page"
                    nativeButton={false}
                    render={
                      <Link
                        to="/organizations/$organizationId"
                        params={{ organizationId }}
                        search={{ view: "agents", agentRun: runId }}
                      />
                    }
                  />
                }
              >
                <Icon icon="external" />
              </TooltipTrigger>
              <TooltipContent>Open the run&apos;s page</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger
                render={
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Close the test run"
                    onClick={onClose}
                  />
                }
              >
                <Icon icon="close" />
              </TooltipTrigger>
              <TooltipContent>
                Close: the canvas stops showing it; the run goes on
              </TooltipContent>
            </Tooltip>
          </span>
        </div>
        {run && shown ? (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-2xs text-muted-foreground">
            <StatusBadge status={shown.status}>{shown.label}</StatusBadge>
            {progress.live && <LiveLabel>Live</LiveLabel>}
            {elapsed !== null && (
              <span className="tabular-nums">{formatDuration(elapsed)}</span>
            )}
            <span>{versionLabel(run)}</span>
          </div>
        ) : (
          <Skeleton className="h-5 w-40" />
        )}
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-4 py-4 [&_[data-slot=waiting-notice]]:mb-0 [&_[data-slot=waiting-notice]]:p-3">
        {detail.error && !run ? (
          <ErrorCallout
            title={failureTitle(detail.error, "Couldn't load the run")}
            action={
              <Button
                variant="outline"
                size="sm"
                onClick={() => void detail.refetch()}
              >
                Retry
              </Button>
            }
          >
            {detail.error.message}
          </ErrorCallout>
        ) : null}
        {run && <StatusNotice organizationId={organizationId} run={run} />}

        <section className="flex flex-col gap-1.5">
          <h3 className="text-xs font-[550] text-foreground">Steps</h3>
          {!steps.data ? (
            <div className="flex flex-col gap-1.5" aria-busy="true">
              <Skeleton className="h-7 w-full" />
              <Skeleton className="h-7 w-full" />
            </div>
          ) : reached.length === 0 ? (
            <p className="flex items-center gap-2 text-2xs text-muted-foreground">
              {progress.live && <Spinner className="size-3.5" />}
              {progress.live
                ? "Waiting for a worker to take it…"
                : "It reached no step."}
            </p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {reached.map((step) => (
                <StepRow
                  key={step.id}
                  step={step}
                  state={progress.steps.get(step.id) ?? "idle"}
                  doc={doc}
                  now={now}
                  selected={selectedStep === step.id}
                  onShow={() => onShowStep(step.id)}
                />
              ))}
            </ul>
          )}
          {steps.data && notReached > 0 && (
            <p className="pl-2 text-3xs text-subtle">
              {notReached} {notReached === 1 ? "step" : "steps"} not reached
              {progress.live ? " yet" : ""}
            </p>
          )}
        </section>

        {run &&
          (hasResult || (run.finished_at && run.status === "succeeded")) && (
            <section className="flex flex-col gap-1.5">
              <h3 className="text-xs font-[550] text-foreground">Result</h3>
              {hasResult ? (
                <JsonView value={run.result} className="max-h-72 text-3xs" />
              ) : (
                <p className="text-2xs text-muted-foreground">
                  It ended without handing anything on.
                </p>
              )}
            </section>
          )}

        {run && <Files organizationId={organizationId} run={run} doc={doc} />}

        {run && run.events.length > 0 && (
          <section className="flex flex-col gap-1.5">
            <h3 className="text-xs font-[550] text-foreground">Events</h3>
            <Activity run={run} />
          </section>
        )}
      </div>

      <div className="flex items-center gap-2 border-t px-4 py-3">
        {onRunAgain && (
          <Button size="sm" variant="outline" onClick={onRunAgain}>
            <Icon icon={PlayIcon} data-icon="inline-start" />
            Run again
          </Button>
        )}
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto"
          nativeButton={false}
          render={
            <Link
              to="/organizations/$organizationId"
              params={{ organizationId }}
              search={{ view: "agents", agentRun: runId }}
            />
          }
        >
          Full run page
          <Icon icon="right" data-icon="inline-end" />
        </Button>
      </div>
      {/* Kept clear for the assistant's launcher, which floats here. */}
      <div aria-hidden="true" className="h-14 shrink-0" />
    </div>
  )
}
