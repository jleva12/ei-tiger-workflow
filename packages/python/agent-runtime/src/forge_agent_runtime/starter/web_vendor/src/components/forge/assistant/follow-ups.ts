/**
 * Follow-up questions after a reply, written by the agent server: a run asks
 * for them in its state (`forge:suggest_follow_ups`, which `useAdkAssistant`
 * sends with `suggestFollowUps`), and the server keeps the last reply's in the
 * ADK session's state (`forge:follow_ups`, forge_common's `FollowUpsPlugin`),
 * which the thread shows above the composer.
 */

/** The state key a run asks for follow-ups with: true, or `{ count }`. */
export const FOLLOW_UPS_REQUEST_KEY = "forge:suggest_follow_ups"

/** The state key the last reply's follow-ups are kept under: `{ suggestions }`. */
export const FOLLOW_UPS_STATE_KEY = "forge:follow_ups"

/** The most follow-ups a run may ask for. */
export const MOST_FOLLOW_UPS = 5

/**
 * Whether runs ask for follow-up questions after the reply: true for the
 * server's default number (3), or how many (1–5).
 */
export type FollowUpsOption = boolean | { count?: number }

/** What a run sends for `option`: nothing when it's off. */
export function followUpsRequest(
  option: FollowUpsOption | undefined
): Record<string, unknown> | undefined {
  if (!option) return undefined
  if (option === true || option.count === undefined) {
    return { [FOLLOW_UPS_REQUEST_KEY]: true }
  }
  const count = Math.round(option.count)
  return {
    [FOLLOW_UPS_REQUEST_KEY]: {
      count: Math.min(
        Math.max(Number.isFinite(count) ? count : 1, 1),
        MOST_FOLLOW_UPS
      ),
    },
  }
}

/** The last reply's follow-ups in the session's state; none when it has none. */
export function followUpsOf(
  state: Readonly<Record<string, unknown>>
): string[] {
  const kept = state[FOLLOW_UPS_STATE_KEY]
  if (typeof kept !== "object" || kept === null) return []
  const { suggestions } = kept as { suggestions?: unknown }
  if (!Array.isArray(suggestions)) return []
  return suggestions.filter(
    (each): each is string => typeof each === "string" && each.trim() !== ""
  )
}
