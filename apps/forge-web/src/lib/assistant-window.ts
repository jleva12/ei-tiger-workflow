import * as React from "react"
import type { AssistantRuntime } from "@assistant-ui/react"

import { toast } from "@/components/ui/toast"
import type { CurrentPage } from "@/lib/forge-agent"
import { clipPageContext, type PageContext } from "@/lib/page-context"
import {
  useUserPreferences,
  useUserPreferencesApi,
} from "@/lib/user-preferences"

/*
 * The assistant in its own window, beside Forge. Where the assistant opens
 * is a preference: a panel over the page (the floating launcher) or the
 * window at `/assistant`, which is the whole window, without the shell.
 *
 * The window can't read the shell's stores, so Forge's tabs tell it what
 * they show over a BroadcastChannel (same origin only): each tab sends its
 * page context (`useCurrentPage`) when it changes and when the tab gets
 * focus, and the window follows the tab the person was in last. Page
 * context is untrusted data either way; the server checks it
 * (forge-admin-api `agents/page_context.py`).
 */

export type AssistantPlacement = "panel" | "window"

declare module "@/lib/user-preferences" {
  interface CustomUserPreferences {
    /**
     * Where the assistant opens: a panel over the page, or its own window
     * beside Forge that follows the page you're on.
     */
    assistantPlacement: AssistantPlacement
  }
}

export const DEFAULT_ASSISTANT_PLACEMENT: AssistantPlacement = "panel"

/** Where the assistant opens, and a way to change it (saved in this browser). */
export function useAssistantPlacement() {
  // A stale or edited stored value falls back to the panel.
  const placement = useUserPreferences((state): AssistantPlacement =>
    state.assistantPlacement === "window" ? "window" : "panel"
  )
  const setPreference = useUserPreferences((state) => state.setPreference)
  const setPlacement = React.useCallback(
    (next: AssistantPlacement) => setPreference("assistantPlacement", next),
    [setPreference]
  )
  return [placement, setPlacement] as const
}

/* -------------------------------------------------------------------------- */
/* Messages                                                                   */
/* -------------------------------------------------------------------------- */

export const ASSISTANT_WINDOW_PATH = "/assistant"

const CHANNEL = "forge-assistant"
const WINDOW_NAME = "forge-assistant"
// A chat window's shape, like an organization chat's pop-out: wide enough for the
// conversations beside the thread (the shell's sidebar hides below 1050px).
const WINDOW_SIZE = { width: 1080, height: 720 }
// Room left around it on a smaller screen.
const SCREEN_MARGIN = 48

type Message =
  // Tab → window: what the tab shows; `activeAt` is when the person last
  // used it.
  | { type: "page"; tab: string; activeAt: number; page: PageContext }
  | { type: "tab-closed"; tab: string }
  // Tab → windows: is one open?
  | { type: "ping" }
  // Window → tabs: whether it's open, and whether the agent is replying. A
  // tab tells a window it hasn't heard from what it shows.
  | { type: "window"; window: string; open: boolean; running: boolean }
  // Window → one tab: put the assistant back in your panel, on `thread`.
  | { type: "dock"; tab: string; thread?: string | undefined }
  // Tab → window: show this conversation.
  | { type: "show-thread"; thread: string }

const openChannel = () =>
  typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(CHANNEL)

const post = (message: Message) => {
  const channel = openChannel()
  channel?.postMessage(message)
  channel?.close()
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null

/** A page context from another window, within the limits, or null. */
function readPage(value: unknown): PageContext | null {
  if (
    !isObject(value) ||
    typeof value.path !== "string" ||
    !isObject(value.params) ||
    !isObject(value.search) ||
    !isObject(value.view) ||
    !Array.isArray(value.breadcrumbs) ||
    !Array.isArray(value.entities)
  )
    return null
  try {
    return clipPageContext(value as PageContext)
  } catch {
    return null
  }
}

/* -------------------------------------------------------------------------- */
/* Opening the window                                                         */
/* -------------------------------------------------------------------------- */

// The window this tab opened, to focus it again rather than open another.
let assistantWindow: Window | null = null

const windowFeatures = () => {
  // In the middle of the screen Forge is on.
  const { availWidth, availHeight } = window.screen
  const { availLeft = 0, availTop = 0 } = window.screen as Screen & {
    availLeft?: number
    availTop?: number
  }
  const width = Math.min(WINDOW_SIZE.width, availWidth - SCREEN_MARGIN * 2)
  const height = Math.min(WINDOW_SIZE.height, availHeight - SCREEN_MARGIN * 2)
  return [
    "popup",
    `width=${width}`,
    `height=${height}`,
    `left=${Math.round(availLeft + (availWidth - width) / 2)}`,
    `top=${Math.round(availTop + (availHeight - height) / 2)}`,
  ].join(",")
}

/**
 * Opens the assistant's window, or brings it forward if it's open, on
 * `thread` (an ADK session ID) when one is given. Call it from a click, or
 * the browser blocks it; false when it did.
 */
export function openAssistantWindow(thread?: string): boolean {
  const url = new URL(ASSISTANT_WINDOW_PATH, window.location.origin)
  if (thread) url.searchParams.set("thread", thread)

  // An empty URL finds a window of that name without reloading it, or
  // opens a blank one to send to the assistant.
  const target =
    assistantWindow && !assistantWindow.closed
      ? assistantWindow
      : window.open("", WINDOW_NAME, windowFeatures())
  if (!target) {
    toast.add({
      title: "Couldn't open the assistant's window",
      description:
        "Your browser blocked it. Allow pop-ups for Forge and try again.",
      type: "error",
    })
    return false
  }
  assistantWindow = target

  let blank = true
  try {
    blank = target.location.href === "about:blank"
  } catch {
    // Another origin's window of that name: send it to the assistant.
  }
  if (blank) target.location.replace(url.href)
  else if (thread) post({ type: "show-thread", thread })
  target.focus()
  return true
}

/**
 * Shows a conversation (an ADK session ID) once the conversations have
 * loaded. A conversation that's gone leaves the current one.
 */
export async function showConversation(
  runtime: AssistantRuntime,
  thread: string
) {
  try {
    await runtime.threads.getLoadThreadsPromise()
    if (runtime.threads.mainItem.getState().remoteId !== thread)
      await runtime.threads.switchToThread(thread)
  } catch (error) {
    console.warn("Couldn't open the conversation:", error)
  }
}

/* -------------------------------------------------------------------------- */
/* In Forge's tabs                                                            */
/* -------------------------------------------------------------------------- */

export type AssistantWindowStatus = {
  /** An assistant window is open. */
  open: boolean
  /** The agent is replying in it. */
  running: boolean
}

/** A request from the window to put the assistant back in this tab's panel. */
export type DockRequest = { thread?: string | undefined }

/**
 * Keeps any open assistant window up to date with what this tab shows
 * (`page`), and hears back from it: whether it's open and replying, and
 * when it hands the conversation back to this tab (`dock`). Use it once per
 * tab, in the shell.
 */
export function useAssistantWindowHost(page: CurrentPage) {
  const preferences = useUserPreferencesApi()
  const [windows, setWindows] = React.useState<ReadonlyMap<string, boolean>>(
    () => new Map()
  )
  const [dock, setDock] = React.useState<DockRequest | null>(null)

  React.useEffect(() => {
    const channel = openChannel()
    if (!channel) return
    const tab = crypto.randomUUID()
    let activeAt = document.hasFocus() ? Date.now() : 0
    // Only while a window is open does anything need sending.
    const open = new Set<string>()
    let sent = ""
    let timer: ReturnType<typeof setTimeout> | undefined

    const send = (always: boolean) => {
      if (open.size === 0) return
      const current = page.read()
      const signature = JSON.stringify(current)
      if (!always && signature === sent) return
      sent = signature
      channel.postMessage({
        type: "page",
        tab,
        activeAt,
        page: current,
      } satisfies Message)
    }
    // A navigation changes several stores; send once they've settled.
    const changed = () => {
      clearTimeout(timer)
      timer = setTimeout(() => send(false), 100)
    }
    // Focus, or a click or key press while it has focus (the window may
    // have had it all along), makes this the tab the window follows.
    const used = () => {
      const now = Date.now()
      if (now - activeAt < 2000) return
      activeAt = now
      send(true)
    }
    const focused = () => {
      activeAt = Date.now()
      send(true)
    }
    const visible = () => {
      if (document.visibilityState === "visible" && document.hasFocus())
        focused()
    }
    const closed = () =>
      channel.postMessage({ type: "tab-closed", tab } satisfies Message)
    const shown = (event: PageTransitionEvent) => {
      // Back from the back/forward cache: find the window again.
      if (event.persisted)
        channel.postMessage({ type: "ping" } satisfies Message)
    }

    channel.onmessage = ({ data }: MessageEvent<Message>) => {
      switch (data?.type) {
        case "window": {
          const known = open.has(data.window)
          if (data.open) open.add(data.window)
          else open.delete(data.window)
          setWindows((current) => {
            const next = new Map(current)
            if (data.open) next.set(data.window, data.running)
            else next.delete(data.window)
            return next
          })
          if (data.open && !known) send(true)
          break
        }
        case "dock":
          if (data.tab !== tab) break
          preferences.getState().setPreference("assistantPlacement", "panel")
          setDock({ thread: data.thread })
          window.focus()
          break
      }
    }

    const stop = page.subscribe(changed)
    const options = { capture: true, passive: true }
    window.addEventListener("focus", focused)
    window.addEventListener("pointerdown", used, options)
    window.addEventListener("keydown", used, options)
    document.addEventListener("visibilitychange", visible)
    window.addEventListener("pagehide", closed)
    window.addEventListener("pageshow", shown)
    channel.postMessage({ type: "ping" } satisfies Message)
    return () => {
      stop()
      clearTimeout(timer)
      window.removeEventListener("focus", focused)
      window.removeEventListener("pointerdown", used, options)
      window.removeEventListener("keydown", used, options)
      document.removeEventListener("visibilitychange", visible)
      window.removeEventListener("pagehide", closed)
      window.removeEventListener("pageshow", shown)
      closed()
      channel.close()
    }
  }, [page, preferences])

  const status: AssistantWindowStatus = {
    open: windows.size > 0,
    running: [...windows.values()].some(Boolean),
  }
  const clearDock = React.useCallback(() => setDock(null), [])
  return { status, dock, clearDock }
}

/* -------------------------------------------------------------------------- */
/* In the assistant's window                                                  */
/* -------------------------------------------------------------------------- */

/** A tab's page, when it was last used, and when it last reported. */
type TabPage = { activeAt: number; heardAt: number; page: PageContext }

/**
 * The tab the person used last, and what it shows. Between tabs they
 * haven't used since opening, the one that changed last.
 */
function lastActive(tabs: ReadonlyMap<string, TabPage>) {
  let linked: ({ tab: string } & TabPage) | null = null
  for (const [tab, entry] of tabs) {
    if (
      !linked ||
      entry.activeAt > linked.activeAt ||
      (entry.activeAt === linked.activeAt && entry.heardAt > linked.heardAt)
    )
      linked = { tab, ...entry }
  }
  return linked
}

/**
 * In the assistant's window: the page in the Forge tab the person was in
 * last (null while no tab is open), a stable `read` for `useForgeAgent`,
 * `dock`, which hands the conversation back to that tab's panel, and
 * `setRunning`, which tells the tabs' launchers the agent is replying.
 * `onShowThread` is called when a tab asks for a conversation.
 */
export function useLinkedPage({
  onShowThread,
}: {
  onShowThread: (thread: string) => void
}) {
  const [tabs, setTabs] = React.useState<ReadonlyMap<string, TabPage>>(
    () => new Map()
  )
  const [id] = React.useState(() => crypto.randomUUID())
  const channelRef = React.useRef<BroadcastChannel | null>(null)
  const runningRef = React.useRef(false)
  const onShowThreadRef = React.useRef(onShowThread)
  React.useEffect(() => {
    onShowThreadRef.current = onShowThread
  })

  React.useEffect(() => {
    const channel = openChannel()
    if (!channel) return
    channelRef.current = channel
    const announce = (open: boolean) =>
      channel.postMessage({
        type: "window",
        window: id,
        open,
        running: runningRef.current,
      } satisfies Message)

    channel.onmessage = ({ data }: MessageEvent<Message>) => {
      switch (data?.type) {
        case "page": {
          const page = readPage(data.page)
          if (!page || typeof data.tab !== "string") break
          const activeAt = Number(data.activeAt) || 0
          const heardAt = Date.now()
          setTabs((current) =>
            new Map(current).set(data.tab, { activeAt, heardAt, page })
          )
          break
        }
        case "tab-closed":
          setTabs((current) => {
            if (!current.has(data.tab)) return current
            const next = new Map(current)
            next.delete(data.tab)
            return next
          })
          break
        case "ping":
          announce(true)
          break
        case "show-thread":
          if (typeof data.thread === "string")
            onShowThreadRef.current(data.thread)
          break
      }
    }

    const closed = () => announce(false)
    const shown = (event: PageTransitionEvent) => {
      if (event.persisted) announce(true)
    }
    window.addEventListener("pagehide", closed)
    window.addEventListener("pageshow", shown)
    announce(true)
    return () => {
      window.removeEventListener("pagehide", closed)
      window.removeEventListener("pageshow", shown)
      closed()
      channel.close()
      channelRef.current = null
    }
  }, [id])

  // The tabs' launchers show when the agent is replying here.
  const setRunning = React.useCallback(
    (running: boolean) => {
      if (runningRef.current === running) return
      runningRef.current = running
      channelRef.current?.postMessage({
        type: "window",
        window: id,
        open: true,
        running,
      } satisfies Message)
    },
    [id]
  )

  const linked = lastActive(tabs)

  const pageRef = React.useRef<PageContext | null>(null)
  React.useEffect(() => {
    pageRef.current = linked?.page ?? null
  })
  const read = React.useCallback(() => pageRef.current, [])

  const linkedTab = linked?.tab
  const dock = React.useCallback(
    (thread: string | undefined) => {
      if (!linkedTab) return
      channelRef.current?.postMessage({
        type: "dock",
        tab: linkedTab,
        thread,
      } satisfies Message)
    },
    [linkedTab]
  )

  return { page: linked?.page ?? null, read, dock, setRunning }
}
