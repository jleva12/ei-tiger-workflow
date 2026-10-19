import { useEditorState } from "@tiptap/react"
import { cn } from "cn"

import { useRichTextEditorContext } from "./context"
import { isAlive } from "./core/lifecycle"

/** Words and characters under the text, and the limit when there is one. */
export function EditorStatusBar({
  maxLength,
  className,
}: {
  maxLength?: number
  className?: string
}) {
  const { editor } = useRichTextEditorContext()
  const count = useEditorState({
    editor,
    selector: ({ editor }) =>
      isAlive(editor) && editor.storage.characterCount
        ? {
            characters: editor.storage.characterCount.characters(),
            words: editor.storage.characterCount.words(),
          }
        : null,
  })
  if (!count) return null
  const near = maxLength !== undefined && count.characters >= maxLength * 0.9
  return (
    <div
      data-slot="rich-text-count"
      className={cn(
        "flex items-center justify-end gap-3 border-t border-border px-3 py-1 text-2xs text-subtle tabular-nums",
        className
      )}
    >
      <span>
        {count.words} {count.words === 1 ? "word" : "words"}
      </span>
      <span
        aria-live={near ? "polite" : undefined}
        className={cn(near && "text-destructive")}
      >
        {maxLength !== undefined
          ? `${count.characters} / ${maxLength} characters`
          : `${count.characters} ${count.characters === 1 ? "character" : "characters"}`}
      </span>
    </div>
  )
}
