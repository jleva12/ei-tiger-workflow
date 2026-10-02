import * as React from "react"
import { useAuiState } from "@assistant-ui/react"
import { create } from "zustand"

import type { AgentModel } from "./assistant-models"

/** One turn's cost: every model call its agent made, summed. */
export type TurnStats = {
  /** Seconds from the person's message to the last event. */
  seconds: number
  calls: number
  inputTokens: number
  outputTokens: number
  /** Of the output tokens, those spent reasoning. */
  reasoningTokens: number
  cachedTokens: number
  /** USD, from the model's prices; 0 when it has none. */
  cost: number
  /** False when any model call has no configured price. */
  costKnown: boolean
  /** provider/model, as the conversation had it chosen. */
  model: string | undefined
  thinkingLevel: string | undefined
}

export type ConversationStats = {
  sessionId: string
  /** By ADK invocation: one per message the person sent. */
  turns: Record<string, TurnStats>
  /** Each event's invocation, to find a reply's turn from its id. */
  eventTurn: Record<string, string>
  /** The last model call's input + output: how full the context is. */
  contextTokens: number
  /** The model that call ran on. */
  contextModel: string | undefined
  cost: number
}

type Usage = {
  promptTokenCount?: number
  candidatesTokenCount?: number
  thoughtsTokenCount?: number
  cachedContentTokenCount?: number
}

type StoredEvent = {
  id: string
  invocationId?: string
  timestamp: number
  usageMetadata?: Usage
  actions?: { stateDelta?: Record<string, unknown> }
}

const perMillion = 1_000_000

/**
 * A conversation's stats from its stored ADK events: each model call's usage,
 * summed per invocation, priced with the model the conversation had chosen
 * at that point (its `model` state), else the agent's default.
 */
export function conversationStats(
  sessionId: string,
  events: StoredEvent[],
  catalog: AgentModel[],
  defaultModel: string | undefined
): ConversationStats {
  const prices = new Map(catalog.map((model) => [model.id, model.cost]))
  const turns: Record<string, TurnStats & { start: number; end: number }> = {}
  const eventTurn: Record<string, string> = {}
  let model = defaultModel
  let thinkingLevel: string | undefined
  let contextTokens = 0
  let contextModel: string | undefined
  let cost = 0

  for (const event of events) {
    const delta = event.actions?.stateDelta ?? {}
    if (typeof delta.model === "string" && delta.model) model = delta.model
    if (typeof delta.thinking_level === "string")
      thinkingLevel = delta.thinking_level
    const turnId = event.invocationId
    if (!turnId) continue
    eventTurn[event.id] = turnId
    const turn = (turns[turnId] ??= {
      seconds: 0,
      calls: 0,
      inputTokens: 0,
      outputTokens: 0,
      reasoningTokens: 0,
      cachedTokens: 0,
      cost: 0,
      costKnown: true,
      model,
      thinkingLevel,
      start: event.timestamp,
      end: event.timestamp,
    })
    turn.start = Math.min(turn.start, event.timestamp)
    turn.end = Math.max(turn.end, event.timestamp)
    turn.seconds = turn.end - turn.start
    const usage = event.usageMetadata
    if (!usage) continue
    const input = usage.promptTokenCount ?? 0
    const output = usage.candidatesTokenCount ?? 0
    const cached = usage.cachedContentTokenCount ?? 0
    const price = model ? prices.get(model) : undefined
    if (!price) turn.costKnown = false
    const callCost = price
      ? ((input - cached) * price.input +
          cached * price.cacheRead +
          output * price.output) /
        perMillion
      : 0
    turn.calls += 1
    turn.inputTokens += input
    turn.outputTokens += output
    turn.reasoningTokens += usage.thoughtsTokenCount ?? 0
    turn.cachedTokens += cached
    turn.cost += callCost
    cost += callCost
    contextTokens = input + output
    contextModel = model
  }
  return { sessionId, turns, eventTurn, contextTokens, contextModel, cost }
}

type TurnStatsState = {
  stats: ConversationStats | null
  /** Replaced when a conversation's events are read. */
  setStats: (stats: ConversationStats | null) => void
}

/** The open conversation's stats, for message footers and the meter. */
export const useTurnStats = create<TurnStatsState>((set) => ({
  stats: null,
  setStats: (stats) => set({ stats }),
}))

/**
 * Keeps `useTurnStats` on the open conversation: reads its stored events
 * when it opens and after each reply finishes. Render it once, inside the
 * assistant's providers.
 */
export function useTurnStatsSync(
  adkUrl: string | undefined,
  appName: string,
  userId: string,
  catalog: AgentModel[],
  defaultModel: string | undefined
) {
  const sessionId = useAuiState((s) => s.threadListItem.remoteId)
  const running = useAuiState((s) => s.thread.isRunning)
  const setStats = useTurnStats((state) => state.setStats)
  React.useEffect(() => {
    // Read after the reply, not during it: stored events are final.
    if (!adkUrl || !sessionId || running) {
      if (!sessionId) setStats(null)
      return
    }
    const controller = new AbortController()
    const url = `${adkUrl}/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(sessionId)}`
    fetch(url, { signal: controller.signal })
      .then((response) => (response.ok ? response.json() : null))
      .then((session: { events?: StoredEvent[] } | null) => {
        if (session?.events)
          setStats(
            conversationStats(sessionId, session.events, catalog, defaultModel)
          )
      })
      .catch(() => {})
    return () => controller.abort()
  }, [
    adkUrl,
    appName,
    userId,
    sessionId,
    running,
    catalog,
    defaultModel,
    setStats,
  ])
}

/** The ADK event a reply's message id names: `<eventId>:ai[n]`. */
export const eventIdOf = (messageId: string) => messageId.split(":")[0]
