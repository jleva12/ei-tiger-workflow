const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

/**
 * A tool's result as the object it returned. ADK results arrive as values or
 * JSON text, sometimes wrapped as `{ result }`.
 */
export function toolResult<T extends object>(result: unknown): Partial<T> {
  let value = result
  if (typeof value === "string") {
    try {
      value = JSON.parse(value)
    } catch {
      return {}
    }
  }
  if (
    isObject(value) &&
    isObject(value.result) &&
    Object.keys(value).length === 1
  )
    value = value.result
  return isObject(value) ? (value as Partial<T>) : {}
}
