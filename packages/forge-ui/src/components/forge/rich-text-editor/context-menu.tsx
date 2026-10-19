import * as React from "react"
import { cn } from "cn"

import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuTrigger,
} from "@/components/ui/context-menu"
import { useRichTextEditorContext } from "./context"
import { createActionContext } from "./core/action-registry"
import { MenuEntries } from "./menu-items"
import {
  defaultContextMenuItems,
  expandAIMenu,
  menuActionIds,
} from "./menu-model"
import type { EditorActionContext, MenuItem } from "./types"

/**
 * The right-click menu over the text. It snapshots the selection as it
 * opens; choosing an entry puts that selection back and runs the action with
 * the snapshot, so focus moving into the menu never changes what it acts on.
 * Keyboard users open it with the context-menu key or Shift+F10.
 */
export function EditorContextMenu({
  items,
  className,
  children,
}: {
  /** Defaults to clipboard, formatting, the app's actions and AI. */
  items?: MenuItem[]
  className?: string
  children: React.ReactNode
}) {
  const { editor, controller, registry } = useRichTextEditorContext()
  const [snapshot, setSnapshot] = React.useState<EditorActionContext | null>(
    null
  )

  const actions = registry.list()
  const aiIds = actions.filter((a) => a.id.startsWith("ai.")).map((a) => a.id)
  const defaults = defaultContextMenuItems([])
  const builtInIds = new Set(menuActionIds(defaults))
  // The app's own actions get a place in the default menu.
  const customIds = actions
    .filter(
      (action) =>
        !registry.isBuiltIn(action.id) &&
        !action.id.startsWith("ai.") &&
        !builtInIds.has(action.id)
    )
    .map((action) => action.id)
  const entries = expandAIMenu(
    items ?? defaultContextMenuItems(customIds),
    aiIds
  )

  const run = (id: string) => {
    if (!snapshot) return
    controller.setSelection(snapshot.selection)
    void registry.run(id, snapshot)
  }

  return (
    <ContextMenu
      onOpenChange={(open) => {
        setSnapshot(
          open ? createActionContext(controller, "contextMenu") : null
        )
      }}
    >
      <ContextMenuTrigger className={cn("select-text", className)}>
        {children}
      </ContextMenuTrigger>
      <ContextMenuContent
        finalFocus={() => editor.view.dom as HTMLElement}
        className="min-w-52"
      >
        {snapshot && (
          <MenuEntries
            items={entries}
            kind="context"
            source="contextMenu"
            onRun={run}
          />
        )}
      </ContextMenuContent>
    </ContextMenu>
  )
}
