import type { BaseStep, BuilderGraph, StepOutput } from "./types"

/*
 * What a connection means, for drawing it: its tone (the colour the canvas
 * draws it in), its role (a loop's body going out or coming back, a branch,
 * or plain next) and the words a label on it says. Tones follow meaning, so
 * a colour reads the same anywhere on the canvas: green where things went
 * well, red where they didn't, violet for a loop going round, a colour of
 * its own for each case of a switch or rule of a match, and graphite for
 * the rest. Words always come with the colour: the branch's row on its
 * step, and labels on the lines that need them. Which tone a way out takes
 * is the builder's to say (its kinds of step).
 */

export type EdgeTone =
  "neutral" | "positive" | "negative" | "loop" | "case-1" | "case-2" | "case-3" | "case-4"

export type EdgeRole =
  /** A step's only way out. */
  | "next"
  /** One of a step's named ways out. */
  | "branch"
  /** A loop's Each item way, into its body. */
  | "each"
  /** The loop's body coming back to it for the next item. */
  | "return"

/** Cases, rules and routes take these in turn. */
export const CASE_TONES: EdgeTone[] = ["case-1", "case-2", "case-3", "case-4"]

export type ConnectionMeaning = {
  tone: EdgeTone
  role: EdgeRole
  /** What a label on it says, when it's more than a plain next. */
  label?: string
  /** For a loop's connections: the loop. */
  loop?: string
}

/** What drawing connections needs to know of a builder's kinds of step. */
export type ConnectionRules<S extends BaseStep> = {
  outputsOf: (step: S) => StepOutput[]
  /** The tone of one of a step's ways out; `index` is its place among them. */
  toneOf: (step: S, output: StepOutput, index: number) => EdgeTone
  /** Each loop's body, by the loop's ID; none for graphs without loops. */
  loopBodies: (graph: BuilderGraph<S>) => Map<string, Set<string>>
}

/**
 * What each connection means, by the builder's edge ID. `edgeId` names a
 * connection the way the builder does.
 */
export function meaningsFor<S extends BaseStep>(
  graph: BuilderGraph<S>,
  edgeId: (c: BuilderGraph<S>["connections"][number]) => string,
  { outputsOf, toneOf, loopBodies }: ConnectionRules<S>
): Map<string, ConnectionMeaning> {
  const byId = new Map(graph.steps.map((s) => [s.id, s.data]))
  const bodies = loopBodies(graph)
  const meanings = new Map<string, ConnectionMeaning>()
  for (const c of graph.connections) {
    const source = byId.get(c.source)
    if (!source || !byId.has(c.target)) continue
    const outputs = outputsOf(source)
    const index = outputs.findIndex((o) => o.id === c.output)
    const output = outputs[index]
    if (!output) continue
    const id = edgeId(c)
    if (bodies.get(c.target)?.has(c.source)) {
      meanings.set(id, {
        tone: "loop",
        role: "return",
        label: "Next item",
        loop: c.target,
      })
    } else if (output.body) {
      meanings.set(id, {
        tone: "loop",
        role: "each",
        label: output.label,
        loop: c.source,
      })
    } else {
      meanings.set(id, {
        tone: toneOf(source, output, index),
        role: output.branch ? "branch" : "next",
        label: output.branch ? output.label : undefined,
      })
    }
  }
  return meanings
}
