import * as React from "react"
import { keepPreviousData, useQuery } from "@tanstack/react-query"

import { api } from "@/lib/api-instance"
import { ADK_APP, FORGE_AGENT_SERVER } from "@/lib/forge-agent"
import type { PageContext } from "@/lib/page-context"

/**
 * What the Forge assistant has on a page, as the admin API's screen
 * configuration (agents/screens.yaml) gives it: the screens the page
 * matched, the toolsets it may use there, and the prompts to suggest under a
 * new conversation's composer. Every tool acts as the person.
 */
export type AssistantCapabilities = {
  screens: { name: string; title: string }[]
  toolsets: AssistantToolset[]
  prompts: AssistantPrompt[]
  /**
   * Every specialist the assistant hands requests to, on any page: each
   * toolset's agent, by its ADK name.
   */
  specialists: { name: string; title: string }[]
}

export type AssistantToolset = {
  name: string
  /** The specialist agent that holds it. */
  agent: string
  title: string
  /** What it helps with. */
  description: string
  /** Kept from an earlier page of the conversation, not this page's. */
  fromEarlier: boolean
  /** Only when asked for (`tools`). */
  tools?: AssistantTool[]
}

export type AssistantTool = {
  name: string
  description: string
  /** It asks the person to confirm each call first. */
  asksFirst: boolean
}

export type AssistantPrompt = { title: string; label: string; prompt: string }

export const assistantCapabilityKeys = {
  all: ["assistant-capabilities"] as const,
}

/**
 * What of a page the screens match on: its route, its search parameters and
 * the kinds of record on it. Other changes (a title, a label) leave the
 * query as it is.
 */
function matchedOn(page: PageContext | null) {
  if (!page) return null
  const kinds = new Set(page.entities?.map((entity) => entity.kind))
  if (page.focus) kinds.add(page.focus.kind)
  return { route: page.route, search: page.search, kinds: [...kinds].sort() }
}

/**
 * What the assistant has for `userId` on `page` (null while they don't
 * share it): its prompts, and, with `tools`, each toolset's tools. With a
 * conversation (`sessionId`), the toolsets it kept from earlier pages too.
 * Nothing when the agent isn't the admin API's.
 */
export function useAssistantCapabilities({
  userId,
  page,
  sessionId,
  tools = false,
  enabled = true,
}: {
  userId: string
  page: PageContext | null
  sessionId?: string | undefined
  tools?: boolean
  enabled?: boolean
}) {
  const on = matchedOn(page)
  return useQuery({
    queryKey: [
      ...assistantCapabilityKeys.all,
      userId,
      on,
      sessionId ?? null,
      tools,
    ],
    queryFn: ({ signal }) =>
      api.post<AssistantCapabilities>(
        `/agents/apps/${encodeURIComponent(ADK_APP)}/users/${encodeURIComponent(userId)}/capabilities`,
        { pageContext: page, sessionId, tools },
        { signal }
      ),
    enabled: enabled && FORGE_AGENT_SERVER && Boolean(userId),
    // It changes with a new release or configuration, not by the minute.
    staleTime: 5 * 60_000,
    // Moving between pages keeps the last prompts until the next arrive.
    placeholderData: keepPreviousData,
    // The assistant works without it; the panel shows its own error.
    meta: { silent: true },
  })
}

/**
 * What the assistant's agents are called, for its `agents` option: each
 * specialist by its toolset's title, e.g. workflows → "Workflows".
 */
export function useAssistantAgents(userId: string) {
  const { data } = useAssistantCapabilities({ userId, page: null })
  return React.useMemo(
    () =>
      data &&
      Object.fromEntries(
        data.specialists.map((specialist) => [
          specialist.name,
          specialist.title,
        ])
      ),
    [data]
  )
}

/**
 * The prompts suggested under a new conversation's composer on `page`, in
 * the shape the assistant takes (`suggestions`).
 */
export function useSuggestedPrompts(userId: string, page: PageContext | null) {
  const { data } = useAssistantCapabilities({ userId, page })
  return React.useMemo(
    () =>
      data?.prompts.map(({ title, label, prompt }) => ({
        title,
        label,
        prompt,
      })),
    [data]
  )
}
