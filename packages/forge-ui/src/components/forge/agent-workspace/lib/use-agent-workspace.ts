import * as React from "react"
import type { AdkStreamCallback } from "@assistant-ui/react-google-adk"

import {
  extendedThinkingLevels,
  useAdkAssistant,
  type AssistantFeedback,
  type ModelSettings,
} from "@/components/forge/assistant"
import { ChatAttachmentAdapter } from "@/lib/attachments"

import { useAgentTools } from "./agent-tools"
import { useAssistantModels } from "./assistant-models"

/** Where the agent is: what every connected workspace part takes. */
export type AgentConnection = {
  /**
   * An ADK API server (`adk api_server`), or the chat API's `/agents`, which
   * serves its agent in the same protocol. Undefined: not connected.
   */
  adkUrl: string | undefined
  /** The agent's app name on the server. */
  appName: string
  /** Whose conversations these are; the server keeps each user's apart. */
  userId: string
}

export type AgentWorkspaceOptions = AgentConnection & {
  /**
   * The server is the chat API (adk-chat), which also lists the agent's
   * models (`/apps/{app}/models`) and tools (`/apps/{app}/tools`) and stores
   * 👍/👎 feedback. Leave it off for a plain ADK API server.
   */
  chatApi?: boolean
  /** The assistant's reply while `adkUrl` is undefined. */
  notConnectedMessage?: string
}

/**
 * The composer's model section, for the assistant's props: shown while the
 * agent offers models.
 */
export type ModelSection =
  { showModels: true; modelSettings: ModelSettings } | { showModels: false }

// Images, PDFs and text files on messages (the composer's + button). One
// object, so the runtime isn't rebuilt on every render.
const ADAPTERS = { attachments: new ChatAttachmentAdapter() }

const DEFAULT_NOT_CONNECTED =
  "The agent isn't connected yet. Point the app at an ADK agent server " +
  "(`adkUrl`) and reload."

// The model section's last choices, so the next visit starts from them.
const MODEL_KEY = "assistant.model"
const THINKING_KEY = "assistant.thinkingLevel"

function readSaved(key: string): string {
  try {
    return localStorage.getItem(key) ?? ""
  } catch {
    return ""
  }
}

function save(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Storage may be unavailable (private mode); the choice lasts the visit.
  }
}

/** Saves a 👍/👎 on a reply with the chat API, by session and message. */
async function saveFeedback(
  { adkUrl, appName, userId }: AgentConnection,
  feedback: AssistantFeedback
) {
  if (!adkUrl || !feedback.sessionId) return
  const url = `${adkUrl}/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(feedback.sessionId)}/feedback`
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      messageId: feedback.message.id,
      rating: feedback.type,
      text: feedback.text,
      comment: feedback.comment,
    }),
  })
  if (!response.ok) console.warn("Couldn't save feedback", response.status)
}

function useSavedModelSettings(settings: ModelSettings): ModelSettings {
  return React.useMemo(
    () => ({
      ...settings,
      setModel: (id: string) => {
        settings.setModel(id)
        save(MODEL_KEY, id)
      },
      setThinkingLevel: (id: string) => {
        settings.setThinkingLevel(id)
        save(THINKING_KEY, id)
      },
    }),
    [settings]
  )
}

/**
 * The assistant for an ADK agent, with the workspace's extras: the models
 * and tools the chat API offers (remembered between visits), attachments,
 * and 👍/👎 feedback. Spread `modelSection` into `AssistantScreen`; pass
 * `tools` to `ToolsMenu` and `catalog` / `defaultModel` to `ContextMeter`.
 *
 * ```tsx
 * const agent = useAgentWorkspace({ adkUrl, appName: "assistant", userId, chatApi: true })
 * <AssistantScreen runtime={agent.runtime} artifacts={agent.artifacts} {...agent.modelSection} />
 * ```
 */
export function useAgentWorkspace({
  adkUrl,
  appName,
  userId,
  chatApi = false,
  notConnectedMessage = DEFAULT_NOT_CONNECTED,
}: AgentWorkspaceOptions) {
  const serverLists = chatApi && Boolean(adkUrl) && Boolean(userId)
  // The models the agent may run on, its default first.
  const { models, defaultModel, catalog } = useAssistantModels(
    adkUrl,
    appName,
    serverLists
  )
  // The tools it may use, and which are switched on.
  const tools = useAgentTools(adkUrl, appName, serverLists)
  const { enabledNames } = tools
  const [savedModel] = React.useState(() => readSaved(MODEL_KEY))
  const [savedLevel] = React.useState(() => readSaved(THINKING_KEY))

  const notConnected = React.useMemo<AdkStreamCallback>(
    () =>
      async function* () {
        yield {
          id: crypto.randomUUID(),
          author: appName,
          content: { role: "model", parts: [{ text: notConnectedMessage }] },
          turnComplete: true,
        }
      },
    [appName, notConnectedMessage]
  )

  const { runtime, artifacts, modelSettings } = useAdkAssistant(
    adkUrl
      ? {
          // ADK keeps each user's conversations apart by this ID.
          adk: { url: adkUrl, appName, userId },
          // The tools switched on, as the run's `tools` state; left out (all
          // of them) until the list arrives.
          runState: () => ({ tools: enabledNames }),
          adapters: ADAPTERS,
          // Shows 👍/👎 on replies; the chat API saves each rating.
          ...(chatApi && {
            onFeedback: (feedback: AssistantFeedback) =>
              saveFeedback({ adkUrl, appName, userId }, feedback),
          }),
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
  const modelSection: ModelSection =
    adkUrl && models.length > 0
      ? { showModels: true, modelSettings: saved }
      : { showModels: false }
  return {
    runtime,
    artifacts,
    modelSection,
    tools,
    /** The models as the server lists them, with context windows and prices. */
    catalog,
    defaultModel,
    /** An agent server is configured; without one it explains how to connect. */
    connected: adkUrl !== undefined,
  }
}
