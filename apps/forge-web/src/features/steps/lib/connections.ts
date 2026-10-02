import {
  CASE_TONES,
  meaningsFor,
  type ConnectionMeaning,
  type EdgeTone,
} from "@/features/builder/lib/connections"
import type { StepGraph } from "./document"
import { outputsOf, type StepData, type StepOutput } from "./model"

/*
 * What a workflow's connections mean, for drawing them: the tone each way
 * out takes, and each loop's body. The rest is the builder kit's
 * (builder/lib/connections).
 */

export type { ConnectionMeaning, EdgeRole, EdgeTone } from "@/features/builder/lib/connections"

/** The tone of one of a step's ways out; `index` is its place among them. */
export function toneOf(step: StepData, output: StepOutput, index: number): EdgeTone {
  if (output.body) return "loop"
  if (!output.branch) return "neutral"
  switch (step.kind) {
    // The second way out is how it went wrong.
    case "http":
    case "approval":
      return output.fallback ? "negative" : "positive"
    case "if":
      return output.fallback ? "neutral" : "positive"
    case "switch":
    case "match":
      return output.fallback ? "neutral" : CASE_TONES[index % CASE_TONES.length]
    default:
      return "neutral"
  }
}

/**
 * Each loop's body, by the loop's ID: the steps its Each item way reaches
 * before coming back to it (as the runner reads it).
 */
export function loopBodies(graph: StepGraph): Map<string, Set<string>> {
  const targets = new Map<string, string[]>()
  for (const c of graph.connections) {
    const list = targets.get(c.source)
    if (list) list.push(c.target)
    else targets.set(c.source, [c.target])
  }
  const bodies = new Map<string, Set<string>>()
  for (const step of graph.steps) {
    if (step.data.kind !== "loop") continue
    const way = outputsOf(step.data).find((o) => o.body)?.id
    const body = new Set<string>()
    const stack = graph.connections
      .filter((c) => c.source === step.id && c.output === way)
      .map((c) => c.target)
    while (stack.length) {
      const at = stack.pop()!
      if (at === step.id || body.has(at)) continue
      body.add(at)
      stack.push(...(targets.get(at) ?? []))
    }
    bodies.set(step.id, body)
  }
  return bodies
}

/**
 * What each connection means, by the builder's edge ID. `edgeId` names a
 * connection the way the builder does.
 */
export function meaningsOf(
  graph: StepGraph,
  edgeId: (c: StepGraph["connections"][number]) => string
): Map<string, ConnectionMeaning> {
  return meaningsFor(graph, edgeId, { outputsOf, toneOf, loopBodies })
}
