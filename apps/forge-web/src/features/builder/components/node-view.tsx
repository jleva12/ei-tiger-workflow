import * as React from "react"
import { Handle, Position, type NodeProps } from "@xyflow/react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { INPUT, type BuilderIssue } from "@/features/builder/lib/types"
import { Spinner } from "@/components/ui/spinner"
import { KindGlyph } from "./glyph"
import {
  useRunOverlay,
  useStepRunState,
  type StepRunState,
} from "./run-overlay"
import { useBuilder, type FlowNode } from "./store"
import { useBuilderUi } from "./ui"

/** The issues on one step, most severe first. */
function useStepIssues(id: string) {
  const issues = useBuilder((s) => s.issues)
  return React.useMemo(() => issues.filter((i) => i.step === id), [issues, id])
}

function IssueMark({ issues }: { issues: BuilderIssue[] }) {
  if (!issues.length) return null
  const error = issues.some((i) => i.level === "error")
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <span
            role="img"
            aria-label={`${issues.length} ${issues.length === 1 ? "issue" : "issues"}`}
            className={cn(
              "ml-auto grid size-5 shrink-0 place-items-center self-start rounded-(--radius-chip)",
              error ? "text-destructive" : "text-tone-amber-foreground"
            )}
          />
        }
      >
        <Icon icon={error ? "failed" : "warning"} size={14} />
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-64">
        <ul className="flex flex-col gap-1">
          {issues.map((issue) => (
            <li key={issue.id}>{issue.message}</li>
          ))}
        </ul>
      </TooltipContent>
    </Tooltip>
  )
}

const RUN_LOOKS: Record<
  Exclude<StepRunState, "idle">,
  { card: string; badge: string; label: string }
> = {
  running: {
    card: "border-signal-running ring-2 ring-signal-running/30 forge-node-running",
    badge: "border-signal-running/50 text-signal-running",
    label: "Running",
  },
  done: {
    card: "border-signal-success/60",
    badge: "border-signal-success/50 text-signal-success",
    label: "Done",
  },
  failed: {
    card: "border-signal-failed ring-2 ring-signal-failed/25",
    badge: "border-signal-failed/50 text-signal-failed",
    label: "Failed",
  },
  waiting: {
    card: "border-notice-accent ring-2 ring-notice-accent/25",
    badge: "border-notice-border text-notice-accent",
    label: "Waiting for a person or a time",
  },
}

/** A step's state in the run drawn over the canvas, in its top right corner. */
function RunBadge({ state }: { state: Exclude<StepRunState, "idle"> }) {
  const look = RUN_LOOKS[state]
  return (
    <span
      role="img"
      aria-label={look.label}
      title={look.label}
      className={cn(
        "absolute -top-2.5 -right-2.5 z-10 grid size-6 place-items-center rounded-full border bg-background shadow-(--shadow-raised)",
        look.badge
      )}
    >
      {state === "running" ? (
        <Spinner className="size-3.5" />
      ) : (
        <Icon
          icon={
            state === "done" ? "check" : state === "failed" ? "failed" : "clock"
          }
          size={13}
        />
      )}
    </span>
  )
}

/**
 * A step on the canvas: a hairline card with its mark, name and what it
 * does. Its way in is on the left of the header; a single way out is on
 * the right of the header, so a straight run of steps draws straight lines;
 * branches are rows of their own, each with its way out at the row's end.
 */
export const FlowNodeView = React.memo(function FlowNodeView({
  id,
  data,
  selected,
  dragging,
}: NodeProps<FlowNode>) {
  const adapter = useBuilder((s) => s.adapter)
  const lookups = useBuilder((s) => s.lookups)
  const ui = useBuilderUi()
  const issues = useStepIssues(id)
  const info = adapter.kinds[data.kind]
  const outputs = adapter.outputsOf(data)
  const branches = outputs.some((o) => o.branch)
  const summary = ui.summaryOf(data, lookups)
  // Its kind leads the line under its name, unless the name already says it.
  const kind =
    ui.kindLabels && data.name.trim().toLowerCase() !== info.label.toLowerCase()
      ? info.label
      : undefined
  const detail = [kind, ui.detailOf(data, lookups)].filter(Boolean).join(" · ")
  const badge = ui.badgeOf?.(data)
  // The run followed over the canvas, if one is.
  const runState = useStepRunState(id)
  const live = useRunOverlay()?.live ?? false
  const run = runState && runState !== "idle" ? runState : undefined

  return (
    <div
      data-kind={data.kind}
      data-run-state={runState}
      className={cn(
        "relative w-[15rem] rounded-(--radius-card) border bg-background text-foreground transition-[border-color,box-shadow,opacity] duration-150",
        info.person && "border-notice-border bg-notice-surface",
        selected && "border-foreground/45",
        dragging ? "shadow-(--shadow-float)" : "shadow-(--shadow-node)",
        run && RUN_LOOKS[run].card,
        runState === "idle" && live && "opacity-55"
      )}
    >
      {run && <RunBadge state={run} />}
      <div className="relative flex items-center gap-2.5 px-3 py-2.5">
        {adapter.hasInput(data.kind) && (
          <Handle type="target" position={Position.Left} id={INPUT} />
        )}
        <KindGlyph info={info} />
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="truncate text-xs font-medium text-foreground">
              {data.name}
            </span>
            {badge && (
              <span className="shrink-0 rounded-full bg-primary px-1.5 text-[0.625rem] leading-4 font-medium text-primary-foreground">
                {badge}
              </span>
            )}
          </span>
          <span
            className={cn(
              "truncate text-2xs",
              info.person ? "text-notice-foreground" : "text-muted-foreground"
            )}
          >
            {detail}
          </span>
        </div>
        <IssueMark issues={issues} />
        {!branches && outputs[0] && (
          <Handle type="source" position={Position.Right} id={outputs[0].id} />
        )}
      </div>

      {summary && (
        <div className="-mt-1 px-3 pb-2.5">
          {summary.code !== undefined ? (
            <p
              className={cn(
                "flex min-w-0 items-baseline gap-1.5 rounded-(--radius-chip) bg-muted px-1.5 py-1 font-mono text-2xs/[1.5] text-muted-foreground",
                info.person && "bg-background/70"
              )}
            >
              {summary.tag && (
                <span className="shrink-0 font-medium text-foreground">
                  {summary.tag}
                </span>
              )}
              <span className="truncate">{summary.code}</span>
            </p>
          ) : (
            <p
              className={cn(
                "line-clamp-2 text-2xs/[1.5]",
                info.person ? "text-notice-foreground" : "text-muted-foreground"
              )}
            >
              {summary.text}
            </p>
          )}
        </div>
      )}

      {branches && (
        <ul
          className={cn("border-t py-1", info.person && "border-notice-border")}
          aria-label="Ways out"
        >
          {outputs.map((output, index) => {
            // Its way out wears the colour of the lines leaving it.
            const tone = adapter.toneOf(data, output, index)
            return (
              <li
                key={output.id}
                className={cn(
                  "relative flex h-7 items-center gap-1.5 pr-4 pl-3 text-2xs",
                  output.fallback ? "text-subtle" : "text-muted-foreground"
                )}
              >
                {output.body && (
                  <Icon icon="refresh" size={12} className="shrink-0" />
                )}
                <span className="ml-auto truncate">{output.label}</span>
                <Handle
                  type="source"
                  position={Position.Right}
                  id={output.id}
                  className={
                    tone === "neutral"
                      ? undefined
                      : `forge-handle-toned forge-tone-${tone}`
                  }
                />
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
})
