/*
 * An HTTP tool's URL and body as templates, read as the runtime reads them
 * (forge_agent_runtime.http_templates):
 *
 * - `{name}`: the model's argument of that name. The tool's parameters are
 *   the ones its URL and body name; one named nowhere isn't offered or sent.
 * - `{{ expression }}`: one of the run's values (a workflow's data, a chat's
 *   request and state), filled in as the tool's called.
 * - `${NAME}`: a secret or setting from the environment.
 *
 * A URL's query parameter that is one argument (`?status={status}`) is left
 * out when the model doesn't send it; any other argument the URL names must
 * be sent. Listed query parameters are templates too, name and value, filled
 * in and then percent-encoded whole: one whose value is one placeholder is
 * left out when it comes to nothing. The body is JSON: a placeholder that's
 * a whole value (`{id}` or `"{id}"`) goes in as it is, a number staying a
 * number; one in a string is written into it.
 */

/** A placeholder: `${NAME}`, `{{ expression }}` or `{name}`, in that order at one place. */
const PLACEHOLDER =
  /\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\{\{([\s\S]*?)\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}/g
/** An argument's name, as `{name}` holds it. */
export const ARGUMENT_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/

/** The methods that send a body. */
export const takesBody = (method: string) =>
  method === "POST" || method === "PUT" || method === "PATCH"

type Part = string | { argument: string } | { value: string }

/** A text as literal strings and placeholders, a setting's `${NAME}` kept as text. */
function partsOf(text: string): Part[] {
  const parts: Part[] = []
  const literal = (piece: string) => {
    if (!piece) return
    const last = parts[parts.length - 1]
    if (typeof last === "string") parts[parts.length - 1] = last + piece
    else parts.push(piece)
  }
  let at = 0
  for (const match of text.matchAll(PLACEHOLDER)) {
    literal(text.slice(at, match.index))
    if (match[2] !== undefined) parts.push({ value: match[2] })
    else if (match[3] !== undefined) parts.push({ argument: match[3] })
    else literal(match[0])
    at = match.index + match[0].length
  }
  literal(text.slice(at))
  return parts
}

const argumentsIn = (parts: Part[]) =>
  parts.flatMap((part) =>
    typeof part === "object" && "argument" in part ? [part.argument] : []
  )

/** The argument a query pair is (`name={name}`); undefined for any other pair. */
function droppable(pair: Part[]): string | undefined {
  const [first, second] = pair
  return pair.length === 2 &&
    typeof first === "string" &&
    first.endsWith("=") &&
    typeof second === "object" &&
    "argument" in second
    ? second.argument
    : undefined
}

/** The arguments a URL names, in order, and those it can't do without: all but a query pair's own. */
export function urlArguments(url: string): {
  names: string[]
  required: Set<string>
} {
  const path: Part[] = []
  let query: Part[][] | undefined
  for (let part of partsOf(url)) {
    if (!query) {
      if (typeof part === "string" && part.includes("?")) {
        const cut = part.indexOf("?")
        path.push(part.slice(0, cut))
        query = [[]]
        part = part.slice(cut + 1)
      } else {
        path.push(part)
        continue
      }
    }
    if (typeof part === "string") {
      const pieces = part.split("&")
      if (pieces[0]) query[query.length - 1].push(pieces[0])
      for (const piece of pieces.slice(1)) query.push(piece ? [piece] : [])
    } else query[query.length - 1].push(part)
  }
  const names: string[] = []
  const required = new Set<string>()
  for (const pair of [path, ...(query ?? [])]) {
    const optional = droppable(pair)
    for (const name of argumentsIn(pair)) {
      if (!names.includes(name)) names.push(name)
      if (name !== optional) required.add(name)
    }
  }
  return { names, required }
}

/**
 * Why a URL can't say where an HTTP tool calls, as the runtime refuses it
 * (`UrlTemplate.check_origin`); undefined when it can. Its scheme and host
 * are its author's to write out: the model's arguments never pick them, and
 * a run's value may only for a tool sending no headers of its own, which go
 * only to a host written in its URL.
 */
export function originProblem(
  url: string,
  sendsHeaders: boolean
): string | undefined {
  const path: Part[] = []
  for (const part of partsOf(url)) {
    if (typeof part === "string" && part.includes("?")) {
      path.push(part.slice(0, part.indexOf("?")))
      break
    }
    path.push(part)
  }
  const lead = typeof path[0] === "string" ? path[0] : ""
  const cut = lead.indexOf("://")
  const rest = cut > 0 ? lead.slice(cut + 3) : undefined
  if (
    path.every((part) => typeof part === "string") ||
    (rest !== undefined && /[/?#\\]/.test(rest))
  )
    return undefined
  const first = path.find((part) => typeof part !== "string")
  if (first && typeof first === "object" && "argument" in first)
    return `{${first.argument}} would let the model pick where it calls: write the host out, and put arguments in the path or query.`
  return sendsHeaders
    ? "Its host comes from a run's value, and its headers go only to a host written in its URL: write the host out, or take the headers away."
    : undefined
}

/** A listed query parameter: its name and its value, templates. */
type QueryRow = { name: string; value: string }

/** What a listed parameter's value is when it's one placeholder and nothing else (but spaces). */
function wholeValue(parts: Part[]): Part | undefined {
  const placeholders = parts.filter((part) => typeof part === "object")
  const literal = parts.filter((part) => typeof part === "string").join("")
  return placeholders.length === 1 && !literal.trim()
    ? placeholders[0]
    : undefined
}

/**
 * The arguments listed query parameters name, in order, and those they
 * can't do without: all but a value that's one argument. One without a
 * name isn't sent.
 */
export function queryArguments(rows: readonly QueryRow[]): {
  names: string[]
  required: Set<string>
} {
  const names: string[] = []
  const required = new Set<string>()
  for (const row of rows) {
    if (!row.name.trim()) continue
    const value = partsOf(row.value)
    const whole = wholeValue(value)
    for (const part of [...partsOf(row.name.trim()), ...value]) {
      if (typeof part !== "object" || !("argument" in part)) continue
      if (!names.includes(part.argument)) names.push(part.argument)
      if (part !== whole) required.add(part.argument)
    }
  }
  return { names, required }
}

/** The JSON text with each placeholder outside its strings quoted: a whole value. */
function quoted(text: string): string {
  let out = ""
  let inString = false
  for (let at = 0; at < text.length;) {
    const char = text[at]
    if (inString) {
      out += char
      if (char === "\\" && at + 1 < text.length) {
        out += text[at + 1]
        at += 2
        continue
      }
      if (char === '"') inString = false
      at += 1
      continue
    }
    if (char === '"') {
      inString = true
      out += char
      at += 1
      continue
    }
    if (char === "{" || char === "$") {
      PLACEHOLDER.lastIndex = at
      const match = PLACEHOLDER.exec(text)
      if (match && match.index === at) {
        out += JSON.stringify(match[0])
        at += match[0].length
        continue
      }
    }
    out += char
    at += 1
  }
  PLACEHOLDER.lastIndex = 0
  return out
}

/** The body as JSON, its placeholders still in its strings; an error's message when it isn't JSON. */
export function parseBody(
  body: string
): { ok: true; value: unknown } | { ok: false; error: string } {
  try {
    return { ok: true, value: JSON.parse(quoted(body)) }
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    }
  }
}

/** The arguments a body names, in order: from its strings when it's JSON, else anywhere in it. */
export function bodyArguments(body: string): string[] {
  if (!body.trim()) return []
  const parsed = parseBody(body)
  if (!parsed.ok) return [...new Set(argumentsIn(partsOf(body)))]
  const names: string[] = []
  const walk = (value: unknown) => {
    if (typeof value === "string") {
      for (const name of argumentsIn(partsOf(value)))
        if (!names.includes(name)) names.push(name)
    } else if (Array.isArray(value)) value.forEach(walk)
    else if (value && typeof value === "object")
      Object.values(value).forEach(walk)
  }
  walk(parsed.value)
  return names
}

type HttpToolConfig = {
  method: string
  url: string
  body: string
  query?: readonly QueryRow[]
}

/**
 * The arguments an HTTP tool's URL, query parameters and body (one it
 * sends) name, in order; those they need; and those only a query
 * parameter's whole value is.
 */
export function toolArguments(config: HttpToolConfig): {
  names: string[]
  required: Set<string>
  optional: Set<string>
} {
  const { names, required } = urlArguments(config.url)
  const query = queryArguments(config.query ?? [])
  for (const name of query.names) if (!names.includes(name)) names.push(name)
  for (const name of query.required) required.add(name)
  const inBody = takesBody(config.method) ? bodyArguments(config.body) : []
  const optional = new Set(
    names.filter((name) => !required.has(name) && !inBody.includes(name))
  )
  for (const name of inBody) if (!names.includes(name)) names.push(name)
  return { names, required, optional }
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

/**
 * The JSON Schema the model calls the tool with: the arguments its URL and
 * body name, each as `parameters` declares it (a string otherwise);
 * required when the URL needs it, `parameters` says so, or it isn't
 * declared and isn't only a query pair. What `parameters` declares that's
 * named nowhere isn't in it.
 */
export function toolParameters(
  config: HttpToolConfig & { parameters: Record<string, unknown> }
): Record<string, unknown> {
  const { names, required, optional } = toolArguments(config)
  if (!names.length) return {}
  const declared = isRecord(config.parameters.properties)
    ? config.parameters.properties
    : {}
  const said = Array.isArray(config.parameters.required)
    ? config.parameters.required
    : []
  const needed = names.filter(
    (name) =>
      required.has(name) ||
      said.includes(name) ||
      (!(name in declared) && !optional.has(name))
  )
  return {
    type: "object",
    properties: Object.fromEntries(
      names.map((name) => [
        name,
        isRecord(declared[name]) ? declared[name] : { type: "string" },
      ])
    ),
    ...(needed.length ? { required: needed } : {}),
  }
}

/**
 * An HTTP tool's settings from before it had a body (2026-10-10), as they
 * are now. Then the arguments its URL didn't name went in the query (GET,
 * DELETE) or as the JSON body; now only what its URL and body name is
 * sent, so they're written there. One with a body is as it is.
 */
export function upgradedHttpTool(
  raw: Record<string, unknown>
): Record<string, unknown> {
  if ("body" in raw) return raw
  const out: Record<string, unknown> = { ...raw, body: "" }
  const properties = isRecord(raw.parameters)
    ? raw.parameters.properties
    : undefined
  if (!isRecord(properties)) return out
  const url = typeof raw.url === "string" ? raw.url : ""
  const named = new Set(argumentsIn(partsOf(url)))
  const rest = Object.keys(properties).filter(
    (name) => ARGUMENT_NAME.test(name) && !named.has(name)
  )
  if (!rest.length) return out
  const method =
    typeof raw.method === "string" ? raw.method.toUpperCase() : "GET"
  if (takesBody(method)) {
    out.body = `{\n${rest.map((name) => `  ${JSON.stringify(name)}: {${name}}`).join(",\n")}\n}`
  } else {
    const query = rest.map((name) => `${name}={${name}}`).join("&")
    const joint = /[?&]$/.test(url) ? "" : url.includes("?") ? "&" : "?"
    out.url = url + joint + query
  }
  return out
}

/** What `parameters` declares that the URL and body (one it sends) don't name: not offered, not sent. */
export function unusedParameters(
  config: HttpToolConfig & { parameters: Record<string, unknown> }
): string[] {
  const declared = config.parameters.properties
  if (!isRecord(declared)) return []
  const { names } = toolArguments(config)
  return Object.keys(declared).filter((name) => !names.includes(name))
}

/**
 * The arguments as their editor shows them: what the model is offered
 * (`toolParameters`), then what `parameters` declares that's named nowhere,
 * as it's declared, so it can be put in or taken out.
 */
export function editedParameters(
  config: HttpToolConfig & { parameters: Record<string, unknown> }
): Record<string, unknown> {
  const offered = toolParameters(config)
  const unused = unusedParameters(config)
  if (!unused.length) return offered
  const declared = config.parameters.properties as Record<string, unknown>
  const said = Array.isArray(config.parameters.required)
    ? config.parameters.required
    : []
  const required = [
    ...((offered.required as string[] | undefined) ?? []),
    ...unused.filter((name) => said.includes(name)),
  ]
  return {
    type: "object",
    properties: {
      ...((offered.properties as Record<string, unknown> | undefined) ?? {}),
      ...Object.fromEntries(unused.map((name) => [name, declared[name]])),
    },
    ...(required.length ? { required } : {}),
  }
}
