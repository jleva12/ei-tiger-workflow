import type { NodeSummary } from "@/components/builder/ui"
import type { StepData } from "@/lib/steps/model"

/*
 * The lines a Forge step shows in an ADK workflow: under its name (how
 * it's set up) and on its card (what it does).
 */

const plural = (n: number, word: string) =>
  `${n} ${n === 1 ? word : word.endsWith("y") ? `${word.slice(0, -1)}ies` : `${word}s`}`

const firstLine = (text: string) => text.trim().split("\n")[0] ?? ""

/**
 * What a step shows under its name: one fact about how it's set up. Its
 * kind is its glyph (and its settings' header), so it isn't repeated.
 */
export function stepDetail(step: StepData): string {
  switch (step.kind) {
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

/** A step's line on its card: what it does. */
export function stepSummary(step: StepData): NodeSummary | null {
  switch (step.kind) {
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
