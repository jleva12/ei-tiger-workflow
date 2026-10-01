import * as React from "react"
import type { AdkStreamCallback } from "@assistant-ui/react-google-adk"
import { useRouter } from "@tanstack/react-router"

import {
  extendedThinkingLevels,
  useAdkAssistant,
  type ModelSettings,
} from "@/components/forge/assistant"
import { useShellStoreApi } from "@/components/forge/shell/shell-store"
import { useAssistantModels } from "@/lib/assistant-models"
import {
  clipPageContext,
  mergePageContext,
  primitives,
  usePageContextApi,
  type PageContext,
} from "@/lib/page-context"
import { useUserPreferences } from "@/lib/user-preferences"
import { useWorkspaceScopeApi } from "@/lib/workspace-scope"

declare module "@/lib/user-preferences" {
  interface CustomUserPreferences {
    /**
     * The model the assistant runs on, as the composer's model section last
     * chose it (provider/model); empty until then, and the agent's default
     * when it's gone.
     */
    assistantModel: string
    /** How long it thinks, as the model section last chose it; empty until then. */
    assistantThinkingLevel: string
  }
}

/** Nothing chosen yet: the agent's default model and a medium level. */
export const DEFAULT_ASSISTANT_MODEL_PREFERENCES = {
  assistantModel: "",
  assistantThinkingLevel: "",
}

// The agent server and the agent's app name on it. The admin API serves the
// Forge agent at `/agents`, in the Google ADK API server's protocol;
// VITE_ADK_URL points the assistant at another ADK server (`adk api_server`)
// instead. Without either URL the assistant explains how to connect one.
const API_URL: string | undefined = import.meta.env.VITE_API_URL || undefined
const ADK_URL: string | undefined =
  import.meta.env.VITE_ADK_URL ||
  (API_URL ? `${API_URL.replace(/\/+$/, "")}/agents` : undefined)
export const ADK_APP: string = import.meta.env.VITE_ADK_APP || "forge"
/**
 * The agent is the admin API's own: it says what it has on each page
 * (`useAssistantCapabilities`). Another ADK server doesn't.
 */
export const FORGE_AGENT_SERVER =
  API_URL !== undefined && !import.meta.env.VITE_ADK_URL
// The same local development token the admin API gets, so the agent can
// call Forge (e.g. its workflow tools) as the signed-in user.
const API_TOKEN: string | undefined = import.meta.env.VITE_API_TOKEN

const NOT_CONNECTED =
  "The Forge agent isn't connected yet. Set `VITE_API_URL` (the admin API, " +
  "which serves the agent) or `VITE_ADK_URL` (another Google ADK agent " +
  "server) in `apps/forge-web/.env.local`, then restart `make web`."

/** Until an agent server is configured: one reply saying how to connect it. */
const notConnected: AdkStreamCallback = async function* () {
  yield {
    id: crypto.randomUUID(),
    author: "forge",
    content: { role: "model", parts: [{ text: NOT_CONNECTED }] },
    turnComplete: true,
  }
}

const authHeaders = (): Record<string, string> =>
  API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {}

/** What the person is looking at, read when it's needed. */
export type CurrentPage = {
  /** The page now, within the server's limits. */
  read: () => PageContext
  /** Calls `listener` whenever something the page is read from changes. */
  subscribe: (listener: () => void) => () => void
}

/**
 * What the person is looking at: the route, the page's title and crumbs,
 * the workspace scope and what the page declared (`usePageContext`). It
 * reads each store as it's called, so nothing re-renders as they change.
 * Only inside the app shell; the assistant's own window gets the page from
 * the shell's tabs instead (`useLinkedPage`).
 */
export function useCurrentPage(): CurrentPage {
  const router = useRouter()
  const shell = useShellStoreApi()
  const scope = useWorkspaceScopeApi()
  const page = usePageContextApi()
  return React.useMemo(
    () => ({
      read: () => {
        const { location, matches } = router.state
        const leaf = matches.at(-1)
        const { header } = shell.getState().shell
        return clipPageContext({
          path: location.pathname,
          route: leaf?.routeId,
          params: (leaf?.params ?? {}) as Record<string, string>,
          search: primitives(location.search as Record<string, unknown>),
          title: header.title,
          breadcrumbs: header.breadcrumbs.map((crumb) => crumb.label),
          scope: scope.getState().scope,
          ...mergePageContext(page.getState().layers),
        })
      },
      subscribe: (listener) => {
        const stops = [
          router.subscribe("onResolved", listener),
          shell.subscribe(listener),
          scope.subscribe(listener),
          page.subscribe(listener),
        ]
        return () => stops.forEach((stop) => stop())
      },
    }),
    [router, shell, scope, page]
  )
}

/**
 * The page `current` reads, as React state: a new value only when something
 * in it changed, so it can key queries.
 */
export function usePageSnapshot(current: CurrentPage): PageContext {
  const cache = React.useRef<{ key: string; page: PageContext } | null>(null)
  const snapshot = React.useCallback(() => {
    const page = current.read()
    const key = JSON.stringify(page)
    if (cache.current?.key !== key) cache.current = { key, page }
    return cache.current.page
  }, [current])
  return React.useSyncExternalStore(current.subscribe, snapshot, snapshot)
}

/**
 * The composer's model section, for the assistant's props: shown while the
 * agent offers models. The person's choices are saved as preferences, so
 * every surface and the next visit start from them.
 */
export type ForgeModelSection =
  { showModels: true; modelSettings: ModelSettings } | { showModels: false }

function useSavedModelSettings(settings: ModelSettings): ModelSettings {
  const setPreference = useUserPreferences((state) => state.setPreference)
  return React.useMemo(
    () => ({
      ...settings,
      setModel: (id: string) => {
        settings.setModel(id)
        setPreference("assistantModel", id)
      },
      setThinkingLevel: (id: string) => {
        settings.setThinkingLevel(id)
        setPreference("assistantThinkingLevel", id)
      },
    }),
    [settings, setPreference]
  )
}

/**
 * The Forge agent for the signed-in user (`userId`, whose conversations it
 * shows), sending what they're looking at (`readPage`, e.g.
 * `useCurrentPage().read`; null when there's no page) with each message.
 * Every assistant surface uses it: the floating panel, pages with the agent
 * built in and the assistant's own window. `sharing` is whether the page
 * goes with messages, for `PageContextChip` at the top of the composer;
 * `modelSection` goes to the assistant's props, for its model and thinking
 * pickers.
 */
export function useForgeAgent(
  userId: string,
  readPage: () => PageContext | null
) {
  const [sharing, setSharing] = React.useState(true)
  // The models the admin API's agent may run on, its default first.
  const { models, defaultModel } = useAssistantModels(
    ADK_APP,
    FORGE_AGENT_SERVER && Boolean(userId)
  )
  const savedModel = useUserPreferences((state) => state.assistantModel)
  const savedLevel = useUserPreferences((state) => state.assistantThinkingLevel)
  const { runtime, artifacts, modelSettings } = useAdkAssistant(
    ADK_URL
      ? {
          // ADK keeps each user's conversations apart by this ID.
          adk: { url: ADK_URL, appName: ADK_APP, userId },
          headers: authHeaders,
          // Where they are with every message; null clears the last one
          // while they're not sharing it.
          runState: () => ({ page_context: sharing ? readPage() : null }),
          // Sent as the run's `model` and `thinking_level` state while the
          // model section shows. A saved model that's gone falls back to the
          // first, the agent's default.
          models,
          defaultModel: savedModel || defaultModel,
          thinkingLevels: extendedThinkingLevels,
          defaultThinkingLevel: savedLevel || undefined,
        }
      : { stream: notConnected }
  )
  const saved = useSavedModelSettings(modelSettings)
  const modelSection: ForgeModelSection =
    ADK_URL && models.length > 0
      ? { showModels: true, modelSettings: saved }
      : { showModels: false }
  return {
    runtime,
    artifacts,
    modelSection,
    /** An agent server is configured; without one it explains how to connect it. */
    connected: ADK_URL !== undefined,
    sharing,
    setSharing,
  }
}
