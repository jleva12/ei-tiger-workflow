import * as React from "react"
import { SparklesIcon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuShortcut,
} from "@/components/ui/dropdown-menu"
import {
  ToolbarAction,
  ToolbarMenuTrigger,
  ToolbarSeparator,
  ToolbarToggle,
} from "./controls"
import {
  useEditorActions,
  useRichTextEditorContext,
  useRunAction,
  type ResolvedAction,
} from "./context"
import { MenuEntries } from "./menu-items"
import {
  TOOLBAR_WIDGETS,
  defaultToolbarItems,
  menuActionIds,
  tidySeparators,
} from "./menu-model"
import { formatShortcut } from "./shortcuts"
import type { EditorAction, MenuItem, ToolbarItem } from "./types"

const BLOCK_TYPES = [
  "paragraph",
  "heading1",
  "heading2",
  "heading3",
  "heading4",
  "heading5",
  "heading6",
  "blockquote",
  "codeBlock",
]

const MORE_ITEMS: MenuItem[] = [
  { type: "group", label: "View", items: ["focusMode", "invisibleCharacters"] },
  "separator",
  {
    type: "group",
    label: "Export",
    items: ["copyMarkdown", "downloadMarkdown"],
  },
]

/** Every action id a toolbar mentions, menus included. */
function toolbarIds(items: ToolbarItem[]): string[] {
  return items.flatMap((item) => {
    if (typeof item !== "string") {
      return item.type === "group"
        ? toolbarIds(item.items)
        : menuActionIds(item.items)
    }
    if (item === "heading" || item === "blockType") return BLOCK_TYPES
    return TOOLBAR_WIDGETS.has(item) ? [] : [item]
  })
}

/** The `/` menu's actions, grouped, for the Insert menu. */
function insertMenu(actions: EditorAction[]): MenuItem[] {
  const groups = new Map<string, string[]>()
  for (const action of actions) {
    if (!action.slashCommand) continue
    const group = action.group ?? "More"
    groups.set(group, [...(groups.get(group) ?? []), action.id])
  }
  return [...groups].flatMap(([label, items], index): MenuItem[] => [
    ...(index > 0 ? ["separator"] : []),
    { type: "group", label, items },
  ])
}

/**
 * The formatting toolbar, drawn from `items`: action ids become buttons
 * (toggles when the action has a pressed state), plus the block type,
 * Insert, AI and More menus. Every button runs through the action registry,
 * so it does what the same id does in menus, `/` and code.
 */
export function EditorToolbar({
  items,
  position = "top",
  className,
  label = "Formatting",
}: {
  items?: ToolbarItem[]
  position?: "top" | "bottom"
  className?: string
  label?: string
}) {
  const { editor, registry, features } = useRichTextEditorContext()
  const all = registry.list()
  const aiIds = all.filter((a) => a.id.startsWith("ai.")).map((a) => a.id)
  const list = items ?? defaultToolbarItems(features, aiIds.length > 0)
  const resolved = useEditorActions(toolbarIds(list))
  const byId = new Map(resolved.map((entry) => [entry.id, entry]))
  const run = useRunAction("toolbar")
  // Menus hand focus back to the text, not their trigger, so typing goes on.
  const toEditor = () => editor.view.dom as HTMLElement
  const editable = editor.isEditable

  const menu = (
    key: string,
    trigger: React.ReactNode,
    entries: MenuItem[],
    align: "start" | "end" = "start"
  ) => (
    <DropdownMenu key={key}>
      {trigger}
      <DropdownMenuContent
        finalFocus={toEditor}
        align={align}
        className="max-h-96 min-w-56"
      >
        <MenuEntries
          items={entries}
          kind="dropdown"
          source="toolbar"
          onRun={run}
        />
      </DropdownMenuContent>
    </DropdownMenu>
  )

  const render = (
    item: ToolbarItem,
    index: number
  ): { separator: boolean; node?: React.ReactNode }[] => {
    if (item === "separator") return [{ separator: true }]
    if (item === "spacer") {
      return [
        {
          separator: false,
          node: <span key={`spacer-${index}`} aria-hidden className="flex-1" />,
        },
      ]
    }
    if (item === "heading" || item === "blockType") {
      const options = BLOCK_TYPES.flatMap((id) => {
        const entry = byId.get(id)
        return entry?.state.visible ? [entry] : []
      })
      if (options.length < 2) return []
      return [
        {
          separator: false,
          node: (
            <BlockTypeMenu
              key="block-type"
              options={options}
              onSelect={run}
              toEditor={toEditor}
            />
          ),
        },
      ]
    }
    if (item === "insert") {
      const entries = insertMenu(all)
      if (!entries.length) return []
      return [
        {
          separator: false,
          node: menu(
            "insert",
            <ToolbarMenuTrigger
              icon="plus"
              label="Insert"
              value="Insert"
              disabled={!editable}
            />,
            entries
          ),
        },
      ]
    }
    if (item === "ai") {
      if (!aiIds.length) return []
      return [
        {
          separator: false,
          node: menu(
            "ai",
            <ToolbarMenuTrigger
              icon={SparklesIcon}
              label="AI"
              value="AI"
              disabled={!editable}
            />,
            aiIds
          ),
        },
      ]
    }
    if (item === "more") {
      return [
        {
          separator: false,
          node: menu(
            "more",
            <ToolbarMenuTrigger icon="more" label="More" className="px-1" />,
            MORE_ITEMS,
            "end"
          ),
        },
      ]
    }
    if (typeof item !== "string") {
      if (item.type === "menu") {
        return [
          {
            separator: false,
            node: menu(
              `menu-${index}`,
              <ToolbarMenuTrigger
                icon={item.icon}
                label={item.label}
                value={item.icon ? undefined : item.label}
              />,
              item.items
            ),
          },
        ]
      }
      const children = tidySeparators(item.items.flatMap(render))
      if (!children.some((child) => !child.separator)) return []
      return [
        {
          separator: false,
          node: (
            <div
              key={`group-${index}`}
              role="group"
              aria-label={item.label}
              className="flex items-center gap-0.5"
            >
              {children.map((child, i) =>
                child.separator ? <ToolbarSeparator key={i} /> : child.node
              )}
            </div>
          ),
        },
      ]
    }
    const entry = byId.get(item)
    if (!entry?.state.visible) return []
    return [
      {
        separator: false,
        node: <ActionControl key={item} entry={entry} onRun={run} />,
      },
    ]
  }

  const entries = tidySeparators(list.flatMap(render))

  return (
    <div
      role="toolbar"
      aria-label={label}
      aria-orientation="horizontal"
      data-slot="rich-text-toolbar"
      data-position={position}
      className={cn(
        "z-10 flex flex-wrap items-center gap-0.5 bg-background px-1.5 py-1",
        position === "top"
          ? "sticky top-0 rounded-t-[inherit] border-b border-border"
          : "rounded-b-[inherit] border-t border-border",
        className
      )}
    >
      {entries.map((entry, index) =>
        entry.separator ? (
          <ToolbarSeparator key={`separator-${index}`} />
        ) : (
          entry.node
        )
      )}
    </div>
  )
}

/** One action as a toolbar button: a toggle when it can be pressed. */
function ActionControl({
  entry,
  onRun,
}: {
  entry: ResolvedAction
  onRun: (id: string) => void
}) {
  const { action, state } = entry
  if (action.isActive) {
    return (
      <ToolbarToggle
        icon={action.icon}
        label={action.label}
        shortcut={action.shortcut}
        pressed={state.active}
        disabled={!state.enabled}
        onPressedChange={() => onRun(action.id)}
      />
    )
  }
  return (
    <ToolbarAction
      icon={action.icon}
      label={action.label}
      shortcut={action.shortcut}
      disabled={!state.enabled}
      busy={state.running}
      onClick={() => onRun(action.id)}
    />
  )
}

/** Text, headings, quote and code block, showing what the caret is in. */
function BlockTypeMenu({
  options,
  onSelect,
  toEditor,
}: {
  options: ResolvedAction[]
  onSelect: (id: string) => void
  toEditor: () => HTMLElement
}) {
  // The most specific block wins: a heading inside a quote shows the heading.
  const current =
    options.find(
      (o) => o.id !== "paragraph" && o.id !== "blockquote" && o.state.active
    ) ??
    options.find((o) => o.id === "blockquote" && o.state.active) ??
    options.find((o) => o.id === "paragraph")
  const disabled = !options.some((option) => option.state.enabled)
  return (
    <DropdownMenu>
      <ToolbarMenuTrigger
        label="Block type"
        value={current?.action.label ?? "Text"}
        disabled={disabled}
        className="w-[6.75rem] justify-between"
      />
      <DropdownMenuContent finalFocus={toEditor} className="min-w-44">
        <DropdownMenuRadioGroup
          value={current?.id ?? "paragraph"}
          onValueChange={(value) => {
            if (value !== current?.id) onSelect(String(value))
          }}
        >
          {options.map(({ id, action, state }) => (
            <DropdownMenuRadioItem
              key={id}
              value={id}
              disabled={!state.enabled && !state.active}
            >
              <span
                className={
                  id.startsWith("heading")
                    ? "font-semibold"
                    : id === "codeBlock"
                      ? "font-mono"
                      : undefined
                }
              >
                {action.label}
              </span>
              {action.shortcut && (
                <DropdownMenuShortcut>
                  {formatShortcut(action.shortcut)}
                </DropdownMenuShortcut>
              )}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
