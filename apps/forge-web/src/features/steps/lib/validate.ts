import type { IssueLevel } from "@/features/builder/lib/types"
import { resolveExpression } from "./expressions"
import type { StepData } from "./model"
import type { Scope } from "./scope"
import { oneOf, valuesOf } from "./types"

/*
 * What's wrong in a Forge step's own settings, for an ADK workflow's
 * checks (adk-workflows/lib/validate): errors (it can't run) and warnings (it
 * can, but probably not as meant). And the cycles in a graph, which both
 * builders look for.
 */

// A name a JSONata path can use without backticks.
const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/
// An address: http(s) and no spaces except inside its {{ }} parts, or an
// expression in {{ }}.
const URLISH =
  /^(https?:\/\/(?:\{\{[^}]*\}\}|[^\s{]|\{(?!\{))+|\{\{[\s\S]*\}\}\S*)$/

/** What's missing or wrong in one step's own settings. */
export function settingsIssues(
  step: StepData
): [code: string, level: IssueLevel, message: string][] {
  const out: [string, IssueLevel, string][] = []
  const need = (ok: boolean, code: string, message: string) => {
    if (!ok) out.push([code, "error", message])
  }
  const warn = (ok: boolean, code: string, message: string) => {
    if (!ok) out.push([code, "warning", message])
  }
  switch (step.kind) {
    case "approval":
      warn(
        Boolean(step.config.message.trim()),
        "message",
        "Tell the approver what they're deciding."
      )
      break
    case "http": {
      const c = step.config
      if (!c.url.trim()) need(false, "url", "Give it a URL.")
      else {
        need(
          URLISH.test(c.url.trim()),
          "url",
          "A URL starts with https:// or is an expression in {{ }}."
        )
      }
      need(
        c.headers.every((h) => h.name.trim()),
        "headers",
        "Every header needs a name."
      )
      warn(
        !(c.method === "GET" && c.body.trim()),
        "body",
        "A GET request sends no body; pick another method or clear it."
      )
      break
    }
    case "transform":
      need(
        Boolean(step.config.expression.trim()),
        "expression",
        "Write the JSONata expression that makes what it hands on."
      )
      break
    case "delay":
      need(step.config.amount > 0, "amount", "Wait longer than zero.")
      break
    case "if":
      need(
        Boolean(step.config.condition.trim()),
        "condition",
        "Write the condition."
      )
      break
    case "switch": {
      const c = step.config
      need(Boolean(c.value.trim()), "value", "Say which value it switches on.")
      need(c.cases.length > 0, "cases", "Add a case.")
      need(
        c.cases.every((x) => x.value.trim()),
        "case-empty",
        "Every case needs the value that takes it."
      )
      const values = c.cases.map((x) => x.value.trim()).filter(Boolean)
      need(
        new Set(values).size === values.length,
        "case-twice",
        "Two cases have the same value; only the first would be taken."
      )
      break
    }
    case "match":
      need(step.config.arms.length > 0, "arms", "Add a rule.")
      need(
        step.config.arms.every((arm) => arm.condition.trim()),
        "arm-empty",
        "Every rule needs its condition."
      )
      break
    case "loop":
      need(
        Boolean(step.config.items.trim()),
        "items",
        "Say which list it goes through."
      )
      need(
        IDENTIFIER.test(step.config.item_name),
        "item-name",
        "The item's name is one word: letters, digits and _."
      )
      need(step.config.max_iterations >= 1, "max", "Allow at least one pass.")
      need(
        step.config.concurrency >= 1,
        "concurrency",
        "Run at least one item at a time."
      )
      break
    default:
      break
  }
  return out
}

/**
 * A Switch's cases against what it switches on: when that can only be
 * some values (a field declared with them, a boolean), a case for any
 * other could never be taken.
 */
export function switchIssues(
  step: StepData,
  scope: Scope
): [code: string, level: IssueLevel, message: string][] {
  if (step.kind !== "switch" || !step.config.value.trim()) return []
  const values = valuesOf(resolveExpression(step.config.value, scope))
  if (!values) return []
  return step.config.cases.flatMap((c): [string, IssueLevel, string][] =>
    !c.value.trim() || values.includes(c.value)
      ? []
      : [
          [
            `case-value-${c.id}`,
            "error",
            `The case ${JSON.stringify(c.value)} could never be taken: ${step.config.value.trim()} is ${oneOf(values)}.`,
          ],
        ]
  )
}

// The setting each of a step's own issues is about, by its code.
const FIELDS: Record<string, string> = {
  message: "message",
  url: "url",
  headers: "headers",
  body: "body",
  expression: "expression",
  amount: "amount",
  condition: "condition",
  value: "value",
  cases: "cases",
  "case-empty": "cases",
  "case-twice": "cases",
  arms: "arms",
  "arm-empty": "arms",
  items: "items",
  "item-name": "item_name",
  max: "max_iterations",
  concurrency: "concurrency",
}

/**
 * The setting an issue of a step is about, by the issue's code: an
 * expression's (`field:<key>:<n>`), a case's, or one of the step's own.
 * None for the step as a whole (a way that goes nowhere).
 */
export function fieldOfCode(code: string): string | undefined {
  if (code.startsWith("field:")) return code.slice(6, code.lastIndexOf(":"))
  if (code.startsWith("case-value-")) return `cases.${code.slice(11)}`
  return FIELDS[code]
}

/** Tarjan's strongly connected components. */
export function stronglyConnected(
  ids: string[],
  outgoing: Map<string, { target: string }[]>
): string[][] {
  let index = 0
  const indices = new Map<string, number>()
  const low = new Map<string, number>()
  const onStack = new Set<string>()
  const stack: string[] = []
  const components: string[][] = []

  const visit = (id: string) => {
    indices.set(id, index)
    low.set(id, index)
    index += 1
    stack.push(id)
    onStack.add(id)
    for (const { target } of outgoing.get(id) ?? []) {
      if (!indices.has(target)) {
        visit(target)
        low.set(id, Math.min(low.get(id)!, low.get(target)!))
      } else if (onStack.has(target)) {
        low.set(id, Math.min(low.get(id)!, indices.get(target)!))
      }
    }
    if (low.get(id) === indices.get(id)) {
      const component: string[] = []
      let member: string
      do {
        member = stack.pop()!
        onStack.delete(member)
        component.push(member)
      } while (member !== id)
      components.push(component.reverse())
    }
  }
  for (const id of ids) if (!indices.has(id)) visit(id)
  return components
}
