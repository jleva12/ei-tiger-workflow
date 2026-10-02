import {
  JSONATA_FUNCTIONS,
  parseJsonata,
  type JsonataError,
  type JsonataNode,
} from "./jsonata"
import { normalizeReferences } from "./references"
import type { Scope, StepInfo, StepMark } from "./scope"
import { fieldNames, isScalar, t, typeLabel, type DataType } from "./types"

/*
 * Reading the JSONata a workflow's settings hold, against the scope of the
 * step they're in: what each path (`steps.read_issue.output.summary`)
 * points at and its type, what's wrong with it, and what could come next
 * where the cursor is.
 *
 * A run evaluates every expression against one object: the scope's roots,
 * `{ input, steps, previous, <a loop's item>, index }`. So a path starts
 * with one of them, and inside a filter (`items[price > 10]`) a path starts
 * from the item. Two kinds of field:
 * - an expression: `{{ steps.classify.output.kind }} = "bug"` (bare paths work too);
 * - a template: text with `{{ expression }}` in it, each written into the
 *   text at run time.
 */

export type FieldMode = "expression" | "template"

export type Diagnostic = {
  from: number
  to: number
  severity: "error" | "warning"
  message: string
  /** Only the field says it (an empty {{ }} being filled in); the workflow's issues don't. */
  local?: boolean
}

export type Completion = {
  label: string
  /** What's inserted, when it isn't the label. */
  apply?: string
  /** The type, or what it is. */
  detail?: string
  info?: string
  kind: "variable" | "property" | "method" | "value" | "argument"
  /** Sorts it up (higher first). */
  boost?: number
  /** Characters before the completion's start it replaces too (`[0]` replaces the dot). */
  replaceBefore?: number
  /** Characters after it the cursor moves past (a value's closing quote). */
  skipAfter?: number
  /** The step it names, so it wears the step's mark, as on the canvas. */
  step?: StepMark
  /** Words beside the label (a step's name), in the UI face. */
  note?: string
}

export type CompletionResult = {
  from: number
  to: number
  options: Completion[]
}

export type FieldCheck = {
  /** What the whole expression must be. */
  expect?: "list" | "value"
  /** Text a template writes: an object there reads as JSON. */
  textual?: boolean
}

/* -------------------------------------------------------------------------- */
/* Words                                                                      */
/* -------------------------------------------------------------------------- */

/** The closest of `names` to `name`, when one is close enough to be a slip. */
export function closest(
  name: string,
  names: Iterable<string>
): string | undefined {
  let best: { name: string; distance: number } | undefined
  for (const candidate of names) {
    const distance = editDistance(name.toLowerCase(), candidate.toLowerCase())
    if (
      distance <= Math.max(1, Math.floor(candidate.length / 3)) &&
      (!best || distance < best.distance)
    ) {
      best = { name: candidate, distance }
    }
  }
  return best?.name
}

function editDistance(a: string, b: string) {
  const row = Array.from({ length: b.length + 1 }, (_, i) => i)
  for (let i = 1; i <= a.length; i++) {
    let previous = row[0]
    row[0] = i
    for (let j = 1; j <= b.length; j++) {
      const saved = row[j]
      row[j] = Math.min(
        row[j] + 1,
        row[j - 1] + 1,
        previous + (a[i - 1] === b[j - 1] ? 0 : 1)
      )
      previous = saved
    }
  }
  return row[b.length]
}

const listOf = (names: string[]) =>
  names.length > 6 ? `${names.slice(0, 6).join(", ")}…` : names.join(", ")

const suggest = (name: string, names: string[]) => {
  const near = closest(name, names)
  return near ? ` Did you mean ${near}?` : ""
}

/* -------------------------------------------------------------------------- */
/* Typing a syntax tree                                                       */
/* -------------------------------------------------------------------------- */

type Hover = { from: number; to: number; type: DataType; step?: StepInfo }

type Env = {
  scope: Scope
  /** The expression's text, and where it sits in the field. */
  text: string
  offset: number
  /** What a path starts from: the scope's roots, or a filter's item. */
  context: DataType
  /** Whether `context` is the roots (a path's first name is one of them). */
  top: boolean
  /** What a path starting from a filter's item is called in messages. */
  contextLabel: string
  roots: DataType
  vars: Map<string, DataType>
  diagnostics: Diagnostic[]
  hovers: Hover[]
}

/** The scope's roots as the object a run evaluates against. */
const rootsOf = (scope: Scope): DataType =>
  t.object(
    Object.fromEntries(
      [...scope.roots].map(([name, root]) => [name, root.type])
    )
  )

function envFor(scope: Scope, text: string, offset: number): Env {
  const roots = rootsOf(scope)
  return {
    scope,
    text,
    offset,
    context: roots,
    top: true,
    contextLabel: "",
    roots,
    vars: new Map(),
    diagnostics: [],
    hovers: [],
  }
}

const within = (env: Env, context: DataType, contextLabel: string): Env => ({
  ...env,
  context,
  top: false,
  contextLabel,
})

/** Where a name (plain or `backticked`) sits in the field. */
function nameRange(node: JsonataNode, env: Env) {
  const end = node.position ?? 0
  const name = String(node.value)
  const quoted = env.text[end - 1] === "`"
  const from = Math.max(0, end - name.length - (quoted ? 2 : 0))
  return { from: env.offset + from, to: env.offset + end }
}

/** Where a string literal sits, its quotes included. */
function stringRange(node: JsonataNode, env: Env) {
  const end = node.position ?? 0
  const quote = env.text[end - 1]
  let start = end - 2
  while (
    start > 0 &&
    !(env.text[start] === quote && env.text[start - 1] !== "\\")
  )
    start--
  return { from: env.offset + Math.max(0, start), to: env.offset + end }
}

/** Where a `$variable` sits. */
function variableRange(node: JsonataNode, env: Env) {
  const end = node.position ?? 0
  return {
    from: env.offset + Math.max(0, end - String(node.value).length - 1),
    to: env.offset + end,
  }
}

const describeKind = (type: DataType) =>
  type.kind === "string"
    ? "text"
    : type.kind === "null"
      ? "null"
      : type.kind === "array"
        ? `a list (${typeLabel(type)})`
        : `a ${typeLabel(type)}`

/**
 * A field of `type`, by name: JSONata's step through a path, which maps
 * over a list (`items.name` is every item's name).
 */
function fieldOf(
  type: DataType,
  node: JsonataNode,
  env: Env,
  path: string
): DataType {
  const name = String(node.value)
  const range = nameRange(node, env)
  const report = (severity: Diagnostic["severity"], message: string) =>
    env.diagnostics.push({ ...range, severity, message })
  switch (type.kind) {
    case "unknown":
      return t.unknown()
    case "object": {
      const next = type.properties[name]
      if (next) return next
      const names = fieldNames(type)
      if (type.open) {
        // It may have more, but they aren't declared: likely a slip.
        if (names.length) {
          report(
            "warning",
            `${path} doesn't declare ${name}, so it may not be there. It declares ${listOf(names)}.${suggest(name, names)}`
          )
        }
        return t.unknown()
      }
      report(
        "error",
        names.length
          ? `${path} has no ${name}. It has ${listOf(names)}.${suggest(name, names)}`
          : `${path} has no fields.`
      )
      return t.unknown()
    }
    case "array": {
      const each = fieldOf(type.items, node, env, `Each item of ${path}`)
      if (each.kind === "unknown") return each
      return t.array(
        each.kind === "array" ? each.items : each,
        each.description
      )
    }
    default:
      report("error", `${path} is ${describeKind(type)}: it has no ${name}.`)
      return t.unknown()
  }
}

const isIndex = (node: JsonataNode | undefined): boolean =>
  !!node &&
  (node.type === "number" ||
    (node.type === "unary" &&
      node.value === "-" &&
      node.expression?.type === "number"))

/** A step's filters: `[0]` picks an item; `[condition]` keeps the items it holds for. */
function staged(
  type: DataType,
  node: JsonataNode,
  env: Env,
  path: string
): DataType {
  let current = type
  for (const stage of node.stages ?? []) {
    if (stage.type !== "filter") {
      visit(stage, env)
      continue
    }
    const item = current.kind === "array" ? current.items : current
    if (isIndex(stage.expr)) {
      current = item
      continue
    }
    typeOf(stage.expr, within(env, item, `Each item of ${path}`))
  }
  return current
}

function pathType(node: JsonataNode, env: Env): DataType {
  const steps = node.steps ?? []
  const first = steps[0]
  if (!first) return t.unknown()
  let type: DataType
  let path: string
  if (first.type === "name") {
    const name = String(first.value)
    path = name
    if (env.top) {
      const root = env.scope.roots.get(name)
      if (!root) {
        const names = [...env.scope.roots.keys()]
        env.diagnostics.push({
          ...nameRange(first, env),
          severity: "error",
          message: `${name} isn't defined here. Use ${listOf(names)}.${suggest(name, names)}`,
        })
        type = t.unknown()
      } else {
        type = root.type
        env.hovers.push({ ...nameRange(first, env), type })
      }
    } else {
      type = fieldOf(env.context, first, env, env.contextLabel)
      if (type.kind !== "unknown")
        env.hovers.push({ ...nameRange(first, env), type })
    }
  } else {
    path = "It"
    type = typeOf(first, env)
  }
  type = staged(type, first, env, path)

  for (let i = 1; i < steps.length; i++) {
    const step = steps[i]
    if (step.type === "name") {
      const name = String(step.value)
      // steps.<id>: whether the step exists, and runs before this one.
      if (
        i === 1 &&
        env.top &&
        first.type === "name" &&
        first.value === "steps"
      ) {
        const info = env.scope.steps.get(name)
        if (!info) {
          const other = env.scope.allSteps.get(name)
          env.diagnostics.push({
            ...nameRange(step, env),
            severity: "error",
            message: other
              ? `${other.name} (${name}) doesn't run before this step, so its output isn't there yet.`
              : `There's no step ${name}.${suggest(name, [...env.scope.steps.keys()])}`,
          })
          type = t.unknown()
        } else {
          type =
            type.kind === "object"
              ? (type.properties[name] ?? t.unknown())
              : t.unknown()
          env.hovers.push({ ...nameRange(step, env), type, step: info })
        }
      } else {
        type = fieldOf(type, step, env, path)
        if (type.kind !== "unknown")
          env.hovers.push({ ...nameRange(step, env), type })
      }
      path = `${path}.${name}`
    } else {
      // A step that isn't a name (an object built, a function called) runs
      // on each item.
      const each = type.kind === "array" ? type.items : type
      const made = typeOf(step, within(env, each, `Each item of ${path}`))
      type =
        type.kind === "array"
          ? t.array(made)
          : type.kind === "unknown"
            ? t.unknown()
            : made
    }
    type = staged(type, step, env, path)
  }
  for (const [key, value] of node.group?.lhs ?? []) {
    typeOf(key, env)
    typeOf(value, env)
  }
  return type
}

/** An allowed value compared with a string that isn't one of them. */
function checkAllowed(
  type: DataType,
  literal: JsonataNode | undefined,
  env: Env
) {
  if (type.kind !== "string" || !type.enum?.length) return
  const values =
    literal?.type === "string"
      ? [literal]
      : literal?.type === "unary" && literal.value === "["
        ? (literal.expressions ?? []).filter((e) => e.type === "string")
        : []
  for (const value of values) {
    const text = String(value.value)
    if (type.enum.includes(text)) continue
    env.diagnostics.push({
      ...stringRange(value, env),
      severity: "warning",
      message: `It's never "${text}": it's one of ${listOf(type.enum.map((v) => `"${v}"`))}.${suggest(text, type.enum)}`,
    })
  }
}

function binaryType(node: JsonataNode, env: Env): DataType {
  const op = String(node.value)
  const lhs = node.lhs as JsonataNode | undefined
  const left = typeOf(lhs, env)
  const right = typeOf(node.rhs, env)
  if (op === "=" || op === "!=") {
    checkAllowed(left, node.rhs, env)
    checkAllowed(right, lhs, env)
  } else if (op === "in") {
    checkAllowed(left, node.rhs, env)
  }
  switch (op) {
    case "=":
    case "!=":
    case "<":
    case "<=":
    case ">":
    case ">=":
    case "in":
    case "and":
    case "or":
      return t.boolean()
    case "&":
      return t.string()
    case "+":
    case "-":
    case "*":
    case "/":
    case "%":
      return t.number()
    case "..":
      return t.array(t.number(undefined, true))
    default:
      return t.unknown()
  }
}

function unaryType(node: JsonataNode, env: Env): DataType {
  switch (node.value) {
    case "-":
      typeOf(node.expression, env)
      return t.number()
    case "[": {
      const items = (node.expressions ?? []).map((e) => typeOf(e, env))
      const [first] = items
      const alike =
        first && isScalar(first) && items.every((i) => i.kind === first.kind)
      return t.array(alike ? ({ kind: first.kind } as DataType) : t.unknown())
    }
    case "{": {
      const properties: Record<string, DataType> = {}
      let open = false
      for (const [key, value] of (node.lhs as JsonataNode[][] | undefined) ??
        []) {
        const type = typeOf(value, env)
        if (key.type === "string") properties[String(key.value)] = type
        else {
          typeOf(key, env)
          open = true
        }
      }
      return t.object(properties, undefined, { open })
    }
    default:
      visit(node, env)
      return t.unknown()
  }
}

function callType(node: JsonataNode, env: Env): DataType {
  for (const argument of node.arguments ?? []) typeOf(argument, env)
  const procedure = node.procedure
  if (procedure?.type !== "variable") {
    typeOf(procedure, env)
    return t.unknown()
  }
  const name = String(procedure.value)
  if (env.vars.has(name)) return t.unknown()
  const fn = JSONATA_FUNCTIONS[name]
  if (!fn) {
    env.diagnostics.push({
      ...variableRange(procedure, env),
      severity: "error",
      message: `There's no function $${name}.${suggest(name, Object.keys(JSONATA_FUNCTIONS)).replace("mean ", "mean $")}`,
    })
    return t.unknown()
  }
  return fn.returns()
}

function variableType(node: JsonataNode, env: Env): DataType {
  const name = String(node.value)
  if (name === "") return env.context
  if (name === "$") return env.roots
  return env.vars.get(name) ?? t.unknown()
}

/** The type of a node, noting what's wrong in it on the way. */
function typeOf(node: JsonataNode | undefined, env: Env): DataType {
  if (!node) return t.unknown()
  switch (node.type) {
    case "string":
      return t.string()
    case "number":
      return t.number(undefined, Number.isInteger(node.value))
    case "value":
      return node.value === null
        ? t.null()
        : typeof node.value === "boolean"
          ? t.boolean()
          : t.unknown()
    case "path":
      return pathType(node, env)
    case "name":
      return pathType({ type: "path", steps: [node] }, env)
    case "variable":
      return variableType(node, env)
    case "binary":
      return binaryType(node, env)
    case "unary":
      return unaryType(node, env)
    case "function":
    case "partial":
      return callType(node, env)
    case "condition": {
      typeOf(node.condition, env)
      const then = typeOf(node.then, env)
      const otherwise = node.else ? typeOf(node.else, env) : t.unknown()
      return then.kind === otherwise.kind && isScalar(then) ? then : t.unknown()
    }
    case "block": {
      const inner = { ...env, vars: new Map(env.vars) }
      let last = t.unknown()
      for (const expression of node.expressions ?? [])
        last = typeOf(expression, inner)
      return last
    }
    case "bind": {
      const value = typeOf(node.rhs, env)
      const name = (node.lhs as JsonataNode | undefined)?.value
      if (typeof name === "string") env.vars.set(name, value)
      return value
    }
    case "lambda": {
      const inner = { ...env, vars: new Map(env.vars) }
      for (const argument of node.arguments ?? []) {
        if (typeof argument.value === "string")
          inner.vars.set(argument.value, t.unknown())
      }
      typeOf(node.body, inner)
      return t.unknown()
    }
    default:
      visit(node, env)
      return t.unknown()
  }
}

/** Check what's inside a node the typing doesn't follow. */
function visit(node: JsonataNode, env: Env) {
  const children: (JsonataNode | undefined)[] = [
    node.expr,
    node.expression,
    node.rhs,
    node.condition,
    node.then,
    node.else,
    node.body,
    node.pattern,
    node.update,
    node.delete,
    ...(node.expressions ?? []),
    ...(node.arguments ?? []),
    ...(node.steps ?? []),
    ...(node.terms ?? []).map((term) => term.expression),
  ]
  if (Array.isArray(node.lhs))
    for (const pair of node.lhs) children.push(...pair)
  else children.push(node.lhs)
  for (const child of children) if (child) typeOf(child, env)
}

/* -------------------------------------------------------------------------- */
/* Checking                                                                   */
/* -------------------------------------------------------------------------- */

const FRIENDLY: Record<string, string> = {
  S0101: "This quote isn't closed.",
  S0102: "This number is too large.",
  S0105: "This `name` isn't closed with a backtick.",
  S0106: "This comment isn't closed with */.",
  S0203: "Something should come before this.",
  S0207: "It ends too soon: something should follow.",
  S0208: "This is a word JSONata keeps for itself.",
  S0211: "This can't start an expression.",
}

function syntaxDiagnostic(
  error: JsonataError,
  text: string,
  offset: number
): Diagnostic {
  const at = Math.min(Math.max(error.position, 0), text.length)
  const token = error.token && error.token !== "(end)" ? error.token : ""
  const from = Math.max(0, token ? at - token.length : at - 1)
  const said = error.message.replace(/\.$/, "")
  return {
    from: offset + from,
    to: offset + Math.max(at, from + 1),
    severity: "error",
    message:
      (error.code && FRIENDLY[error.code]) ??
      `This isn't valid JSONata: ${said}.`,
  }
}

type Checked = {
  diagnostics: Diagnostic[]
  type: DataType | undefined
  hovers: Hover[]
  ast?: JsonataNode
}

function checkExpression(text: string, offset: number, scope: Scope): Checked {
  const parsed = parseJsonata(text)
  if (parsed.error) {
    return {
      diagnostics: [syntaxDiagnostic(parsed.error, text, offset)],
      type: undefined,
      hovers: [],
    }
  }
  const env = envFor(scope, text, offset)
  const type = typeOf(parsed.ast, env)
  return {
    diagnostics: env.diagnostics,
    type: env.diagnostics.length ? undefined : type,
    hovers: env.hovers,
    ast: parsed.ast,
  }
}

/**
 * The type of a whole expression when it can be told: a path, a literal,
 * a comparison, an object built of those. Anything else is `unknown`.
 */
export function resolveExpression(
  text: string,
  scope: Scope
): DataType | undefined {
  if (!text.trim()) return undefined
  const parsed = parseJsonata(text)
  if (parsed.error) return t.unknown()
  return typeOf(parsed.ast, envFor(scope, text, 0))
}

/** The `{{ … }}` parts of a template, and any `{{` left open. */
export function templateParts(text: string) {
  const parts: {
    from: number
    to: number
    inner: string
    innerFrom: number
  }[] = []
  const open: number[] = []
  let at = 0
  for (;;) {
    const start = text.indexOf("{{", at)
    if (start === -1) break
    const end = text.indexOf("}}", start + 2)
    if (end === -1) {
      open.push(start)
      break
    }
    parts.push({
      from: start,
      to: end + 2,
      inner: text.slice(start + 2, end),
      innerFrom: start + 2,
    })
    at = end + 2
  }
  return { parts, open }
}

function checkTemplate(
  text: string,
  scope: Scope,
  options: FieldCheck
): Diagnostic[] {
  const out: Diagnostic[] = []
  const { parts, open } = templateParts(text)
  for (const start of open) {
    out.push({
      from: start,
      to: start + 2,
      severity: "error",
      message: "Close this with }}.",
    })
  }
  for (const part of parts) {
    if (!part.inner.trim()) {
      const first = scope.roots.has("previous") ? "previous" : "input"
      out.push({
        from: part.from,
        to: part.to,
        severity: "warning",
        message: `Nothing goes in this {{ }} yet: name what does, like {{ ${first} }}.`,
        local: true,
      })
      continue
    }
    const { diagnostics, type } = checkExpression(
      part.inner,
      part.innerFrom,
      scope
    )
    out.push(...diagnostics)
    if (options.textual && type && !isScalar(type)) {
      out.push({
        from: part.from,
        to: part.to,
        severity: "warning",
        message: `This is ${type.kind === "array" ? `a list (${typeLabel(type)})` : "an object"}: it's written out as JSON. Pick one of its fields to write text.`,
      })
    }
  }
  return out
}

/** What's wrong with a field's text, with where. */
export function checkField(
  text: string,
  mode: FieldMode,
  scope: Scope,
  options: FieldCheck = {}
): Diagnostic[] {
  switch (mode) {
    case "template":
      return checkTemplate(text, scope, options)
    case "expression": {
      if (!text.trim()) return []
      const { diagnostics, type } = checkExpression(text, 0, scope)
      if (
        type &&
        options.expect === "list" &&
        type.kind !== "array" &&
        type.kind !== "unknown"
      ) {
        diagnostics.push({
          from: 0,
          to: text.length,
          severity: "error",
          message: `A loop goes through a list; this is ${typeLabel(type)}.`,
        })
      }
      if (
        type &&
        options.expect === "value" &&
        (type.kind === "object" || type.kind === "array")
      ) {
        diagnostics.push({
          from: 0,
          to: text.length,
          severity: "warning",
          message: `This is ${typeLabel(type)}; a case can only equal text, a number or true/false.`,
        })
      }
      return diagnostics
    }
  }
}

/* -------------------------------------------------------------------------- */
/* Completing                                                                 */
/* -------------------------------------------------------------------------- */

// A JSONata name, plain or `backticked`.
const NAME = String.raw`(?:[A-Za-z_][\w]*|\x60[^\x60]*\x60)`
// A path: names, an index (`[0]`), or a filter already written, as `[*]`.
const PATH = String.raw`${NAME}(?:\.${NAME}|\[\d+\]|\[\*\])*`

/** The parts of a path as written: `a.b[0].c` → a, b, [0], c. */
const partsOf = (path: string) =>
  [...path.matchAll(/`([^`]*)`|([A-Za-z_]\w*)|(\[\d+\]|\[\*\])/g)].map(
    (m) => m[1] ?? m[2] ?? m[3]
  )

/** The text with each finished filter (`[score > 0.5]`) as `[*]`, for reading paths. */
function withoutFilters(text: string) {
  let before = ""
  let after = text
  while (before !== after) {
    before = after
    after = after.replace(/\[(?!\s*-?\d+\s*\])[^[\]]*\]/g, "[*]")
  }
  return after
}

type Mark = {
  kind: "quote" | "backtick" | "comment" | "open"
  char: string
  at: number
}

/**
 * What's open at `pos`: a string, a backticked name, a comment, and the
 * brackets not yet closed, innermost last.
 */
function openAt(text: string, pos: number) {
  const brackets: Mark[] = []
  let inside: Mark | undefined
  for (let i = 0; i < pos; i++) {
    const c = text[i]
    if (inside) {
      if (inside.kind === "comment") {
        if (c === "*" && text[i + 1] === "/" && i + 1 < pos) {
          inside = undefined
          i++
        }
      } else if (c === "\\" && inside.kind === "quote") i++
      else if (c === inside.char) inside = undefined
      continue
    }
    if (c === '"' || c === "'") inside = { kind: "quote", char: c, at: i }
    else if (c === "`") inside = { kind: "backtick", char: c, at: i }
    else if (c === "/" && text[i + 1] === "*")
      inside = { kind: "comment", char: "*/", at: i }
    else if (c === "[" || c === "(" || c === "{")
      brackets.push({ kind: "open", char: c, at: i })
    else if (c === "]" || c === ")" || c === "}") brackets.pop()
  }
  return { inside, brackets }
}

/**
 * What a path typed at `pos` starts from: the roots, or, inside a filter
 * (`items[price > …`), the item of the list before it.
 */
function baseAt(
  text: string,
  pos: number,
  scope: Scope
): { type: DataType; top: boolean } {
  const { brackets } = openAt(text, pos)
  for (let k = brackets.length - 1; k >= 0; k--) {
    const mark = brackets[k]
    if (mark.char !== "[") continue
    const subject = new RegExp(`(${PATH})$`).exec(text.slice(0, mark.at))
    if (!subject) continue
    const inner = text.slice(mark.at + 1, pos)
    // `[2]` picks an item; anything else is a filter over the items.
    if (/^\s*-?\d+\s*$/.test(inner)) continue
    const outer = baseAt(text, mark.at, scope)
    const list = typeOfParts(partsOf(subject[1]), outer, scope)
    return {
      type: list?.kind === "array" ? list.items : (list ?? t.unknown()),
      top: false,
    }
  }
  return { type: rootsOf(scope), top: true }
}

/** The type at the end of a written path, from where it starts. */
function typeOfParts(
  parts: string[],
  base: { type: DataType; top: boolean },
  scope: Scope
): DataType | undefined {
  let type: DataType | undefined = base.top
    ? scope.roots.get(parts[0])?.type
    : step(base.type, parts[0])
  for (const part of parts.slice(1)) {
    if (!type) return undefined
    type = step(type, part)
  }
  return type

  function step(from: DataType, part: string): DataType | undefined {
    // A filter keeps the list; an index picks an item.
    if (part === "[*]") return from
    if (part.startsWith("[")) return from.kind === "array" ? from.items : from
    if (from.kind === "object")
      return from.properties[part] ?? (from.open ? t.unknown() : undefined)
    if (from.kind === "array") {
      const each = step(from.items, part)
      return each && t.array(each.kind === "array" ? each.items : each)
    }
    return from.kind === "unknown" ? from : undefined
  }
}

/** The path being typed at `pos`: the parts before its last one, and that last one so far. */
function pathBefore(text: string, pos: number) {
  const before = withoutFilters(text.slice(0, pos))
  const member = new RegExp(`(${PATH})(\\.)(\\w*)$`).exec(before)
  if (member) {
    return {
      partial: member[3],
      from: pos - member[3].length,
      parts: partsOf(member[1]),
      isProperty: true,
      dotLength: 1,
    }
  }
  const name = /(^|[^\w$.`])([A-Za-z_]\w*|)$/.exec(before)
  if (!name) return undefined
  return {
    partial: name[2],
    from: pos - name[2].length,
    parts: [],
    isProperty: false,
    dotLength: 0,
  }
}

function propertyOptions(type: DataType): Completion[] {
  switch (type.kind) {
    case "object":
      return Object.entries(type.properties).map(([name, field]) => ({
        label: /^[A-Za-z_]\w*$/.test(name) ? name : `\`${name}\``,
        detail: typeLabel(field),
        info: field.description,
        kind: "property",
        boost: type.required?.includes(name) ? 1 : 0,
      }))
    case "array":
      return [
        {
          label: "[0]",
          detail: typeLabel(type.items),
          info: "Its first item.",
          kind: "property",
          boost: 2,
        },
        // A field of a list is that field of every item.
        ...propertyOptions(type.items).map((option) => ({
          ...option,
          detail: option.detail && `list of ${option.detail}`,
          info: option.info
            ? `${option.info} Of every item.`
            : "Of every item.",
        })),
      ]
    default:
      return []
  }
}

function rootOptions(scope: Scope): Completion[] {
  return [...scope.roots.values()].map((root) => ({
    label: root.name,
    detail: typeLabel(root.type),
    note: root.detail,
    info:
      root.type.description && root.type.description !== root.detail
        ? root.type.description
        : undefined,
    kind: "variable",
    boost: root.name === "previous" ? 3 : root.name === "steps" ? 2 : 1,
  }))
}

function stepOptions(scope: Scope): Completion[] {
  return [...scope.steps.values()].map((step) => ({
    label: step.id,
    detail: typeLabel(step.output),
    note: step.name,
    info: step.output.description ?? `${step.mark.label}: what it handed on.`,
    kind: "property",
    step: step.mark,
  }))
}

function functionOptions(): Completion[] {
  return Object.entries(JSONATA_FUNCTIONS).map(([name, fn]) => ({
    label: `$${name}`,
    apply: `$${name}(`,
    detail: typeLabel(fn.returns()),
    note: fn.signature.slice(name.length + 1),
    info: fn.description,
    kind: "method",
  }))
}

/** Inside a string after `path =` or `!=`: the values it could equal. */
function valueOptions(
  text: string,
  pos: number,
  scope: Scope,
  quoteAt: number
): CompletionResult | null {
  const before = text.slice(0, quoteAt)
  const match = new RegExp(
    `(${PATH})\\s*\\)?\\s*(?:!=|=|in\\s*\\[(?:[^\\]]*,)?)\\s*$`
  ).exec(before)
  if (!match) return null
  const base = baseAt(text, match.index, scope)
  const type = typeOfParts(partsOf(match[1]), base, scope)
  if (type?.kind !== "string" || !type.enum?.length) return null
  const quote = text[quoteAt]
  return {
    from: quoteAt + 1,
    to: pos,
    options: type.enum.map((value) =>
      text[pos] === quote
        ? { label: value, kind: "value" as const, skipAfter: 1 }
        : { label: value, apply: `${value}${quote}`, kind: "value" as const }
    ),
  }
}

function expressionCompletions(
  text: string,
  pos: number,
  scope: Scope
): CompletionResult | null {
  const { inside } = openAt(text, pos)
  if (inside?.kind === "quote") return valueOptions(text, pos, scope, inside.at)
  if (inside) return null
  const fn = /\$(\w*)$/.exec(text.slice(0, pos))
  if (fn)
    return { from: pos - fn[0].length, to: pos, options: functionOptions() }
  const path = pathBefore(text, pos)
  if (!path) return null
  const base = baseAt(text, path.from, scope)
  if (!path.isProperty) {
    // A new name: the roots, or the item's fields inside a filter.
    return {
      from: path.from,
      to: pos,
      options: base.top ? rootOptions(scope) : propertyOptions(base.type),
    }
  }
  if (base.top && path.parts.length === 1 && path.parts[0] === "steps") {
    return { from: path.from, to: pos, options: stepOptions(scope) }
  }
  if (base.top && path.parts.length === 2 && path.parts[0] === "steps") {
    const step = scope.steps.get(path.parts[1])
    if (!step) return null
    return {
      from: path.from,
      to: pos,
      options: [
        {
          label: "output",
          detail: typeLabel(step.output),
          info: step.output.description ?? "What it handed on.",
          kind: "property",
          boost: 1,
        },
        ...(step.error
          ? [
              {
                label: "error",
                detail: "object",
                info: "Why it took its error way.",
                kind: "property" as const,
              },
            ]
          : []),
      ],
    }
  }
  const type = typeOfParts(path.parts, base, scope)
  if (!type) return null
  // `[0]` replaces the dot it's typed after.
  return {
    from: path.from,
    to: pos,
    options: propertyOptions(type).map((o) =>
      o.label === "[0]"
        ? { ...o, apply: "[0]", replaceBefore: path.dotLength }
        : o
    ),
  }
}

/** What could go at `pos`, or null for nothing to offer. */
export function completeField(
  text: string,
  pos: number,
  mode: FieldMode,
  scope: Scope
): CompletionResult | null {
  if (mode !== "template") text = normalizeReferences(text, false)
  if (mode === "expression") return expressionCompletions(text, pos, scope)
  // Templates: only inside an open {{.
  const open = text.lastIndexOf("{{", pos - 1)
  const close = text.lastIndexOf("}}", pos - 1)
  if (open === -1 || open < close) return null
  const inner = text.slice(open + 2, pos)
  const result = expressionCompletions(inner, inner.length, scope)
  return (
    result && {
      ...result,
      from: result.from + open + 2,
      to: result.to + open + 2,
    }
  )
}

/* -------------------------------------------------------------------------- */
/* Hovering                                                                   */
/* -------------------------------------------------------------------------- */

/** The name under `pos`, with its type, for a tooltip. */
export function typeAt(
  text: string,
  pos: number,
  mode: FieldMode,
  scope: Scope
): { from: number; to: number; label: string; description?: string } | null {
  const regions =
    mode === "template"
      ? templateParts(text).parts
      : [{ inner: text, innerFrom: 0 }]
  for (const region of regions) {
    const { hovers } = checkExpression(region.inner, region.innerFrom, scope)
    const hit = hovers
      .filter((h) => pos >= h.from && pos <= h.to)
      .sort((a, b) => a.to - a.from - (b.to - b.from))[0]
    if (!hit) continue
    return {
      from: hit.from,
      to: hit.to,
      label: hit.step
        ? `${hit.step.name} · output ${typeLabel(hit.step.output)}`
        : typeLabel(hit.type),
      description: hit.step ? undefined : hit.type.description,
    }
  }
  return null
}
