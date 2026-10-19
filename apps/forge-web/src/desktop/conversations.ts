import * as React from "react"
import type { AssistantRuntime } from "@assistant-ui/react"
import {
  useQueries,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query"

import { titleOf } from "@/components/forge/assistant/session-titles"
import {
  chatPageRuntime,
  chatPageVisitorQuery,
  fetchChatPageInfo,
  visitorHeaders,
  type ChatPageInfo,
  type ChatPageVisitor,
} from "@/features/agents/lib/chat-page"

import { keyOf, type DesktopAgent, type PageAgent } from "./library"
import { runtimeOf } from "./links"
import { signInHeaders, type Me } from "./session"

/*
 * Each added agent's conversations, for the sidebar, newest first: a
 * member agent's from the runtime, as the person signed in
 * (`…/runtime/apps/<agent>/users/<subject>/sessions`); a page's from its
 * page, as this app's visitor, with the token the page's API signed saying
 * so (`…/pages/<token>/apps/<app>/users/<visitor>/sessions`).
 * Each agent's chat says when its list changed (`useFollowChat`), and the
 * open one which conversation it shows (`useShownConversation`).
 */

/** A conversation as the sidebar lists it. */
export type Conversation = {
  /** Its ADK session's ID: the chat's thread ID on the server. */
  id: string
  title: string | undefined
  /** When it last changed, in ms. */
  updated: number
}

/** An agent's conversations, as the sidebar shows them. */
export type AgentConversations = {
  agent: DesktopAgent
  conversations: Conversation[]
  loading: boolean
  error: Error | null
  retry: () => void
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

const NOT_SIGNED_IN = new Error("No one is signed in to this app")

/** Where an agent's conversations are kept, and the headers that reach them. */
type Place = { sessions: string; headers: Record<string, string> }

const enc = encodeURIComponent

/** A page's: its page's runtime, as this app's visitor. */
const pagePlace = (
  agent: PageAgent,
  info: ChatPageInfo,
  visitor: ChatPageVisitor
): Place => ({
  sessions: `${chatPageRuntime(agent.token, runtimeOf(agent.server))}/apps/${enc(info.app_name)}/users/${enc(visitor.visitor_id)}/sessions`,
  headers: visitorHeaders(visitor),
})

/** A page's visitor query: once its info says which agent it's for. */
const visitorQueryOf = (agent: PageAgent, info: ChatPageInfo) =>
  chatPageVisitorQuery(agent.token, runtimeOf(agent.server), info.app_name)

/** A member agent's: the runtime, as the person signed in. */
const placeOf = (
  agent: DesktopAgent,
  info: ChatPageInfo | undefined,
  visitor: ChatPageVisitor | undefined,
  me: Me | undefined
): Place | undefined => {
  if (agent.kind !== "member")
    return info && visitor && pagePlace(agent, info, visitor)
  if (!me) return undefined
  return {
    sessions: `${runtimeOf(agent.server)}/apps/${enc(agent.id)}/users/${enc(me.subject)}/sessions`,
    headers: signInHeaders(),
  }
}

const infoKey = (agent: PageAgent) => [
  "chat-page",
  runtimeOf(agent.server),
  agent.token,
]
const listKey = (agent: DesktopAgent) => ["desktop-conversations", keyOf(agent)]

const pageInfo = (agent: PageAgent, signal?: AbortSignal) =>
  fetchChatPageInfo(agent.token, runtimeOf(agent.server), signal)

async function listConversations(
  place: Place,
  signal: AbortSignal
): Promise<Conversation[]> {
  const response = await fetch(place.sessions, {
    signal,
    headers: place.headers,
  })
  if (!response.ok)
    throw new Error(`Couldn't list conversations (${response.status})`)
  const sessions: unknown = await response.json()
  if (!Array.isArray(sessions)) return []
  return sessions
    .filter(isRecord)
    .filter((session) => typeof session.id === "string")
    .map((session) => ({
      id: session.id as string,
      title: titleOf(session),
      // ADK keeps seconds.
      updated:
        typeof session.lastUpdateTime === "number"
          ? session.lastUpdateTime * 1000
          : 0,
    }))
    .sort((a, b) => b.updated - a.updated)
}

/**
 * Every added agent's conversations, newest first. `me` is who's signed in
 * (member agents' conversations are theirs); `meError` why no one is.
 */
export function useAgentConversations(
  agents: DesktopAgent[],
  me: Me | undefined,
  meError: Error | null
): AgentConversations[] {
  // A page's info first (its app name, as the chat page reads it).
  const infos = useQueries({
    queries: agents.map((agent) =>
      agent.kind === "member"
        ? { queryKey: ["desktop-no-info"], enabled: false }
        : {
            queryKey: infoKey(agent),
            queryFn: ({ signal }: { signal: AbortSignal }) =>
              pageInfo(agent, signal),
            staleTime: 60_000,
            meta: { silent: true },
          }
    ),
  })
  // Then who this app is to each page: its visitor, as its chat is.
  const visitors = useQueries({
    queries: agents.map((agent, index) => {
      const info = infos[index]?.data as ChatPageInfo | undefined
      return agent.kind === "member" || !info
        ? { queryKey: ["desktop-no-visitor"], enabled: false }
        : visitorQueryOf(agent, info)
    }),
  })
  const places = agents.map((agent, index) =>
    placeOf(
      agent,
      infos[index]?.data as ChatPageInfo | undefined,
      visitors[index]?.data as ChatPageVisitor | undefined,
      me
    )
  )
  const lists = useQueries({
    queries: agents.map((agent, index) => {
      const place = places[index]
      return {
        queryKey: listKey(agent),
        queryFn: ({ signal }: { signal: AbortSignal }) =>
          listConversations(place!, signal),
        enabled: Boolean(place),
        staleTime: 30_000,
        meta: { silent: true },
      }
    }),
  })
  return agents.map((agent, index) => {
    const info = agent.kind === "member" ? undefined : infos[index]
    const visitor = agent.kind === "member" ? undefined : visitors[index]
    const list = lists[index]
    const waiting =
      agent.kind === "member"
        ? !me && !meError
        : Boolean(info?.isPending || (info?.data && visitor?.isPending))
    const unplaced = agent.kind === "member" && !me && meError
    return {
      agent,
      conversations: list?.data ?? [],
      loading: !unplaced && (waiting || Boolean(list?.isPending)),
      error: unplaced
        ? NOT_SIGNED_IN
        : ((info?.error as Error | null | undefined) ??
          (visitor?.error as Error | null | undefined) ??
          list?.error ??
          null),
      retry: () => {
        if (info?.error) void info.refetch()
        else if (visitor?.error) void visitor.refetch()
        else void list?.refetch()
      },
    }
  })
}

/** Reads an agent's conversations again: one began, changed, or went. */
export const refreshConversations = (
  client: QueryClient,
  agent: DesktopAgent
) => client.invalidateQueries({ queryKey: listKey(agent) })

/**
 * Deletes a conversation on its agent's server: through the open chat when
 * it's that agent's (so the chat lets go of it too), or the runtime itself.
 */
export async function deleteConversation(
  client: QueryClient,
  agent: DesktopAgent,
  id: string,
  runtime: AssistantRuntime | undefined,
  me: Me | undefined
) {
  if (runtime) {
    await runtime.threads.getItemById(id).delete()
  } else {
    const info =
      agent.kind === "member"
        ? undefined
        : await client.query({
            queryKey: infoKey(agent),
            queryFn: ({ signal }) => pageInfo(agent, signal),
            staleTime: 60_000,
          })
    const visitor =
      agent.kind === "member" || !info
        ? undefined
        : await client.query(visitorQueryOf(agent, info))
    const place = placeOf(agent, info, visitor, me)
    if (!place) throw NOT_SIGNED_IN
    const response = await fetch(`${place.sessions}/${enc(id)}`, {
      method: "DELETE",
      headers: place.headers,
    })
    if (!response.ok && response.status !== 404)
      throw new Error(`Couldn't delete the conversation (${response.status})`)
  }
  await refreshConversations(client, agent)
}

/** The conversation a chat shows: its server ID, none for a new one not yet sent. */
const shownBy = (runtime: AssistantRuntime) => {
  const { mainThreadId, threadItems } = runtime.threads.getState()
  return threadItems[mainThreadId]?.remoteId
}

/**
 * A chat's conversations with a reply running, shown or not (the chat keeps
 * each conversation's own going when another is opened in it): their
 * thread IDs, and their server IDs (none for a new one until it has one).
 */
const replyingIn = (runtime: AssistantRuntime) =>
  Object.values(runtime.threads.getState().threadItems)
    .filter((item) => runtime.threads.getItemById(item.id).getState().isRunning)
    .map((item) => ({ id: item.id, remoteId: item.remoteId }))

/** The conversation the open agent's chat shows; none for a new one. */
export function useShownConversation(
  runtime: AssistantRuntime | undefined
): string | undefined {
  return React.useSyncExternalStore(
    React.useCallback(
      (changed: () => void) =>
        runtime?.threads.subscribe(changed) ?? (() => {}),
      [runtime]
    ),
    () => (runtime ? shownBy(runtime) : undefined)
  )
}

/**
 * Follows an agent's chat (the open one's, or one kept replying out of
 * sight): its list read again as it changes (a conversation begins, a reply
 * ends), and `onReplying` told which of its conversations have a reply
 * running (a new one by its thread ID, until it has a server ID), none once
 * nothing runs. `onReplying` should keep its identity.
 */
export function useFollowChat(
  runtime: AssistantRuntime | undefined,
  agent: DesktopAgent,
  onReplying: (conversations: string[]) => void
) {
  const client = useQueryClient()
  React.useEffect(() => {
    if (!runtime) return
    const ids = () => runtime.threads.getState().threadIds.join(" ")
    // Unknown until the chat's first read of its list (the sidebar read it too).
    let known: string | undefined
    void runtime.threads.getLoadThreadsPromise().then(() => (known = ids()))
    const named = (replying: ReturnType<typeof replyingIn>) =>
      replying.map(({ id, remoteId }) => remoteId ?? id)
    let replying = replyingIn(runtime)
    onReplying(named(replying))
    const changed = runtime.threads.subscribe(() => {
      const now = replyingIn(runtime)
      // A reply ending: its conversation moves to the top, and has its title.
      const ended = replying.some(({ id }) => !now.some((r) => r.id === id))
      if (named(now).join(" ") !== named(replying).join(" "))
        onReplying(named(now))
      replying = now
      const listed = ids()
      const listChanged = known !== undefined && listed !== known
      if (known !== undefined) known = listed
      if (ended || listChanged) void refreshConversations(client, agent)
    })
    return () => {
      changed()
      onReplying([])
    }
  }, [runtime, agent, client, onReplying])
}

/** Opens a conversation in a chat, once its list has loaded. */
export async function showConversation(
  runtime: AssistantRuntime,
  id: string | undefined
) {
  await runtime.threads.getLoadThreadsPromise()
  if (id === undefined) await runtime.threads.switchToNewThread()
  else if (shownBy(runtime) !== id) await runtime.threads.switchToThread(id)
}
