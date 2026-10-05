import * as React from "react"
import { useAuiState, type AssistantRuntime } from "@assistant-ui/react"
import { SquareArrowMoveDownLeftIcon } from "@hugeicons/core-free-icons"

import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { AssistantCapabilitiesButton } from "@/features/assistant/components/assistant-capabilities"
import { AssistantScreen } from "@/components/forge/assistant/index"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { LinkedPageChip } from "@/features/assistant/components/page-context-chip"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  showConversation,
  useAssistantPlacement,
  useLinkedPage,
} from "@/features/assistant/lib/assistant-window"
import {
  useAssistantAgents,
  useSuggestedPrompts,
} from "@/features/assistant/lib/assistant-capabilities"
import { useForgeAgent } from "@/features/assistant/lib/forge-agent"
import { useMe } from "@/lib/users"

const TITLE = "Forge assistant"

/**
 * In the top bar: puts the conversation back in the panel of the Forge tab
 * the person was in last, and closes this window.
 */
function DockButton({
  canDock,
  onDock,
}: {
  canDock: boolean
  onDock: (thread: string | undefined) => void
}) {
  const running = useAuiState((s) => s.thread.isRunning)
  const thread = useAuiState((s) => s.threadListItem.remoteId)
  return (
    <TooltipIconButton
      tooltip={
        !canDock
          ? "Open Forge in a tab to move the assistant back into it"
          : running
            ? "Move back into Forge once the reply finishes"
            : "Move back into Forge"
      }
      side="bottom"
      disabled={!canDock || running}
      className="size-7 rounded-md p-0 text-muted-foreground hover:text-foreground"
      onClick={() => onDock(thread)}
    >
      <Icon icon={SquareArrowMoveDownLeftIcon} size={15} />
    </TooltipIconButton>
  )
}

function AssistantWindow({
  userId,
  thread,
  onThreadShown,
}: {
  userId: string
  thread: string | undefined
  onThreadShown: () => void
}) {
  const runtimeRef = React.useRef<AssistantRuntime | null>(null)
  const linked = useLinkedPage({
    onShowThread: (next) => {
      if (runtimeRef.current) void showConversation(runtimeRef.current, next)
    },
  })
  const { runtime, artifacts, connected, sharing, setSharing, modelSection } =
    useForgeAgent(userId, linked.read)
  const agents = useAssistantAgents(userId)
  const suggestions = useSuggestedPrompts(userId, sharing ? linked.page : null)
  const [, setPlacement] = useAssistantPlacement()

  const { setRunning } = linked
  React.useEffect(() => {
    runtimeRef.current = runtime
    const report = () => setRunning(runtime.thread.getState().isRunning)
    report()
    return runtime.thread.subscribe(report)
  }, [runtime, setRunning])

  // Opened from the panel: carry on its conversation.
  React.useEffect(() => {
    if (thread) void showConversation(runtime, thread).then(onThreadShown)
  }, [runtime, thread, onThreadShown])

  const dock = (current: string | undefined) => {
    linked.dock(current)
    setPlacement("panel")
    // Only a window Forge opened can close itself; otherwise it stays.
    window.close()
  }

  return (
    <AssistantScreen
      runtime={runtime}
      artifacts={artifacts}
      agents={agents}
      {...modelSection}
      title={TITLE}
      welcome={{
        title: "How can I help?",
        description: connected
          ? "Ask about your organizations, their workflows and agents, or have it help you build one. It sees the page you're on in Forge."
          : "No agent is connected yet; send a message to see how to connect one.",
      }}
      composerContext={
        connected ? (
          <LinkedPageChip
            page={linked.page}
            sharing={sharing}
            onSharingChange={setSharing}
          />
        ) : undefined
      }
      actions={
        <>
          <AssistantCapabilitiesButton
            userId={userId}
            page={sharing ? linked.page : null}
          />
          <DockButton canDock={linked.page !== null} onDock={dock} />
        </>
      }
      suggestions={suggestions}
    />
  )
}

/**
 * The assistant in its own window (`/assistant`), beside Forge: the whole
 * window, with past conversations down the side, following the page the
 * person is on in their Forge tabs (`useLinkedPage`). `thread` is the
 * conversation it opens on, when it's opened from the panel.
 */
export function AssistantWindowPage({
  thread,
  onThreadShown,
}: {
  thread: string | undefined
  onThreadShown: () => void
}) {
  const me = useMe()
  React.useEffect(() => {
    const previous = document.title
    document.title = TITLE
    return () => {
      document.title = previous
    }
  }, [])

  if (me.data)
    return (
      <AssistantWindow
        userId={me.data.subject}
        thread={thread}
        onThreadShown={onThreadShown}
      />
    )
  return (
    <main className="flex h-dvh items-start bg-background p-5">
      {me.error ? (
        <ErrorCallout
          title="Couldn't start the assistant"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void me.refetch()}
            >
              Retry
            </Button>
          }
        >
          {me.error.message}
        </ErrorCallout>
      ) : (
        <div className="flex w-full flex-col gap-3" aria-busy="true">
          <Skeleton className="h-5 w-48" />
          <Skeleton className="h-4 w-80 max-w-full" />
        </div>
      )}
    </main>
  )
}
