import * as React from "react"

import {
  ContextMenuCheckboxItem,
  ContextMenuGroup,
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuSeparator,
  ContextMenuShortcut,
  ContextMenuSub,
  ContextMenuSubContent,
  ContextMenuSubTrigger,
} from "@/components/ui/context-menu"
import {
  DropdownMenuCheckboxItem,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "@/components/ui/dropdown-menu"
import { Spinner } from "@/components/ui/spinner"
import { ActionIcon } from "./controls"
import { useEditorActions, type ResolvedAction } from "./context"
import { menuActionIds, tidySeparators } from "./menu-model"
import { formatShortcut } from "./shortcuts"
import type { ActionSource, MenuItem } from "./types"

// Dropdown and context menus are the same Base UI menu underneath; these
// tables let one renderer draw either.
const PARTS = {
  dropdown: {
    Item: DropdownMenuItem,
    CheckboxItem: DropdownMenuCheckboxItem,
    Group: DropdownMenuGroup,
    Label: DropdownMenuLabel,
    Separator: DropdownMenuSeparator,
    Shortcut: DropdownMenuShortcut,
    Sub: DropdownMenuSub,
    SubTrigger: DropdownMenuSubTrigger,
    SubContent: DropdownMenuSubContent,
  },
  context: {
    Item: ContextMenuItem,
    CheckboxItem: ContextMenuCheckboxItem,
    Group: ContextMenuGroup,
    Label: ContextMenuLabel,
    Separator: ContextMenuSeparator,
    Shortcut: ContextMenuShortcut,
    Sub: ContextMenuSub,
    SubTrigger: ContextMenuSubTrigger,
    SubContent: ContextMenuSubContent,
  },
}

/**
 * Menu entries for actions, drawn as dropdown or context menu items:
 * hidden actions disappear, disabled ones grey out, toggles show a check,
 * and running ones a spinner.
 */
export function MenuEntries({
  items,
  kind,
  source,
  onRun,
}: {
  items: MenuItem[]
  kind: "dropdown" | "context"
  source: ActionSource
  onRun: (id: string) => void
}) {
  const resolved = useEditorActions(menuActionIds(items), source)
  const byId = new Map(resolved.map((entry) => [entry.id, entry]))
  return <>{renderEntries(items, kind, (id) => byId.get(id), onRun)}</>
}

function renderEntries(
  items: MenuItem[],
  kind: "dropdown" | "context",
  lookup: (id: string) => ResolvedAction | undefined,
  onRun: (id: string) => void
): React.ReactNode[] {
  const P = PARTS[kind]
  type Entry = { separator: true } | { separator: false; node: React.ReactNode }
  const entries: Entry[] = items.flatMap((item, index): Entry[] => {
    if (item === "separator") return [{ separator: true }]
    if (typeof item === "string") {
      const entry = lookup(item)
      if (!entry || !entry.state.visible) return []
      const { action, state } = entry
      const content = (
        <>
          {state.running ? <Spinner /> : <ActionIcon icon={action.icon} />}
          {action.label}
          {action.shortcut && (
            <P.Shortcut>{formatShortcut(action.shortcut)}</P.Shortcut>
          )}
        </>
      )
      return [
        {
          separator: false,
          node: action.isActive ? (
            <P.CheckboxItem
              key={action.id}
              checked={state.active}
              disabled={!state.enabled}
              closeOnClick
              onCheckedChange={() => onRun(action.id)}
            >
              {content}
            </P.CheckboxItem>
          ) : (
            <P.Item
              key={action.id}
              disabled={!state.enabled}
              onClick={() => onRun(action.id)}
            >
              {content}
            </P.Item>
          ),
        },
      ]
    }
    const children = renderEntries(item.items, kind, lookup, onRun)
    if (!children.length) return []
    if (item.type === "group") {
      return [
        {
          separator: false,
          node: (
            <P.Group key={`group-${index}`}>
              <P.Label>{item.label}</P.Label>
              {children}
            </P.Group>
          ),
        },
      ]
    }
    return [
      {
        separator: false,
        node: (
          <P.Sub key={`sub-${index}`}>
            <P.SubTrigger>
              <ActionIcon icon={item.icon} />
              {item.label}
            </P.SubTrigger>
            <P.SubContent className="min-w-48">{children}</P.SubContent>
          </P.Sub>
        ),
      },
    ]
  })
  return tidySeparators(entries).map((entry, index) =>
    entry.separator ? <P.Separator key={`separator-${index}`} /> : entry.node
  )
}
