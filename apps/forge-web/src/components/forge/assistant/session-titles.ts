import type { createAdkSessionAdapter } from "@assistant-ui/react-google-adk"

type SessionAdapter = ReturnType<typeof createAdkSessionAdapter>["adapter"]

/**
 * Where a conversation keeps its title in its ADK session's state. Forge's
 * agent servers write it with the conversation's first turn
 * (`forge_common.adk.titles`); other ADK servers don't, and their
 * conversations stay untitled.
 */
export const TITLE_KEY = "forge:title"

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

/** A session's title, from its state; undefined without one. */
export function titleOf(session: unknown): string | undefined {
  const state = isRecord(session) ? session.state : undefined
  const title = isRecord(state) ? state[TITLE_KEY] : undefined
  return typeof title === "string" && title.trim() ? title.trim() : undefined
}

const idOf = (session: unknown) =>
  isRecord(session) && typeof session.id === "string" && session.id
    ? session.id
    : undefined

/**
 * The ADK session adapter, listing each conversation with its title: the
 * package's lists them untitled (ADK sessions have no title of their own),
 * so every conversation read "New conversation" after a reload. A thread's
 * title is still named in the browser as it starts (`AutoTitle`); the server
 * keeps the same one with its first turn.
 */
export function withSessionTitles(
  adapter: SessionAdapter,
  {
    url,
    appName,
    userId,
    headers,
  }: {
    url: string
    appName: string
    userId: string
    headers: () => Record<string, string>
  }
): SessionAdapter {
  const base = `${url.replace(/\/+$/, "")}/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions`
  return {
    ...adapter,
    async list() {
      const res = await fetch(base, { headers: headers() })
      if (!res.ok) throw new Error(`Failed to list sessions: ${res.status}`)
      const sessions: unknown = await res.json()
      if (!Array.isArray(sessions))
        throw new Error(
          "Invalid ADK session list response: expected an array of sessions."
        )
      return {
        threads: sessions.flatMap((session) => {
          const id = idOf(session)
          return id
            ? [
                {
                  status: "regular" as const,
                  remoteId: id,
                  externalId: id,
                  title: titleOf(session),
                },
              ]
            : []
        }),
      }
    },
    async fetch(threadId) {
      const res = await fetch(`${base}/${encodeURIComponent(threadId)}`, {
        headers: headers(),
      })
      if (!res.ok) throw new Error(`Session not found: ${res.status}`)
      const session: unknown = await res.json()
      const id = idOf(session)
      if (!id)
        throw new Error(
          'Invalid ADK session fetch response: expected an object with a non-empty string "id".'
        )
      return {
        status: "regular" as const,
        remoteId: id,
        externalId: id,
        title: titleOf(session),
      }
    },
  }
}
