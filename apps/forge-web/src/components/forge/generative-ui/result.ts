/**
 * What the agent server answered a `present` call with, as chats read it.
 *
 * A call of team components (`org.…`, an organization's compositions of
 * base components) is drawn as base components on the server
 * (`forge_agent_runtime.present`), which answers with that tree (`ui`).
 * Chats draw it in place of the call's arguments, which name components no
 * chat has, and it stays with the conversation: an old message shows what
 * was drawn then, whatever the component is now. A call of base
 * components alone answers `shown` only and is drawn from its arguments.
 */

/** The version of the server's drawn answer this chat reads. */
export const UI_PROTOCOL = 1

/** What team components' keys start with; base components' names never do. */
const TEAM_PREFIX = "org."

/** A tree the server drew, and the team components it drew it from. */
export type DrawnUI = {
  protocol: number
  components: { key: string; version: number | null }[]
  tree: unknown
}

/**
 * A tool result's value: ADK hands results over as JSON text or values,
 * sometimes wrapped as `{ result }`.
 */
export function resultValue(result: unknown): unknown {
  let value = result
  if (typeof value === "string") {
    try {
      value = JSON.parse(value)
    } catch {
      return undefined
    }
  }
  if (typeof value === "object" && value !== null && "result" in value) {
    value = (value as { result: unknown }).result
  }
  return value
}

/** The tree the server drew a call as, in a version this chat reads; undefined without one. */
export function drawnOf(result: unknown): DrawnUI | undefined {
  const value = resultValue(result)
  if (typeof value !== "object" || value === null) return undefined
  const ui = (value as { ui?: unknown }).ui
  if (typeof ui !== "object" || ui === null) return undefined
  const drawn = ui as Partial<DrawnUI>
  if (drawn.protocol !== UI_PROTOCOL || !("tree" in drawn)) return undefined
  return {
    protocol: drawn.protocol,
    components: Array.isArray(drawn.components) ? drawn.components : [],
    tree: drawn.tree,
  }
}

/** Whether a tree names a team component (`org.…`), however deep. */
export function namesTeamComponents(node: unknown, depth = 0): boolean {
  if (depth > 32 || typeof node !== "object" || node === null) return false
  if (Array.isArray(node))
    return node.some((each) => namesTeamComponents(each, depth + 1))
  const element = node as {
    _type?: unknown
    $type?: unknown
    children?: unknown
  }
  const type = element._type ?? element.$type
  if (typeof type === "string" && type.startsWith(TEAM_PREFIX)) return true
  return namesTeamComponents(element.children, depth + 1)
}

/**
 * The tree a `present` call is drawn from: the server's, when it drew one
 * this chat reads; otherwise the call's arguments, unless they name team
 * components, which only the server can draw (undefined: nothing to draw).
 */
export function treeOf(args: unknown, result: unknown): unknown {
  const drawn = drawnOf(result)
  if (drawn) return drawn.tree
  return namesTeamComponents(args) ? undefined : args
}
