/*
 * present's own JSON quoted back. The present tool answers the model with
 * `{"shown": true}` (or `false` and its problems), meant for the model
 * alone, and the agent is told never to write JSON in its reply
 * (`forge_agent_runtime.present`); still, a model sometimes writes back
 * that answer, or the composition it sent (`{"_type": "Card", …}`), as its
 * reply, in braces or a code fence of its own. The chat leaves such a
 * reply's text out: the composition is drawn above it already.
 */

const ECHO_START = /^\{+\s*"(?:shown|_type)"\s*:/

/**
 * Whether a reply's text is only present's answer or composition written
 * back: true from its first `{"shown"` or `{"_type"` while it streams in
 * and, once whole, only when nothing but braces or a fence's end follows it.
 */
export function isPresentEcho(text: string): boolean {
  const body = text.trim().replace(/^```(?:json)?\s*/i, "")
  if (!ECHO_START.test(body)) return false
  // Past the object and the braces around it, minding its strings.
  let depth = 0
  let quoted = false
  let escaped = false
  let end = -1
  for (let i = 0; i < body.length; i++) {
    const char = body[i]
    if (quoted) {
      if (escaped) escaped = false
      else if (char === "\\") escaped = true
      else if (char === '"') quoted = false
      continue
    }
    if (char === '"') quoted = true
    else if (char === "{") depth++
    else if (char === "}" && --depth === 0) {
      end = i + 1
      break
    }
  }
  // Still coming in: hide it as it streams.
  if (end < 0) return true
  return /^[\s}`]*$/.test(body.slice(end))
}
