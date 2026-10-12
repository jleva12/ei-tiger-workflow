import * as React from "react"

/*
 * A run drawn over the canvas while it's followed: each step's state (the
 * one at work spinning, those done ticked, a failure marked, a step waiting
 * for a person) and the lines it went along, the ones into a step at work
 * moving. The builder that runs provides it; the canvas's step and line views
 * read it, and draw nothing of it without one.
 */

/** A step's state in the run drawn over the canvas. */
export type StepRunState =
  /** At work: it started, or the step before handed on to it. */
  | "running"
  | "done"
  | "failed"
  /** Waiting for a person (an approval, an answer) or a time. */
  | "waiting"
  /** Not reached (yet). */
  | "idle"

export type RunOverlay = {
  /** Each step's state, by its ID; a step not listed is idle. */
  steps: ReadonlyMap<string, StepRunState>
  /** The lines the run went along, by edge ID. */
  taken: ReadonlySet<string>
  /** The run is still going: idle steps are faded, lines into a step at work move. */
  live: boolean
}

export const RunOverlayContext = React.createContext<RunOverlay | null>(null)

/** The run drawn over the canvas, if one is. */
export const useRunOverlay = () => React.useContext(RunOverlayContext)

/** A step's state in the run drawn over the canvas; undefined when none is. */
export function useStepRunState(id: string): StepRunState | undefined {
  const overlay = useRunOverlay()
  return overlay ? (overlay.steps.get(id) ?? "idle") : undefined
}
