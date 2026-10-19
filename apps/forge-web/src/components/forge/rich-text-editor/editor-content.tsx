import { EditorContent } from "@tiptap/react"
import { cn } from "cn"

import { useRichTextEditorContext } from "./context"

/**
 * The editable text, in the editor's typography. `RichTextEditor` renders
 * it; a headless editor renders it inside `RichTextEditorProvider`.
 */
export function RichTextEditorContent({
  className,
  focusMode = false,
  bare = false,
}: {
  className?: string
  /** Dims every block but the one being edited. */
  focusMode?: boolean
  /** No padding around the text, for showing it inside something else. */
  bare?: boolean
}) {
  const { editor, features } = useRichTextEditorContext()
  const editable = editor.isEditable
  return (
    <EditorContent
      editor={editor}
      data-editable={editable || undefined}
      data-drag-handle={(editable && features.dragHandle) || undefined}
      data-focus-mode={focusMode || undefined}
      data-bare={bare || undefined}
      className={cn("rich-text relative min-h-[inherit]", className)}
    />
  )
}
