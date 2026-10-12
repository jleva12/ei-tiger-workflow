/**
 * How full an agent's context window is, as the agent server keeps it in the
 * ADK session's state (`forge:context:<agent>`, forge_common's
 * `ContextWindowPlugin`): what its conversation holds, what its model may
 * read, and where it's compacted. The composer's context meter shows it.
 */

/** The state key each agent's context window is kept under, before its name. */
export const CONTEXT_STATE_PREFIX = "forge:context:"

/** An agent's context window, as the agent server keeps it. */
export type AgentContext = {
  /** What its conversation holds: its last request, or once the model answered, that and the reply. */
  tokens: number
  /** What a request may hold: the model's window less what's kept for the reply. */
  limit: number
  /** Where it's compacted. */
  threshold: number
  /** The model's whole window, and of it what's kept for the reply. */
  window?: number
  reply?: number
  /** The threshold as a share of the limit, e.g. 0.8. */
  share?: number
  /** Whether it compacts at the threshold by itself; otherwise only when asked. */
  auto?: boolean
  /** `estimate`: measured before the request went out; `provider`: what the model reported. */
  counted?: "estimate" | "provider"
  /** The model that last answered it (`provider/model`). */
  model?: string
  compactions?: number
  lastCompaction?: {
    at?: number
    tokensBefore: number
    tokensAfter: number
  }
  /** A compaction was asked for, before its next request. */
  requested?: boolean
  /**
   * It works apart from the conversation (a task, an agent called as a tool,
   * a parallel branch): each call starts from what it was handed.
   */
  apart?: boolean
  /** When it was counted, in seconds since the epoch. */
  updated?: number
}

/** What a model may read, as the model list gives it. */
export type ModelWindow = {
  id: string
  name?: string
  contextWindow?: number
  maxTokens?: number
}

const isNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value)

/** One agent's entry, if it's one the server wrote. */
export function parseAgentContext(value: unknown): AgentContext | undefined {
  if (!value || typeof value !== "object") return undefined
  const raw = value as Record<string, unknown>
  if (!isNumber(raw.tokens) || !isNumber(raw.limit) || raw.limit <= 0) {
    return undefined
  }
  const last = raw.last_compaction as Record<string, unknown> | undefined
  return {
    tokens: raw.tokens,
    limit: raw.limit,
    threshold: isNumber(raw.threshold) ? raw.threshold : raw.limit,
    ...(isNumber(raw.window) && { window: raw.window }),
    ...(isNumber(raw.reply) && { reply: raw.reply }),
    ...(isNumber(raw.share) && { share: raw.share }),
    ...(typeof raw.auto === "boolean" && { auto: raw.auto }),
    ...((raw.counted === "estimate" || raw.counted === "provider") && {
      counted: raw.counted,
    }),
    ...(typeof raw.model === "string" && raw.model && { model: raw.model }),
    ...(isNumber(raw.compactions) && { compactions: raw.compactions }),
    ...(last &&
      isNumber(last.tokens_before) &&
      isNumber(last.tokens_after) && {
        lastCompaction: {
          tokensBefore: last.tokens_before,
          tokensAfter: last.tokens_after,
          ...(isNumber(last.at) && { at: last.at }),
        },
      }),
    ...(raw.requested === true && { requested: true }),
    ...(raw.apart === true && { apart: true }),
    ...(isNumber(raw.updated) && { updated: raw.updated }),
  }
}

/**
 * The agent whose context the composer shows: the conversation's own agent,
 * the one answering when it has an entry, else the one counted last. Agents
 * that work apart from it (a task the agent hands a question to) are left
 * out, since each of their calls starts afresh; only when there's no other
 * is the one counted last shown.
 */
export function currentContext(
  state: Readonly<Record<string, unknown>>,
  agent?: string
): { agent: string; context: AgentContext } | undefined {
  type Found = { agent: string; context: AgentContext }
  const later = (found: Found, than?: Found) =>
    !than || (found.context.updated ?? 0) > (than.context.updated ?? 0)
  let latest: Found | undefined
  let latestApart: Found | undefined
  for (const [key, value] of Object.entries(state)) {
    if (!key.startsWith(CONTEXT_STATE_PREFIX)) continue
    const context = parseAgentContext(value)
    if (!context) continue
    const found = { agent: key.slice(CONTEXT_STATE_PREFIX.length), context }
    if (context.apart) {
      if (later(found, latestApart)) latestApart = found
      continue
    }
    if (agent && found.agent === agent) return found
    if (later(found, latest)) latest = found
  }
  return latest ?? latestApart
}

/**
 * - `clear`: well below where it's compacted;
 * - `near`: within a tenth of it;
 * - `compacting`: past it, so it's compacted before the next request (or,
 *   when it only compacts when asked, would be);
 * - `full`: past what a request may hold.
 */
export type ContextPhase = "clear" | "near" | "compacting" | "full"

export type ContextView = {
  /** Tokens the conversation holds; 0 before the agent has been counted. */
  used: number
  /** What a request may hold. */
  limit: number
  /** Where it's compacted; unknown until the agent has been counted. */
  threshold?: number
  /** `used` and `threshold` as shares of the limit (0–1, `used` may pass 1). */
  usedShare: number
  thresholdShare?: number
  /** Before the limit, and before the compaction (negative once past). */
  left: number
  untilCompaction?: number
  phase: ContextPhase
  auto: boolean
  /** No count yet: the conversation hasn't reached the agent. */
  empty: boolean
  /** The count is an estimate (before a request) rather than the model's. */
  estimated: boolean
  /** The model whose window this is: the one chosen in the composer, else the one that answered. */
  model?: string
  window?: number
  reply?: number
  /** The model chosen in the composer isn't the one the count is from. */
  modelChanged: boolean
  compactions: number
  lastCompaction?: AgentContext["lastCompaction"]
  requested: boolean
}

/**
 * The meter's numbers. A model chosen in the composer (`model`) whose window
 * is known wins over the window the count was made against: the next request
 * runs on it, and the server compacts at the same share of its limit.
 */
export function contextView(
  context: AgentContext | undefined,
  model?: ModelWindow
): ContextView | undefined {
  const modelLimit =
    model && isNumber(model.contextWindow) && model.contextWindow > 0
      ? Math.max(1, model.contextWindow - (model.maxTokens ?? 0))
      : undefined
  if (!context && modelLimit === undefined) return undefined

  // The server's own numbers, unless the next request runs on another model.
  const switched =
    modelLimit !== undefined &&
    context?.model !== undefined &&
    context.model !== model?.id
  const fromModel = modelLimit !== undefined && (!context || switched)
  const limit = fromModel ? modelLimit : context!.limit
  const threshold = !context
    ? undefined
    : fromModel
      ? Math.max(
          1,
          Math.floor(
            limit * (context.share ?? context.threshold / context.limit)
          )
        )
      : context.threshold
  const used = context?.tokens ?? 0

  const phase: ContextPhase =
    used > limit
      ? "full"
      : threshold !== undefined && used >= threshold
        ? "compacting"
        : threshold !== undefined && used >= threshold * 0.9
          ? "near"
          : "clear"

  return {
    used,
    limit,
    ...(threshold !== undefined && {
      threshold,
      thresholdShare: threshold / limit,
      untilCompaction: threshold - used,
    }),
    usedShare: used / limit,
    left: limit - used,
    phase,
    auto: context?.auto ?? true,
    empty: context === undefined,
    estimated: context?.counted === "estimate",
    ...(fromModel
      ? {
          model: model?.name ?? model?.id,
          window: model?.contextWindow,
          reply: model?.maxTokens,
        }
      : {
          // The model that answered, by its name when it's the one chosen.
          ...(context?.model && {
            model:
              model?.id === context.model
                ? (model.name ?? context.model)
                : context.model,
          }),
          ...(context?.window !== undefined && { window: context.window }),
          ...(context?.reply !== undefined && { reply: context.reply }),
        }),
    modelChanged: switched,
    compactions: context?.compactions ?? 0,
    ...(context?.lastCompaction && { lastCompaction: context.lastCompaction }),
    requested: context?.requested ?? false,
  }
}

const compactFormat = new Intl.NumberFormat("en", {
  notation: "compact",
  maximumFractionDigits: 1,
})
const fullFormat = new Intl.NumberFormat("en")

/** Tokens in a few characters: 950, 152.3K, 1.2M. */
export const compactTokens = (tokens: number) =>
  compactFormat.format(Math.max(0, Math.round(tokens)))

/** Tokens in full: 152,340. */
export const fullTokens = (tokens: number) =>
  fullFormat.format(Math.round(tokens))

/** A share as a whole percentage, at least 1% once anything is used. */
export function percent(share: number) {
  if (share <= 0) return "0%"
  return `${Math.max(1, Math.round(share * 100))}%`
}
