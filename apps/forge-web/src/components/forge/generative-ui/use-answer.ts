import * as React from "react"
import { useAui, useAuiState } from "@assistant-ui/react"
import type { MessagePart } from "./messages"

/** The text of the person's message right after this one; none when there's none yet. */
function nextUserText(s: {
  message: { id: string }
  thread: {
    messages: readonly {
      id: string
      role: string
      content: readonly MessagePart[]
    }[]
  }
}) {
  const messages = s.thread.messages
  const next = messages[messages.findIndex((m) => m.id === s.message.id) + 1]
  if (next?.role !== "user") return undefined
  return next.content
    .flatMap((part) => (part.type === "text" && part.text ? [part.text] : []))
    .join("\n")
    .trim()
}

/**
 * What one call's composition may do: act while its message is the
 * conversation's last, nothing runs and the chat can send; what it sends
 * goes as the person's message, with what the composer sends beside it.
 */
export function useAnswer() {
  const aui = useAui()
  const [sent, setSent] = React.useState(false)
  const live = useAuiState(
    (s) => s.message.isLast && !s.thread.isRunning && !s.thread.isDisabled
  )
  const picked = useAuiState(nextUserText)
  const send = React.useCallback(
    (text: string) => {
      if (!text || aui.thread.getState().isRunning) return
      setSent(true)
      aui.thread.append({
        content: [{ type: "text", text }],
        runConfig: aui.composer.getState().runConfig,
      })
    },
    [aui]
  )
  return React.useMemo(
    () => ({ live: live && !sent, picked, send }),
    [live, sent, picked, send]
  )
}
