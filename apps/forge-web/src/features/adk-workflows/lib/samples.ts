/*
 * Example values of JSON Schemas, for the calls the builders show other
 * apps making.
 */

/** A value of a JSON Schema's type, to show in an example call. */
function sample(schema: unknown): unknown {
  if (!schema || typeof schema !== "object") return null
  const s = schema as Record<string, unknown>
  if (Array.isArray(s.enum) && s.enum.length) return s.enum[0]
  switch (s.type) {
    case "object": {
      const properties = (s.properties ?? {}) as Record<string, unknown>
      return Object.fromEntries(
        Object.entries(properties).map(([key, value]) => [key, sample(value)])
      )
    }
    case "array":
      return []
    case "string":
      return "…"
    case "integer":
    case "number":
      return 0
    case "boolean":
      return false
    default:
      return null
  }
}

/** What a run of a workflow is started with, for an example call: from its start's schema. */
export function sampleInput(schema: Record<string, unknown>): unknown {
  return Object.keys(schema).length ? sample(schema) : {}
}
