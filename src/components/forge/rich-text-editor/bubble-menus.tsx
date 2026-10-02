import {
  Delete02Icon,
  DeleteColumnIcon,
  DeleteRowIcon,
  HighlighterIcon,
  InsertColumnLeftIcon,
  InsertColumnRightIcon,
  InsertRowDownIcon,
  InsertRowUpIcon,
  Layout2ColumnIcon,
  Layout2RowIcon,
  SourceCodeIcon,
  TableCellsMergeIcon,
  TableCellsSplitIcon,
  TextBoldIcon,
  TextItalicIcon,
  TextStrikethroughIcon,
  TextUnderlineIcon,
  Unlink02Icon,
} from "@hugeicons/core-free-icons"
import { NodeSelection } from "@tiptap/pm/state"
import { CellSelection } from "@tiptap/pm/tables"
import { useEditorState, type Editor } from "@tiptap/react"
import { BubbleMenu } from "@tiptap/react/menus"
import { cn } from "cn"

import { Icon } from "../icon"
import { hasExtension } from "./commands"
import { ToolbarAction, ToolbarSeparator, ToolbarToggle } from "./controls"
import type { RichTextActions } from "./types"

const MENU =
  "z-30 flex items-center gap-0.5 rounded-(--radius-band) border border-border bg-popover p-1 text-popover-foreground shadow-(--shadow-float)"

type MenuProps = {
  editor: Editor
  actions: RichTextActions
  scrollTarget: HTMLElement
}

/** Formatting for a text selection: marks, highlight and link. */
function TextMenu({ editor, actions, scrollTarget }: MenuProps) {
  const state = useEditorState({
    editor,
    selector: ({ editor }) => ({
      bold: editor.isActive("bold"),
      italic: editor.isActive("italic"),
      underline: editor.isActive("underline"),
      strike: editor.isActive("strike"),
      code: editor.isActive("code"),
      highlight: editor.isActive("highlight"),
      link: editor.isActive("link"),
    }),
  })
  const chain = () => editor.chain().focus()
  const highlight = hasExtension(editor, "highlight")

  return (
    <BubbleMenu
      editor={editor}
      pluginKey="richTextSelectionMenu"
      className={MENU}
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
      <ToolbarToggle
        icon={TextBoldIcon}
        label="Bold"
        shortcut="Mod-b"
        pressed={state.bold}
        onPressedChange={() => chain().toggleBold().run()}
      />
      <ToolbarToggle
        icon={TextItalicIcon}
        label="Italic"
        shortcut="Mod-i"
        pressed={state.italic}
        onPressedChange={() => chain().toggleItalic().run()}
      />
      <ToolbarToggle
        icon={TextUnderlineIcon}
        label="Underline"
        shortcut="Mod-u"
        pressed={state.underline}
        onPressedChange={() => chain().toggleUnderline().run()}
      />
      <ToolbarToggle
        icon={TextStrikethroughIcon}
        label="Strikethrough"
        shortcut="Mod-Shift-s"
        pressed={state.strike}
        onPressedChange={() => chain().toggleStrike().run()}
      />
      <ToolbarToggle
        icon={SourceCodeIcon}
        label="Inline code"
        shortcut="Mod-e"
        pressed={state.code}
        onPressedChange={() => chain().toggleCode().run()}
      />
      {highlight && (
        <ToolbarToggle
          icon={HighlighterIcon}
          label="Highlight"
          shortcut="Mod-Shift-h"
          pressed={state.highlight}
          onPressedChange={() => chain().toggleHighlight().run()}
        />
      )}
      <ToolbarSeparator />
      <ToolbarToggle
        icon="link"
        label={state.link ? "Edit link" : "Link"}
        shortcut="Mod-k"
        pressed={state.link}
        onPressedChange={() => actions.openDialog({ kind: "link" })}
      />
    </BubbleMenu>
  )
}

/** Shown with the caret in a link: where it goes, edit, copy, remove. */
function LinkMenu({ editor, actions, scrollTarget }: MenuProps) {
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
        onClick={() => actions.openDialog({ kind: "link" })}
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

/** Rows, columns, headers and cells of the table under the caret. */
function TableMenu({ editor, scrollTarget }: MenuProps) {
  const can = useEditorState({
    editor,
    selector: ({ editor }) =>
      editor.isDestroyed
        ? { merge: false, split: false, deleteRow: false, deleteColumn: false }
        : {
            merge: editor.can().mergeCells(),
            split: editor.can().splitCell(),
            deleteRow: editor.can().deleteRow(),
            deleteColumn: editor.can().deleteColumn(),
          },
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
        icon={TableCellsMergeIcon}
        label="Merge cells"
        disabled={!can.merge}
        onClick={() => chain().mergeCells().run()}
      />
      <ToolbarAction
        icon={TableCellsSplitIcon}
        label="Split cell"
        disabled={!can.split}
        onClick={() => chain().splitCell().run()}
      />
      <ToolbarAction
        icon={Layout2RowIcon}
        label="Header row"
        onClick={() => chain().toggleHeaderRow().run()}
      />
      <ToolbarAction
        icon={Layout2ColumnIcon}
        label="Header column"
        onClick={() => chain().toggleHeaderColumn().run()}
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

/** A selected image: alt text, original size, open, delete. */
function ImageMenu({ editor, actions, scrollTarget }: MenuProps) {
  const image = useEditorState({
    editor,
    selector: ({ editor }) => ({
      src: (editor.getAttributes("image").src as string | undefined) ?? "",
      resized: Boolean(editor.getAttributes("image").width),
    }),
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
          actions.openDialog({
            kind: "image",
            pos: editor.state.selection.from,
          })
        }
      />
      <ToolbarAction
        icon="refresh"
        label="Original size"
        disabled={!image.resized}
        onClick={() =>
          editor
            .chain()
            .focus()
            .updateAttributes("image", { width: null, height: null })
            .run()
        }
      />
      {!image.src.startsWith("data:") && (
        <ToolbarAction
          icon="external"
          label="Open image"
          onClick={() =>
            window.open(image.src, "_blank", "noopener,noreferrer")
          }
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
export function RichTextBubbleMenus(props: MenuProps) {
  const { editor } = props
  return (
    <>
      <TextMenu {...props} />
      <LinkMenu {...props} />
      {hasExtension(editor, "table") && <TableMenu {...props} />}
      {hasExtension(editor, "image") && <ImageMenu {...props} />}
    </>
  )
}
