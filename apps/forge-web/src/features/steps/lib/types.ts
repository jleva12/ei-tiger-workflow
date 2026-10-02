/*
 * The shapes of the data a workflow moves around: what the run's input
 * holds, what each step hands on, what a loop's item is. They come from
 * how the workflow is configured (the input its start declares, an
 * operation's declared output, the JSON an agent is told to return) and
 * let the builder complete and check the expressions that read them.
 * `unknown` is anything: nothing below it is checked.
 */

type Described = { description?: string }

export type DataType =
  | ({ kind: "string"; enum?: string[]; format?: string } & Described)
  | ({ kind: "number"; integer?: boolean } & Described)
  | ({ kind: "boolean" } & Described)
  | ({ kind: "null" } & Described)
  | ({
      kind: "object"
      properties: Record<string, DataType>
      required?: string[]
      /** It may hold fields beyond `properties`, of any type. */
      open: boolean
    } & Described)
  | ({ kind: "array"; items: DataType } & Described)
  | ({ kind: "unknown" } & Described)

/* -------------------------------------------------------------------------- */
/* Building                                                                   */
/* -------------------------------------------------------------------------- */

export const t = {
  string: (
    description?: string,
    extra: { enum?: string[]; format?: string } = {}
  ): DataType => ({
    kind: "string",
    description,
    ...extra,
  }),
  number: (description?: string, integer = false): DataType => ({
    kind: "number",
    description,
    integer,
  }),
  boolean: (description?: string): DataType => ({
    kind: "boolean",
    description,
  }),
  null: (description?: string): DataType => ({ kind: "null", description }),
  unknown: (description?: string): DataType => ({
    kind: "unknown",
    description,
  }),
  array: (items: DataType, description?: string): DataType => ({
    kind: "array",
    items,
    description,
  }),
  object: (
    properties: Record<string, DataType>,
    description?: string,
    { open = false, required }: { open?: boolean; required?: string[] } = {}
  ): DataType => ({ kind: "object", properties, description, open, required }),
}

export const withDescription = (
  type: DataType,
  description: string | undefined
): DataType => (description ? { ...type, description } : type)

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

/**
 * A JSON Schema as a data type: its types, properties, required fields,
 * items and allowed values. What it can't say precisely (a union of
 * unlike types, a reference) becomes `unknown`, so it's never wrongly
 * refused. Objects without `additionalProperties: false` stay open.
 */
export function fromJsonSchema(schema: unknown, depth = 0): DataType {
  if (!isRecord(schema) || depth > 12) return t.unknown()
  const description =
    typeof schema.description === "string" ? schema.description : undefined
  if ("const" in schema) {
    const value = schema.const
    if (typeof value === "string")
      return t.string(description, { enum: [value] })
    if (typeof value === "number") return t.number(description)
    if (typeof value === "boolean") return t.boolean(description)
  }
  if (Array.isArray(schema.enum)) {
    const values = schema.enum.filter((v): v is string => typeof v === "string")
    if (values.length && values.length === schema.enum.length) {
      return t.string(description, { enum: values })
    }
  }
  const variants = (schema.anyOf ?? schema.oneOf) as unknown
  if (Array.isArray(variants)) {
    const types = variants
      .map((v) => fromJsonSchema(v, depth + 1))
      .filter((v) => v.kind !== "null")
    if (types.length === 1) return withDescription(types[0], description)
    if (types.length > 1 && types.every((v) => v.kind === "object")) {
      return withDescription(mergeObjects(types), description)
    }
    return t.unknown(description)
  }
  let type = schema.type
  if (Array.isArray(type)) {
    const concrete = type.filter((v) => v !== "null")
    type = concrete.length === 1 ? concrete[0] : undefined
  }
  switch (type) {
    case "string":
      return t.string(description, {
        format: typeof schema.format === "string" ? schema.format : undefined,
      })
    case "integer":
      return t.number(description, true)
    case "number":
      return t.number(description)
    case "boolean":
      return t.boolean(description)
    case "null":
      return { kind: "null", description }
    case "array":
      return t.array(fromJsonSchema(schema.items, depth + 1), description)
    case "object":
    case undefined: {
      if (type === undefined && !isRecord(schema.properties))
        return t.unknown(description)
      const properties: Record<string, DataType> = {}
      if (isRecord(schema.properties)) {
        for (const [name, property] of Object.entries(schema.properties)) {
          properties[name] = fromJsonSchema(property, depth + 1)
        }
      }
      return t.object(properties, description, {
        open: schema.additionalProperties !== false,
        required: Array.isArray(schema.required)
          ? schema.required.filter((r): r is string => typeof r === "string")
          : undefined,
      })
    }
    default:
      return t.unknown(description)
  }
}

/**
 * Objects that may each be the one: every field of any of them, typed
 * where they agree and `unknown` where they don't.
 */
export function mergeObjects(types: DataType[]): DataType {
  const properties: Record<string, DataType> = {}
  let open = false
  for (const type of types) {
    if (type.kind !== "object") return t.unknown()
    open ||= type.open
    for (const [name, property] of Object.entries(type.properties)) {
      const seen = properties[name]
      properties[name] =
        !seen || sameKind(seen, property) ? (seen ?? property) : t.unknown()
    }
  }
  // A field only some of them have may be missing: never required.
  return t.object(properties, undefined, { open })
}

const sameKind = (a: DataType, b: DataType) => a.kind === b.kind

/* -------------------------------------------------------------------------- */
/* Reading                                                                    */
/* -------------------------------------------------------------------------- */

/** How a type reads in a completion or a message: `string`, `"bug" | "task"`, `array<object>`. */
export function typeLabel(type: DataType, depth = 0): string {
  switch (type.kind) {
    case "string":
      if (type.enum?.length) {
        const shown = type.enum.slice(0, 3).map((v) => JSON.stringify(v))
        return type.enum.length > 3
          ? `${shown.join(" | ")} | …`
          : shown.join(" | ")
      }
      return type.format === "date-time" ? "date-time" : "string"
    case "number":
      return type.integer ? "integer" : "number"
    case "array":
      return depth > 1 ? "array" : `array<${typeLabel(type.items, depth + 1)}>`
    case "unknown":
      return "any"
    default:
      return type.kind
  }
}

/**
 * The only values a value of this type can have, as text (as a Switch
 * compares them): a string's allowed values, or true and false. Undefined
 * when any value could come.
 */
export function valuesOf(type: DataType | undefined): string[] | undefined {
  if (type?.kind === "string" && type.enum?.length) return type.enum
  if (type?.kind === "boolean") return ["true", "false"]
  return undefined
}

/** Values in a sentence: `BUG`, `BUG or TASK`, `BUG, TASK or IDEA`. */
export function oneOf(values: string[]): string {
  if (values.length < 2) return values.join("")
  return `${values.slice(0, -1).join(", ")} or ${values[values.length - 1]}`
}

/** The fields an object has, for "it has: …" in a message. */
export const fieldNames = (type: DataType) =>
  type.kind === "object" ? Object.keys(type.properties) : []

/** Whether a value of this type can stand where text goes without looking like `[object Object]`. */
export const isScalar = (type: DataType) =>
  type.kind === "string" ||
  type.kind === "number" ||
  type.kind === "boolean" ||
  type.kind === "null" ||
  type.kind === "unknown"

/** Whether a value of `type` fits where a `wanted` one goes (unknown on either side fits). */
export function fits(type: DataType, wanted: DataType): boolean {
  if (type.kind === "unknown" || wanted.kind === "unknown") return true
  if (
    wanted.kind === "string" &&
    wanted.enum?.length &&
    type.kind === "string" &&
    type.enum
  ) {
    return type.enum.every((v) => wanted.enum!.includes(v))
  }
  if (type.kind !== wanted.kind) return false
  if (type.kind === "number" && wanted.kind === "number" && wanted.integer) {
    return type.integer === true
  }
  return true
}
