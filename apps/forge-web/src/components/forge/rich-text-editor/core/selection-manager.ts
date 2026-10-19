import { Extension, type Editor } from "@tiptap/core"
import type { Node as PMNode } from "@tiptap/pm/model"
import { Plugin, PluginKey, type Transaction } from "@tiptap/pm/state"
import { Decoration, DecorationSet } from "@tiptap/pm/view"

import type { EditorBookmark, EditorRange } from "../types.ts"
import { isAlive } from "./lifecycle.ts"

interface Bookmark {
  from: number
  to: number
  /** The text when it was taken; a range whose text changed is lost. */
  text: string
  lost: boolean
  /** Tint the range while something works on it. */
  highlight: boolean
}

interface BookmarkState {
  bookmarks: Map<string, Bookmark>
  decorations: DecorationSet
}

type BookmarkMeta =
  | { add: { id: string; from: number; to: number; highlight: boolean } }
  | { remove: string }

export const bookmarkKey = new PluginKey<BookmarkState>("selectionBookmarks")

/** The text a bookmark compares, blocks joined by newlines. */
export function rangeText(doc: PMNode, from: number, to: number) {
  return doc.textBetween(from, to, "\n", "\ufffc")
}

function decorate(doc: PMNode, bookmarks: Map<string, Bookmark>) {
  const decorations: Decoration[] = []
  for (const bookmark of bookmarks.values()) {
    if (!bookmark.highlight || bookmark.lost || bookmark.from >= bookmark.to) {
      continue
    }
    decorations.push(
      Decoration.inline(bookmark.from, bookmark.to, {
        class: "rich-text-pending",
      })
    )
  }
  return DecorationSet.create(doc, decorations)
}

function mapBookmark(bookmark: Bookmark, tr: Transaction): Bookmark {
  // Text typed at either edge stays outside the range; a caret bookmark
  // moves past what's typed at it, so work lands after the new text.
  const empty = bookmark.from === bookmark.to
  const from = tr.mapping.mapResult(bookmark.from, 1)
  const to = tr.mapping.mapResult(bookmark.to, empty ? 1 : -1)
  const lost =
    bookmark.lost ||
    to.pos < from.pos ||
    (empty ? from.deleted : from.deletedAfter && to.deletedBefore)
  return { ...bookmark, from: from.pos, to: Math.max(from.pos, to.pos), lost }
}

/**
 * Ranges that follow edits: anything that works on the selection after a
 * wait (AI, uploads, async actions) bookmarks it first, then resolves the
 * bookmark to find where that text is now, or learns that it has changed.
 */
export const SelectionBookmarks = Extension.create({
  name: "selectionBookmarks",

  addProseMirrorPlugins() {
    return [
      new Plugin<BookmarkState>({
        key: bookmarkKey,
        state: {
          init: (_, state) => ({
            bookmarks: new Map(),
            decorations: DecorationSet.create(state.doc, []),
          }),
          apply: (tr, previous) => {
            const meta = tr.getMeta(bookmarkKey) as BookmarkMeta | undefined
            if (!tr.docChanged && !meta) return previous
            let bookmarks = previous.bookmarks
            if (tr.docChanged) {
              bookmarks = new Map(
                [...bookmarks].map(([id, bookmark]) => [
                  id,
                  mapBookmark(bookmark, tr),
                ])
              )
            }
            if (meta && "add" in meta) {
              const { id, from, to, highlight } = meta.add
              bookmarks = new Map(bookmarks).set(id, {
                from,
                to,
                text: rangeText(tr.doc, from, to),
                lost: false,
                highlight,
              })
            } else if (meta && "remove" in meta) {
              bookmarks = new Map(bookmarks)
              bookmarks.delete(meta.remove)
            }
            return { bookmarks, decorations: decorate(tr.doc, bookmarks) }
          },
        },
        props: {
          decorations: (state) => bookmarkKey.getState(state)?.decorations,
        },
      }),
    ]
  },
})

let nextId = 0

function dispatchMeta(editor: Editor, meta: BookmarkMeta) {
  if (!isAlive(editor)) return
  const tr = editor.state.tr
    .setMeta(bookmarkKey, meta)
    .setMeta("addToHistory", false)
  editor.view.dispatch(tr)
}

/**
 * Bookmarks `range` (clamped to the document). `resolve` returns where it
 * is now, or null once its text has changed or been deleted.
 */
export function createBookmark(
  editor: Editor,
  range: EditorRange,
  { highlight = false }: { highlight?: boolean } = {}
): EditorBookmark {
  const id = `bookmark-${(nextId += 1)}`
  const size = editor.state.doc.content.size
  const from = Math.max(0, Math.min(range.from, size))
  const to = Math.max(from, Math.min(range.to, size))
  dispatchMeta(editor, { add: { id, from, to, highlight } })
  let released = false

  return {
    id,
    resolve: () => {
      if (released || !isAlive(editor)) return null
      const bookmark = bookmarkKey.getState(editor.state)?.bookmarks.get(id)
      if (!bookmark || bookmark.lost) return null
      const now = rangeText(editor.state.doc, bookmark.from, bookmark.to)
      if (now !== bookmark.text) return null
      return { from: bookmark.from, to: bookmark.to }
    },
    release: () => {
      if (released) return
      released = true
      dispatchMeta(editor, { remove: id })
    },
  }
}
