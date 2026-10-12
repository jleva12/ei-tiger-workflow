import type { StepRunState } from "@/features/builder/components/run-overlay"
import { isLiveRun } from "@/features/runs/lib/display"
import type { AdkRun, AdkRunStep, AdkRunSteps } from "@/features/runs/lib/runs"

/*
 * A run's progress as its builder draws it over the canvas: each step's
 * state and the lines the run went along (builder/components/run-overlay).
 */

export type RunProgress = {
  steps: Map<string, StepRunState>
  taken: Set<string>
  live: boolean
}

/**
 * Each step's state in the run, and the lines it went along. A step at work
 * spins: one that started and hasn't finished, or one the step before handed
 * on to (ADK records most steps only once they hand on); before anything is
 * recorded, the start is. Once the run has stopped, a step it was at counts as
 * failed when the run failed or was abandoned, and as not reached otherwise.
 */
export function runProgress(
  run: Pick<AdkRun, "status"> | undefined,
  data: Pick<AdkRunSteps, "steps" | "taken_edges"> | undefined
): RunProgress {
  const live = isLiveRun(run)
  const ended = run?.status === "failed" || run?.status === "abandoned"
  const steps = new Map<string, StepRunState>()
  const list: AdkRunStep[] = data?.steps ?? []
  for (const step of list) {
    let state: StepRunState
    switch (step.status) {
      case "done":
        state = "done"
        break
      case "failed":
        state = "failed"
        break
      case "waiting":
        state = "waiting"
        break
      case "running":
        state = live ? "running" : ended ? "failed" : "idle"
        break
      default:
        state = step.handed_to && live ? "running" : "idle"
    }
    steps.set(step.id, state)
  }
  // Queued, or just picked up: the run is at its start.
  if (live && ![...steps.values()].some((state) => state !== "idle")) {
    const start = list.find((step) => step.kind === "start")
    if (start) steps.set(start.id, "running")
  }
  return { steps, taken: new Set(data?.taken_edges ?? []), live }
}

/** The steps the run reached, in the order it did: started first, at work last. */
export function reachedSteps(
  data: Pick<AdkRunSteps, "steps"> | undefined,
  progress: RunProgress
): AdkRunStep[] {
  const at = (step: AdkRunStep) =>
    step.started_at ? Date.parse(step.started_at) : Number.POSITIVE_INFINITY
  return (data?.steps ?? [])
    .filter((step) => progress.steps.get(step.id) !== "idle")
    .map((step, index) => ({ step, index }))
    .sort((a, b) => at(a.step) - at(b.step) || a.index - b.index)
    .map(({ step }) => step)
}
