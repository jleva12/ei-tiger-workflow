import { extendedThinkingLevels } from "@/components/forge/assistant"
import type { WorkflowDocument } from "@/lib/workflows/document"
import { slugify, type StepData } from "@/lib/workflows/model"
import { modelIdOf } from "@/lib/workflows/models"
import { inputFields } from "@/lib/workflows/scope"
import type { Lookups } from "./builder-store"

/* The workflow builder's helpers that aren't components; the rest are the builder kit's. */

export { downloadJson } from "@/components/builder/utils"

const plural = (n: number, word: string) =>
  `${n} ${n === 1 ? word : word.endsWith("y") ? `${word.slice(0, -1)}ies` : `${word}s`}`

/**
 * What a step shows under its name: one fact about how it's set up. Its
 * kind is its glyph (and its settings' header), so it isn't repeated.
 */
export function stepDetail(step: StepData, lookups: Lookups): string {
  switch (step.kind) {
    case "entry": {
      const { names } = inputFields(step.config.input_schema)
      return names.length ? `Needs ${plural(names.length, "input field")}` : "No input declared"
    }
    case "agent": {
      const named = modelIdOf(step.config.model)
      const model = named
        ? (lookups.models[named] ?? step.config.model.name.trim())
        : lookups.models[""]
          ? `Default · ${lookups.models[""]}`
          : "Default model"
      const level = extendedThinkingLevels.find((l) => l.id === step.config.thinking_level)
      return [model, level && `${level.label} thinking`].filter(Boolean).join(" · ")
    }
    case "approval":
      return step.config.approvers === "org:admin" ? "An organization admin decides" : "Any member decides"
    case "http":
      return step.config.retries
        ? `${step.config.timeout_seconds} s timeout · ${plural(step.config.retries, "retry")}`
        : `${step.config.timeout_seconds} s timeout`
    case "transform":
      return Object.keys(step.config.output_schema).length ? "JSONata, declared output" : "JSONata"
    case "delay":
      return `Waits ${step.config.amount} ${step.config.unit}`
    case "if":
      return "True or false"
    case "switch":
      return `${plural(step.config.cases.length, "case")} and a default`
    case "match":
      return `${plural(step.config.arms.length, "rule")}, first that holds`
    case "subworkflow":
      return step.config.wait ? "Waits for its result" : "Starts it and goes on"
    case "loop":
      return step.config.concurrency > 1
        ? `${step.config.concurrency} at a time`
        : "One at a time"
    case "merge":
      return step.config.mode === "all" ? "Waits for every way" : "Goes on at the first"
    case "end":
      return step.config.outcome === "succeeded" ? "Succeeds" : "Fails"
  }
}

export const workflowFileName = (doc: Pick<WorkflowDocument, "name">) =>
  `${slugify(doc.name) || "workflow"}.workflow.json`
