import { choicesOf } from "./choices-options"
import { resultValue, treeOf } from "./result"

/** A message part, as much of it as these read. */
export type MessagePart = {
  type: string
  text?: string
  toolName?: string
  args?: unknown
  result?: unknown
}

/**
 * What the agent server said of a `present` call: true when it took it,
 * false when it refused it (the model sends it again), undefined before it
 * answered. ADK hands results over as JSON text or values, sometimes
 * wrapped as `{ result }`.
 */
export function shownOf(result: unknown): boolean | undefined {
  const value = resultValue(result)
  if (typeof value !== "object" || value === null) return undefined
  const shown = (value as { shown?: unknown }).shown
  return typeof shown === "boolean" ? shown : undefined
}

/** Whether a tree has a Choices node with an option, a few levels down. */
function hasChoices(node: unknown, depth = 0): boolean {
  if (depth > 8 || typeof node !== "object" || node === null) return false
  if (Array.isArray(node)) return node.some((n) => hasChoices(n, depth + 1))
  const element = node as {
    _type?: unknown
    children?: unknown
    options?: unknown
  }
  if (element._type === "Choices" && choicesOf(element.options).length)
    return true
  return hasChoices(element.children, depth + 1)
}

/**
 * Whether a message's rich answer offers Choices, which the chat's
 * follow-up chips would only repeat: in the tree it's drawn from, a team
 * component's too (`treeOf`).
 */
export function offersChoices(
  message: { role: string; content: readonly MessagePart[] } | undefined
) {
  return (
    message?.role === "assistant" &&
    message.content.some(
      (part) =>
        part.type === "tool-call" &&
        part.toolName === "present" &&
        shownOf(part.result) !== false &&
        hasChoices(treeOf(part.args, part.result))
    )
  )
}
