import type { NodeSummary } from "@/features/builder/components/ui"
import { placeLabel, placeOf } from "@/features/adk-workflows/lib/folders"
import type { AgentStep } from "@/features/adk-workflows/lib/model"
import type { StepData } from "@/features/steps/lib/model"

/*
 * The lines a Forge step shows in a workflow: under its name (how
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
      return step.config.approvers === "org:admin"
        ? "An organization admin decides"
        : "Any member decides"
    case "http":
      return step.config.retries
        ? `${step.config.timeout_seconds} s timeout · ${plural(step.config.retries, "retry")}`
        : `${step.config.timeout_seconds} s timeout`
    case "bash":
      return step.config.cwd.trim()
        ? `In ${step.config.cwd.trim()}`
        : "In the run's folder"
    case "git_clone": {
      const ref = step.config.ref.trim()
      const depth = step.config.depth
      const into = placeLabel(
        placeOf(step.config.folder, step as AgentStep, new Map())
      )
      return `Into ${into} · ${ref ? `on ${ref}` : "default branch"}${depth > 0 ? ` · last ${plural(depth, "commit")}` : ""}`
    }
    case "git_branch":
      return step.config.start.trim()
        ? `From ${step.config.start.trim()}`
        : "From what's checked out"
    case "git_push":
      return step.config.folder.trim()
        ? `In ${step.config.folder.trim()}`
        : "In the run's folder"
    case "github_pull_request":
      return `${step.config.base.trim() ? `Into ${step.config.base.trim()}` : "Into the default branch"}${step.config.draft ? " · draft" : ""}`
    case "transform":
      return Object.keys(step.config.output_schema).length
        ? "JSONata, declared output"
        : "JSONata"
    case "filter":
      return `Keeps each ${step.config.item_name || "item"} that holds`
    case "delay":
      return `Waits ${step.config.amount} ${step.config.unit}`
    case "if":
      return "True or false"
    case "switch":
      return `${plural(step.config.cases.length, "case")} and a default`
    case "match":
      return `${plural(step.config.arms.length, "rule")}, ${step.config.mode === "all" ? "every one that holds" : "first that holds"}`
    case "loop":
    case "for_each":
      return step.config.concurrency > 1
        ? `${step.config.concurrency} at a time`
        : "One at a time"
    case "merge":
      return step.config.mode === "all"
        ? "Waits for every way"
        : "Goes on at the first"
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
    case "bash":
      return step.config.command.trim()
        ? { tag: "$", code: firstLine(step.config.command) }
        : null
    case "transform":
      return step.config.expression.trim()
        ? { code: firstLine(step.config.expression) }
        : null
    case "git_clone":
      return step.config.repository.trim()
        ? { code: step.config.repository.trim() }
        : null
    case "git_branch":
      return step.config.branch.trim()
        ? { code: step.config.branch.trim() }
        : null
    case "git_push":
      return step.config.message.trim()
        ? { text: firstLine(step.config.message) }
        : null
    case "github_pull_request":
      return step.config.title.trim()
        ? { text: step.config.title.trim() }
        : null
    case "filter":
      return step.config.condition.trim()
        ? { tag: "where", code: firstLine(step.config.condition) }
        : step.config.items.trim()
          ? { code: step.config.items.trim() }
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
    case "for_each":
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
