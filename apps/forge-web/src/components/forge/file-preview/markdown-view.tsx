import * as React from "react"
import { SourceCodeIcon, ViewIcon } from "@hugeicons/core-free-icons"

import { readText } from "./content"
import { MarkdownDocument } from "./markdown-document"
import {
  BarDivider,
  BarText,
  DownloadButton,
  ModeSwitch,
  PreviewFrame,
  PreviewLoading,
  PreviewProblem,
} from "./frame"
import { usePreviewTask, type ViewProps } from "./preview"
import { Lines } from "./text-view"

// Past this, the document opens on its source: laying out a very long one
// as rich text takes a while.
const RICH_LIMIT = 1_500_000

type Mode = "preview" | "source"

/**
 * Markdown as the document it describes (headings, lists, tables, task
 * lists, code), in the workspace's type; or its source, with line
 * numbers.
 */
export default function MarkdownView({
  data,
  name,
  toolbarEnd,
  onDownload,
}: ViewProps) {
  const text = usePreviewTask(() => readText(data), [data])
  const [mode, setMode] = React.useState<Mode>()
  const source = React.useMemo(
    () => text.value?.text.replace(/\r\n?/g, "\n") ?? "",
    [text.value]
  )
  const lines = React.useMemo(
    () => source.replace(/\n$/, "").split("\n"),
    [source]
  )
  const shown: Mode =
    mode ??
    (source.length > RICH_LIMIT || text.value?.truncated ? "source" : "preview")

  if (text.loading)
    return (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewLoading kind="markdown" label="Reading the document…" />
      </PreviewFrame>
    )
  if (text.error || !text.value)
    return (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewProblem
          kind="markdown"
          title="Couldn't read this document"
          description={
            text.error?.message ?? "It holds binary data, not Markdown text."
          }
          actions={<DownloadButton onDownload={onDownload} />}
        />
      </PreviewFrame>
    )

  return (
    <PreviewFrame
      toolbarEnd={toolbarEnd}
      toolbar={
        <>
          <ModeSwitch
            label="Show"
            value={shown}
            onChange={setMode}
            options={[
              { value: "preview", label: "Preview", icon: ViewIcon },
              { value: "source", label: "Source", icon: SourceCodeIcon },
            ]}
          />
          {shown === "source" && (
            <>
              <BarDivider />
              <BarText>
                {lines.length.toLocaleString()}{" "}
                {lines.length === 1 ? "line" : "lines"}
              </BarText>
            </>
          )}
        </>
      }
    >
      {shown === "preview" ? (
        <div
          tabIndex={0}
          role="document"
          aria-label={name}
          className="absolute inset-0 overflow-auto outline-none"
        >
          <article className="mx-auto max-w-[78ch] px-8 pt-8 pb-16 text-sm/[1.7] @max-[600px]/preview:px-5">
            <MarkdownDocument source={source} />
          </article>
        </div>
      ) : (
        <Lines lines={lines} wrap />
      )}
    </PreviewFrame>
  )
}
