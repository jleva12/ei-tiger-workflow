import type { ToolCallMessagePartComponent } from "@assistant-ui/react"
import { FileCodeIcon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { useCanvas } from "../lib/canvas"
import { toolResult } from "../lib/tool-result"
import { useToolFinished } from "../lib/use-tool-finished"

import { ToolFrame } from "./tool-frame"

type WriteArgs = { filename: string; content: string }
type WriteResult = { filename: string; version: number; lines: number }

const PREVIEW_LINES = 6

/**
 * `write_document`: a card for the saved file (its name, version and first
 * lines) that opens it in the canvas, which opens by itself as the agent
 * saves it.
 */
export const WriteDocumentUI: ToolCallMessagePartComponent<WriteArgs> = (
  part
) => {
  const openDocument = useCanvas((state) => state.openDocument)
  const saved = toolResult<WriteResult>(part.result)
  const filename = saved.filename ?? part.args.filename
  useToolFinished(part.status, () => {
    if (saved.filename) openDocument(saved.filename)
  })
  const preview = (part.args.content ?? "")
    .split("\n")
    .slice(0, PREVIEW_LINES)
    .join("\n")
  return (
    <ToolFrame
      part={part}
      running={<>Writing {filename ?? "a document"}</>}
      label={
        <>
          {saved.version ? "Revised" : "Wrote"}{" "}
          <b className="font-mono">{filename}</b>
        </>
      }
    >
      {saved.filename && (
        <button
          type="button"
          onClick={() => openDocument(saved.filename!, saved.version)}
          className="group ms-6 flex max-w-lg flex-col overflow-hidden rounded-(--radius-card) border bg-card text-start shadow-xs transition-[border-color,box-shadow] hover:border-foreground/20 hover:shadow-sm focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none"
        >
          <span className="flex items-center gap-2 border-b bg-muted/40 px-3 py-2 text-xs">
            <Icon
              icon={FileCodeIcon}
              size={14}
              className="text-muted-foreground"
            />
            <span className="min-w-0 flex-1 truncate font-mono text-foreground">
              {saved.filename}
            </span>
            <span className="text-muted-foreground tabular-nums">
              v{(saved.version ?? 0) + 1} · {saved.lines} lines
            </span>
          </span>
          <pre
            className="max-h-28 overflow-hidden px-3 py-2 font-mono text-2xs leading-4 text-muted-foreground"
            style={{
              maskImage: "linear-gradient(to bottom, black 55%, transparent)",
            }}
          >
            {preview}
          </pre>
          <span className="flex justify-end px-3 pb-2">
            <Button
              render={<span />}
              variant="outline"
              size="xs"
              className="pointer-events-none group-hover:bg-muted"
            >
              Open in canvas
            </Button>
          </span>
        </button>
      )}
    </ToolFrame>
  )
}

export const ReadDocumentUI: ToolCallMessagePartComponent<{
  filename: string
}> = (part) => {
  const { error } = toolResult<{ error: string }>(part.result)
  return (
    <ToolFrame
      part={part}
      running={<>Reading {part.args.filename}</>}
      label={
        error ? (
          <>{error}</>
        ) : (
          <>
            Read <b className="font-mono">{part.args.filename}</b>
          </>
        )
      }
    />
  )
}
