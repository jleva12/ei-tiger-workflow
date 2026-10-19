import * as React from "react"

import { RichTextEditorContext } from "./context"
import type { RichTextEditorInstance } from "./use-rich-text-editor"

/**
 * Gives the parts inside (toolbar, menus, text, `useEditorActions`) the
 * editor that `useRichTextEditor` returned.
 */
export function RichTextEditorProvider({
  editor,
  children,
}: {
  editor: RichTextEditorInstance
  children: React.ReactNode
}) {
  return (
    <RichTextEditorContext.Provider value={editor}>
      {children}
    </RichTextEditorContext.Provider>
  )
}
