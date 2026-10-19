import type * as React from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"

export function RichMarkdown({
  value,
  children,
}: {
  value?: unknown
  children?: React.ReactNode
}) {
  return (
    <div data-aui="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {typeof value === "string" ? value : ""}
      </ReactMarkdown>
      {children}
    </div>
  )
}
