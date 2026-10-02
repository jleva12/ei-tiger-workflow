import type { IconProp } from "@/components/forge/icons"

/*
 * What every builder's graph is made of, whatever it builds (a workflow,
 * an agent): steps of some kind, each with a name and its settings, and
 * connections from one of a step's ways out to another step's one way in.
 * A builder's own model says which kinds there are, what they're set up
 * with and which ways out each has; the canvas, the library, the step
 * dialog and Tidy up work from that.
 */

export type Point = { x: number; y: number }

/** A step as a builder holds it: its kind, its name and its settings. */
export type BaseStep = { kind: string; name: string; config: object }

/** Every step has one way in. */
export const INPUT = "in"

/** A way out of a step: its ID in connections and what the canvas calls it. */
export type StepOutput = {
  id: string
  label: string
  /** A branch's own, named way out (a case, a rule, true or false). */
  branch?: boolean
  /** The way out for everything no branch took. */
  fallback?: boolean
  /** A loop's body: it comes back to the loop. */
  body?: boolean
}

/** The step and connection lists a builder edits. */
export type BuilderGraph<S extends BaseStep = BaseStep> = {
  steps: { id: string; data: S; position: Point }[]
  connections: { source: string; output: string; target: string }[]
}

/** A connection's ID: the same connection always has the same one. */
export const edgeId = (source: string, output: string, target: string) =>
  `${source}:${output}->${target}`

export type IssueLevel = "error" | "warning"

/** Something that stands in the way: on a step, or on the whole graph. */
export type BuilderIssue = {
  /** Stable while the problem lasts: `<step>:<code>`. */
  id: string
  level: IssueLevel
  /** The step it's about; none for the graph as a whole. */
  step?: string
  /**
   * The setting it's about, when it's one: a config key, or a row's
   * (`cases.<id>`) or a sub-agent's (`agents.<id>.instruction`). Its field
   * in the step's settings shows it.
   */
  field?: string
  message: string
}

/** "2 errors, 1 warning", or null with nothing to say. */
export function issueSummary(issues: BuilderIssue[]) {
  const errors = issues.filter((i) => i.level === "error").length
  const warnings = issues.length - errors
  const parts = [
    errors ? `${errors} ${errors === 1 ? "error" : "errors"}` : "",
    warnings ? `${warnings} ${warnings === 1 ? "warning" : "warnings"}` : "",
  ].filter(Boolean)
  return parts.length ? parts.join(", ") : null
}

/** A group of kinds in the library. */
export type KindGroup = { id: string; label: string }

/** What the library, the canvas and the step dialog show of a kind. */
export type KindInfo = {
  label: string
  /** Its group's ID. */
  group: string
  icon: IconProp
  /** One line: what the step does. */
  summary: string
  /** Words the library's filter also matches. */
  keywords: string
  /** Agent steps wear an agent orb: which of the five. */
  orb?: number
  /** Drawn as a start or finish: an ink tile. */
  terminal?: boolean
  /** A person decides here: the warm notice surface. */
  person?: boolean
  /** The prefix of a new step's ID. */
  idPrefix: string
}
