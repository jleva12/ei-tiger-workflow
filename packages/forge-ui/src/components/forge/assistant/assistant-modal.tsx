import * as React from "react"
import { useAuiState } from "@assistant-ui/react"

import { AssistantModal as AssistantModalElement } from "@/components/assistant-ui/elements/assistant-modal.aui"

import { AdkArtifactsMenu, AdkEscalationBanner } from "./adk"
import { AssistantMark } from "./assistant-mark"
import {
  AssistantProvider,
  AssistantThread,
  type AssistantOptions,
} from "./assistant-provider"

export type AssistantModalProps = AssistantOptions & {
  /** Extra header controls, before the artifacts and conversations. */
  actions?: React.ReactNode
  /** Start with the panel open. */
  defaultOpen?: boolean
  /** Whether the panel is open, to control it; `defaultOpen` otherwise. */
  open?: boolean
  /** Called when the panel opens or closes. */
  onOpenChange?: (open: boolean) => void
  /**
   * Classes for the launcher's anchor. It's fixed to the viewport's
   * bottom-right corner by default; `absolute` pins it inside a positioned
   * container instead.
   */
  className?: string
}

/** The launcher's mark; its spark turns while the agent works. */
function LauncherMark() {
  const running = useAuiState((s) => s.thread.isRunning)
  return <AssistantMark active={running} className="size-[26px]" />
}

/**
 * The same ADK assistant as `AssistantScreen`, as a floating launcher in the
 * corner of any page. It opens a resizable panel with the conversation,
 * past conversations and a new-conversation button, and opens itself when
 * the agent starts replying. Render it once, anywhere in the app.
 */
function AssistantModal({
  actions,
  defaultOpen,
  open,
  onOpenChange,
  className,
  ...options
}: AssistantModalProps) {
  return (
    <AssistantProvider {...options}>
      <AssistantModalElement
        label={options.title ?? "Assistant"}
        defaultOpen={defaultOpen}
        open={open}
        onOpenChange={onOpenChange}
        icon={<LauncherMark />}
        className={className}
        thread={<AssistantThread />}
        headerActions={
          <>
            {actions}
            <AdkArtifactsMenu compact />
          </>
        }
        banner={<AdkEscalationBanner />}
      />
    </AssistantProvider>
  )
}

export { AssistantModal }
