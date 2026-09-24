import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  WebSpeechDictationAdapter,
  type AssistantRuntime,
  type DictationAdapter,
  type FeedbackAdapter,
  type ThreadMessage,
} from "@assistant-ui/react"
import {
  createAdkSessionAdapter,
  createAdkStream,
  useAdkRuntime,
  type AdkStreamCallback,
  type UseAdkRuntimeOptions,
} from "@assistant-ui/react-google-adk"

import {
  defaultModelStateDelta,
  defaultThinkingLevels,
  type AssistantModel,
  type ModelSelection,
  type ModelSettings,
  type ThinkingLevel,
} from "./model-settings"

type Headers = Record<string, string> | (() => Record<string, string>)

/** Fetch, list and delete a session's artifacts (direct ADK connections). */
export type AdkArtifactsApi = ReturnType<
  typeof createAdkSessionAdapter
>["artifacts"]

export type UseAdkAssistantOptions = Omit<
  UseAdkRuntimeOptions,
  "stream" | "sessionAdapter"
> & {
  /**
   * Proxy mode: your own API route, e.g. one built with `createAdkApiRoute`
   * from `@assistant-ui/react-google-adk/server`.
   */
  api?: string
  /**
   * Direct mode: an ADK server (`adk api_server`, `adk web`). Its sessions
   * become the thread list, and artifacts can be fetched.
   */
  adk?: { url: string; appName: string; userId: string }
  /** Sent with every request; a function is read per request. */
  headers?: Headers
  /** A custom transport instead of `api` / `adk` (tests, mocks). */
  stream?: AdkStreamCallback
  /**
   * Voice input for the composer's mic button; the words appear in the input
   * as they're recognised. Default: the browser's own speech recognition
   * (Web Speech API), where the browser has it. `false` turns it off; any
   * assistant-ui `DictationAdapter` (e.g. a server speech-to-text) replaces it.
   */
  dictation?: boolean | DictationAdapter
  /**
   * Shows thumbs up / down on the agent's replies and receives each rating,
   * e.g. to store it next to the ADK session. Without it the buttons are
   * hidden.
   */
  onFeedback?: (feedback: AssistantFeedback) => void | Promise<void>
  /** Models to choose from in the composer's model section (`showModels`). */
  models?: AssistantModel[]
  /** Default: the first model. */
  defaultModel?: string
  /** Thinking levels for the model section. Default: off, low, medium, high. */
  thinkingLevels?: ThinkingLevel[]
  /** Default: "medium" when it's a level, otherwise the first. */
  defaultThinkingLevel?: string
  /**
   * Turns the model section's selection into the ADK `stateDelta` sent with
   * each run. Default: `{ model, thinking_level }`.
   */
  modelStateDelta?: (selection: ModelSelection) => Record<string, unknown>
}

/**
 * The assistant-ui runtime for a Google ADK agent. Pass the result to
 * `AssistantScreen`: `runtime`, `artifacts` and the model section's
 * `modelSettings`.
 */
export function useAdkAssistant({
  api,
  adk,
  headers,
  stream,
  dictation = true,
  onFeedback,
  models = noModels,
  defaultModel,
  thinkingLevels = defaultThinkingLevels,
  defaultThinkingLevel,
  modelStateDelta = defaultModelStateDelta,
  ...options
}: UseAdkAssistantOptions) {
  // Headers may change (a refreshed token) without rebuilding the transport:
  // the getter is created once and reads the latest headers per request.
  const headersRef = useRef(headers)
  useEffect(() => {
    headersRef.current = headers
  })
  const [getHeaders] = useState(() => () => {
    const current = headersRef.current
    return typeof current === "function" ? current() : (current ?? {})
  })

  const { url, appName, userId } = adk ?? {}
  const connection = useMemo(() => {
    if (stream) return { stream }
    if (url && appName && userId) {
      const session = createAdkSessionAdapter({
        apiUrl: url,
        appName,
        userId,
        headers: getHeaders,
      })
      return {
        stream: createAdkStream({
          api: url,
          appName,
          userId,
          headers: getHeaders,
        }),
        sessionAdapter: session.adapter,
        load: session.load,
        artifacts: session.artifacts,
      }
    }
    if (api) return { stream: createAdkStream({ api, headers: getHeaders }) }
    throw new Error(
      "useAdkAssistant needs `api` (your route), `adk` (an ADK server) or `stream`."
    )
  }, [stream, url, appName, userId, api, getHeaders])

  const { settings: modelSettings, shown: modelsShown } = useModelSettings({
    models,
    defaultModel,
    thinkingLevels,
    defaultThinkingLevel,
  })

  // Runs carry the model section's selection as ADK state while it's shown.
  const selectionRef = useRef<ModelSelection | undefined>(undefined)
  const toStateDeltaRef = useRef(modelStateDelta)
  useEffect(() => {
    const { model, thinkingLevel } = modelSettings
    selectionRef.current = modelsShown ? { model, thinkingLevel } : undefined
    toStateDeltaRef.current = modelStateDelta
  })
  const send = useCallback<AdkStreamCallback>(
    (messages, config) => {
      const selection = selectionRef.current
      if (!selection) return connection.stream(messages, config)
      return connection.stream(messages, {
        ...config,
        stateDelta: {
          ...config.stateDelta,
          ...toStateDeltaRef.current(selection),
        },
      })
    },
    [connection]
  )

  // Created once: the browser adapter starts a fresh recognition per session.
  const [browserDictation] = useState(() =>
    WebSpeechDictationAdapter.isSupported()
      ? new WebSpeechDictationAdapter()
      : undefined
  )
  const dictationAdapter =
    dictation === true ? browserDictation : dictation || undefined
  // Created once: reads the latest handler and session for each rating.
  const onFeedbackRef = useRef(onFeedback)
  const runtimeRef = useRef<AssistantRuntime | null>(null)
  // Only direct connections know ADK session ids; other threads are local.
  const sessionsRef = useRef(false)
  const [feedbackAdapter] = useState<FeedbackAdapter>(() => ({
    submit: ({ message, type, comment }) => {
      const feedback: AssistantFeedback = {
        type,
        message,
        text: messageText(message),
        sessionId: sessionsRef.current
          ? runtimeRef.current?.threads.mainItem.getState().remoteId
          : undefined,
        ...(comment && { comment }),
      }
      Promise.resolve()
        .then(() => onFeedbackRef.current?.(feedback))
        .catch((error: unknown) => console.error("Feedback failed:", error))
    },
  }))
  const hasFeedback = Boolean(onFeedback)

  const adapters = useMemo(
    () => ({
      dictation: dictationAdapter,
      ...(hasFeedback && { feedback: feedbackAdapter }),
      ...options.adapters,
    }),
    [dictationAdapter, hasFeedback, feedbackAdapter, options.adapters]
  )

  const runtime = useAdkRuntime({
    ...options,
    adapters,
    stream: send,
    sessionAdapter: connection.sessionAdapter,
    load: options.load ?? connection.load,
  })
  useEffect(() => {
    onFeedbackRef.current = onFeedback
    runtimeRef.current = runtime
    sessionsRef.current = Boolean(connection.sessionAdapter)
  })

  return {
    runtime,
    artifacts: connection.artifacts,
    modelSettings,
  }
}

/** A thumbs up or down on one of the agent's replies. */
export type AssistantFeedback = {
  type: "positive" | "negative"
  /** The rated reply. */
  message: ThreadMessage
  /** The reply's text, for logs and review queues. */
  text: string
  /** The reply's ADK session, for direct `adk` connections once it exists. */
  sessionId: string | undefined
  /** A comment, when the UI collects one. */
  comment?: string
}

const messageText = (message: ThreadMessage) =>
  message.content
    .flatMap((part) => (part.type === "text" ? [part.text] : []))
    .join("\n\n")

const noModels: AssistantModel[] = []

function useModelSettings({
  models,
  defaultModel,
  thinkingLevels,
  defaultThinkingLevel,
}: {
  models: AssistantModel[]
  defaultModel: string | undefined
  thinkingLevels: ThinkingLevel[]
  defaultThinkingLevel: string | undefined
}) {
  const [model, setModel] = useState(defaultModel ?? models[0]?.id)
  const [thinkingLevel, setThinkingLevel] = useState(
    () =>
      defaultThinkingLevel ??
      (thinkingLevels.some((level) => level.id === "medium")
        ? "medium"
        : (thinkingLevels[0]?.id ?? ""))
  )
  const [shownBy, setShownBy] = useState(0)
  const show = useCallback(() => {
    setShownBy((count) => count + 1)
    return () => setShownBy((count) => count - 1)
  }, [])

  const settings = useMemo<ModelSettings>(
    () => ({
      models,
      thinkingLevels,
      model,
      thinkingLevel,
      setModel,
      setThinkingLevel,
      show,
    }),
    [models, thinkingLevels, model, thinkingLevel, show]
  )
  return { settings, shown: shownBy > 0 }
}
