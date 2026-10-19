import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  WebSpeechDictationAdapter,
  type AssistantRuntime,
  type AttachmentAdapter,
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
import { noteWholeReply } from "@/components/assistant-ui/elements/whole-replies"

import { defaultAttachments } from "./attachments"
import { followUpsRequest, type FollowUpsOption } from "./follow-ups"
import {
  defaultModelStateDelta,
  defaultThinkingLevels,
  nearestThinkingLevel,
  type AssistantModel,
  type ModelSelection,
  type ModelSettings,
  type ThinkingLevel,
} from "./model-settings"
import { withSessionTitles } from "./session-titles"
import {
  createHistoryFeedback,
  registerHistoryFeedback,
} from "./history-feedback"
import { createAdkHistoryLoader } from "./history-snapshot"
import { CLIENT_TURN_STATE_KEY, withTurnLifecycle } from "./turn-lifecycle"

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
  /** Require Forge's explicit invocation outcome; enable for Forge runtime connections. */
  requireTurnOutcome?: boolean
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
   * What the composer's attach button takes. Default: images and documents
   * (PDF, Word, PowerPoint) as inline parts of the message, which ADK passes
   * to the model, and text files (txt, md, csv, html, json) as text
   * (`./attachments`). `false` turns attaching off; any assistant-ui
   * `AttachmentAdapter` replaces it.
   */
  attachments?: boolean | AttachmentAdapter
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
  /**
   * Asks the agent server for follow-up questions after each reply: every run
   * sends `forge:suggest_follow_ups` in its state (true, or `{ count }`, 1–5),
   * and once the turn ends with a reply, the server's model writes a few
   * messages the person might send next (forge_common's `FollowUpsPlugin`,
   * in Forge's agent runtimes). The thread shows them above the composer as
   * chips that send them. A server that doesn't know it ignores it, or refuses
   * the key if it checks the state it's sent.
   */
  suggestFollowUps?: FollowUpsOption
  /**
   * Sees each event of a run as it streams in, before the thread shows it:
   * the agent's and its sub-agents' messages, tool calls and tool results
   * (`content.parts[].functionResponse`), as the ADK server sent them. For
   * side effects of what the agent did, e.g. reloading a record a tool
   * changed. Not called for a conversation's history as it loads.
   */
  onEvent?: (event: AdkEvent) => void
  /**
   * Whether replies show as the model writes them (default true). False
   * shows each reply once it's done, as from an agent server that doesn't
   * stream, and writes it in word by word: the run's partial events are
   * left out. To see a chat page as its visitors will, when the server
   * streams for you.
   */
  streaming?: boolean
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
  requireTurnOutcome = false,
  dictation = true,
  attachments = true,
  onFeedback,
  models = noModels,
  defaultModel,
  thinkingLevels = defaultThinkingLevels,
  defaultThinkingLevel,
  modelStateDelta = defaultModelStateDelta,
  suggestFollowUps,
  onEvent,
  streaming = true,
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
    const history = createHistoryFeedback()
    if (stream) return { stream, history }
    if (url && appName && userId) {
      const session = createAdkSessionAdapter({
        apiUrl: url,
        appName,
        userId,
        headers: getHeaders,
      })
      return {
        history,
        stream: createAdkStream({
          api: url,
          appName,
          userId,
          headers: getHeaders,
        }),
        // Listed with their titles, which the server keeps (`session-titles`).
        sessionAdapter: withSessionTitles(session.adapter, {
          url,
          appName,
          userId,
          headers: getHeaders,
        }),
        load: createAdkHistoryLoader({
          url,
          appName,
          userId,
          headers: getHeaders,
        }),
        artifacts: session.artifacts,
      }
    }
    if (api)
      return { history, stream: createAdkStream({ api, headers: getHeaders }) }
    throw new Error(
      "useAdkAssistant needs `api` (your route), `adk` (an ADK server) or `stream`."
    )
  }, [stream, url, appName, userId, api, getHeaders])

  const history = connection.history
  const sessionAdapter = useMemo(
    () =>
      connection.sessionAdapter
        ? history.wrapAdapter(connection.sessionAdapter)
        : undefined,
    [connection, history]
  )
  const load = useMemo(() => {
    const loader = options.load ?? connection.load
    return loader ? history.wrapLoad(loader) : undefined
  }, [options.load, connection, history])

  const { settings: modelSettings, shown: modelsShown } = useModelSettings({
    models,
    defaultModel,
    thinkingLevels,
    defaultThinkingLevel,
  })

  // Runs carry `runState`, the request for follow-ups when they're on and,
  // while it's shown, the model section's selection as ADK state.
  const selectionRef = useRef<ModelSelection | undefined>(undefined)
  const toStateDeltaRef = useRef(modelStateDelta)
  const runStateRef = useRef(runState)
  const followUpsRef = useRef(suggestFollowUps)
  const onEventRef = useRef(onEvent)
  const streamingRef = useRef(streaming)
  useEffect(() => {
    const { model, thinkingLevel } = modelSettings
    selectionRef.current = modelsShown ? { model, thinkingLevel } : undefined
    toStateDeltaRef.current = modelStateDelta
    runStateRef.current = runState
    followUpsRef.current = suggestFollowUps
    onEventRef.current = onEvent
    streamingRef.current = streaming
  })
  const send = useCallback<AdkStreamCallback>(
    async function* (messages, config) {
      const streamWithLifecycle = withTurnLifecycle(
        async function* (messages, config) {
          if (load) {
            const session = await config.initialize()
            history.assertReady(session.externalId ?? session.remoteId)
          }
          const selection = selectionRef.current
          const stateDelta = {
            ...config.stateDelta,
            ...runStateRef.current?.(),
            ...followUpsRequest(followUpsRef.current),
            ...(selection && toStateDeltaRef.current(selection)),
          }
          // This key is response-only, including when a caller copied session state.
          delete stateDelta[CLIENT_TURN_STATE_KEY]
          const events = await connection.stream(messages, {
            ...config,
            stateDelta,
          })
          // A server that streams sends partial events as the model writes;
          // one that doesn't sends each reply whole once it's done, and the
          // thread writes those in word by word, as if they streamed.
          let streamed = false
          for await (const event of events) {
            if (event.partial && !streamingRef.current) continue
            try {
              onEventRef.current?.(event)
            } catch (error) {
              // A watcher's mistake never stops the reply.
              console.error("onEvent failed", error)
            }
            if (event.partial) streamed = true
            else if (!streamed) noteWholeReplies(event)
            yield event
          }
        },
        { requireTurnOutcome }
      )
      yield* await streamWithLifecycle(messages, config)
    },
    [connection, requireTurnOutcome, history, load]
  )

  // Created once: the browser adapter starts a fresh recognition per session.
  const [browserDictation] = useState(() =>
    WebSpeechDictationAdapter.isSupported()
      ? new WebSpeechDictationAdapter()
      : undefined
  )
  const dictationAdapter =
    dictation === true ? browserDictation : dictation || undefined
  // Created once, like the dictation adapter.
  const [defaultAttachmentAdapter] = useState(defaultAttachments)
  const attachmentAdapter =
    attachments === true ? defaultAttachmentAdapter : attachments || undefined
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
      attachments: attachmentAdapter,
      ...(hasFeedback && { feedback: feedbackAdapter }),
      ...options.adapters,
    }),
    [
      dictationAdapter,
      attachmentAdapter,
      hasFeedback,
      feedbackAdapter,
      options.adapters,
    ]
  )

  const runtime = useAdkRuntime({
    ...options,
    adapters,
    stream: send,
    sessionAdapter,
    load,
  })
  useEffect(() => registerHistoryFeedback(runtime, history), [runtime, history])
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

/** The agent's text in an event that arrived whole, for `MarkdownText`. */
function noteWholeReplies(event: AdkEvent) {
  if (event.author === "user") return
  for (const part of event.content?.parts ?? []) {
    if (part.text && !part.thought) noteWholeReply(part.text)
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
