import { useQuery } from "@tanstack/react-query"

import { ApiError, createApiClient } from "@/lib/api/index"
import { RUNTIME_URL } from "@/lib/runtime"

import type { ChatPageSettings } from "./page-settings"

/*
 * Chat pages: a published chat agent shared as a full-page chat at a link
 * of its own (`/chat/<token>`), for people outside Forge. The page talks to
 * the runtime through its link (`{RUNTIME_URL}/pages/<token>`), which is
 * its only credential: it sends no sign-in, it reaches only the agent's
 * latest published version, and each visitor's conversations are under a
 * random ID (`visitor_…`) this browser keeps.
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
}

/** The page's address: the link to share. */
export const chatPageLink = (token: string) =>
  new URL(`/chat/${token}`, window.location.origin).toString()

/** The run API through the page's link: its chat's `adk.url`. */
export const chatPageRuntime = (token: string) =>
  RUNTIME_URL ? `${RUNTIME_URL}/pages/${encodeURIComponent(token)}` : undefined

// Without the console's sign-in: the link is a visitor's only credential.
const visitors = createApiClient({ baseURL: RUNTIME_URL, retry: false })

/** Whether the link answers no more: turned off, or given a new link. */
export const isGone = (error: unknown) =>
  error instanceof ApiError && (error.status === 404 || error.status === 422)

/** What the page at a link shows; a 404 when it doesn't answer. */
export function useChatPageInfo(token: string) {
  return useQuery({
    queryKey: ["chat-page", token],
    queryFn: ({ signal }) =>
      visitors.get<ChatPageInfo>(`/pages/${encodeURIComponent(token)}`, {
        signal,
        silent: true,
      }),
    enabled: Boolean(RUNTIME_URL),
    retry: (count, error) => !isGone(error) && count < 2,
    refetchOnWindowFocus: false,
    meta: { silent: true },
  })
}

const VISITOR = /^visitor_[A-Za-z0-9]{16,64}$/
// When the browser keeps nothing (private windows, blocked storage): this
// tab's, for as long as it's open.
const unkept = new Map<string, string>()

function newVisitorId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return `visitor_${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`
}

/**
 * Who this browser is to an agent's page: a random ID made on the first
 * visit and kept, so its conversations are there next time. One per agent,
 * so different organizations' pages can't tell it's the same visitor.
 */
export function visitorId(agentId: string): string {
  const key = `forge.chat-page.visitor:${agentId}`
  try {
    const kept = localStorage.getItem(key)
    if (kept && VISITOR.test(kept)) return kept
    const made = newVisitorId()
    localStorage.setItem(key, made)
    return made
  } catch {
    const made = unkept.get(key) ?? newVisitorId()
    unkept.set(key, made)
    return made
  }
}
