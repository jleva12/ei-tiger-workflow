import { useQuery } from "@tanstack/react-query"

import type { ModelWindow } from "@/components/forge/assistant/context-window"
import { ApiError, createApiClient, type ApiClient } from "@/lib/api/index"
import { RUNTIME_URL } from "@/lib/runtime"

import { desktopLinkOf, serverOfRuntime } from "./desktop-link"
import type { ChatPageSettings } from "./page-settings"

/*
 * Chat pages: a published chat agent shared as a full-page chat at a link
 * of its own (`/chat/<token>`), for people outside Forge. The page talks to
 * the runtime through its link (`{runtime}/pages/<token>`), which is
 * its only credential: it sends no sign-in, and it reaches only the agent's
 * latest published version. Each visitor's conversations are under a
 * visitor ID (`visitor_…`) the API makes, with a token it signs saying so,
 * which this browser keeps and its chat sends with every call
 * (`X-Forge-Visitor`): a user ID alone reaches nobody's.
 *
 * The runtime is the console's own (`RUNTIME_URL`) unless a caller names
 * another: the desktop app talks to whichever server a page's link is on.
 */

/**
 * What a chat page shows, and the `appName` its chat sends: its agent, and
 * the page's settings (an older server leaves some out: `pageSettingsOf`).
 */
export type ChatPageInfo = Partial<ChatPageSettings> & {
  app_name: string
  name: string
  description: string
  /** The published version it runs now. */
  version: number
  /** Its agents by the names ADK calls them, as the canvas names them. */
  agents?: Record<string, string>
  /** Its workflow tools by the names the model calls them, as the canvas names them. */
  workflows?: Record<string, string>
  /** The model its agent starts on (an older server leaves it out). */
  model?: AgentModel
}

/** The model an agent starts on, as the admin API gives it. */
export type AgentModel = {
  id: string
  name?: string
  context_window: number
  max_tokens?: number
}

/**
 * The model an agent starts on, as the context meter reads it: its window
 * before the agent server has counted the conversation.
 */
export const contextModelOf = (
  model: AgentModel | undefined
): ModelWindow | undefined =>
  model && {
    id: model.id,
    name: model.name,
    contextWindow: model.context_window,
    maxTokens: model.max_tokens,
  }

/** The page's address: the link to share. */
export const chatPageLink = (token: string) =>
  new URL(`/chat/${token}`, window.location.origin).toString()

/** The run API through the page's link: its chat's `adk.url`. */
export const chatPageRuntime = (
  token: string,
  runtime: string | undefined = RUNTIME_URL
) => (runtime ? `${runtime}/pages/${encodeURIComponent(token)}` : undefined)

/**
 * The link that opens a page's agent in the desktop app
 * (`forge-agents://add?…`), naming the API its runtime is on; none when
 * there's no runtime.
 */
export function chatPageDesktopLink(
  token: string,
  runtime: string | undefined = RUNTIME_URL
) {
  const server = runtime ? serverOfRuntime(runtime) : undefined
  return server ? desktopLinkOf({ server, token }) : undefined
}

// Without the console's sign-in: the link is a visitor's only credential.
// One client per runtime, made on first use.
const visitors = new Map<string, ApiClient>()
const visitorsAt = (runtime: string) => {
  let client = visitors.get(runtime)
  if (!client) {
    client = createApiClient({ baseURL: runtime, retry: false })
    visitors.set(runtime, client)
  }
  return client
}

/** What the page at a link on a runtime shows; a 404 when it doesn't answer. */
export const fetchChatPageInfo = (
  token: string,
  runtime: string,
  signal?: AbortSignal
) =>
  visitorsAt(runtime).get<ChatPageInfo>(`/pages/${encodeURIComponent(token)}`, {
    signal,
    silent: true,
  })

/** Whether the link answers no more: turned off, or given a new link. */
export const isGone = (error: unknown) =>
  error instanceof ApiError && (error.status === 404 || error.status === 422)

/**
 * Whether the page's agent is only for some people (some of its
 * organization's agent permissions): its visitors don't sign in, so it
 * doesn't answer.
 */
export const isLimited = (error: unknown) =>
  error instanceof ApiError && error.status === 403

/** What the page at a link shows; a 404 when it doesn't answer. */
export function useChatPageInfo(
  token: string,
  runtime: string | undefined = RUNTIME_URL
) {
  return useQuery({
    queryKey: ["chat-page", runtime, token],
    queryFn: ({ signal }) => fetchChatPageInfo(token, runtime!, signal),
    enabled: Boolean(runtime),
    retry: (count, error) => !isGone(error) && !isLimited(error) && count < 2,
    refetchOnWindowFocus: false,
    meta: { silent: true },
  })
}

/** The header a page's chat sends its visitor's token in. */
export const VISITOR_HEADER = "X-Forge-Visitor"

/**
 * A chat page's visitor: the user ID their conversations are kept under,
 * and the token the API signed saying so, which every call sends.
 */
export type ChatPageVisitor = {
  visitor_id: string
  token: string
  /** When the token stops working, in seconds since the epoch. */
  expires_at: number
}

const VISITOR = /^visitor_[A-Za-z0-9]{16,64}$/
// When the browser keeps nothing (private windows, blocked storage): this
// tab's, for as long as it's open.
const unkept = new Map<string, ChatPageVisitor>()
const visitorKey = (agentId: string) => `forge.chat-page.visitor:${agentId}`

const isVisitor = (value: unknown): value is ChatPageVisitor =>
  typeof value === "object" &&
  value !== null &&
  typeof (value as ChatPageVisitor).token === "string" &&
  typeof (value as ChatPageVisitor).visitor_id === "string" &&
  VISITOR.test((value as ChatPageVisitor).visitor_id)

/** The visitor this browser kept for an agent's page, if any. */
function keptVisitor(agentId: string): ChatPageVisitor | undefined {
  const key = visitorKey(agentId)
  try {
    const kept = JSON.parse(localStorage.getItem(key) ?? "null") as unknown
    // Before the API made visitors, the page kept a bare ID: it can't be proven, so it's dropped.
    return isVisitor(kept) ? kept : undefined
  } catch {
    return unkept.get(key)
  }
}

function keepVisitor(agentId: string, visitor: ChatPageVisitor) {
  const key = visitorKey(agentId)
  unkept.set(key, visitor)
  try {
    localStorage.setItem(key, JSON.stringify(visitor))
  } catch {
    // Kept for this tab only.
  }
}

/** What a page's chat sends to be its visitor. */
export const visitorHeaders = (visitor: ChatPageVisitor) => ({
  [VISITOR_HEADER]: visitor.token,
})

/**
 * Who this browser is to an agent's page: the visitor it kept, its token
 * renewed, or else a new one, made by the API on the first visit and kept,
 * so its conversations are there next time. One per agent, so different
 * organizations' pages can't tell it's the same visitor.
 */
export async function fetchVisitor(
  token: string,
  runtime: string,
  agentId: string,
  signal?: AbortSignal
): Promise<ChatPageVisitor> {
  const kept = keptVisitor(agentId)
  const visitor = await visitorsAt(runtime).post<ChatPageVisitor>(
    `/pages/${encodeURIComponent(token)}/visitors`,
    undefined,
    { signal, silent: true, headers: kept ? visitorHeaders(kept) : undefined }
  )
  keepVisitor(agentId, visitor)
  return visitor
}

/**
 * The query finding this browser's visitor of an agent's page
 * (`fetchVisitor`): one for the page's chat and whatever lists its
 * conversations (the desktop app's sidebar), so they're the same visitor.
 */
export const chatPageVisitorQuery = (
  token: string,
  runtime: string,
  agentId: string
) => ({
  queryKey: ["chat-page-visitor", runtime, token, agentId],
  queryFn: ({ signal }: { signal: AbortSignal }) =>
    fetchVisitor(token, runtime, agentId, signal),
  staleTime: Infinity,
  retry: (count: number, error: Error) => !isGone(error) && count < 2,
  refetchOnWindowFocus: false,
  meta: { silent: true },
})

/** This browser's visitor of an agent's page (`fetchVisitor`), once its info is known. */
export function useChatPageVisitor(
  token: string,
  runtime: string | undefined,
  agentId: string | undefined
) {
  return useQuery({
    ...chatPageVisitorQuery(token, runtime ?? "", agentId ?? ""),
    enabled: Boolean(runtime && agentId),
  })
}
