import * as React from "react"
import { cn } from "cn"
import { useVirtualizer } from "@tanstack/react-virtual"
import { Copy01Icon, TextWrapIcon } from "@hugeicons/core-free-icons"

import { toast } from "@/components/ui/toast"
import { readText, TEXT_PREVIEW_LIMIT } from "./content"
import {
  BarButton,
  BarDivider,
  BarText,
  DownloadButton,
  PreviewFrame,
  PreviewLoading,
  PreviewProblem,
} from "./frame"
import { formatSize, usePreviewTask, type ViewProps } from "./preview"

// Up to this many lines render at once, so the browser's find sees them
// all; more draw as they scroll into view.
const PLAIN_LINES = 4000

/**
 * A text file with line numbers, in the monospace face: long lines wrap
 * (or scroll, with the switch), and very long files draw as they scroll.
 * The start of an enormous file is shown, and it says so.
 */
export default function TextView({ data, toolbarEnd, onDownload }: ViewProps) {
  const text = usePreviewTask(() => readText(data), [data])
  const [wrap, setWrap] = React.useState(true)
  const lines = React.useMemo(() => {
    const all = text.value?.text.replace(/\r\n?/g, "\n").split("\n") ?? []
    // A final newline doesn't make an empty last line.
    if (all.length > 1 && all[all.length - 1] === "") all.pop()
    return all
  }, [text.value])

  if (text.loading)
    return (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewLoading kind="text" label="Reading the file…" />
      </PreviewFrame>
    )
  if (text.error || !text.value)
    return (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewProblem
          kind="text"
          title={
            text.error ? "Couldn't read this file" : "This isn't a text file"
          }
          description={
            text.error?.message ??
            "It holds binary data the browser can't show as text."
          }
          actions={<DownloadButton onDownload={onDownload} />}
        />
      </PreviewFrame>
    )

  const copy = () =>
    void navigator.clipboard.writeText(text.value?.text ?? "").then(
      () => toast.add({ type: "success", title: "Copied the text" }),
      () => toast.add({ type: "error", title: "Couldn't copy the text" })
    )

  return (
    <PreviewFrame
      toolbarEnd={toolbarEnd}
      toolbar={
        <>
          <BarText>
            {lines.length.toLocaleString()}{" "}
            {lines.length === 1 ? "line" : "lines"}
          </BarText>
          {text.value.truncated && (
            <BarText className="text-danger-foreground">
              Showing the first {formatSize(TEXT_PREVIEW_LIMIT)}
            </BarText>
          )}
          <BarDivider />
          <BarButton
            label={wrap ? "Don't wrap lines" : "Wrap lines"}
            icon={TextWrapIcon}
            pressed={wrap}
            onClick={() => setWrap((on) => !on)}
          />
          {!text.value.truncated && (
            <BarButton label="Copy all" icon={Copy01Icon} onClick={copy} />
          )}
        </>
      }
    >
      <Lines lines={lines} wrap={wrap} />
    </PreviewFrame>
  )
}

export function Lines({ lines, wrap }: { lines: string[]; wrap: boolean }) {
  const scrollRef = React.useRef<HTMLDivElement>(null)
  const gutter = `${String(lines.length).length + 2}ch`
  const virtual = lines.length > PLAIN_LINES
  const virtualizer = useVirtualizer({
    count: virtual ? lines.length : 0,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 20,
    overscan: 30,
  })
  const line = (index: number, props?: React.ComponentProps<"div">) => (
    <div
      key={index}
      {...props}
      className={cn("flex min-w-fit", props?.className)}
    >
      <span
        aria-hidden="true"
        className="sticky left-0 shrink-0 bg-background pr-4 text-right text-subtle select-none"
        style={{ width: gutter }}
      >
        {index + 1}
      </span>
      <span
        className={cn(
          "min-w-0 pr-6 text-foreground",
          wrap ? "break-words whitespace-pre-wrap" : "whitespace-pre"
        )}
      >
        {lines[index] || "​"}
      </span>
    </div>
  )
  return (
    <div
      ref={scrollRef}
      tabIndex={0}
      role="document"
      aria-label="File text"
      className="absolute inset-0 overflow-auto py-3 font-mono text-[0.78125rem] leading-5 [tab-size:4] outline-none"
    >
      {virtual ? (
        <div
          className="relative"
          style={{ height: virtualizer.getTotalSize() }}
        >
          {virtualizer.getVirtualItems().map((item) =>
            line(item.index, {
              ref: virtualizer.measureElement,
              "data-index": item.index,
              className: "absolute top-0 left-0 min-w-full",
              style: { transform: `translateY(${item.start}px)` },
            } as React.ComponentProps<"div">)
          )}
        </div>
      ) : (
        lines.map((_, index) => line(index))
      )}
    </div>
  )
}
