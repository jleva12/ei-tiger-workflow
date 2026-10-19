const valueText = (value: unknown): string => {
  if (value === undefined || value === null) return ""
  if (typeof value === "boolean") return value ? "Yes" : "No"
  if (Array.isArray(value))
    return value.map(valueText).filter(Boolean).join(", ")
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, each]) => [key, valueText(each)])
      .filter(([, each]) => each)
      .map(([key, each]) => `- ${key}: ${each}`)
      .join("\n")
  }
  return String(value).trim()
}

/**
 * The person's message for an action a component fired: its `text`, then
 * what they chose or entered (`$input`: a value, or a form's values by
 * name); an action without text sends its type in words.
 */
export function replyOf(action: unknown): string {
  if (typeof action !== "object" || action === null) return ""
  const { text, type, $input } = action as Record<string, unknown>
  const said =
    typeof text === "string" && text.trim()
      ? text.trim()
      : typeof type === "string" && type !== "reply"
        ? type.replace(/[_-]+/g, " ").trim()
        : ""
  const value = valueText($input)
  if (!value) return said
  if (!said) return value
  return value.includes("\n") ? `${said}\n${value}` : `${said} ${value}`
}
