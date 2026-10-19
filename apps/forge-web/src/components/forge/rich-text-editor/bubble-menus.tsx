import * as React from "react"
import {
  Delete02Icon,
  DeleteColumnIcon,
  DeleteRowIcon,
  InsertColumnLeftIcon,
  InsertColumnRightIcon,
  InsertRowDownIcon,
  InsertRowUpIcon,
  SparklesIcon,
  Unlink02Icon,
} from "@hugeicons/core-free-icons"
import { NodeSelection } from "@tiptap/pm/state"
import { CellSelection } from "@tiptap/pm/tables"
import { useEditorState, type Editor } from "@tiptap/react"
import { BubbleMenu } from "@tiptap/react/menus"
import { cn } from "cn"

import {
  DropdownMenu,
  DropdownMenuContent,
} from "@/components/ui/dropdown-menu"
import { Icon } from "../icon"
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
} from "./context"
import { isAlive } from "./core/lifecycle"
import { MenuEntries } from "./menu-items"
import { DEFAULT_BUBBLE_ITEMS, tidySeparators } from "./menu-model"
import type { EditorUiHandlers, ToolbarItem } from "./types"

const MENU =
  "z-30 flex items-center gap-0.5 rounded-(--radius-band) border border-border bg-popover p-1 text-popover-foreground shadow-(--shadow-float)"

type MenuProps = {
  editor: Editor
  ui: () => EditorUiHandlers
  scrollTarget: HTMLElement
}

const hasNode = (editor: Editor, name: string) =>
  Boolean(editor.schema.nodes[name])

/**
 * Formatting for a text selection, drawn from `items` through the action
 * registry; `"ai"` opens the AI actions.
 */
function TextMenu({
  editor,
  scrollTarget,
  items,
  hidden,
}: Omit<MenuProps, "ui"> & { items: ToolbarItem[]; hidden: boolean }) {
  const { registry } = useRichTextEditorContext()
  const ids = items.filter(
    (item): item is string =>
      typeof item === "string" && item !== "separator" && item !== "ai"
  )
  const resolved = useEditorActions(ids, "bubbleMenu")
  const byId = new Map(resolved.map((entry) => [entry.id, entry]))
  const run = useRunAction("bubbleMenu")
  const aiIds = registry
    .list()
    .filter((action) => action.id.startsWith("ai."))
    .map((action) => action.id)

  type Entry = { separator: boolean; node: React.ReactNode }
  const entries = tidySeparators(
    items.flatMap((item): Entry[] => {
      if (item === "separator") return [{ separator: true, node: null }]
      if (item === "ai") {
        if (!aiIds.length) return []
        return [
          {
            separator: false,
            node: (
              <DropdownMenu key="ai">
                <ToolbarMenuTrigger icon={SparklesIcon} label="AI" value="AI" />
                <DropdownMenuContent
                  finalFocus={() => editor.view.dom as HTMLElement}
                  className="min-w-56"
                >
                  <MenuEntries
                    items={aiIds}
                    kind="dropdown"
                    source="bubbleMenu"
                    onRun={run}
                  />
                </DropdownMenuContent>
              </DropdownMenu>
            ),
          },
        ]
      }
      if (typeof item !== "string") return []
      const entry = byId.get(item)
      if (!entry?.state.visible) return []
      const { action, state } = entry
      return [
        {
          separator: false,
          node: action.isActive ? (
            <ToolbarToggle
              key={item}
              icon={action.icon}
              label={action.label}
              shortcut={action.shortcut}
              pressed={state.active}
              disabled={!state.enabled}
              onPressedChange={() => run(action.id)}
            />
          ) : (
            <ToolbarAction
              key={item}
              icon={action.icon}
              label={action.label}
              shortcut={action.shortcut}
              disabled={!state.enabled}
              busy={state.running}
              onClick={() => run(action.id)}
            />
          ),
        },
      ]
    })
  )

  return (
    <BubbleMenu
      editor={editor}
      pluginKey="richTextSelectionMenu"
      className={cn(MENU, hidden && "hidden")}
      options={{ placement: "top", offset: 8, scrollTarget }}
      shouldShow={({ editor, state, from, to, view }) => {
        if (!editor.isEditable || from === to || !view.hasFocus()) return false
        const { selection } = state
        if (selection instanceof NodeSelection) return false
        if (selection instanceof CellSelection) return false
        if (editor.isActive("codeBlock")) return false
        return state.doc.textBetween(from, to, " ").trim().length > 0
      }}
    >
      {entries.map((entry, index) =>
        entry.separator ? <ToolbarSeparator key={index} /> : entry.node
      )}
    </BubbleMenu>
  )
}

/** Shown with the caret in a link: where it goes, edit, copy, remove. */
function LinkMenu({ editor, ui, scrollTarget }: MenuProps) {
  const href = useEditorState({
    editor,
    selector: ({ editor }) =>
      (editor.getAttributes("link").href as string | undefined) ?? "",
  })
  return (
    <BubbleMenu
      editor={editor}
      pluginKey="richTextLinkMenu"
      className={cn(MENU, "max-w-sm")}
      options={{ placement: "bottom-start", offset: 6, scrollTarget }}
      shouldShow={({ editor, from, to, view }) =>
        editor.isEditable &&
        from === to &&
        view.hasFocus() &&
        editor.isActive("link")
      }
    >
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer nofollow"
        className="flex min-w-0 items-center gap-1.5 rounded-(--radius-item) px-1.5 py-1 text-xs text-foreground hover:bg-muted"
      >
        <Icon
          icon="external"
          size={14}
          className="shrink-0 text-muted-foreground"
        />
        <span className="truncate">{href}</span>
      </a>
      <ToolbarSeparator />
      <ToolbarAction
        icon="settings"
        label="Edit link"
        shortcut="Mod-k"
        onClick={() => ui().openDialog({ kind: "link" })}
      />
      <ToolbarAction
        icon="copy"
        label="Copy link"
        onClick={() => void navigator.clipboard.writeText(href)}
      />
      <ToolbarAction
        icon={Unlink02Icon}
        label="Remove link"
        onClick={() =>
          editor.chain().focus().extendMarkRange("link").unsetLink().run()
        }
      />
    </BubbleMenu>
  )
}

/** The table under the caret, found in the DOM for positioning the menu. */
function tableElement(editor: Editor) {
  const { node } = editor.view.domAtPos(editor.state.selection.from)
  const element = node instanceof Element ? node : node.parentElement
  return element?.closest(".tableWrapper") ?? element?.closest("table") ?? null
}

/**
 * Rows and columns of the table under the caret. GFM tables have one header
 * row and no merged cells, so those are all the menu offers.
 */
function TableMenu({ editor, scrollTarget }: MenuProps) {
  const can = useEditorState({
    editor,
    selector: ({ editor }) =>
      isAlive(editor)
        ? {
            deleteRow: editor.can().deleteRow(),
            deleteColumn: editor.can().deleteColumn(),
          }
        : { deleteRow: false, deleteColumn: false },
  })
  const chain = () => editor.chain().focus()
  return (
    <BubbleMenu
      editor={editor}
      pluginKey="richTextTableMenu"
      className={MENU}
      options={{ placement: "top-start", offset: 6, scrollTarget }}
      getReferencedVirtualElement={() => {
        const table = tableElement(editor)
        return table
          ? { getBoundingClientRect: () => table.getBoundingClientRect() }
          : null
      }}
      // Out of the way of the selection menu while text is selected.
      shouldShow={({ editor, view, state, from, to }) =>
        editor.isEditable &&
        view.hasFocus() &&
        editor.isActive("table") &&
        (from === to || state.selection instanceof CellSelection)
      }
    >
      <ToolbarAction
        icon={InsertRowUpIcon}
        label="Row above"
        onClick={() => chain().addRowBefore().run()}
      />
      <ToolbarAction
        icon={InsertRowDownIcon}
        label="Row below"
        onClick={() => chain().addRowAfter().run()}
      />
      <ToolbarAction
        icon={DeleteRowIcon}
        label="Delete row"
        disabled={!can.deleteRow}
        onClick={() => chain().deleteRow().run()}
      />
      <ToolbarSeparator />
      <ToolbarAction
        icon={InsertColumnLeftIcon}
        label="Column left"
        onClick={() => chain().addColumnBefore().run()}
      />
      <ToolbarAction
        icon={InsertColumnRightIcon}
        label="Column right"
        onClick={() => chain().addColumnAfter().run()}
      />
      <ToolbarAction
        icon={DeleteColumnIcon}
        label="Delete column"
        disabled={!can.deleteColumn}
        onClick={() => chain().deleteColumn().run()}
      />
      <ToolbarSeparator />
      <ToolbarAction
        icon={Delete02Icon}
        label="Delete table"
        onClick={() => chain().deleteTable().run()}
        className="hover:bg-destructive/10 hover:text-destructive"
      />
    </BubbleMenu>
  )
}

/** A selected image: alt text and source, open, delete. */
function ImageMenu({ editor, ui, scrollTarget }: MenuProps) {
  const src = useEditorState({
    editor,
    selector: ({ editor }) =>
      (editor.getAttributes("image").src as string | undefined) ?? "",
  })
  return (
    <BubbleMenu
      editor={editor}
      pluginKey="richTextImageMenu"
      className={MENU}
      options={{ placement: "top", offset: 8, scrollTarget }}
      shouldShow={({ editor, state }) =>
        editor.isEditable &&
        state.selection instanceof NodeSelection &&
        state.selection.node.type.name === "image"
      }
    >
      <ToolbarAction
        icon="settings"
        label="Alt text and source"
        onClick={() =>
          ui().openDialog({
            kind: "image",
            pos: editor.state.selection.from,
          })
        }
      />
      {!src.startsWith("data:") && (
        <ToolbarAction
          icon="external"
          label="Open image"
          onClick={() => window.open(src, "_blank", "noopener,noreferrer")}
        />
      )}
      <ToolbarSeparator />
      <ToolbarAction
        icon={Delete02Icon}
        label="Delete image"
        onClick={() => editor.chain().focus().deleteSelection().run()}
        className="hover:bg-destructive/10 hover:text-destructive"
      />
    </BubbleMenu>
  )
}

/** Every contextual menu the editor shows, for the features it has. */
export function EditorBubbleMenu({
  scrollTarget,
  items = DEFAULT_BUBBLE_ITEMS,
}: {
  scrollTarget: HTMLElement
  /** What the selection menu shows. */
  items?: ToolbarItem[]
}) {
  const { editor, ui, ai } = useRichTextEditorContext()
  // While the AI panel is open, focus is there: the selection menu would
  // float over the text with nothing to act on. Hidden, not unmounted: its
  // AI menu may still be closing, and unmounting it would send focus back
  // to the text instead of the prompt box.
  const aiOpen = React.useSyncExternalStore(
    ai.subscribe,
    () => ai.getState().status !== "idle"
  )
  const props = { editor, ui, scrollTarget }
  return (
    <>
      <TextMenu
        editor={editor}
        scrollTarget={scrollTarget}
        items={items}
        hidden={aiOpen}
      />
      {editor.schema.marks.link && <LinkMenu {...props} />}
      {hasNode(editor, "table") && <TableMenu {...props} />}
      {hasNode(editor, "image") && <ImageMenu {...props} />}
    </>
  )
}
