/**
 * Tool approvals (ADK's `adk_request_confirmation`): what the card asks,
 * which of the call's arguments it shows, and the person's decisions, held
 * until every approval of the turn is answered.
 *
 * A reply to one approval resumes the run at once. Answering a second one
 * while that run goes on would start another run on the same session, which
 * ADK refuses ("The session has been modified in storage since it was
 * loaded"). So the approvals one turn asked for are answered together: each
 * decision waits here until the turn's last one is made (and no run is
 * going), then they all go in one message, which resumes every call in one
 * run.
 */

export const CONFIRMATION_TOOL = "adk_request_confirmation"

// ADK's own hint, from `require_confirmation`, is written for the client that
// answers it ("... by responding with a FunctionResponse ..."), not for the
// person; the card says it in its own words.
export const ADK_PROTOCOL_HINT = /^Please approve or reject the tool call /

/** "test_agent" → "Test agent"; "dryRun" → "Dry run". */
export const humanizeName = (name: string) =>
  name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim()
    .toLowerCase()
    .replace(/^\w/, (c) => c.toUpperCase())

export type ApprovalWords = {
  /** What the card asks: "Try “Support” with this message?" */
  question: string
  /** What approving does, when the tool says. */
  detail: string | undefined
}

/**
 * What the card says for a call to `toolName`. A hint whose first line is a
 * question (Forge's tools word theirs so) is the card's question, and its
 * other lines say what approving does; any other hint explains a general
 * question; ADK's own hint is no hint.
 */
export function approvalWords(
  toolName: string,
  hint: string | undefined
): ApprovalWords {
  const general = `Allow “${humanizeName(toolName) || "this tool"}”?`
  const text = hint?.trim()
  if (!text || ADK_PROTOCOL_HINT.test(text))
    return { question: general, detail: undefined }
  const [first = "", ...rest] = text.split("\n")
  if (!first.trim().endsWith("?")) return { question: general, detail: text }
  const detail = rest.join(" ").replace(/\s+/g, " ").trim()
  return { question: first.trim(), detail: detail || undefined }
}

export type ShownArgument = {
  key: string
  label: string
  /** One value, or each of a list's. */
  value: string | readonly string[]
}

// An ID in UUID form, alone or scoped (org:<ID>).
const UUID =
  /^([a-z]+:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
// Forge's own IDs: a short prefix and a code with a digit (ca_k2x8d1q, ag_…).
const FORGE_ID = /^[a-z]{2,4}_(?=[a-z0-9]*\d)[a-z0-9]{6,}(@[a-z]+)?$/
// organization_id, agentId, permission_ids, id ...
const ID_KEY = /(^|_)ids?$|[a-z]Ids?$/
// The most characters of one value the card shows.
const VALUE_CHARS = 400

const clip = (text: string) =>
  text.length > VALUE_CHARS ? `${text.slice(0, VALUE_CHARS - 1)}…` : text

const isId = (value: unknown) =>
  typeof value === "string" && (UUID.test(value) || FORGE_ID.test(value))

const scalar = (value: unknown): string | undefined => {
  if (typeof value === "boolean") return value ? "Yes" : "No"
  if (typeof value === "number") return String(value)
  if (typeof value === "string") return value.trim() ? clip(value) : undefined
  return undefined
}

const shown = (value: unknown): string | readonly string[] | undefined => {
  if (value == null || isId(value)) return undefined
  const one = scalar(value)
  if (one !== undefined || typeof value !== "object") return one
  const entries = Array.isArray(value)
    ? value.map((item) => [undefined, item] as const)
    : Object.entries(value).map(([key, item]) => [key, item] as const)
  const lines: string[] = []
  for (const [key, item] of entries) {
    if (item == null || isId(item) || (key && ID_KEY.test(key))) continue
    const text =
      typeof item === "object" ? clip(JSON.stringify(item)) : scalar(item)
    if (text === undefined) continue
    lines.push(key ? `${humanizeName(key)}: ${text}` : text)
  }
  return lines.length ? lines : undefined
}

/**
 * The call's arguments as the card lists them, for someone who isn't
 * technical: by name, without the IDs (the question names what they point
 * at), empty values or JSON where a list or a word will do.
 */
export function shownArguments(
  args: Readonly<Record<string, unknown>> | undefined
): ShownArgument[] {
  const found: ShownArgument[] = []
  for (const [key, raw] of Object.entries(args ?? {})) {
    if (ID_KEY.test(key)) continue
    const value = shown(raw)
    if (value !== undefined)
      found.push({ key, label: humanizeName(key), value })
  }
  return found
}

type PartLike = {
  type?: string
  toolName?: string
  toolCallId?: string
  result?: unknown
  approval?: { approved?: boolean }
}

/** The approvals still waiting in a message's parts, by their call's ID, in order. */
export function pendingApprovals(parts: readonly unknown[]): string[] {
  return parts.flatMap((raw) => {
    const part = raw as PartLike
    return part.type === "tool-call" &&
      part.toolName === CONFIRMATION_TOOL &&
      part.toolCallId &&
      part.result === undefined &&
      part.approval?.approved === undefined
      ? [part.toolCallId]
      : []
  })
}

/** The reply ADK resumes a call on, as the ADK runtime sends it. */
export type ConfirmationReply = {
  id: string
  type: "tool"
  tool_call_id: string
  name: typeof CONFIRMATION_TOOL
  content: string
  status: "success"
}

export const confirmationReply = (
  toolCallId: string,
  confirmed: boolean,
  id: string = crypto.randomUUID()
): ConfirmationReply => ({
  id,
  type: "tool",
  tool_call_id: toolCallId,
  name: CONFIRMATION_TOOL,
  content: JSON.stringify({ confirmed }),
  status: "success",
})

export type ApprovalBatch = ReturnType<typeof createApprovalBatch>

/**
 * The person's decisions on approvals, by their call's ID, until they're
 * sent. Several cards share one, and read it with `subscribe` and
 * `version` (`useSyncExternalStore`).
 */
export function createApprovalBatch() {
  const decisions = new Map<string, boolean>()
  const sent = new Set<string>()
  const listeners = new Set<() => void>()
  let version = 0
  const changed = () => {
    version += 1
    for (const listener of listeners) listener()
  }
  return {
    subscribe(listener: () => void) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    version: () => version,
    /** The decision made on an approval and not sent yet, if any. */
    decisionOf: (toolCallId: string) => decisions.get(toolCallId),
    isSent: (toolCallId: string) => sent.has(toolCallId),
    decide(toolCallId: string, confirmed: boolean) {
      if (sent.has(toolCallId)) return
      decisions.set(toolCallId, confirmed)
      changed()
    },
    /** Takes a decision back before it's sent. */
    undo(toolCallId: string) {
      if (!sent.has(toolCallId) && decisions.delete(toolCallId)) changed()
    },
    /**
     * The replies to every approval in `pending`, once each is decided and
     * none has been sent; they count as sent from then on. Otherwise
     * nothing, so only one card sends them.
     */
    take(pending: readonly string[]): ConfirmationReply[] | undefined {
      if (
        pending.length === 0 ||
        pending.some((id) => !decisions.has(id) || sent.has(id))
      )
        return undefined
      for (const id of pending) sent.add(id)
      changed()
      return pending.map((id) => confirmationReply(id, decisions.get(id)!))
    },
    /** Sending failed: the approvals can be decided and sent again. */
    release(toolCallIds: readonly string[]) {
      for (const id of toolCallIds) {
        sent.delete(id)
        decisions.delete(id)
      }
      changed()
    },
  }
}

/** The decisions of every conversation's approvals: call IDs are unique. */
export const approvalBatch = createApprovalBatch()
