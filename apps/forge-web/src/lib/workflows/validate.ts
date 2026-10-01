import type { BuilderIssue, IssueLevel } from "@/lib/builder/types"
import type { WorkflowGraph } from "./document"
import { checkField, resolveExpression } from "./expressions"
import { expressionSettings } from "./fields"
import { outputsOf, STEP_KINDS, type StepData } from "./model"
import { scopesOf, type Scope } from "./scope"
import { oneOf, valuesOf } from "./types"

/*
 * What stands between a workflow and a runner that could run it: errors
 * (it can't run) and warnings (it can, but probably not as meant). The
 * builder shows them on the steps, in their settings, in the workflow's
 * details and in the top bar.
 */

export { issueSummary, type IssueLevel } from "@/lib/builder/types"

export type WorkflowIssue = BuilderIssue

// A name a JSONata path can use without backticks.
const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/
// An address: http(s) and no spaces except inside its {{ }} parts, or an
// expression in {{ }}.
const URLISH =
  /^(https?:\/\/(?:\{\{[^}]*\}\}|[^\s{]|\{(?!\{))+|\{\{[\s\S]*\}\}\S*)$/

const isJson = (text: string) => {
  try {
    JSON.parse(text)
    return true
  } catch {
    return false
  }
}

/** What's missing or wrong in one step's own settings. */
export function settingsIssues(
  step: StepData,
  context: ValidationContext
): [code: string, level: IssueLevel, message: string][] {
  const out: [string, IssueLevel, string][] = []
  const need = (ok: boolean, code: string, message: string) => {
    if (!ok) out.push([code, "error", message])
  }
  const warn = (ok: boolean, code: string, message: string) => {
    if (!ok) out.push([code, "warning", message])
  }
  switch (step.kind) {
    case "entry": {
      // What starts it is hooked up elsewhere; the start step only declares its input.
      const properties = step.config.input_schema.properties
      need(
        !(properties && typeof properties === "object" && "" in properties),
        "input-name",
        "One of its input fields has no name."
      )
      break
    }
    case "agent": {
      const c = step.config
      need(
        Boolean(c.instructions.trim()),
        "instructions",
        "Tell the agent what to do."
      )
      if (c.output === "json") {
        if (c.output_schema.trim()) {
          need(
            isJson(c.output_schema),
            "schema",
            "The output schema isn't valid JSON."
          )
        } else {
          warn(
            false,
            "schema",
            "Describe the JSON it returns, so later steps can rely on it."
          )
        }
      }
      break
    }
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
    case "subworkflow": {
      const target = step.config.workflow
      if (!target) need(false, "workflow", "Pick the workflow to run.")
      else if (target === context.selfId)
        need(false, "workflow", "A workflow can't run itself.")
      else if (context.workflowIds && !context.workflowIds.has(target)) {
        need(false, "workflow", "That workflow was deleted; pick another.")
      }
      break
    }
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
  "input-name": "input_schema",
  instructions: "instructions",
  schema: "output_schema",
  message: "message",
  url: "url",
  headers: "headers",
  body: "body",
  expression: "expression",
  amount: "amount",
  workflow: "workflow",
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

export type ValidationContext = {
  /** This workflow's ID: a Run workflow step can't pick it. */
  selfId?: string
  /** The organization's workflows, for Run workflow steps. */
  workflowIds?: Set<string>
}

// A field with more than this many problems reports only these.
const PER_FIELD = 3

/** Every issue, errors first, in the order of the steps. */
export function validateWorkflow(
  graph: WorkflowGraph,
  context: ValidationContext = {},
  scopeOf: (id: string) => Scope = scopesOf(graph)
): WorkflowIssue[] {
  const issues: WorkflowIssue[] = []
  const add = (
    level: IssueLevel,
    step: string | undefined,
    code: string,
    message: string
  ) =>
    issues.push({
      id: `${step ?? "workflow"}:${code}`,
      level,
      step,
      field: step ? fieldOfCode(code) : undefined,
      message,
    })

  const byId = new Map(graph.steps.map((s) => [s.id, s]))
  const outgoing = new Map<string, { output: string; target: string }[]>()
  const incoming = new Map<string, string[]>()
  for (const c of graph.connections) {
    if (!byId.has(c.source) || !byId.has(c.target)) continue
    outgoing.set(c.source, [...(outgoing.get(c.source) ?? []), c])
    incoming.set(c.target, [...(incoming.get(c.target) ?? []), c.source])
  }

  // One entry point, which nothing leads back to.
  const entries = graph.steps.filter((s) => s.data.kind === "entry")
  if (entries.length === 0) {
    add(
      "error",
      undefined,
      "entry",
      "Add a start step: every run starts there."
    )
  }
  for (const extra of entries.slice(1)) {
    add(
      "error",
      extra.id,
      "entry-twice",
      "A workflow has one start step; remove this one or the other."
    )
  }
  for (const entry of entries) {
    if (incoming.get(entry.id)?.length) {
      add("error", entry.id, "entry-in", "Nothing can lead back to the start.")
    }
  }

  // What a run can reach.
  const reached = new Set<string>()
  if (entries[0]) {
    const queue = [entries[0].id]
    while (queue.length) {
      const id = queue.shift()!
      if (reached.has(id)) continue
      reached.add(id)
      for (const c of outgoing.get(id) ?? []) queue.push(c.target)
    }
  }

  // Circles without a loop.
  for (const component of stronglyConnected(
    graph.steps.map((s) => s.id),
    outgoing
  )) {
    const circular =
      component.length > 1 ||
      (outgoing.get(component[0]) ?? []).some((c) => c.target === component[0])
    if (!circular) continue
    if (component.some((id) => byId.get(id)?.data.kind === "loop")) continue
    const names = component.map((id) => byId.get(id)?.data.name ?? id)
    for (const id of component) {
      add(
        "error",
        id,
        "cycle",
        `${names.join(" → ")} go round in a circle. Only a loop may repeat steps.`
      )
    }
  }

  for (const step of graph.steps) {
    for (const [code, level, message] of [
      ...settingsIssues(step.data, context),
      ...switchIssues(step.data, scopeOf(step.id)),
    ]) {
      add(level, step.id, code, message)
    }
    // What its expressions read: paths that lead nowhere, wrong types.
    for (const setting of expressionSettings(step.data)) {
      if (!setting.text.trim()) continue
      const found = checkField(
        setting.text,
        setting.mode,
        scopeOf(step.id),
        setting.check
      ).filter((d) => !d.local)
      found
        .slice(0, PER_FIELD)
        .forEach((d, index) =>
          add(
            d.severity,
            step.id,
            `field:${setting.key}:${index}`,
            `${setting.label}: ${d.message}`
          )
        )
    }
    if (entries[0] && !reached.has(step.id)) {
      add("warning", step.id, "unreached", "No way from the start leads here.")
    }
    // A logic step's branch that goes nowhere is a slip; an action's
    // success that goes nowhere is just where the run ends.
    const links = outgoing.get(step.id) ?? []
    const logic = STEP_KINDS[step.data.kind].group === "logic"
    for (const output of logic ? outputsOf(step.data) : []) {
      if (!output.branch || output.fallback) continue
      if (output.id === "done" || links.some((l) => l.output === output.id))
        continue
      add(
        "warning",
        step.id,
        `open-${output.id}`,
        output.body
          ? "The loop's body is empty: connect Each item to the steps to repeat."
          : `The ${output.label} way goes nowhere; a run that takes it ends here.`
      )
    }
    if (step.data.kind === "merge") {
      const ways = incoming.get(step.id)?.length ?? 0
      if (ways < 2) {
        add(
          "warning",
          step.id,
          "merge",
          `A merge waits for two or more ways; ${ways === 0 ? "none comes" : "one comes"} into this one.`
        )
      }
    }
  }

  const order = new Map(graph.steps.map((s, index) => [s.id, index]))
  return issues.sort(
    (a, b) =>
      (a.level === b.level ? 0 : a.level === "error" ? -1 : 1) ||
      (order.get(a.step ?? "") ?? -1) - (order.get(b.step ?? "") ?? -1)
  )
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

/** The label a step kind goes by, for issue lists. */
export const kindLabel = (step: StepData) => STEP_KINDS[step.kind].label
