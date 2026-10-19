import * as React from "react"
import type { AssistantRuntime } from "@assistant-ui/react"

import {
  openAssistantWindow,
  tellWindowOfAsk,
} from "@/features/assistant/lib/assistant-window"

/*
 * Asking the assistant from a page: a page's own button (the Agents
 * builder's "Build it with the assistant") hands it a request, which it
 * sends in a new conversation, with the page it was asked from.
 *
 * The panel in this tab takes it while it's mounted (`useAssistantAsks`).
 * Otherwise the assistant's window does: the request waits in this
 * browser's storage until the window, opened or brought forward, takes it
 * (`takePendingAsk`), once it knows the page it was asked from.
 */

const KEY = "forge.assistant.ask"
/** How long a request waits for the window. */
const FRESH_MS = 2 * 60_000

type Listener = (prompt: string) => void

const listeners = new Set<Listener>()

/** Has the assistant send `prompt` in a new conversation, and shows it. */
export function askAssistant(prompt: string) {
  const text = prompt.trim()
  if (!text) return
  const panel = [...listeners].at(-1)
  if (panel) {
    panel(text)
    return
  }
  try {
    localStorage.setItem(KEY, JSON.stringify({ prompt: text, at: Date.now() }))
  } catch {
    return
  }
  if (openAssistantWindow()) tellWindowOfAsk()
}

/** Calls `onAsk` with each request a page makes, while mounted. */
export function useAssistantAsks(onAsk: Listener) {
  const latest = React.useRef(onAsk)
  React.useEffect(() => {
    latest.current = onAsk
  })
  React.useEffect(() => {
    const listener: Listener = (prompt) => latest.current(prompt)
    listeners.add(listener)
    return () => {
      listeners.delete(listener)
    }
  }, [])
}

/** The request waiting for the assistant's window, taken; null for none. */
export function takePendingAsk(): string | null {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return null
    localStorage.removeItem(KEY)
    const { prompt, at } = JSON.parse(raw) as { prompt?: unknown; at?: unknown }
    return typeof prompt === "string" &&
      typeof at === "number" &&
      Date.now() - at < FRESH_MS
      ? prompt
      : null
  } catch {
    return null
  }
}

/** Sends `prompt` in a new conversation, once the conversations have loaded. */
export async function sendInNewConversation(
  runtime: AssistantRuntime,
  prompt: string
) {
  try {
    await runtime.threads.getLoadThreadsPromise()
    await runtime.threads.switchToNewThread()
    runtime.thread.append({
      role: "user",
      content: [{ type: "text", text: prompt }],
      // The model and thinking level the composer has picked.
      runConfig: runtime.thread.composer.getState().runConfig,
    })
  } catch (error) {
    console.warn("Couldn't send the request to the assistant:", error)
  }
}
