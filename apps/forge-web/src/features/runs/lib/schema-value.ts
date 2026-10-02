import * as React from "react"

import { parseJsonSchema, type SchemaProperty } from "@/features/json/lib/json-schema"

/*
 * A value a JSON Schema describes, as a person fills it in: the fields its
 * schema declares (nested objects as groups; lists and free-form objects
 * as JSON), or JSON outright. A run's input (the run dialog) and a
 * person's answer to a paused ADK workflow (its human input panel) are
 * filled in this way; `SchemaValueEditor` draws it.
 */

/** What the fields hold, by path (`address.city`): text, or a checkbox's. */
export type SchemaRaw = Record<string, string | boolean>

/** What's wrong with the fields, by path. */
export type SchemaErrors = Record<string, string>

export type SchemaBuilt = { value: unknown; errors: SchemaErrors }

export const pathOf = (parent: string, name: string) =>
  parent ? `${parent}.${name}` : name

/** An object with properties of its own: a group of fields. */
export const isGroup = (p: SchemaProperty) =>
  p.type === "object" && Boolean(p.properties?.length)

/** A list or a free-form object: a JSON field. */
export const isJson = (p: SchemaProperty) =>
  p.type === "array" || (p.type === "object" && !isGroup(p))

/** The properties that have a name, the ones a form shows. */
export const named = (properties: SchemaProperty[] = []) =>
  properties.filter((p) => p.name.trim())

/** The fields a schema declares; none for `{}` or one that isn't an object's. */
export const schemaProperties = (schema: Record<string, unknown>) =>
  named(parseJsonSchema(schema).draft.properties)

/** The value the fields make, and what's wrong with them, by path. */
export function buildValue(
  properties: SchemaProperty[],
  raw: SchemaRaw,
  parent = ""
): SchemaBuilt {
  const value: Record<string, unknown> = {}
  const errors: SchemaErrors = {}
  for (const property of named(properties)) {
    const path = pathOf(parent, property.name)
    if (isGroup(property)) {
      const inner = buildValue(property.properties ?? [], raw, path)
      const empty = Object.keys(inner.value as object).length === 0
      if (empty && !property.required) continue
      Object.assign(errors, inner.errors)
      value[property.name] = inner.value
      continue
    }
    const entry = raw[path]
    if (property.type === "boolean") {
      if (entry === true || property.required)
        value[property.name] = entry === true
      continue
    }
    const text = typeof entry === "string" ? entry.trim() : ""
    if (!text) {
      if (property.required) errors[path] = "Required"
      continue
    }
    if (property.type === "number" || property.type === "integer") {
      const number = Number(text)
      if (
        !Number.isFinite(number) ||
        (property.type === "integer" && !Number.isInteger(number))
      )
        errors[path] =
          property.type === "integer" ? "A whole number" : "A number"
      else value[property.name] = number
    } else if (isJson(property)) {
      try {
        value[property.name] = JSON.parse(text)
      } catch {
        errors[path] =
          property.type === "array"
            ? "A JSON list, e.g. [1, 2]"
            : "A JSON object"
      }
    } else {
      value[property.name] = typeof entry === "string" ? entry : ""
    }
  }
  return { value, errors }
}

/** The fields for a value: the reverse of `buildValue`. */
export function flattenValue(
  properties: SchemaProperty[],
  value: unknown,
  parent = "",
  raw: SchemaRaw = {}
): SchemaRaw {
  const object =
    value && typeof value === "object" && !Array.isArray(value) ? value : {}
  for (const property of named(properties)) {
    const path = pathOf(parent, property.name)
    const item = (object as Record<string, unknown>)[property.name]
    if (isGroup(property))
      flattenValue(property.properties ?? [], item, path, raw)
    else if (property.type === "boolean") raw[path] = item === true
    else if (item === undefined || item === null) continue
    else if (isJson(property)) raw[path] = JSON.stringify(item, null, 2)
    else raw[path] = String(item)
  }
  return raw
}

/**
 * What JSON text reads as: its value; blank, `empty`; with `lenient`,
 * text that isn't JSON is itself (a plain answer), else an error.
 */
export function readJsonText(
  text: string,
  { empty = null, lenient = false }: { empty?: unknown; lenient?: boolean } = {}
): { ok: true; value: unknown } | { ok: false; error: string } {
  const trimmed = text.trim()
  if (!trimmed) return { ok: true, value: empty }
  try {
    return { ok: true, value: JSON.parse(trimmed) }
  } catch (caught) {
    if (lenient) return { ok: true, value: trimmed }
    return { ok: false, error: `Not JSON: ${(caught as Error).message}` }
  }
}

export type SchemaValueMode = "form" | "json"

/** What's filled in, read: the value, or not (the fields say why). */
export type SchemaRead = { ok: true; value: unknown } | { ok: false }

/**
 * A value being filled in against a schema: in its fields when it declares
 * some, else as JSON, and switchable between the two. `read()` checks it,
 * marks what's wrong, and gives the value. Mount it again (a `key`) to
 * start from empty fields.
 */
export function useSchemaValue(
  schema: Record<string, unknown>,
  {
    empty = null,
    lenient = false,
  }: {
    /** The value of blank JSON. */
    empty?: unknown
    /** Without fields, text that isn't JSON is the value as it is. */
    lenient?: boolean
  } = {}
) {
  const properties = React.useMemo(() => schemaProperties(schema), [schema])
  const hasFields = properties.length > 0
  const [mode, setMode] = React.useState<SchemaValueMode>(
    hasFields ? "form" : "json"
  )
  const [raw, setRaw] = React.useState<SchemaRaw>({})
  const [text, setText] = React.useState(hasFields ? "{}" : "")
  const [errors, setErrors] = React.useState<SchemaErrors>({})
  const [jsonError, setJsonError] = React.useState<string>()

  const switchTo = (next: SchemaValueMode) => {
    if (next === mode) return
    if (next === "json") {
      setText(JSON.stringify(buildValue(properties, raw).value, null, 2))
      setJsonError(undefined)
    } else {
      try {
        setRaw(flattenValue(properties, text.trim() ? JSON.parse(text) : {}))
        setErrors({})
      } catch (caught) {
        setJsonError(`Not JSON: ${(caught as Error).message}`)
        return
      }
    }
    setMode(next)
  }

  const setField = (path: string, value: string | boolean) => {
    setRaw((current) => ({ ...current, [path]: value }))
    setErrors((current) => {
      const next = { ...current }
      delete next[path]
      return next
    })
  }

  const editText = (value: string) => {
    setText(value)
    setJsonError(undefined)
  }

  const read = (): SchemaRead => {
    if (mode === "form") {
      const built = buildValue(properties, raw)
      setErrors(built.errors)
      if (Object.keys(built.errors).length) return { ok: false }
      return { ok: true, value: built.value }
    }
    const parsed = readJsonText(text, { empty, lenient: lenient && !hasFields })
    if (!parsed.ok) {
      setJsonError(parsed.error)
      return { ok: false }
    }
    setJsonError(undefined)
    return { ok: true, value: parsed.value }
  }

  return {
    properties,
    hasFields,
    mode,
    switchTo,
    raw,
    setField,
    text,
    editText,
    errors,
    jsonError,
    read,
  }
}

export type SchemaValue = ReturnType<typeof useSchemaValue>
