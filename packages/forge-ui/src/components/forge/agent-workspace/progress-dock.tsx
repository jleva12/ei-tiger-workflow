import { useId, useState } from "react"
import { useAuiState } from "@assistant-ui/react"
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { ActivityMark } from "./activity-mark"
import { Icon } from "@/components/forge/icon"
import { PlanSteps } from "./plan-steps"
import { activityLabels } from "./lib/activity"
import { finishedSteps, usePlan } from "./lib/plan"
import { useScreen } from "./lib/screen-store"
import { useAgentActivity } from "./lib/use-agent-activity"
import { cn } from "cn"

/** Show progress only when the current turn has an agent-created task list. */
export function ProgressDock() {
  const activity = useAgentActivity()
  const plan = usePlan()
  const userMessageId = useAuiState(
    (s) => s.thread.messages.findLast((message) => message.role === "user")?.id
  )
  const progressId = `${activity.sessionId ?? "new"}:${userMessageId ?? "empty"}`
  const dismissed = useScreen((s) => s.dismissedProgress.includes(progressId))
  const dismissProgress = useScreen((s) => s.dismissProgress)
  if (dismissed) return null
  return (
    <ProgressDockContent
      key={progressId}
      activity={activity}
      plan={plan}
      onDismiss={() => dismissProgress(progressId)}
    />
  )
}

function ProgressDockContent({
  activity,
  plan,
  onDismiss,
}: {
  activity: ReturnType<typeof useAgentActivity>
  plan: ReturnType<typeof usePlan>
  onDismiss: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const id = useId()
  const openPanel = useScreen((s) => s.openPanel)
  const relevantPlan = activity.hasPlanActivity ? plan : null
  // Ordinary replies and tool calls stay in the conversation. A saved plan
  // from an earlier turn must not make this panel appear again.
  if (!relevantPlan) return null
  const done = relevantPlan ? finishedSteps(relevantPlan) : 0
  const total = relevantPlan?.steps.length ?? 0
  const activeStep = relevantPlan?.steps.find(
    (step) => step.status === "in_progress"
  )
  const title =
    activity.state === "waiting"
      ? "Your input is needed to continue"
      : activity.state === "error"
        ? "There’s an issue to review"
        : activity.state === "stopped"
          ? "Work stopped. Your progress is saved."
          : activity.running
            ? (activeStep?.text ??
              activity.current?.label ??
              "Working on your request")
            : relevantPlan
              ? relevantPlan.title
              : "Finished this response"
  const label =
    relevantPlan &&
    !activity.running &&
    done < total &&
    activity.state === "complete"
      ? "Plan unfinished"
      : relevantPlan && done === total && activity.state === "complete"
        ? "Plan finished"
        : activityLabels[activity.state]
  const markState = label === "Plan unfinished" ? "stopped" : activity.state
  return (
    <section
      aria-label="Task progress"
      className="overflow-hidden rounded-xl border border-border bg-muted/25"
    >
      <div className="flex items-center gap-1 px-1">
        <button
          type="button"
          aria-expanded={relevantPlan ? expanded : undefined}
          aria-controls={relevantPlan ? id : undefined}
          onClick={() =>
            relevantPlan ? setExpanded(!expanded) : openPanel("plan")
          }
          className="flex min-w-0 flex-1 items-center gap-3 rounded-lg px-2.5 py-3 text-start transition-colors hover:bg-muted/70"
        >
          <ActivityMark state={markState} />
          <span className="min-w-0 flex-1">
            <span className="mb-0.5 block text-2xs text-muted-foreground">
              {label}
            </span>
            <span className="block truncate text-xs font-medium" title={title}>
              {title}
            </span>
          </span>
          {total > 0 && (
            <span className="shrink-0 text-2xs text-muted-foreground tabular-nums">
              {done} / {total}
            </span>
          )}
          <Icon
            icon={relevantPlan ? "up" : "right"}
            size={14}
            className={cn(
              "shrink-0 text-muted-foreground transition-transform duration-200",
              relevantPlan && !expanded && "rotate-180"
            )}
          />
        </button>
        <button
          type="button"
          onClick={() => openPanel("plan")}
          aria-label="Open task details"
          className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <Icon icon="sidebar" size={16} />
        </button>
        <TooltipIconButton
          tooltip="Dismiss task progress"
          className="mr-1 size-8 shrink-0 rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
          onClick={(event) => {
            // Return keyboard focus to the nearby composer before removing
            // the focused dismiss control from the document.
            event.currentTarget
              .closest(".aui-thread-viewport-footer")
              ?.querySelector<HTMLTextAreaElement>(
                'textarea[aria-label="Message input"]'
              )
              ?.focus()
            onDismiss()
          }}
        >
          <Icon icon="close" size={14} />
        </TooltipIconButton>
      </div>
      {relevantPlan && (
        <div id={id} hidden={!expanded} className="border-t px-2 py-2">
          <div className="max-h-[min(32dvh,18rem)] overflow-y-auto">
            <PlanSteps
              plan={relevantPlan}
              live={activity.running && activity.hasPlanActivity}
            />
          </div>
          <button
            type="button"
            onClick={() => openPanel("plan")}
            className="mt-1 flex w-full items-center justify-between rounded-md px-2 py-2 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <span>View activity and details</span>
            <Icon icon="right" size={14} />
          </button>
        </div>
      )}
      {total > 0 && (
        <div
          role="progressbar"
          aria-label="Plan progress"
          aria-valuenow={done}
          aria-valuemin={0}
          aria-valuemax={total}
          aria-valuetext={`${done} of ${total} steps resolved`}
          className="h-0.5 bg-border/60"
        >
          <div
            className="h-full origin-left bg-foreground/50 transition-transform duration-200 ease-out"
            style={{ transform: `scaleX(${done / total})` }}
          />
        </div>
      )}
    </section>
  )
}
