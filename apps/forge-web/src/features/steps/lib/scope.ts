import type {
  BaseStep,
  BuilderGraph,
  KindInfo,
  StepOutput,
} from "@/features/builder/lib/types"
import { resolveExpression } from "./expressions"
import { BODY_KINDS, type EachConfig, type StepData } from "./model"
import { fromJsonSchema, t, unionOf, type DataType } from "./types"

/*
 * What a step can read when it runs, and the type of each: the run's
 * `input` (typed by the fields the start step declares), the
 * output of every step that can run before it (`steps.<id>.output`, and
 * `.error` where a step can fail its way), `previous` (the step just
 * before it, looking through logic steps, which hand on what they got),
 * and, in a loop's or for-each's body (or a Filter's condition), the
 * current item and its index. These are the contract a runner implements; the builder
 * completes and checks expressions against them.
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
  /** How high completions list it (higher first); by default previous, then steps. */
  boost?: number
}

export type Scope = {
  /** What an expression may start with: input, steps, previous, a loop's item… */
  roots: Map<string, Root>
  /** Every step that can run before this one. */
  steps: Map<string, StepInfo>
  /** Every step in the workflow, to tell "not before this one" from "no such step". */
  allSteps: Map<string, { name: string }>
  /**
   * The loops (and for-eachs) whose body the step is in, by ID: they hand
   * on once every item has run, so inside, their output isn't there yet.
   */
  enclosing?: Set<string>
}

// What a run records as `steps.<id>.error` (StepFailed.as_error): its
// message, and the status, body or expression when they're known.
const ERROR = t.object(
  {
    message: t.string("What went wrong."),
    status: t.number("The HTTP status, when there was a response.", true),
    body: t.unknown(
      "The response's body, when it answered: a status that isn't 2xx, or a body that doesn't fit what it returns."
    ),
    method: t.string("The request's method, when it answered with a status that isn't 2xx."),
    url: t.string("The URL it sent the request to, when it answered with a status that isn't 2xx."),
    expression: t.string("The expression that failed, when one did."),
    may_have_run: t.boolean(
      "True when it was cut off as it acted (the run stopped while it ran): what it does may have happened, and it isn't run again on its own."
    ),
    idempotency_key: t.string(
      "Its key, when it was cut off: the same for every attempt of it (its Idempotency-Key, its FORGE_IDEMPOTENCY_KEY)."
    ),
  },
  "Why the step took its Error way.",
  { required: ["message"] }
)

// What a Bash step's error is: how its command ended, and what it wrote.
const COMMAND_ERROR = t.object(
  {
    message: t.string(
      "What went wrong: its exit code and the last line it wrote, a folder outside the run's, or its timeout."
    ),
    exit_code: t.number(
      "Its exit code, when it ran to its end: minus the signal's number when one killed it.",
      true
    ),
    stdout: t.string("What it wrote to stdout, when it ran."),
    stderr: t.string("What it wrote to stderr, when it ran."),
    may_have_run: t.boolean(
      "True when it was cut off as it acted (the run stopped while it ran): what it does may have happened, and it isn't run again on its own."
    ),
    idempotency_key: t.string(
      "Its key, when it was cut off: the same for every attempt of it (its Idempotency-Key, its FORGE_IDEMPOTENCY_KEY)."
    ),
  },
  "Why the step took its Error way.",
  { required: ["message"] }
)

// What a git step's error is: the git command that failed and what it said.
const GIT_ERROR = t.object(
  {
    message: t.string(
      "What went wrong: the git command that failed and the last thing it said, a repository, folder or branch it can't use, its credential, or its timeout."
    ),
    exit_code: t.number("git's exit code, when a git command failed.", true),
    stderr: t.string("What git wrote to stderr, when a git command failed."),
    may_have_run: t.boolean(
      "True when it was cut off as it acted (the run stopped while it ran): what it does may have happened, and it isn't run again on its own."
    ),
    idempotency_key: t.string(
      "Its key, when it was cut off: the same for every attempt of it (its Idempotency-Key, its FORGE_IDEMPOTENCY_KEY)."
    ),
  },
  "Why the step took its Error way.",
  { required: ["message"] }
)

/** The steps that run git: their errors are git's. */
const GIT_KINDS = new Set(["git_clone", "git_branch", "git_push"])

/** Logic steps that hand on what came into them (a run records their way as their output). */
const PASS_THROUGH = new Set(["if", "switch", "match"])

/**
 * What a list's items are: what it holds. A run takes one value on its
 * own (JSONata gives a single match unwrapped) as a list of one.
 */
export function itemTypeOf(list: DataType | undefined): DataType {
  if (!list || list.kind === "unknown") return t.unknown()
  return list.kind === "array" ? list.items : list
}

/** A setting read once per item of a list: what's read, and as what. */
export type ItemBinding = {
  /** The list's expression. */
  items: string
  /** What the item is called. */
  name: string
  /** The step it's read in, by name. */
  of: string
}

/**
 * A scope with an item of a list in it (a Filter's condition): the item
 * under its name, typed by what the list holds, and `index`. The innermost
 * wins a name, as a run binds it.
 */
export function withItem(
  scope: Scope,
  { items, name, of }: ItemBinding
): Scope {
  const item = itemTypeOf(resolveExpression(items, scope))
  const roots = new Map(scope.roots)
  roots.set(name, {
    name,
    type: { ...item, description: `The item ${of} is on.` },
    detail: `${of}'s current item`,
    boost: 4,
  })
  roots.set("index", {
    name: "index",
    type: t.number(`Where the item is in ${of}'s list, from 0.`, true),
    detail: `${of}'s current position`,
    boost: 0,
  })
  return { ...scope, roots }
}

/** A loop's or filter's `{count, results}`, its results' items typed. */
function withResults(type: DataType, items: DataType): DataType {
  if (type.kind !== "object" || type.properties.results?.kind !== "array")
    return type
  const results = type.properties.results
  return {
    ...type,
    properties: { ...type.properties, results: { ...results, items } },
  }
}

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

/** A loop's or a for-each's list settings. */
const eachOf = (step: BaseStep) =>
  BODY_KINDS.has(step.kind)
    ? (step as unknown as { config: EachConfig }).config
    : undefined

/** What an HTTP request hands on: its response, with `body`. */
const httpResponse = (body: DataType): DataType =>
  t.object({
    status: t.number("The response's HTTP status.", true),
    headers: t.object({}, "The response's headers, by lowercase name.", {
      open: true,
    }),
    body,
  })

/** What a step hands on, as far as its settings say. */
export function outputTypeOf(
  step: StepData,
  { incoming }: { input: DataType; incoming: Map<string, DataType> }
): DataType {
  switch (step.kind) {
    case "approval":
      return t.object({
        approved: t.boolean("Whether the person approved."),
        decided_by: t.string("Who decided."),
        comment: t.string("What they said, if anything."),
        decided_at: t.string("When.", { format: "date-time" }),
      })
    case "decision_board": {
      const decision = t.object({
        choice: t.string("The choice's ID."),
        label: t.string("The choice's name."),
        question: t.string("What they were asked."),
        approved: t.boolean("Whether they accepted it."),
        decided_by: t.string("Who decided; Forge when its time ran out."),
        comment: t.string("What they said, if anything."),
        decided_at: t.string("When.", { format: "date-time" }),
      })
      const ids = (what: string) => t.array(t.string("A choice's ID."), what)
      return t.object({
        accepted: ids("The choices accepted so far, by ID."),
        rejected: ids("The choices rejected so far, by ID."),
        open: ids("The choices nobody has decided yet, by ID: none at Done."),
        decisions: t.object(
          Object.fromEntries(
            step.config.choices.map((choice, index) => [
              choice.id,
              {
                ...decision,
                description: `${choice.label.trim() || `Choice ${index + 1}`}'s decision, once it's made.`,
              },
            ])
          ),
          "Each decision made so far, by choice ID."
        ),
        decided: {
          ...decision,
          description:
            "The decision just made: the choice whose way this is. Null at Done when nothing was laid out.",
        },
      })
    }
    case "http":
      return httpResponse(
        Object.keys(step.config.output_schema).length
          ? {
              ...fromJsonSchema(step.config.output_schema),
              description: "The response's body, as declared.",
            }
          : t.unknown("The response's body: parsed when it's JSON, else text.")
      )
    case "bash":
      return t.object({
        exit_code: t.number("Its exit code: 0, by its Success way.", true),
        stdout: t.string(
          "What it wrote to stdout, without its last line ends (the end of it, when it was long)."
        ),
        stderr: t.string("What it wrote to stderr (the end of it, when it was long)."),
        cwd: t.string(
          "The folder it ran in: an absolute path in the run's folder."
        ),
        truncated: t.boolean(
          "Whether stdout or stderr was longer than the worker keeps: then only its end is."
        ),
      })
    case "git_clone":
      return t.object({
        folder: t.string(
          "Where it's cloned: an absolute path in the run's folder. Give it to the next git step's folder."
        ),
        repository: t.string("The repository: owner/name."),
        url: t.string("The repository's page: https://github.com/owner/name."),
        branch: t.string("The branch checked out; null when a tag is."),
        commit: t.string("The commit checked out: its full SHA."),
        cloned: t.boolean(
          "false when a run tried again found its clone already there."
        ),
      })
    case "git_branch":
      return t.object({
        folder: t.string("The repository's folder: an absolute path."),
        branch: t.string("The branch, now checked out."),
        commit: t.string("The commit it's at: its full SHA."),
        created: t.boolean("false when the branch was there already."),
      })
    case "git_push":
      return t.object({
        folder: t.string("The repository's folder: an absolute path."),
        repository: t.string("The repository pushed to: owner/name."),
        branch: t.string("The branch pushed. Give it to Open pull request."),
        commit: t.string("The commit pushed: its full SHA."),
        committed: t.boolean(
          "Whether anything had changed: false pushes the branch as it was."
        ),
        files: t.array(t.string(), "The files the commit changed, by path."),
      })
    case "github_pull_request":
      return t.object({
        number: t.number("The pull request's number.", true),
        url: t.string("Its page on GitHub."),
        title: t.string("Its title."),
        state: t.string("open, as it's just opened.", { enum: ["open", "closed"] }),
        draft: t.boolean("Whether it's a draft."),
        head: t.string("The branch it merges."),
        base: t.string("The branch it merges into."),
        created: t.boolean(
          "false when one was open for the branch already: that one is handed on."
        ),
      })
    case "transform":
      // Undeclared, what it makes is worked out from its expression (scopesOf).
      return Object.keys(step.config.output_schema).length
        ? fromJsonSchema(step.config.output_schema)
        : t.unknown("What its expression makes.")
    case "filter":
      // Its results' items are what its list holds (scopesFor).
      return t.object({
        count: t.number("How many items it kept.", true),
        results: t.array(
          t.unknown(),
          "The items its condition held for, in order."
        ),
      })
    case "delay":
      return t.object({
        resumed_at: t.string("When it went on.", { format: "date-time" }),
      })
    case "match":
      if (step.config.mode === "all")
        return t.object({
          branches: t.array(
            t.string(),
            "The ways it took, by output ID: every rule that held, else otherwise."
          ),
        })
      return t.object({ branch: t.string("The way it took, by output ID.") })
    case "if":
    case "switch":
      return t.object({ branch: t.string("The way it took, by output ID.") })
    case "for_each":
      return t.object({
        count: t.number("How many items it went through.", true),
      })
    case "loop":
      // Its results' items are what its body hands back (scopesFor).
      return t.object({
        count: t.number("How many items it went through.", true),
        results: t.array(
          t.unknown(),
          "What the body's last step handed on, for each item; null for one whose body ended before coming back."
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

/**
 * Every step's scope, and what each hands on, computed once for a graph.
 * Types flow in order: a step's output can depend on what came into it
 * (a merge), a loop's item and a filter's results on their list's type,
 * and a loop's results on what its body hands back.
 *
 * What a way in hands on is what a run hands on by it: a step's output; a
 * loop's Each item way, the item; an Error way, nothing (why is the step's
 * error); If, Switch and Match, what came into them. A loop's body leads
 * back to it, but those ways bring each item's result to what collects
 * them: the loop itself is handed only what comes from outside its body.
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

  // Set when a type is worked out while one it needs is still being
  // worked out (a circle): what's found meanwhile isn't kept.
  let cut = false

  const outputs = new Map<string, DataType>()
  // Steps whose output is being worked out: a transform, filter or loop
  // reads what's before it (a loop, its body), which may lead back to it.
  const computing = new Set<string>()
  const outputOf = (id: string): DataType => {
    const known = outputs.get(id)
    if (known) return known
    const step = byId.get(id)
    if (!step) return t.unknown()
    if (computing.has(id)) {
      cut = true
      return t.unknown()
    }
    computing.add(id)
    try {
      // A merge hands on what each way into it brought, by the step it came from.
      const incoming = new Map<string, DataType>(
        step.data.kind === "merge"
          ? waysIn(id).map((link) => [link.source, handedOn(link)])
          : []
      )
      let type = rules.outputTypeOf(step.data, { input, incoming })
      const transform = configOf(step.data, "transform")
      if (
        transform &&
        !Object.keys(transform.output_schema).length &&
        transform.expression.trim()
      ) {
        const made = resolveExpression(transform.expression, scopeOf(id))
        if (made && made.kind !== "unknown")
          type = { ...made, description: "What its expression makes." }
      }
      const filter = configOf(step.data, "filter")
      if (filter?.items.trim()) {
        const item = itemTypeOf(resolveExpression(filter.items, scopeOf(id)))
        if (item.kind !== "unknown") type = withResults(type, item)
      }
      if (step.data.kind === "loop") {
        const result = resultOf(id)
        if (result.kind !== "unknown") type = withResults(type, result)
      }
      outputs.set(id, type)
      return type
    } finally {
      computing.delete(id)
    }
  }

  /**
   * The ways into a step that hand it something: all of them, but for a
   * loop's (or a for-each's) ways back from its body.
   */
  const waysIn = (id: string) => {
    const links = into.get(id) ?? []
    const step = byId.get(id)
    if (!step || !BODY_KINDS.has(step.data.kind)) return links
    const body = bodyOf(id)
    return links.filter((link) => link.source !== id && !body.has(link.source))
  }

  /** What a way hands the step it leads to, as a run hands it on. */
  const handedOn = (link: { source: string; output: string }): DataType => {
    const source = byId.get(link.source)
    if (!source) return t.unknown()
    // A loop's body starts from the item it's on.
    if (BODY_KINDS.has(source.data.kind) && link.output === "each") {
      return {
        ...itemOf(source.id),
        description: `The item ${source.data.name} is on.`,
      }
    }
    if (link.output === "error") {
      return t.null(
        `Nothing: ${source.data.name} took its Error way. Why is in steps.${source.id}.error.`
      )
    }
    // An HTTP request's status way hands on the response, its body not the
    // one a success declares.
    if (source.data.kind === "http" && link.output !== "success")
      return httpResponse(t.unknown("The response's body: parsed when it's JSON, else text."))
    // If, Switch and Match hand on what came into them.
    if (PASS_THROUGH.has(source.data.kind))
      return previousOf(source.id) ?? t.unknown()
    return outputOf(source.id)
  }

  /**
   * What a loop's body hands back for an item: what each way back to the
   * loop hands on (an Each item way straight back, the item). A way that
   * hands on nothing (an Error way) adds nothing.
   */
  const resultOf = (loopId: string): DataType => {
    const body = bodyOf(loopId)
    return unionOf(
      (into.get(loopId) ?? [])
        .filter((link) =>
          link.source === loopId
            ? link.output === "each"
            : body.has(link.source)
        )
        .map(handedOn)
    )
  }

  const previousTypes = new Map<string, DataType | undefined>()
  const reading = new Set<string>()
  /**
   * What `previous` is in a step: what its way in hands it. With several
   * ways in, it runs once for each that arrives, so it's any of theirs:
   * typed where they agree.
   */
  const previousOf = (id: string): DataType | undefined => {
    if (previousTypes.has(id)) return previousTypes.get(id)
    const links = waysIn(id)
    if (!links.length) return undefined
    if (reading.has(id)) {
      cut = true
      return t.unknown("What the step before handed on.")
    }
    const outer = cut
    cut = false
    reading.add(id)
    let type: DataType
    try {
      // Ways from one step hand on the same, but for its Each item and Error
      // ways, and an HTTP request's status ways.
      const ways = new Map<string, { source: string; output: string }>()
      for (const link of links) {
        const special =
          link.output === "each" ||
          link.output === "error" ||
          (byId.get(link.source)?.data.kind === "http" && link.output !== "success")
        ways.set(`${link.source}:${special ? link.output : ""}`, link)
      }
      const types = [...ways.values()].map(handedOn)
      if (types.length === 1) type = types[0]
      else {
        const union = unionOf(types)
        type =
          union.kind === "unknown"
            ? t.unknown(
                "What the step before handed on: its ways in hand on different things."
              )
            : {
                ...union,
                description:
                  "What the step before handed on, by whichever way came in.",
              }
      }
    } finally {
      reading.delete(id)
    }
    if (!cut) previousTypes.set(id, type)
    cut ||= outer
    return type
  }

  /** What a loop's (or a for-each's) items are: what its list holds. */
  const itemOf = (loopId: string): DataType => {
    const loop = byId.get(loopId)
    const config = loop && eachOf(loop.data)
    if (!config) return t.unknown()
    if (working.has(loopId)) {
      cut = true
      return t.unknown()
    }
    return itemTypeOf(resolveExpression(config.items, scopeOf(loopId)))
  }

  // The body: reachable from Each item without coming back through the loop.
  const bodies = new Map<string, Set<string>>()
  const bodyOf = (loopId: string) => {
    const known = bodies.get(loopId)
    if (known) return known
    const seen = new Set<string>()
    const queue = (outOf.get(loopId) ?? [])
      .filter((l) => l.output === "each")
      .map((l) => l.target)
    while (queue.length) {
      const at = queue.shift()!
      if (at === loopId || seen.has(at)) continue
      seen.add(at)
      for (const link of outOf.get(at) ?? []) queue.push(link.target)
    }
    bodies.set(loopId, seen)
    return seen
  }

  /** The loops (and for-eachs) whose body `id` is in, innermost last. */
  const loopsAround = (id: string) =>
    graph.steps.filter(
      (loop) => BODY_KINDS.has(loop.data.kind) && bodyOf(loop.id).has(id)
    )

  const scopes = new Map<string, Scope>()
  const working = new Set<string>()
  const scopeOf = (id: string): Scope => {
    const cached = scopes.get(id)
    if (cached) return cached
    // Loops in each other's bodies: the inner one sees no item of the outer.
    const nested = working.has(id)
    working.add(id)
    const around = loopsAround(id)
    const steps = new Map<string, StepInfo>()
    for (const other of before(id)) {
      const step = byId.get(other)
      if (!step || step.data.kind === "end") continue
      // A step in a loop's body is read only inside it: a run keeps each
      // item's apart (and the loop itself runs before its body).
      if (loopsAround(other).some((loop) => !around.includes(loop))) continue
      // A loop around it hands on once every item has run: inside its
      // body, it hasn't yet.
      if (around.some((loop) => loop.id === other)) continue
      const canFail = rules
        .outputsOf(step.data)
        .some((o) => o.id === "error" || o.id === "failed")
      steps.set(other, {
        id: other,
        name: step.data.name,
        kind: step.data.kind,
        mark: rules.markOf(step.data),
        output: outputOf(other),
        error: canFail
          ? step.data.kind === "bash"
            ? COMMAND_ERROR
            : GIT_KINDS.has(step.data.kind)
              ? GIT_ERROR
              : ERROR
          : undefined,
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
    // One whose own scope is being worked out can't give it yet: this one
    // isn't kept, so it's worked out again whole.
    let partial = false
    for (const loop of nested ? [] : around) {
      const config = eachOf(loop.data)
      if (!config) continue
      if (working.has(loop.id)) {
        partial = true
        continue
      }
      const item = withItem(scopeOf(loop.id), {
        items: config.items,
        name: config.item_name || "item",
        of: loop.data.name,
      })
      for (const name of [config.item_name || "item", "index"])
        roots.set(name, item.roots.get(name)!)
    }
    for (const root of rules.roots ?? []) roots.set(root.name, root)
    const scope: Scope = {
      roots,
      steps,
      allSteps,
      enclosing: new Set(around.map((loop) => loop.id)),
    }
    working.delete(id)
    if (!nested && !partial) scopes.set(id, scope)
    return scope
  }

  return {
    scopeOf,
    outputOf: (id: string) => outputOf(id),
    /** What `previous` is in a step; undefined when nothing leads to it. */
    previousOf: (id: string) => previousOf(id),
  }
}
