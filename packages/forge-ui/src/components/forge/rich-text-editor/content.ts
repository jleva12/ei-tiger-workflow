import type { Editor, JSONContent } from "@tiptap/react"
// Brings in the Markdown extension's `getMarkdown` and `contentType` types.
import type {} from "@tiptap/markdown"

import type { RichTextFormat, RichTextValue } from "./types"

/**
 * Reads the document in `format`. An empty document is `""` in HTML and
 * Markdown, so required-field checks can test the string.
 */
export function readContent(editor: Editor, format: "json"): JSONContent
export function readContent(
  editor: Editor,
  format: RichTextFormat
): RichTextValue
export function readContent(
  editor: Editor,
  format: RichTextFormat
): RichTextValue {
  if (format === "json") return editor.getJSON()
  if (editor.isEmpty) return ""
  return format === "markdown" ? editor.getMarkdown() : editor.getHTML()
}

/** Replaces the document without firing `onUpdate`. */
export function writeContent(
  editor: Editor,
  value: RichTextValue | null | undefined,
  format: RichTextFormat
) {
  editor.commands.setContent(value ?? "", {
    emitUpdate: false,
    contentType: typeof value === "string" ? format : "json",
  })
}

/** Whether two values hold the same content, to skip redundant resets. */
export function sameContent(
  a: RichTextValue | null | undefined,
  b: RichTextValue | null | undefined
) {
  if (a === b) return true
  if (a == null || b == null) return (a ?? "") === (b ?? "")
  if (typeof a === "string" || typeof b === "string") return false
  return JSON.stringify(a) === JSON.stringify(b)
}

/** Saves `text` as a file the person downloads. */
export function downloadText(text: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement("a")
  link.href = url
  link.download = filename
  link.click()
  // Revoking in the same task can cancel the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
