import { SparklesIcon } from "@hugeicons/core-free-icons"

import type { MenuItem, ResolvedEditorFeatures, ToolbarItem } from "./types"

/** Controls the toolbar draws itself rather than as one action. */
export const TOOLBAR_WIDGETS = new Set([
  "heading",
  "blockType",
  "insert",
  "ai",
  "more",
  "separator",
  "spacer",
])

/**
 * The toolbar for the enabled features. Ids whose feature is off are simply
 * skipped, so this lists everything the editor can show.
 */
export function defaultToolbarItems(
  features: ResolvedEditorFeatures,
  hasAI: boolean
): ToolbarItem[] {
  return [
    "undo",
    "redo",
    "separator",
    "heading",
    "separator",
    "bold",
    "italic",
    "underline",
    "strike",
    "inlineCode",
    "highlight",
    "subscript",
    "superscript",
    "link",
    "clearFormatting",
    "separator",
    "bulletList",
    "orderedList",
    "taskList",
    "indent",
    "outdent",
    "separator",
    "insert",
    ...(hasAI ? ["separator", "ai"] : []),
    "spacer",
    ...(features.findReplace ? ["find"] : []),
    ...(features.outline ? ["outline"] : []),
    ...(features.fullscreen ? ["fullscreen"] : []),
    "more",
  ]
}

/** The selection menu's default buttons. */
export const DEFAULT_BUBBLE_ITEMS: ToolbarItem[] = [
  "bold",
  "italic",
  "underline",
  "strike",
  "inlineCode",
  "highlight",
  "separator",
  "link",
  "ai",
]

/** The right-click menu: clipboard, formatting, the app's actions, AI. */
export function defaultContextMenuItems(customIds: string[]): MenuItem[] {
  return [
    "cut",
    "copy",
    "paste",
    "selectAll",
    "separator",
    "bold",
    "italic",
    "link",
    "unlink",
    "clearFormatting",
    "separator",
    ...customIds,
    "separator",
    "ai",
  ]
}

/** Every action id a menu tree mentions, for one state lookup. */
export function menuActionIds(items: MenuItem[]): string[] {
  return items.flatMap((item) => {
    if (typeof item === "string") return item === "separator" ? [] : [item]
    return menuActionIds(item.items)
  })
}

/** `"ai"` becomes a submenu of the AI actions there are. */
export function expandAIMenu(items: MenuItem[], aiIds: string[]): MenuItem[] {
  return items.map((item) => {
    if (item === "ai") {
      return { type: "submenu", label: "AI", icon: SparklesIcon, items: aiIds }
    }
    if (typeof item === "string") return item
    return { ...item, items: expandAIMenu(item.items, aiIds) }
  })
}

/** Drops separators at the ends and next to each other (after hiding items). */
export function tidySeparators<T extends { separator: boolean }>(entries: T[]) {
  return entries.filter((entry, index) => {
    if (!entry.separator) return true
    const before = entries.slice(0, index).some((e) => !e.separator)
    const next = entries[index + 1]
    return before && next !== undefined && !next.separator
  })
}
