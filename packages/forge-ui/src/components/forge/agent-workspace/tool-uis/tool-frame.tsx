import type * as React from "react"
import type { ToolCallMessagePartProps } from "@assistant-ui/react"

import { ToolFallback } from "@/components/assistant-ui/elements/tool-fallback.aui"

/**
 * A tool call's card in the conversation: the standard line (spinner and
 * shimmer while it runs, the call's args and raw result when opened), labelled
 * with what the call did, and `children` below it once it's done, e.g. dice
 * or a clock. A failed or cancelled call keeps its label, with the standard
 * cross (and the error when opened).
 */
export function ToolFrame({
  part,
  label,
  running,
  children,
}: {
  part: ToolCallMessagePartProps
  /** What it did, once it's done. */
  label: React.ReactNode
  /** What it's doing, while it runs. */
  running: React.ReactNode
  children?: React.ReactNode
}) {
  const { toolName, status, argsText, result } = part
  const done = status.type === "complete"
  return (
    <div data-slot="tool-ui" className="flex flex-col gap-1.5">
      <ToolFallback.Root>
        <ToolFallback.Trigger
          toolName={toolName}
          status={status}
          label={
            done || status.type === "incomplete"
              ? label
              : status.type === "running"
                ? running
                : undefined
          }
        />
        <ToolFallback.Content>
          <ToolFallback.Error status={status} />
          <ToolFallback.Args argsText={argsText} />
          <ToolFallback.Result result={result} />
        </ToolFallback.Content>
      </ToolFallback.Root>
      {done && children}
    </div>
  )
}
