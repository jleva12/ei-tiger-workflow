import * as React from "react"
import type { RemoteThreadListAdapter } from "@assistant-ui/react"
import {
  AdkEventAccumulator,
  useAdkRuntime,
  type AdkEvent,
  type AdkStreamCallback,
  type AdkThreadSnapshot,
} from "@assistant-ui/react-google-adk"

import {
  AssistantProvider,
  AssistantThread,
} from "@/components/forge/assistant/index"
import { showConversation } from "@/features/assistant/lib/assistant-window"
import { useAssistantAgents } from "@/features/assistant/lib/assistant-capabilities"
import { assistantSources } from "@/features/knowledge/lib/sources"
import {
  ASSISTANT_APP,
  type ConversationDetail,
} from "@/features/conversations/lib/conversations"
import { useMe } from "@/lib/users"

/*
 * A conversation as the person saw it: its ADK events replayed into the same
 * chat thread the assistant and the agents' chat draw (`AssistantThread`),
 * its messages, tool cards, agents' names and cited sources as they appeared,
 * with nothing to send. The thread is the conversation's ADK session, read
 * through ADK's own event accumulator, as the chat reads a session it
 * reopens.
 */

/** Nothing is sent from a replay: the thread is disabled, and this says why if anything tries. */
const nothingToSend: AdkStreamCallback = () =>
  Promise.reject(new Error("This conversation is read only."))

/** What ADK's session load makes of the events: the whole thread, and its state. */
function snapshotOf(
  events: readonly Record<string, unknown>[]
): AdkThreadSnapshot {
  if (!events.length) return { messages: [] }
  const accumulator = new AdkEventAccumulator()
  let messages: AdkThreadSnapshot["messages"] = []
  for (const event of events)
    messages = accumulator.processEvent(event as AdkEvent)
  return {
    messages,
    longRunningToolIds: accumulator.getLongRunningToolIds(),
    toolConfirmations: accumulator.getToolConfirmations(),
    authRequests: accumulator.getAuthRequests(),
    escalated: accumulator.isEscalated(),
    messageMetadata: accumulator.getMessageMetadata(),
    stateDelta: accumulator.getStateDelta(),
    artifactDelta: accumulator.getArtifactDelta(),
    agentInfo: accumulator.getAgentInfo(),
  }
}

/** A thread list of one: the conversation, which nothing renames, archives or deletes. */
function readOnlyThreads(
  sessionId: string,
  title: string
): RemoteThreadListAdapter {
  const thread = {
    status: "regular" as const,
    remoteId: sessionId,
    externalId: sessionId,
    title,
  }
  const refuse = () =>
    Promise.reject(new Error("This conversation is read only."))
  return {
    list: async () => ({ threads: [thread] }),
    fetch: async () => thread,
    initialize: refuse,
    delete: refuse,
    rename: async () => {},
    archive: async () => {},
    unarchive: async () => {},
    generateTitle: () =>
      Promise.resolve(
        new ReadableStream({
          start(controller) {
            controller.close()
          },
        })
      ),
  } as unknown as RemoteThreadListAdapter
}

function Replay({ detail }: { detail: ConversationDetail }) {
  const snapshot = React.useMemo(
    () => snapshotOf(detail.adk_events),
    [detail.adk_events]
  )
  const sessionAdapter = React.useMemo(
    () => readOnlyThreads(detail.session_id, detail.agent_name),
    [detail.session_id, detail.agent_name]
  )
  // The latest snapshot, read when the thread (re)loads.
  const latest = React.useRef(snapshot)
  React.useEffect(() => {
    latest.current = snapshot
  })
  const load = React.useCallback(async () => latest.current, [])
  const runtime = useAdkRuntime({
    stream: nothingToSend,
    sessionAdapter,
    load,
    isDisabled: true,
  })
  // Open the conversation, and show it again as new events come in.
  React.useEffect(() => {
    void showConversation(runtime, detail.session_id)
  }, [runtime, detail.session_id])
  const opened = React.useRef(false)
  React.useEffect(() => {
    if (!opened.current) {
      opened.current = true
      return
    }
    void runtime.threads.reloadMainThread()
  }, [runtime, snapshot])

  const assistant = detail.app_name === ASSISTANT_APP
  const me = useMe()
  // The assistant's specialists by name, as its own chat shows them.
  const agents = useAssistantAgents(assistant ? (me.data?.subject ?? "") : "")
  return (
    <AssistantProvider
      runtime={runtime}
      title={assistant ? "Forge assistant" : detail.agent_name}
      sources={assistantSources}
      agents={assistant ? agents : undefined}
      welcome={{
        title: "Nothing was said",
        description: detail.partial
          ? "Nothing in this conversation was asked from inside this organization."
          : "This conversation has no messages.",
      }}
    >
      <AssistantThread readOnly />
    </AssistantProvider>
  )
}

/**
 * The conversation in the chat window the person used: the same thread,
 * messages and tool cards, read only. Remounts per conversation, so each
 * gets a runtime of its own.
 */
export function ConversationReplay({ detail }: { detail: ConversationDetail }) {
  return (
    <div className="h-[min(78vh,960px)] min-h-[28rem] overflow-hidden rounded-(--radius-card) border">
      <Replay
        key={`${detail.app_name}/${detail.user_id}/${detail.session_id}`}
        detail={detail}
      />
    </div>
  )
}
