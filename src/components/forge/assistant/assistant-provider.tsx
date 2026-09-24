import * as React from "react"
import {
  AssistantRuntimeProvider,
  AuiConfig,
  Suggestions,
  Tools,
  useAui,
  useAuiState,
  type AssistantRuntime,
} from "@assistant-ui/react"

import { Thread } from "@/components/assistant-ui/elements/thread.aui"

import { AdkMessageAuthor, AdkModelSection, AdkToolFallback } from "./adk"
import { adkToolkit } from "./adk-toolkit"
import {
  AssistantSettingsContext,
  useAssistantSettings,
  type AssistantWelcome,
  type AuthRequestHandler,
} from "./assistant-context"
import { AssistantCommandMenu, type AssistantCommand } from "./commands"
import type { ModelSettings } from "./model-settings"
import type { AdkArtifactsApi } from "./use-adk-assistant"

type SuggestionList = Parameters<typeof Suggestions>[0]
type Toolkit = NonNullable<Parameters<typeof Tools>[0]>["toolkit"]

/** What every assistant surface (screen, modal) is configured with. */
export type AssistantOptions = {
  /** From `useAdkAssistant` (or any assistant-ui runtime). */
  runtime: AssistantRuntime
  /** From `useAdkAssistant`; enables artifact downloads. */
  artifacts?: AdkArtifactsApi
  /** Completes ADK sign-in requests; see `AuthRequestHandler`. */
  onAuthRequest?: AuthRequestHandler
  /** The assistant's name. Default "Assistant". */
  title?: string
  /** Shown above the composer while a conversation is empty. */
  welcome?: AssistantWelcome
  /** Starter prompts for an empty conversation. */
  suggestions?: SuggestionList
  /** Your tool UIs, merged over the ADK ones (`defineToolkit`). */
  toolkit?: Toolkit
  /** `/` commands in the composer; see `AssistantCommand`. */
  commands?: AssistantCommand[]
} & (
  | { showModels?: false; modelSettings?: ModelSettings }
  | {
      /**
       * Shows the model section in the composer: the agent's name, its model
       * (when there's more than one) and the thinking level.
       */
      showModels: true
      /** From `useAdkAssistant`: the choices, and what runs send. */
      modelSettings: ModelSettings
    }
)

const titleFrom = (text: string) => {
  const line = text.trim().split("\n")[0] ?? ""
  return line.length > 60 ? `${line.slice(0, 57).trimEnd()}…` : line
}

/**
 * ADK sessions have no titles, so a conversation is named after its first
 * message once it exists on the server. Adapters that persist titles keep it.
 */
function AutoTitle() {
  const aui = useAui()
  const untitled = useAuiState((s) => {
    const item = s.threads.threadItems.find(
      (t) => t.id === s.threads.mainThreadId
    )
    return Boolean(item?.remoteId) && !item?.title
  })
  const firstMessage = useAuiState((s) => {
    const message = s.thread.messages.find((m) => m.role === "user")
    const part = message?.content.find((p) => p.type === "text")
    return part?.type === "text" ? part.text : undefined
  })
  React.useEffect(() => {
    if (untitled && firstMessage) {
      aui.threads.item("main").rename(titleFrom(firstMessage))
    }
  }, [aui, untitled, firstMessage])
  return null
}

/**
 * The runtime, the ADK tool UIs and the settings every assistant part reads.
 * `AssistantScreen` and `AssistantModal` render it for you; use it directly
 * to lay the parts out yourself.
 */
function AssistantProvider({
  runtime,
  toolkit,
  suggestions,
  artifacts,
  onAuthRequest,
  title = "Assistant",
  welcome,
  commands,
  showModels,
  modelSettings,
  children,
}: AssistantOptions & { children: React.ReactNode }) {
  const config = React.useMemo(
    () =>
      AuiConfig({
        tools: Tools({ toolkit: { ...adkToolkit, ...toolkit } }),
        ...(suggestions && { suggestions: Suggestions(suggestions) }),
      }),
    [toolkit, suggestions]
  )
  const settings = React.useMemo(
    () => ({
      onAuthRequest,
      artifacts,
      welcome,
      title,
      commands,
      showModels,
      modelSettings,
    }),
    [
      onAuthRequest,
      artifacts,
      welcome,
      title,
      commands,
      showModels,
      modelSettings,
    ]
  )

  return (
    <AssistantSettingsContext.Provider value={settings}>
      <AssistantRuntimeProvider runtime={runtime} config={config}>
        <AutoTitle />
        {children}
      </AssistantRuntimeProvider>
    </AssistantSettingsContext.Provider>
  )
}

function AssistantWelcomeMessage() {
  const { welcome } = useAssistantSettings()
  return (
    <div
      data-slot="assistant-welcome"
      className="mb-6 flex flex-col gap-2 px-2"
    >
      <p className="text-[1.3125rem] font-medium tracking-[-.5px] text-foreground">
        {welcome?.title ?? "How can I help?"}
      </p>
      {welcome?.description && (
        <p className="text-sm text-muted-foreground">{welcome.description}</p>
      )}
    </div>
  )
}

const threadComponents = {
  Welcome: AssistantWelcomeMessage,
  ToolFallback: AdkToolFallback,
  MessageHeader: AdkMessageAuthor,
}

/**
 * The conversation and composer with the Forge and ADK parts: welcome, ADK
 * tool cards, agent labels, and the composer's model section and `/`
 * commands when they're configured. Render it inside `AssistantProvider`.
 */
function AssistantThread() {
  const { showModels, commands } = useAssistantSettings()
  const hasCommands = Boolean(commands?.length)
  const components = React.useMemo(
    () => ({
      ...threadComponents,
      ...(showModels && { ComposerActions: AdkModelSection }),
      ...(hasCommands && { ComposerTriggers: AssistantCommandMenu }),
    }),
    [showModels, hasCommands]
  )
  return (
    <Thread
      components={components}
      placeholder={
        hasCommands ? "Send a message, or type / for commands..." : undefined
      }
    />
  )
}

export { AssistantProvider, AssistantThread }
