import { Extension, type Editor } from "@tiptap/core"

const destroyed = new WeakSet<Editor>()

/**
 * Records when an editor is destroyed. Tiptap's own `isDestroyed` is also
 * true for an editor that was never mounted, which a headless one isn't.
 */
export const EditorLifecycle = Extension.create({
  name: "editorLifecycle",

  onDestroy() {
    destroyed.add(this.editor)
  },
})

/** Whether the editor exists and hasn't been destroyed. */
export function isAlive(editor: Editor | null | undefined): editor is Editor {
  return Boolean(editor) && !destroyed.has(editor as Editor)
}

/** Whether the editor has a view in the page (a headless one doesn't). */
export function isMounted(editor: Editor | null | undefined) {
  if (!isAlive(editor)) return false
  try {
    return Boolean(editor.view.dom)
  } catch {
    return false
  }
}
