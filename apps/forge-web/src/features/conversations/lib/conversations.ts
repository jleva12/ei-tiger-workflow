import { keepPreviousData, useQuery } from "@tanstack/react-query"

import type { ChipTone } from "@/components/forge/variants"
import { api } from "@/lib/api-instance"
import type { AdkRunTrace } from "@/features/runs/lib/trace"

/*
 * The conversation audit: every conversation of an organization's chat
 * agents (from the builder's chat, the run API, A2A or its chat page) and of the assistant
 * asked from inside it (`agents:audit`, its admins), and on the site every
 * assistant conversation (`agents:audit` on the site, its admins). Each is an
 * ADK session; its turns say how they came in (their channel) and who called
 * them, and its model and tool calls what they used.
 */

/* -------------------------------------------------------------------------- */
/* Contract                                                                   */
/* -------------------------------------------------------------------------- */

/** How a turn came in. */
export type Channel =
  "console" | "api" | "a2a" | "assistant" | "page" | "unknown"

export const CHANNELS: Record<
  Channel,
  { label: string; description: string; tone: ChipTone }
> = {
  console: {
    label: "Console",
    description: "The builder's chat, in Forge",
    tone: "neutral",
  },
  api: {
    label: "API",
    description: "The hosted run API, from an outside app",
    tone: "notice",
  },
  a2a: { label: "A2A", description: "The A2A endpoint", tone: "success" },
  assistant: {
    label: "Assistant",
    description: "The on-screen assistant",
    tone: "outline",
  },
  page: {
    label: "Chat page",
    description: "The agent's chat page, from someone with its link",
    tone: "warning",
  },
  unknown: {
    label: "Unknown",
    description: "From before channels were kept",
    tone: "outline",
  },
}

export const channelOf = (value: string) =>
  CHANNELS[value as Channel] ?? {
    label: value,
    description: "",
    tone: "outline" as ChipTone,
  }

/** How far back the list looks. */
export type Since = "1d" | "7d" | "30d" | "90d" | "all"

export const SINCE: Record<Since, string> = {
  "1d": "Last day",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "90d": "Last 90 days",
  all: "Any time",
}

/** Someone Forge knows: a person, or one of the organization's API keys. */
export type Person = {
  kind: "person" | "api_key"
  name: string
  email: string | null
}

/** A conversation, as the audit lists it. */
export type ConversationSummary = {
  /** A chat agent's ID (`ca_…`), or `forge` for the assistant. */
  app_name: string
  agent_name: string
  /** The conversation's user, as its session keeps it. */
  user_id: string
  session_id: string
  created_at: string | null
  updated_at: string | null
  events: number
  turns: number
  channels: string[]
  /** Who called its turns: a person, `apikey:<id>`, or the caller's own ID. */
  callers: string[]
  last_status: string | null
  failed_turns: number
  model_calls: number
  tool_calls: number
  input_tokens: number
  output_tokens: number
  /** USD; null when no model it used has a known price. */
  cost: number | null
}

export type ConversationPage = {
  items: ConversationSummary[]
  total: number
  people: Record<string, Person>
  /** The organization's chat agents, by ID. */
  agents: Record<string, string>
}

/** A turn of a conversation: how it came in, who called it, how it ended. */
export type ConversationTurn = {
  id: string
  channel: string | null
  caller: string | null
  version: string | null
  status: string
  error: string | null
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  organization_id: string | null
}

/** A model or tool call of a conversation, with what it used. */
export type ConversationCall = {
  id: number
  /** The ADK invocation (turn) it was part of. */
  invocation_id: string | null
  /** The ADK agent that called. */
  author: string
  call: "model" | "tool"
  /** The model (`provider/model`) or the tool. */
  name: string
  at: string | null
  input_tokens: number
  cached_tokens: number
  output_tokens: number
  thinking_tokens: number
  cost: number | null
  failed: boolean
}

/** One conversation, whole: its events and state (as a run's trace), turns and calls. */
export type ConversationDetail = Omit<
  AdkRunTrace,
  "invocation_id" | "session_id" | "earlier_sessions" | "worker"
> & {
  app_name: string
  agent_name: string
  user_id: string
  session_id: string
  created_at: string | null
  updated_at: string | null
  /** Only the turns asked from inside the organization are shown. */
  partial: boolean
  /** Its events as ADK's session API serves them (camelCase), which the chat window replays. */
  adk_events: Record<string, unknown>[]
  turns: ConversationTurn[]
  calls: ConversationCall[]
  people: Record<string, Person>
}

/** Whose conversations: an organization's, or the site's (every assistant conversation). */
export type AuditScope = { organizationId: string } | { site: true }

export type ConversationFilters = {
  q?: string
  agent?: string
  channel?: Channel
  since?: Since
}

/** Which conversation: its session, agent (ADK app) and user. */
export type ConversationRef = { session: string; app: string; user: string }

/* -------------------------------------------------------------------------- */
/* Words                                                                      */
/* -------------------------------------------------------------------------- */

export const ASSISTANT_APP = "forge"

/** Who an ID is, in words: a person's name, an API key's, or the ID itself. */
export function whoIs(
  id: string | null | undefined,
  people: Record<string, Person>
): { label: string; detail?: string; kind: "person" | "api_key" | "other" } {
  if (!id) return { label: "Anonymous", kind: "other" }
  const known = people[id]
  if (known?.kind === "person")
    return {
      label: known.name,
      detail: known.email ?? undefined,
      kind: "person",
    }
  if (known?.kind === "api_key")
    return { label: `API key ${known.name}`, kind: "api_key" }
  if (id.startsWith("apikey:"))
    return { label: "Deleted API key", detail: id, kind: "api_key" }
  return { label: id, kind: "other" }
}

/** "$0.0123", "<$0.0001"; "—" when unknown. */
export function formatCost(cost: number | null | undefined) {
  if (cost === null || cost === undefined) return "—"
  if (cost > 0 && cost < 0.0001) return "<$0.0001"
  return `$${cost.toFixed(cost < 1 ? 4 : 2)}`
}

/** "12.3k", "1.2M": tokens, short. */
export function formatTokens(count: number) {
  if (count < 1000) return count.toLocaleString()
  if (count < 1_000_000)
    return `${(count / 1000).toFixed(count < 10_000 ? 1 : 0)}k`
  return `${(count / 1_000_000).toFixed(1)}M`
}

/** Totals of a conversation's calls, by kind. */
export function callTotals(calls: ConversationCall[]) {
  const models = calls.filter((call) => call.call === "model")
  return {
    modelCalls: models.length,
    toolCalls: calls.length - models.length,
    inputTokens: models.reduce((sum, call) => sum + call.input_tokens, 0),
    outputTokens: models.reduce((sum, call) => sum + call.output_tokens, 0),
    cachedTokens: models.reduce((sum, call) => sum + call.cached_tokens, 0),
    thinkingTokens: models.reduce((sum, call) => sum + call.thinking_tokens, 0),
    cost: models.some((call) => call.cost !== null)
      ? models.reduce((sum, call) => sum + (call.cost ?? 0), 0)
      : null,
    failed: calls.filter((call) => call.failed).length,
  }
}

/* -------------------------------------------------------------------------- */
/* Queries                                                                    */
/* -------------------------------------------------------------------------- */

/** Conversations a page lists. */
export const PAGE_SIZE = 50

const basePath = (scope: AuditScope) =>
  "organizationId" in scope
    ? `/organizations/${encodeURIComponent(scope.organizationId)}/conversations`
    : "/conversations"

const scopeKey = (scope: AuditScope) =>
  "organizationId" in scope ? scope.organizationId : "site"

/** A page of the conversations, the latest active first. */
export function useConversations(
  scope: AuditScope,
  filters: ConversationFilters,
  page: number
) {
  return useQuery({
    queryKey: ["conversations", scopeKey(scope), "list", filters, page],
    queryFn: ({ signal }) =>
      api.get<ConversationPage>(basePath(scope), {
        params: {
          q: filters.q?.trim() || undefined,
          agent: filters.agent || undefined,
          channel: filters.channel || undefined,
          since: filters.since || undefined,
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
        },
        signal,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
    meta: { errorTitle: "Couldn't refresh the conversations" },
  })
}

/** One conversation, whole. */
export function useConversation(
  scope: AuditScope,
  ref: ConversationRef | undefined
) {
  return useQuery({
    queryKey: ["conversations", scopeKey(scope), "one", ref],
    queryFn: ({ signal }) =>
      api.get<ConversationDetail>(
        `${basePath(scope)}/${encodeURIComponent(ref!.session)}`,
        { params: { app: ref!.app, user: ref!.user }, signal }
      ),
    enabled: Boolean(ref),
    refetchInterval: 15_000,
    meta: { errorTitle: "Couldn't refresh the conversation" },
  })
}
