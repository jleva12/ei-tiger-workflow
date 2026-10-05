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
  type AdkEvent,
  type AdkStreamCallback,
  type UseAdkRuntimeOptions,
} from "@assistant-ui/react-google-adk"

import {
  defaultModelStateDelta,
  defaultThinkingLevels,
  nearestThinkingLevel,
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
  /**
   * ADK session state sent with every run, read as it's sent: e.g. what the
   * person is looking at, `() => ({ page_context: currentPage() })`. The
   * model section's keys win over the same keys here.
   */
  runState?: () => Record<string, unknown> | undefined
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
  /**
   * Models to choose from in the composer's model section (`showModels`).
   * They may arrive after the first render, e.g. from the agent server.
   */
  models?: AssistantModel[]
  /** Default: the first model. */
  defaultModel?: string
  /**
   * Thinking levels for the model section, from least to most thinking; a
   * model's own `thinkingLevels` narrows them. Default: off, low, medium,
   * high.
   */
  thinkingLevels?: ThinkingLevel[]
  /**
   * Default: "medium" when it's a level, otherwise the first. A model that
   * doesn't offer it gets the nearest level it does.
   */
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
  runState,
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

  // Runs carry `runState` and, while it's shown, the model section's
  // selection as ADK state.
  const selectionRef = useRef<ModelSelection | undefined>(undefined)
  const toStateDeltaRef = useRef(modelStateDelta)
  const runStateRef = useRef(runState)
  useEffect(() => {
    const { model, thinkingLevel } = modelSettings
    selectionRef.current = modelsShown ? { model, thinkingLevel } : undefined
    toStateDeltaRef.current = modelStateDelta
    runStateRef.current = runState
  })
  const send = useCallback<AdkStreamCallback>(
    async function* (messages, config) {
      const selection = selectionRef.current
      try {
        const state = runStateRef.current?.()
        yield* await connection.stream(
          messages,
          selection || state
            ? {
                ...config,
                stateDelta: {
                  ...config.stateDelta,
                  ...state,
                  ...(selection && toStateDeltaRef.current(selection)),
                },
              }
            : config
        )
      } catch (error) {
        // Stop aborts the run; that isn't a failure to show.
        if (config.abortSignal.aborted) throw error
        // The SDK would drop the error and leave the thread silent; as an ADK
        // error event it shows as a failed reply, like the agent's own errors.
        yield requestFailed(error)
      }
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

/** A failed request to the agent server (unreachable, refused, not found) as an ADK error event. */
const requestFailed = (error: unknown): AdkEvent => ({
  id: crypto.randomUUID(),
  errorCode: "REQUEST_FAILED",
  errorMessage: `The assistant couldn't reach the agent: ${
    error instanceof Error ? error.message : String(error)
  }`,
})

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
  // What the person chose; what's selected follows the models as they load
  // or change: the choice while it's one of them, else the default, else
  // the first.
  const [chosenModel, setModel] = useState<string>()
  const [chosenLevel, setThinkingLevel] = useState<string>()
  const listed = (id: string | undefined) =>
    id !== undefined && models.some((option) => option.id === id)
  const model = listed(chosenModel)
    ? chosenModel
    : listed(defaultModel)
      ? defaultModel
      : (models[0]?.id ?? defaultModel)

  // As a string, so a new array with the same levels keeps the same list.
  const offered = models
    .find((option) => option.id === model)
    ?.thinkingLevels?.join(" ")
  const modelLevels = useMemo(() => {
    if (offered === undefined) return thinkingLevels
    const ids = offered.split(" ")
    return thinkingLevels.filter((level) => ids.includes(level.id))
  }, [thinkingLevels, offered])
  const preferred =
    chosenLevel ??
    defaultThinkingLevel ??
    (thinkingLevels.some((level) => level.id === "medium")
      ? "medium"
      : (thinkingLevels[0]?.id ?? ""))
  const thinkingLevel =
    nearestThinkingLevel(modelLevels, thinkingLevels, preferred) ?? preferred

  const [shownBy, setShownBy] = useState(0)
  const show = useCallback(() => {
    setShownBy((count) => count + 1)
    return () => setShownBy((count) => count - 1)
  }, [])

  const settings = useMemo<ModelSettings>(
    () => ({
      models,
      thinkingLevels: modelLevels,
      model,
      thinkingLevel,
      setModel,
      setThinkingLevel,
      show,
    }),
    [models, modelLevels, model, thinkingLevel, show]
  )
  return { settings, shown: shownBy > 0 }
}
