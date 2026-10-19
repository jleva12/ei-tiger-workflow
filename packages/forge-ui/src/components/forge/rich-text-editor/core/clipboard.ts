import { getHTMLFromFragment, type Editor } from "@tiptap/core"

import type { EditorController } from "../types.ts"

/** Thrown when the browser won't let a menu use the clipboard. */
export class ClipboardBlockedError extends Error {}

const shortcut = (key: string) =>
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad/.test(navigator.userAgent)
    ? `⌘${key}`
    : `Ctrl+${key}`

function clipboard() {
  return typeof navigator !== "undefined" ? navigator.clipboard : undefined
}

/** Whether menus can paste (reading the clipboard needs this API). */
export function canReadClipboard() {
  return typeof clipboard()?.readText === "function"
}

/**
 * Copies the selection as rich text (HTML) with Markdown as its plain-text
 * form, falling back to Markdown alone where rich writes aren't allowed.
 */
export async function copySelection(
  editor: Editor,
  controller: EditorController
) {
  const api = clipboard()
  const markdown = controller.getSelectedMarkdown()
  if (!api || !markdown) {
    throw new ClipboardBlockedError(
      `The browser blocked copying from the menu. Use ${shortcut("C")} instead.`
    )
  }
  const { from, to } = editor.state.selection
  try {
    if (typeof ClipboardItem !== "undefined" && api.write) {
      const html = getHTMLFromFragment(
        editor.state.doc.slice(from, to).content,
        editor.schema
      )
      await api.write([
        new ClipboardItem({
          "text/plain": new Blob([markdown], { type: "text/plain" }),
          "text/html": new Blob([html], { type: "text/html" }),
        }),
      ])
      return
    }
  } catch {
    // Some browsers refuse rich writes but allow text; try that next.
  }
  try {
    await api.writeText(markdown)
  } catch {
    throw new ClipboardBlockedError(
      `The browser blocked copying from the menu. Use ${shortcut("C")} instead.`
    )
  }
}

/**
 * Pastes at the selection: HTML through the editor's schema (which drops
 * what it doesn't know), plain text as Markdown.
 */
export async function pasteClipboard(
  editor: Editor,
  controller: EditorController
) {
  const api = clipboard()
  const blocked = new ClipboardBlockedError(
    `The browser blocked pasting from the menu. Use ${shortcut("V")} instead.`
  )
  if (!api) throw blocked
  try {
    if (api.read) {
      const items = await api.read()
      for (const item of items) {
        if (item.types.includes("text/html")) {
          const html = await (await item.getType("text/html")).text()
          editor.chain().focus().insertContent(html).run()
          return
        }
      }
    }
  } catch {
    // Reading rich content is often refused where reading text isn't.
  }
  let text: string
  try {
    text = await api.readText()
  } catch {
    throw blocked
  }
  if (text) controller.insertMarkdown(text)
}
