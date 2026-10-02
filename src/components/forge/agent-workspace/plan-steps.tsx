import { useAuiState } from "@assistant-ui/react"
import { MinusSignIcon, Tick02Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Spinner } from "@/components/ui/spinner"
import type { Plan, StepStatus } from "./lib/plan"
import { duration } from "./lib/format"

/**
 * A step's mark: an empty ring, a started one (a ring with a dot, spinning
 * only while `live`: the agent is working on it now), a green tick or a dash.
 */
export function StepMark({
  status,
  live = false,
}: {
  status: StepStatus
  live?: boolean
}) {
  if (status === "in_progress" && live)
    return <Spinner className="size-4 shrink-0 text-foreground" />
  if (status === "in_progress")
    return (
      <span className="flex size-4 shrink-0 items-center justify-center rounded-full ring-1 ring-foreground/40 ring-inset">
        <span className="size-1.5 rounded-full bg-foreground/70" />
      </span>
    )
  return (
    <span
      className={cn(
        "flex size-4 shrink-0 items-center justify-center rounded-full transition-colors duration-300",
        status === "done" &&
          "bg-success-surface text-success-foreground ring-1 ring-success-border",
        status === "skipped" && "bg-muted text-muted-foreground",
        status === "pending" && "ring-1 ring-border ring-inset"
      )}
    >
      {status === "done" && <Icon icon={Tick02Icon} size={10} />}
      {status === "skipped" && <Icon icon={MinusSignIcon} size={10} />}
    </span>
  )
}

/**
 * The plan's steps as a checklist, numbered, the current one highlighted; it
 * spins only while a reply is running, so a step the agent left started
 * doesn't spin after it's done.
 */
export function PlanSteps({
  plan,
  detailed = false,
  live,
}: {
  plan: Plan
  detailed?: boolean
  live?: boolean
}) {
  const threadRunning = useAuiState((s) => s.thread.isRunning)
  const running = live ?? threadRunning
  return (
    <ol className="flex flex-col gap-0.5">
      {plan.steps.map((step, index) => (
        <li
          key={index}
          className={cn(
            "flex items-start gap-2.5 rounded-md px-2 py-2.5 text-sm transition-colors duration-200",
            step.status === "in_progress" && "bg-muted"
          )}
        >
          <span className="mt-0.5">
            <StepMark status={step.status} live={running} />
          </span>
          <span
            className={cn(
              "min-w-0 flex-1 leading-snug",
              step.status === "done" && "text-muted-foreground",
              step.status === "skipped" &&
                "text-muted-foreground line-through decoration-muted-foreground/50",
              step.status === "in_progress" && "font-medium text-foreground"
            )}
          >
            <span className="me-1.5 text-2xs text-muted-foreground tabular-nums">
              {index + 1}.
            </span>
            {step.text}
            {!detailed && (
              <span className="sr-only">
                {" "}
                (
                {step.status === "done"
                  ? "Completed"
                  : step.status === "skipped"
                    ? "Skipped"
                    : step.status === "in_progress"
                      ? running
                        ? "In progress"
                        : "Unfinished"
                      : "Not started"}
                )
              </span>
            )}
            {detailed && (
              <span className="mt-1.5 flex items-center gap-2 text-2xs font-normal text-muted-foreground no-underline">
                <span>
                  {step.status === "done"
                    ? "Completed"
                    : step.status === "skipped"
                      ? "Skipped"
                      : step.status === "in_progress"
                        ? running
                          ? "In progress"
                          : "Unfinished"
                        : "Not started"}
                </span>
                {step.started_at != null &&
                  step.finished_at != null &&
                  step.finished_at >= step.started_at && (
                    <>
                      <span aria-hidden>·</span>
                      <span className="tabular-nums">
                        {duration(step.finished_at - step.started_at)} elapsed
                      </span>
                    </>
                  )}
              </span>
            )}
          </span>
        </li>
      ))}
    </ol>
  )
}
