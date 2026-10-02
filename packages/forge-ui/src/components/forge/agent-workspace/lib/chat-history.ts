import { useCallback, useEffect, useRef, useState } from "react"

import type { AgentConnection } from "./use-agent-workspace"

export type HistoryEntry = {
  id: string
  title: string
  text: string
  updatedAt: number
}
type Session = {
  id: string
  lastUpdateTime?: number
  events?: {
    author?: string
    content?: { role?: string; parts?: { text?: string; thought?: boolean }[] }
  }[]
}

/** Search visible conversation text; omit model thoughts and raw tool payloads. */
export function historyEntry(session: Session): HistoryEntry {
  const messages = (session.events ?? [])
    .map((event) => ({
      user: event.author === "user" || event.content?.role === "user",
      text: (event.content?.parts ?? [])
        .filter((part) => !part.thought && typeof part.text === "string")
        .map((part) => part.text)
        .join(" ")
        .trim(),
    }))
    .filter((message) => message.text)
  const first = messages
    .find((message) => message.user)
    ?.text.replace(/\s+/g, " ")
  return {
    id: session.id,
    title: first
      ? first.length > 100
        ? `${first.slice(0, 97)}…`
        : first
      : "Untitled chat",
    text: messages.map((message) => message.text).join("\n"),
    updatedAt: session.lastUpdateTime ?? 0,
  }
}

/** The ADK sessions endpoint for a user's conversations. */
const sessionsUrl = ({ adkUrl, appName, userId }: AgentConnection) =>
  adkUrl
    ? `${adkUrl}/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions`
    : undefined

/**
 * The user's conversations, searchable by their text: indexed while `open`,
 * four requests at most at a time, unchanged sessions cached.
 */
export function useChatHistory(open: boolean, connection: AgentConnection) {
  const baseUrl = sessionsUrl(connection)
  const cache = useRef(new Map<string, HistoryEntry>())
  const [entries, setEntries] = useState<HistoryEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const retry = useCallback(() => setRevision((value) => value + 1), [])
  useEffect(() => {
    if (!open || !baseUrl) return
    const controller = new AbortController()
    const { signal } = controller
    const read = async (url: string) => {
      const response = await fetch(url, { signal })
      if (!response.ok)
        throw new Error(`History request failed: ${response.status}`)
      return response.json()
    }
    async function load() {
      setLoading(true)
      setError(null)
      try {
        const sessions: Session[] = await read(baseUrl!)
        if (!Array.isArray(sessions))
          throw new Error("Invalid history response")
        const ids = new Set(sessions.map((session) => session.id))
        for (const id of cache.current.keys())
          if (!ids.has(id)) cache.current.delete(id)
        const publish = () => {
          if (!signal.aborted)
            setEntries(
              [...cache.current.values()].sort(
                (a, b) => b.updatedAt - a.updatedAt
              )
            )
        }
        publish()
        const pending = sessions.filter(
          (session) =>
            !cache.current.has(session.id) ||
            !session.lastUpdateTime ||
            cache.current.get(session.id)?.updatedAt !== session.lastUpdateTime
        )
        let failures = 0
        const worker = async () => {
          while (pending.length && !signal.aborted) {
            const session = pending.shift()!
            try {
              const full: Session = await read(
                `${baseUrl}/${encodeURIComponent(session.id)}`
              )
              if (signal.aborted) return
              cache.current.set(
                session.id,
                historyEntry({
                  ...full,
                  id: session.id,
                  lastUpdateTime: session.lastUpdateTime,
                })
              )
              publish()
            } catch {
              if (!signal.aborted) failures += 1
            }
          }
        }
        await Promise.all(
          Array.from({ length: Math.min(4, pending.length) }, worker)
        )
        if (!signal.aborted && failures)
          setError("Some chats couldn’t be searched.")
      } catch {
        if (!signal.aborted) setError("Chat history couldn’t be loaded.")
      } finally {
        if (!signal.aborted) setLoading(false)
      }
    }
    void load()
    return () => controller.abort()
  }, [open, revision, baseUrl])
  return { entries, loading, error, retry }
}

export function matchesHistory(entry: HistoryEntry, query: string) {
  const haystack = `${entry.title}\n${entry.text}`.toLocaleLowerCase()
  return query
    .trim()
    .toLocaleLowerCase()
    .split(/\s+/)
    .every((term) => haystack.includes(term))
}

export function historyExcerpt(entry: HistoryEntry, query: string) {
  if (!query.trim()) return undefined
  const term = query.trim().split(/\s+/)[0].toLocaleLowerCase()
  const index = entry.text.toLocaleLowerCase().indexOf(term)
  if (index < 0) return undefined
  const start = Math.max(0, index - 35)
  const excerpt = entry.text.slice(start, start + 130).replace(/\s+/g, " ")
  return `${start ? "…" : ""}${excerpt}${entry.text.length > start + 130 ? "…" : ""}`
}
