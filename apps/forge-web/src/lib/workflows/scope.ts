import type {
  BaseStep,
  BuilderGraph,
  KindInfo,
  StepOutput,
} from "@/lib/builder/types"
import type { WorkflowGraph } from "./document"
import { resolveExpression } from "./expressions"
import { outputsOf, STEP_KINDS, type StepData } from "./model"
import { fromJsonSchema, t, type DataType } from "./types"

/*
 * What a step can read when it runs, and the type of each: the run's
 * `input` (typed by the fields the start step declares), the
 * output of every step that can run before it (`steps.<id>.output`, and
 * `.error` where a step can fail its way), `previous` (the step just
 * before it, looking through logic steps, which hand on what they got),
 * and, in a loop's body, the current item and its index. These are the
 * contract a runner implements; the builder completes and checks
 * expressions against them.
 *
 * An agent reads the same (its graph holds Forge's steps by the same
 * kind names), so the reckoning takes rules for what's its own: what the
 * run starts with, what each kind hands on and how it looks, and any
 * roots besides (an agent's session state).
 */

/** How a step is drawn where an expression names it: its kind's look. */
export type StepMark = Pick<
  KindInfo,
  "label" | "icon" | "orb" | "terminal" | "person"
>

export type StepInfo = {
  id: string
  name: string
  kind: string
  mark: StepMark
  output: DataType
  /** Why it failed, for a step with an error or failed way out. */
  error?: DataType
}

export type Root = {
  name: string
  type: DataType
  /** One line: what it is. */
  detail: string
}

export type Scope = {
  /** What an expression may start with: input, steps, previous, a loop's item… */
  roots: Map<string, Root>
  /** Every step that can run before this one. */
  steps: Map<string, StepInfo>
  /** Every step in the workflow, to tell "not before this one" from "no such step". */
  allSteps: Map<string, { name: string }>
}

const ERROR = t.object(
  {
    message: t.string("What went wrong."),
    status: t.number("The HTTP status, when there was a response.", true),
  },
  "Why the step took its error way."
)

const LOGIC = new Set(["if", "switch", "match", "merge"])

/** What a graph's own kinds bring to the reckoning. */
export type ScopeRules<S extends BaseStep> = {
  /** What the run starts with. */
  input: DataType
  /** What a step hands on, as far as its settings say (a merge: what came into it). */
  outputTypeOf: (
    step: S,
    context: { input: DataType; incoming: Map<string, DataType> }
  ) => DataType
  outputsOf: (step: S) => StepOutput[]
  /** How it looks where an expression names it. */
  markOf: (step: S) => StepMark
  /** What every step reads besides input, steps, previous and a loop's item. */
  roots?: Root[]
}

// Forge's steps by kind name, as both graphs hold them.
const configOf = <K extends StepData["kind"]>(step: BaseStep, kind: K) =>
  step.kind === kind ? (step as StepData<K>).config : undefined

/** What a step hands on, as far as its settings say. */
export function outputTypeOf(
  step: StepData,
  { input, incoming }: { input: DataType; incoming: Map<string, DataType> }
): DataType {
  switch (step.kind) {
    case "entry":
      return input
    case "agent":
      if (step.config.output === "json") {
        try {
          const schema: unknown = JSON.parse(
            step.config.output_schema || "null"
          )
          return schema
            ? fromJsonSchema(schema)
            : t.unknown("The JSON it returns.")
        } catch {
          return t.unknown("The JSON it returns (its schema isn't valid JSON).")
        }
      }
      return t.string("The agent's answer.")
    case "approval":
      return t.object({
        approved: t.boolean("Whether the person approved."),
        decided_by: t.string("Who decided."),
        comment: t.string("What they said, if anything."),
        decided_at: t.string("When.", { format: "date-time" }),
      })
    case "http":
      return t.object({
        status: t.number("The response's HTTP status.", true),
        headers: t.object({}, "The response's headers, by lowercase name.", {
          open: true,
        }),
        body: Object.keys(step.config.output_schema).length
          ? {
              ...fromJsonSchema(step.config.output_schema),
              description: "The response's body, as declared.",
            }
          : t.unknown("The response's body: parsed when it's JSON, else text."),
      })
    case "transform":
      // Undeclared, what it makes is worked out from its expression (scopesOf).
      return Object.keys(step.config.output_schema).length
        ? fromJsonSchema(step.config.output_schema)
        : t.unknown("What its expression makes.")
    case "delay":
      return t.object({
        resumed_at: t.string("When it went on.", { format: "date-time" }),
      })
    case "subworkflow":
      return step.config.wait
        ? t.unknown("The other workflow's result.")
        : t.object({ run_id: t.string("The run it started.") })
    case "if":
    case "switch":
    case "match":
      return t.object({ branch: t.string("The way it took, by output ID.") })
    case "loop":
      return t.object({
        count: t.number("How many items it went through.", true),
        results: t.array(
          t.unknown(),
          "What the body's last step handed on, for each item."
        ),
      })
    case "merge":
      return t.object(
        Object.fromEntries(incoming),
        "What each step that came into it handed on, by step ID."
      )
    case "end":
      return t.unknown()
  }
}

/** The values a run starts with: the start step's input fields. */
export function inputType(graph: WorkflowGraph): DataType {
  const entry = graph.steps.find((s) => s.data.kind === "entry")?.data
  if (entry?.kind !== "entry")
    return t.unknown("The run's input (the workflow has no start step).")
  const schema = entry.config.input_schema
  return Object.keys(schema).length
    ? {
        ...fromJsonSchema(schema),
        description: "The values the run started with.",
      }
    : t.unknown(
        "The start step declares no input fields, so input isn't checked. Declare them on the start step."
      )
}

/** The start step's input fields, in order, and which are required. */
export function inputFields(schema: Record<string, unknown>) {
  const type = Object.keys(schema).length ? fromJsonSchema(schema) : undefined
  if (type?.kind !== "object")
    return { names: [] as string[], required: new Set<string>() }
  // A field still being named isn't one yet.
  return {
    names: Object.keys(type.properties).filter((name) => name.trim()),
    required: new Set(type.required ?? []),
  }
}

/** Every step's scope in a workflow, computed once for a graph. */
export function scopesOf(graph: WorkflowGraph) {
  return scopesFor(graph, {
    input: inputType(graph),
    outputTypeOf,
    outputsOf,
    markOf: (step) => STEP_KINDS[step.kind],
  }).scopeOf
}

/**
 * Every step's scope, and what each hands on, computed once for a graph.
 * Types flow in order: a step's output can depend on what came into it
 * (a merge), and a loop's item on its list's type.
 */
export function scopesFor<S extends BaseStep>(
  graph: BuilderGraph<S>,
  rules: ScopeRules<S>
) {
  const byId = new Map(graph.steps.map((s) => [s.id, s]))
  const into = new Map<string, { source: string; output: string }[]>()
  const outOf = new Map<string, { target: string; output: string }[]>()
  for (const c of graph.connections) {
    if (!byId.has(c.source) || !byId.has(c.target)) continue
    into.set(c.target, [
      ...(into.get(c.target) ?? []),
      { source: c.source, output: c.output },
    ])
    outOf.set(c.source, [
      ...(outOf.get(c.source) ?? []),
      { target: c.target, output: c.output },
    ])
  }
  const { input } = rules
  const allSteps = new Map(
    graph.steps.map((s) => [s.id, { name: s.data.name }])
  )

  /** Every step that can run before `id`. */
  const before = (id: string) => {
    const seen = new Set<string>()
    const queue = [id]
    while (queue.length) {
      const at = queue.shift()!
      for (const link of into.get(at) ?? []) {
        if (!seen.has(link.source)) {
          seen.add(link.source)
          queue.push(link.source)
        }
      }
    }
    seen.delete(id)
    return seen
  }

  const outputs = new Map<string, DataType>()
  // Transforms being worked out: their expressions read what's before them.
  const inferring = new Set<string>()
  const outputOf = (id: string, trail = new Set<string>()): DataType => {
    const known = outputs.get(id)
    if (known) return known
    const step = byId.get(id)
    if (!step || trail.has(id) || inferring.has(id)) return t.unknown()
    trail.add(id)
    const incoming = new Map(
      (into.get(id) ?? []).map((link) => [
        link.source,
        outputOf(link.source, trail),
      ])
    )
    let type = rules.outputTypeOf(step.data, { input, incoming })
    const transform = configOf(step.data, "transform")
    if (
      transform &&
      !Object.keys(transform.output_schema).length &&
      transform.expression.trim()
    ) {
      inferring.add(id)
      const made = resolveExpression(transform.expression, scopeOf(id))
      inferring.delete(id)
      if (made && made.kind !== "unknown")
        type = { ...made, description: "What its expression makes." }
    }
    outputs.set(id, type)
    return type
  }

  /** What the step just before hands on, looking through logic steps. */
  const previousOf = (
    id: string,
    trail = new Set<string>()
  ): DataType | undefined => {
    const links = into.get(id) ?? []
    if (links.length !== 1 || trail.has(id)) {
      return links.length
        ? t.unknown("What the step before handed on.")
        : undefined
    }
    trail.add(id)
    const source = byId.get(links[0].source)
    if (!source) return undefined
    // A loop's body starts from the item it's on, as a run hands it on.
    if (source.data.kind === "loop" && links[0].output === "each") {
      return {
        ...itemOf(source.id),
        description: `The item ${source.data.name} is on.`,
      }
    }
    if (LOGIC.has(source.data.kind) && source.data.kind !== "merge") {
      return previousOf(source.id, trail) ?? t.unknown()
    }
    return outputOf(source.id)
  }

  /**
   * What a loop's items are: what its list holds. A run takes one value
   * on its own (JSONata gives a single match unwrapped) as a list of one.
   */
  const itemOf = (loopId: string): DataType => {
    const loop = byId.get(loopId)
    const config = loop && configOf(loop.data, "loop")
    if (!config || working.has(loopId)) return t.unknown()
    const list = resolveExpression(config.items, scopeOf(loopId))
    if (!list || list.kind === "unknown") return t.unknown()
    return list.kind === "array" ? list.items : list
  }

  /** The loops whose body `id` is in, innermost last. */
  const loopsAround = (id: string) =>
    graph.steps.filter((loop) => {
      if (loop.data.kind !== "loop") return false
      // The body: reachable from Each item without coming back through the loop.
      const seen = new Set<string>()
      const queue = (outOf.get(loop.id) ?? [])
        .filter((l) => l.output === "each")
        .map((l) => l.target)
      while (queue.length) {
        const at = queue.shift()!
        if (at === loop.id || seen.has(at)) continue
        seen.add(at)
        for (const link of outOf.get(at) ?? []) queue.push(link.target)
      }
      return seen.has(id)
    })

  const scopes = new Map<string, Scope>()
  const working = new Set<string>()
  const scopeOf = (id: string): Scope => {
    const cached = scopes.get(id)
    if (cached) return cached
    // Loops in each other's bodies: the inner one sees no item of the outer.
    const nested = working.has(id)
    working.add(id)
    const steps = new Map<string, StepInfo>()
    for (const other of before(id)) {
      const step = byId.get(other)
      if (!step || step.data.kind === "end") continue
      const canFail = rules
        .outputsOf(step.data)
        .some((o) => o.id === "error" || o.id === "failed")
      steps.set(other, {
        id: other,
        name: step.data.name,
        kind: step.data.kind,
        mark: rules.markOf(step.data),
        output: outputOf(other),
        error: canFail ? ERROR : undefined,
      })
    }
    const roots = new Map<string, Root>()
    roots.set("input", {
      name: "input",
      type: input,
      detail: "The run's input",
    })
    roots.set("steps", {
      name: "steps",
      type: t.object(
        Object.fromEntries(
          [...steps.values()].map((s) => [
            s.id,
            t.object(
              { output: s.output, ...(s.error ? { error: s.error } : {}) },
              `${s.name} (${s.mark.label})`
            ),
          ])
        ),
        "The steps that run before this one, by ID."
      ),
      detail: "Earlier steps, by ID",
    })
    const previous = previousOf(id)
    if (previous) {
      roots.set("previous", {
        name: "previous",
        type: previous,
        detail: "What the step just before handed on",
      })
    }
    // The current item of each loop around it (the innermost wins a name).
    const scopeForLoops = new Map<string, Scope>()
    for (const loop of nested ? [] : loopsAround(id)) {
      const config = configOf(loop.data, "loop")
      if (!config || working.has(loop.id)) continue
      const loopScope = scopeForLoops.get(loop.id) ?? scopeOf(loop.id)
      scopeForLoops.set(loop.id, loopScope)
      const list = resolveExpression(config.items, loopScope)
      const item =
        list?.kind === "array"
          ? list.items
          : list && list.kind !== "unknown"
            ? list
            : t.unknown()
      const name = config.item_name || "item"
      roots.set(name, {
        name,
        type: { ...item, description: `The item ${loop.data.name} is on.` },
        detail: `${loop.data.name}'s current item`,
      })
      roots.set("index", {
        name: "index",
        type: t.number(
          `Where the item is in ${loop.data.name}'s list, from 0.`,
          true
        ),
        detail: `${loop.data.name}'s current position`,
      })
    }
    for (const root of rules.roots ?? []) roots.set(root.name, root)
    const scope = { roots, steps, allSteps }
    working.delete(id)
    if (!nested) scopes.set(id, scope)
    return scope
  }

  return { scopeOf, outputOf: (id: string) => outputOf(id) }
}
