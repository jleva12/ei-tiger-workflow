import * as React from "react"
import { useAuiState } from "@assistant-ui/react"
import { SquareArrowOutUpRightIcon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { AssistantCapabilitiesButton } from "@/features/assistant/components/assistant-capabilities"
import { AssistantModalButton } from "@/components/assistant-ui/elements/assistant-modal.aui"
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { AssistantMark, AssistantModal } from "@/components/forge/assistant/index"
import { Icon } from "@/components/forge/icon"
import { PageContextChip } from "@/features/assistant/components/page-context-chip"
import { useAssistantLauncher } from "@/features/assistant/lib/assistant-launcher"
import {
  openAssistantWindow,
  showConversation,
  useAssistantPlacement,
  useAssistantWindowHost,
  type AssistantWindowStatus,
  type DockRequest,
} from "@/features/assistant/lib/assistant-window"
import {
  useAssistantAgents,
  useSuggestedPrompts,
} from "@/features/assistant/lib/assistant-capabilities"
import {
  useCurrentPage,
  useForgeAgent,
  usePageSnapshot,
  type CurrentPage,
} from "@/features/assistant/lib/forge-agent"
import { useMe } from "@/lib/users"

const TITLE = "Forge assistant"

// Above the status footer, which runs along the bottom of the page.
const LAUNCHER_CLASS = "bottom-12"

/** In the panel's header: moves the conversation to the assistant's window. */
function PopOutButton({ onPopOut }: { onPopOut: (thread?: string) => void }) {
  const running = useAuiState((s) => s.thread.isRunning)
  const thread = useAuiState((s) => s.threadListItem.remoteId)
  return (
    <TooltipIconButton
      tooltip={
        running
          ? "Open in a window once the reply finishes"
          : "Open in a window"
      }
      side="bottom"
      // Moving mid-reply would stop it.
      disabled={running}
      className="size-7 rounded-md p-0 text-muted-foreground hover:text-foreground"
      onClick={() => onPopOut(thread)}
    >
      <Icon icon={SquareArrowOutUpRightIcon} size={14} />
    </TooltipIconButton>
  )
}

function AssistantPanel({
  userId,
  page,
  dock,
  onDocked,
}: {
  userId: string
  page: CurrentPage
  dock: DockRequest | null
  onDocked: () => void
}) {
  const { runtime, artifacts, connected, sharing, setSharing, modelSection } =
    useForgeAgent(userId, page.read)
  const agents = useAssistantAgents(userId)
  // What the agent sees: the page, while it's shared.
  const snapshot = usePageSnapshot(page)
  const seen = sharing ? snapshot : null
  const suggestions = useSuggestedPrompts(userId, seen)
  const [, setPlacement] = useAssistantPlacement()
  const [open, setOpen] = React.useState(false)

  // The window handed the conversation back: show it here.
  const [docked, setDocked] = React.useState<DockRequest | null>(null)
  if (dock && dock !== docked) {
    setDocked(dock)
    setOpen(true)
  }
  React.useEffect(() => {
    if (!dock) return
    onDocked()
    if (dock.thread) void showConversation(runtime, dock.thread)
  }, [dock, onDocked, runtime])

  const popOut = (thread?: string) => {
    // The panel goes away with it.
    if (openAssistantWindow(thread)) setPlacement("window")
  }

  return (
    <AssistantModal
      runtime={runtime}
      artifacts={artifacts}
      agents={agents}
      {...modelSection}
      title={TITLE}
      open={open}
      onOpenChange={setOpen}
      actions={
        <>
          <AssistantCapabilitiesButton userId={userId} page={seen} />
          <PopOutButton onPopOut={popOut} />
        </>
      }
      suggestions={suggestions}
      welcome={{
        title: "How can I help?",
        description: connected
          ? "Ask about your organizations, their ADK workflows and agents, or have it help you build one."
          : "No agent is connected yet; send a message to see how to connect one.",
      }}
      composerContext={
        connected ? (
          <PageContextChip sharing={sharing} onSharingChange={setSharing} />
        ) : undefined
      }
      className={LAUNCHER_CLASS}
    />
  )
}

/**
 * The launcher while the assistant opens in its own window: it opens the
 * window, or brings it forward, and its spark turns while the agent works
 * there.
 */
function WindowLauncher({ status }: { status: AssistantWindowStatus }) {
  return (
    <div
      className={cn(
        "aui-root fixed end-4 bottom-4 z-40 size-11",
        LAUNCHER_CLASS
      )}
    >
      <AssistantModalButton
        open={false}
        label={status.open ? `${TITLE} window` : `${TITLE} in a window`}
        icon={<AssistantMark active={status.running} className="size-[26px]" />}
        onClick={() => openAssistantWindow()}
      />
    </div>
  )
}

/**
 * The assistant, in the bottom-right corner of every page but those with
 * the agent built in (`useHideAssistantLauncher`): a panel over the page, or
 * a launcher for its own window, as the person chose (`assistantPlacement`,
 * in Settings, or the panel's pop-out button). Either way this tab keeps an
 * open window up to date with what it shows. Render it once, inside
 * `PageContextProvider` and `AssistantLauncherProvider`; it waits for the
 * signed-in user, whose conversations it shows.
 */
export function AppAssistant() {
  const me = useMe()
  const hidden = useAssistantLauncher((state) => state.hiddenBy > 0)
  const [placement] = useAssistantPlacement()
  const page = useCurrentPage()
  const { status, dock, clearDock } = useAssistantWindowHost(page)
  if (!me.data || hidden) return null
  return placement === "window" ? (
    <WindowLauncher status={status} />
  ) : (
    <AssistantPanel
      userId={me.data.subject}
      page={page}
      dock={dock}
      onDocked={clearDock}
    />
  )
}
