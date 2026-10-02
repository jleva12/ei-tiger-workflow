import type { ToolCallMessagePartComponent } from "@assistant-ui/react"

import { StepMark } from "../plan-steps"
import type { Plan, StepStatus } from "../lib/plan"
import { toolResult } from "../lib/tool-result"

import { ToolFrame } from "./tool-frame"

export const SetPlanUI: ToolCallMessagePartComponent<{
  title: string
  steps: string[]
}> = (part) => {
  const { plan, error } = toolResult<{ plan: Plan; error: string }>(part.result)
  const steps = plan?.steps.length ?? part.args.steps?.length ?? 0
  return (
    <ToolFrame
      part={part}
      running={<>Planning</>}
      label={
        error ? (
          <>Couldn't make a plan: {error}</>
        ) : (
          <>
            Planned <b>{part.args.title}</b>: {steps} steps
          </>
        )
      }
    />
  )
}

type StepResult = {
  step: number
  text: string
  status: StepStatus
  done: number
  total: number
  error: string
}

const VERBS: Record<StepStatus, string> = {
  in_progress: "Started",
  done: "Finished",
  skipped: "Skipped",
  pending: "Reopened",
}

export const UpdatePlanStepUI: ToolCallMessagePartComponent<{
  step: number
  status: StepStatus
}> = (part) => {
  const result = toolResult<StepResult>(part.result)
  if (result.error)
    return <ToolFrame part={part} running={null} label={<>{result.error}</>} />
  // Successful status changes live in the plan and Activity view; avoid
  // repeating every transition in the conversation. Errors remain inline.
  if (part.status.type === "complete" && result.status) return null
  return (
    <ToolFrame
      part={part}
      running={<>Updating the plan</>}
      label={
        <span className="flex items-center gap-2">
          {/* What the call did, not what's happening now: never spins. */}
          {result.status && <StepMark status={result.status} />}
          <span>
            {VERBS[part.args.status] ?? "Updated"} step {part.args.step}
            {result.text && <>: {result.text}</>}
          </span>
          {result.total ? (
            <span className="text-muted-foreground/70 tabular-nums">
              {result.done}/{result.total}
            </span>
          ) : null}
        </span>
      }
    />
  )
}
