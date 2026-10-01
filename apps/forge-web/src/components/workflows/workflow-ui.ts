import type { BuilderUi, NodeSummary } from "@/components/builder/ui"
import { stepIcon, WORKFLOWS_ICON, type StepData } from "@/lib/workflows/model"
import { inputFields } from "@/lib/workflows/scope"
import type { Lookups } from "./builder-store"
import { stepDetail } from "./builder-utils"
import { StepFields } from "./step-fields"
import { DataSection } from "./workflow-data-section"

/*
 * What the workflow builder shows that the builder kit can't know: its
 * words, each kind's settings (./step-fields), the line under a step's
 * name and on its card, and how later steps read a step.
 */

const firstLine = (text: string) => text.trim().split("\n")[0] ?? ""

/** A step's line on its card: what it does. */
export function stepSummary(
  step: StepData,
  lookups: Lookups
): NodeSummary | null {
  switch (step.kind) {
    case "entry": {
      // Its input fields, the required ones marked.
      const { names, required } = inputFields(step.config.input_schema)
      return names.length
        ? {
            code: names.map((n) => (required.has(n) ? n : `${n}?`)).join(" · "),
          }
        : null
    }
    case "agent":
      return step.config.instructions.trim()
        ? { text: step.config.instructions.trim() }
        : null
    case "approval":
      return step.config.message.trim()
        ? { text: step.config.message.trim() }
        : null
    case "http":
      return {
        tag: step.config.method,
        code: step.config.url.trim() || "No URL yet",
      }
    case "transform":
      return step.config.expression.trim()
        ? { code: firstLine(step.config.expression) }
        : null
    case "subworkflow": {
      const name = lookups.workflows[step.config.workflow]
      return { text: name ? `Runs ${name}` : "No workflow picked" }
    }
    case "if":
      return step.config.condition.trim()
        ? { code: step.config.condition.trim() }
        : null
    case "switch":
      return step.config.value.trim()
        ? { tag: "on", code: step.config.value.trim() }
        : null
    case "loop":
      return step.config.items.trim()
        ? { code: `${step.config.item_name} of ${step.config.items.trim()}` }
        : null
    case "end":
      return step.config.result.trim()
        ? { code: step.config.result.trim() }
        : null
    default:
      return null
  }
}

export const WORKFLOW_UI: BuilderUi = {
  nouns: { doc: "workflow", docs: "workflows", step: "step", steps: "steps" },
  docIcon: WORKFLOWS_ICON,
  listView: "workflows",
  permission: "workflows:manage",
  detailOf: (step, lookups) => stepDetail(step as StepData, lookups as Lookups),
  summaryOf: (step, lookups) =>
    stepSummary(step as StepData, lookups as Lookups),
  iconOf: (step) => stepIcon(step as StepData),
  Fields: StepFields as BuilderUi["Fields"],
  DataSection: DataSection as BuilderUi["DataSection"],
  emptyCanvas:
    "Drag steps here from the library, or click one to add it. Begin with Start.",
  ready: "Every step is set up and reached from the start.",
}
