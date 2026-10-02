import { useAdkSessionState } from "@assistant-ui/react-google-adk"

export type StepStatus = "pending" | "in_progress" | "done" | "skipped"
export type Plan = {
  id?: string
  title: string
  steps: {
    text: string
    status: StepStatus
    started_at?: number
    finished_at?: number
  }[]
}

/**
 * The open conversation's plan, which its agent keeps in the session state
 * `plan` (adk_chat.builtin_tools: set_plan, update_plan_step). Updates as
 * each step is marked.
 */
export function usePlan(): Plan | null {
  const plan = useAdkSessionState().plan as Plan | undefined
  return plan &&
    typeof plan.title === "string" &&
    Array.isArray(plan.steps) &&
    plan.steps.length > 0 &&
    plan.steps.every(
      (step) =>
        step &&
        typeof step.text === "string" &&
        ["pending", "in_progress", "done", "skipped"].includes(step.status)
    )
    ? plan
    : null
}

export const finishedSteps = (plan: Plan) =>
  plan.steps.filter(
    (step) => step.status === "done" || step.status === "skipped"
  ).length
