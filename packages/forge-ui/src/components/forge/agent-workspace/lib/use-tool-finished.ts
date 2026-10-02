import * as React from "react"
import type { ToolCallMessagePartStatus } from "@assistant-ui/react"

/**
 * Calls `onFinish` once when a tool call this page watched running finishes
 * successfully: for a tool UI to change the screen (`useScreen`) or refresh
 * a section. A call that was already done when it rendered, e.g. in a past
 * conversation being opened, doesn't call it, so replaying history leaves the
 * screen alone.
 */
export function useToolFinished(
  status: ToolCallMessagePartStatus,
  onFinish: () => void
) {
  const sawRunning = React.useRef(status.type === "running")
  const onFinishRef = React.useRef(onFinish)
  React.useEffect(() => {
    onFinishRef.current = onFinish
  })
  React.useEffect(() => {
    if (status.type === "running") {
      sawRunning.current = true
    } else if (status.type === "complete" && sawRunning.current) {
      sawRunning.current = false
      onFinishRef.current()
    }
  }, [status.type])
}

/**
 * Calls `onDone` once when a call this page saw waiting for the person's
 * approval (ADK `require_confirmation`) is approved and its result arrives,
 * the counterpart of `useToolFinished` for a gated tool, which doesn't show
 * as running while it waits. A call already decided when it rendered doesn't
 * call it.
 */
export function useToolApproved(
  part: {
    approval?: { approved?: boolean } | undefined
    result?: unknown
  },
  isDone: (result: unknown) => boolean,
  onDone: () => void
) {
  const approved = part.approval?.approved
  const done = approved === true && isDone(part.result)
  const sawWaiting = React.useRef(
    part.approval !== undefined && approved === undefined
  )
  const onDoneRef = React.useRef(onDone)
  React.useEffect(() => {
    onDoneRef.current = onDone
  })
  React.useEffect(() => {
    if (part.approval !== undefined && approved === undefined) {
      sawWaiting.current = true
    } else if (done && sawWaiting.current) {
      sawWaiting.current = false
      onDoneRef.current()
    }
  }, [part.approval, approved, done])
}
