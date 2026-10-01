import { tidyGraph, type Measure } from "@/lib/builder/layout"
import type { Point } from "@/lib/builder/types"
import { loopBodies } from "./connections"
import type { WorkflowGraph } from "./document"
import { outputsOf } from "./model"

/*
 * Tidy up for a workflow: the builder kit's layout (lib/builder/layout),
 * from the start step, with each loop keeping room over its body.
 */

export { DEFAULT_SIZE, type Measure, type Size } from "@/lib/builder/layout"

export function tidy(graph: WorkflowGraph, measure: Measure): Map<string, Point> {
  return tidyGraph(graph, measure, {
    outputsOf,
    loopBodies,
    isRoot: (step) => step.kind === "entry",
  })
}
