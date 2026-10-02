/**
 * The JSON Schema builder's model: a tree of properties people edit, and
 * the JSON Schema (draft 2020-12) it stands for. `buildJsonSchema` writes
 * the schema, `parseJsonSchema` reads one back (saying what it can't
 * represent), and `inferFromSample` drafts one from an example payload.
 */

export type SchemaType =
  "string" | "number" | "integer" | "boolean" | "array" | "object"

export const SCHEMA_TYPES: { value: SchemaType; label: string }[] = [
  { value: "string", label: "string" },
  { value: "number", label: "number" },
  { value: "integer", label: "integer" },
  { value: "boolean", label: "boolean" },
  { value: "array", label: "array" },
  { value: "object", label: "object" },
]

/** String formats the endpoint checks. `none` stands for no format. */
export const STRING_FORMATS: { value: string; label: string }[] = [
  { value: "none", label: "None" },
  { value: "date", label: "date" },
  { value: "date-time", label: "date-time" },
  { value: "time", label: "time" },
  { value: "email", label: "email" },
  { value: "uri", label: "uri" },
  { value: "uuid", label: "uuid" },
  { value: "ipv4", label: "ipv4" },
  { value: "ipv6", label: "ipv6" },
]

export type ConstValue = string | number | boolean

/**
 * A value's shape: its type and the constraints on it. Each property is
 * one, and so are an array's items.
 */
export interface SchemaNode {
  /** Identifies it in the editor (keys, errors); never in the schema. */
  uid: string
  type: SchemaType
  /** Null is allowed too: `"type": [type, "null"]`. */
  nullable?: boolean
  description?: string
  /** The one value it may have, e.g. the sending system's name. */
  const?: ConstValue
  // Strings.
  minLength?: number
  maxLength?: number
  pattern?: string
  format?: string
  /** Allowed values: strings for a string, numbers for a number. */
  enum?: (string | number)[]
  // Numbers and integers.
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number
  exclusiveMaximum?: number
  multipleOf?: number
  // Arrays.
  minItems?: number
  maxItems?: number
  uniqueItems?: boolean
  items?: SchemaNode
  // Objects.
  properties?: SchemaProperty[]
  /** False refuses properties it doesn't list; absent allows them. */
  additionalProperties?: boolean
}

/** One of an object's properties. */
export interface SchemaProperty extends SchemaNode {
  name: string
  required: boolean
}

/** What the builder edits: the payload's properties. */
export interface SchemaDraft {
  properties: SchemaProperty[]
  /** False refuses properties the schema doesn't list. */
  additionalProperties: boolean
}

type Json = Record<string, unknown>

let counter = 0
export const newUid = () => `n${++counter}`

export const newNode = (type: SchemaType = "string"): SchemaNode => ({
  uid: newUid(),
  type,
  ...(type === "object" && { properties: [] }),
  ...(type === "array" && { items: newNode("string") }),
})

export const newProperty = (name = ""): SchemaProperty => ({
  ...newNode("string"),
  name,
  required: false,
})

export const emptyDraft = (): SchemaDraft => ({
  properties: [],
  additionalProperties: true,
})

/** Scalar types, which can have a constant value. */
export const isScalar = (type: SchemaType) =>
  type !== "array" && type !== "object"

/**
 * The node as another type: what only made sense for the old type (its
 * constant, its allowed values) goes; an object starts with no
 * properties, an array with string items.
 */
export function withType<T extends SchemaNode>(node: T, type: SchemaType): T {
  return {
    ...node,
    type,
    const: undefined,
    enum: undefined,
    properties: type === "object" ? (node.properties ?? []) : node.properties,
    items: type === "array" ? (node.items ?? newNode("string")) : node.items,
  }
}

const present = (value: unknown) =>
  value !== undefined && value !== null && value !== ""

function buildNode(node: SchemaNode): Json {
  const out: Json = { type: node.nullable ? [node.type, "null"] : node.type }
  if (node.description?.trim()) out.description = node.description.trim()
  if (present(node.const) && isScalar(node.type)) {
    // A fixed value says it all.
    out.const = node.const
    return out
  }
  if (node.type === "string") {
    if (present(node.minLength)) out.minLength = node.minLength
    if (present(node.maxLength)) out.maxLength = node.maxLength
    if (node.pattern) out.pattern = node.pattern
    if (node.format && node.format !== "none") out.format = node.format
  }
  if (node.type === "number" || node.type === "integer") {
    if (present(node.minimum)) out.minimum = node.minimum
    if (present(node.maximum)) out.maximum = node.maximum
    if (present(node.exclusiveMinimum))
      out.exclusiveMinimum = node.exclusiveMinimum
    if (present(node.exclusiveMaximum))
      out.exclusiveMaximum = node.exclusiveMaximum
    if (present(node.multipleOf)) out.multipleOf = node.multipleOf
  }
  if (isScalar(node.type) && node.type !== "boolean" && node.enum?.length) {
    // Null must be listed too, or a nullable one could never be null.
    out.enum = node.nullable ? [...node.enum, null] : node.enum
  }
  if (node.type === "array") {
    out.items = buildNode(node.items ?? newNode("string"))
    if (present(node.minItems)) out.minItems = node.minItems
    if (present(node.maxItems)) out.maxItems = node.maxItems
    if (node.uniqueItems) out.uniqueItems = true
  }
  if (node.type === "object") {
    Object.assign(
      out,
      buildObject(node.properties ?? [], node.additionalProperties)
    )
  }
  return out
}

function buildObject(
  properties: SchemaProperty[],
  additionalProperties: boolean | undefined
): Json {
  const out: Json = {
    properties: Object.fromEntries(
      properties.map((property) => [property.name.trim(), buildNode(property)])
    ),
  }
  const required = properties
    .filter((p) => p.required)
    .map((p) => p.name.trim())
  if (required.length) out.required = required
  if (additionalProperties === false) out.additionalProperties = false
  return out
}

/** The JSON Schema a draft stands for: always an object at the root. */
export function buildJsonSchema(draft: SchemaDraft): Json {
  return {
    type: "object",
    ...buildObject(draft.properties, draft.additionalProperties),
  }
}

// What the builder represents; anything else in an imported schema is
// reported and left out.
const NODE_KEYWORDS = new Set([
  "type",
  "description",
  "const",
  "enum",
  "minLength",
  "maxLength",
  "pattern",
  "format",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "exclusiveMaximum",
  "multipleOf",
  "items",
  "minItems",
  "maxItems",
  "uniqueItems",
  "properties",
  "required",
  "additionalProperties",
])
// Left out without a word: they don't change what's accepted.
const QUIET_KEYWORDS = new Set([
  "$schema",
  "$id",
  "title",
  "examples",
  "$comment",
])

const isObject = (value: unknown): value is Json =>
  typeof value === "object" && value !== null && !Array.isArray(value)

const num = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined

function readNode(
  schema: unknown,
  path: string,
  warnings: string[]
): SchemaNode {
  if (!isObject(schema)) {
    warnings.push(`${path}: not a schema object; read as a string`)
    return newNode("string")
  }
  for (const key of Object.keys(schema)) {
    if (!NODE_KEYWORDS.has(key) && !QUIET_KEYWORDS.has(key)) {
      warnings.push(`${path}: "${key}" isn't supported and was left out`)
    }
  }
  let types = Array.isArray(schema.type)
    ? schema.type.filter((t): t is string => typeof t === "string")
    : typeof schema.type === "string"
      ? [schema.type]
      : []
  const nullable = types.includes("null")
  types = types.filter((t) => t !== "null")
  let type: SchemaType
  if (types.length === 0) {
    type = isObject(schema.properties)
      ? "object"
      : schema.items !== undefined
        ? "array"
        : typeof schema.const === "number"
          ? "number"
          : typeof schema.const === "boolean"
            ? "boolean"
            : "string"
    if (!("const" in schema) && !("enum" in schema)) {
      warnings.push(`${path}: no type; read as ${type}`)
    }
  } else {
    type = SCHEMA_TYPES.some((t) => t.value === types[0])
      ? (types[0] as SchemaType)
      : "string"
    if (types.length > 1) {
      warnings.push(
        `${path}: several types (${types.join(", ")}); kept ${type}`
      )
    }
  }
  const node: SchemaNode = { uid: newUid(), type }
  if (nullable) node.nullable = true
  if (typeof schema.description === "string")
    node.description = schema.description
  if ("const" in schema) {
    const value = schema.const
    if (
      typeof value === "string" ||
      typeof value === "boolean" ||
      (typeof value === "number" && Number.isFinite(value))
    ) {
      node.const = value
    } else {
      warnings.push(`${path}: a constant object, array or null was left out`)
    }
  }
  if (Array.isArray(schema.enum)) {
    const values = schema.enum.filter((v) => v !== null)
    if (schema.enum.includes(null)) node.nullable = true
    const kept = values.filter(
      (v): v is string | number =>
        (type === "string" && typeof v === "string") ||
        ((type === "number" || type === "integer") && typeof v === "number")
    )
    if (kept.length < values.length) {
      warnings.push(
        `${path}: allowed values that aren't ${type}s were left out`
      )
    }
    if (kept.length) node.enum = kept
  }
  node.minLength = num(schema.minLength)
  node.maxLength = num(schema.maxLength)
  if (typeof schema.pattern === "string") node.pattern = schema.pattern
  if (typeof schema.format === "string") {
    if (STRING_FORMATS.some((f) => f.value === schema.format)) {
      node.format = schema.format
    } else {
      warnings.push(
        `${path}: format "${schema.format}" isn't offered; left out`
      )
    }
  }
  node.minimum = num(schema.minimum)
  node.maximum = num(schema.maximum)
  node.exclusiveMinimum = num(schema.exclusiveMinimum)
  node.exclusiveMaximum = num(schema.exclusiveMaximum)
  node.multipleOf = num(schema.multipleOf)
  node.minItems = num(schema.minItems)
  node.maxItems = num(schema.maxItems)
  if (schema.uniqueItems === true) node.uniqueItems = true
  if (type === "array") {
    if (Array.isArray(schema.items)) {
      warnings.push(`${path}: tuple items aren't supported; read the first`)
      node.items = readNode(schema.items[0] ?? {}, `${path}[]`, warnings)
    } else if (schema.items === undefined) {
      node.items = newNode("string")
      warnings.push(`${path}: no items; read as strings`)
    } else {
      node.items = readNode(schema.items, `${path}[]`, warnings)
    }
  }
  if (type === "object") {
    const object = readObject(schema, path, warnings)
    node.properties = object.properties
    if (!object.additionalProperties) node.additionalProperties = false
  }
  return node
}

function readObject(
  schema: Json,
  path: string,
  warnings: string[]
): SchemaDraft {
  const required = Array.isArray(schema.required)
    ? schema.required.filter((r): r is string => typeof r === "string")
    : []
  const listed = isObject(schema.properties) ? schema.properties : {}
  const properties = Object.entries(listed).map(
    ([name, value]): SchemaProperty => ({
      ...readNode(value, path === "$" ? name : `${path}.${name}`, warnings),
      name,
      required: required.includes(name),
    })
  )
  for (const name of required) {
    if (!(name in listed)) {
      warnings.push(
        `${path}: required "${name}" isn't a listed property; left out`
      )
    }
  }
  if (isObject(schema.additionalProperties)) {
    warnings.push(
      `${path}: a schema for other properties isn't supported; they're allowed`
    )
  }
  return {
    properties,
    additionalProperties: schema.additionalProperties !== false,
  }
}

/**
 * Read a JSON Schema into the builder. What it can't represent is left
 * out, and said in `warnings`.
 */
export function parseJsonSchema(schema: unknown): {
  draft: SchemaDraft
  warnings: string[]
} {
  const warnings: string[] = []
  if (!isObject(schema)) {
    return {
      draft: emptyDraft(),
      warnings: ["That isn't a JSON Schema object"],
    }
  }
  if (schema.type !== undefined && schema.type !== "object") {
    warnings.push(
      "An event's schema describes an object; read its properties only"
    )
  }
  for (const key of Object.keys(schema)) {
    if (!NODE_KEYWORDS.has(key) && !QUIET_KEYWORDS.has(key)) {
      warnings.push(`"${key}" isn't supported and was left out`)
    }
  }
  return { draft: readObject(schema, "$", warnings), warnings }
}

const DATE_TIME =
  /^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$/
const DATE = /^\d{4}-\d{2}-\d{2}$/
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const URI = /^[a-z][a-z0-9+.-]*:\/\/\S+$/i

function inferNode(value: unknown): SchemaNode {
  if (value === null) return { ...newNode("string"), nullable: true }
  if (typeof value === "boolean") return newNode("boolean")
  if (typeof value === "number") {
    return newNode(Number.isInteger(value) ? "integer" : "number")
  }
  if (typeof value === "string") {
    const format = DATE_TIME.test(value)
      ? "date-time"
      : DATE.test(value)
        ? "date"
        : UUID.test(value)
          ? "uuid"
          : EMAIL.test(value)
            ? "email"
            : URI.test(value)
              ? "uri"
              : undefined
    return { ...newNode("string"), ...(format && { format }) }
  }
  if (Array.isArray(value)) {
    const node = newNode("array")
    const objects = value.filter(isObject)
    node.items =
      value.length === 0
        ? newNode("string")
        : objects.length === value.length
          ? // Every key any element has.
            {
              ...newNode("object"),
              properties: inferProperties(Object.assign({}, ...objects)),
            }
          : inferNode(value.find((v) => v !== null) ?? null)
    return node
  }
  if (isObject(value)) {
    return { ...newNode("object"), properties: inferProperties(value) }
  }
  return newNode("string")
}

function inferProperties(value: Json): SchemaProperty[] {
  return Object.entries(value).map(([name, v]) => ({
    ...inferNode(v),
    name,
    required: false,
  }))
}

/**
 * A draft from an example payload: its properties with the types (and
 * string formats) the example shows. None is required; say which are.
 */
export function inferFromSample(sample: unknown): SchemaDraft {
  if (!isObject(sample)) {
    throw new Error("An example event is a JSON object: { … }")
  }
  return { properties: inferProperties(sample), additionalProperties: true }
}

/** Problems with one node, by field. */
export type NodeIssues = Partial<
  Record<
    "name" | "pattern" | "length" | "range" | "items" | "multipleOf",
    string
  >
>

function validRegex(pattern: string) {
  try {
    new RegExp(pattern, "u")
    return true
  } catch {
    return false
  }
}

const over = (low: number | undefined, high: number | undefined) =>
  low !== undefined && high !== undefined && low > high

function checkNode(node: SchemaNode, issues: Map<string, NodeIssues>) {
  const found: NodeIssues = {}
  if (node.pattern && !validRegex(node.pattern)) {
    found.pattern = "Not a valid regular expression."
  }
  if (over(node.minLength, node.maxLength)) found.length = "Min is over max."
  if (over(node.minimum, node.maximum)) found.range = "Minimum is over maximum."
  if (over(node.minItems, node.maxItems)) found.items = "Min is over max."
  if (node.multipleOf !== undefined && node.multipleOf <= 0) {
    found.multipleOf = "Must be over 0."
  }
  if (Object.keys(found).length) issues.set(node.uid, found)
  if (node.type === "array" && node.items) checkNode(node.items, issues)
  if (node.type === "object") checkProperties(node.properties ?? [], issues)
}

function checkProperties(
  properties: SchemaProperty[],
  issues: Map<string, NodeIssues>
) {
  const seen = new Set<string>()
  for (const property of properties) {
    checkNode(property, issues)
    const name = property.name.trim()
    const problem = !name
      ? "Name it."
      : seen.has(name)
        ? "Another property has this name."
        : undefined
    seen.add(name)
    if (problem) {
      issues.set(property.uid, { ...issues.get(property.uid), name: problem })
    }
  }
}

/** What stops a draft from being saved, by node. Empty when it's fine. */
export function checkDraft(draft: SchemaDraft): Map<string, NodeIssues> {
  const issues = new Map<string, NodeIssues>()
  checkProperties(draft.properties, issues)
  return issues
}

/** How many properties a draft has, at every depth, and how many are required. */
export function countProperties(draft: SchemaDraft) {
  let total = 0
  let required = 0
  const walk = (node: SchemaNode) => {
    if (node.type === "array" && node.items) walk(node.items)
    for (const property of node.properties ?? []) {
      total += 1
      if (property.required) required += 1
      walk(property)
    }
  }
  walk({ uid: "", type: "object", properties: draft.properties })
  return { total, required }
}

const FORMAT_EXAMPLES: Record<string, string> = {
  date: "2026-09-26",
  "date-time": "2026-09-26T08:15:00Z",
  time: "08:15:00Z",
  email: "someone@example.com",
  uri: "https://example.com",
  uuid: "0f8fad5b-d9cb-469f-a165-70867728950e",
  ipv4: "192.0.2.10",
  ipv6: "2001:db8::10",
}

function exampleOf(node: SchemaNode, name = ""): unknown {
  if (node.const !== undefined && isScalar(node.type)) return node.const
  if (node.enum?.length) return node.enum[0]
  switch (node.type) {
    case "string": {
      if (node.format && FORMAT_EXAMPLES[node.format]) {
        return FORMAT_EXAMPLES[node.format]
      }
      const text = name || "text"
      return node.minLength && text.length < node.minLength
        ? text.padEnd(node.minLength, "x")
        : text
    }
    case "integer":
    case "number": {
      // The least it may be, or 0.
      const least =
        node.minimum ??
        (node.exclusiveMinimum !== undefined
          ? Math.floor(node.exclusiveMinimum) + 1
          : 0)
      return node.type === "integer" ? Math.ceil(least) : least
    }
    case "boolean":
      return true
    case "array":
      return Array.from({ length: Math.max(1, node.minItems ?? 1) }, () =>
        exampleOf(node.items ?? newNode("string"), name)
      ).slice(0, node.uniqueItems ? 1 : undefined)
    case "object":
      return exampleObject(node.properties ?? [])
  }
}

function exampleObject(properties: SchemaProperty[]): Json {
  return Object.fromEntries(
    properties.map((property) => [
      property.name,
      exampleOf(property, property.name),
    ])
  )
}

/** An example payload for a draft, to start a test from. */
export const exampleFrom = (draft: SchemaDraft): Json =>
  exampleObject(draft.properties.filter((p) => p.name.trim()))

/** A key from a name: "Incident opened" → "incident_opened". */
export const keyFrom = (name: string) =>
  name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9.]+/g, "_")
    .replace(/^[^a-z]+/, "")
    .replace(/_+$/, "")
    .slice(0, 100)
