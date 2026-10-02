import * as React from "react"
import {
  ArrowDown02Icon,
  ArrowUp02Icon,
  Copy01Icon,
  Delete02Icon,
} from "@hugeicons/core-free-icons"
import { DragHandle } from "@tiptap/extension-drag-handle-react"
import type { Node as PMNode } from "@tiptap/pm/model"
import { TextSelection } from "@tiptap/pm/state"
import type { Editor } from "@tiptap/react"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Icon } from "../icon"
import { BLOCK_COMMANDS, hasExtension } from "./commands"
import type { RichTextActions } from "./types"

// Blocks "Turn into" can convert: text blocks, lists and quotes.
const CONVERTIBLE = new Set([
  "paragraph",
  "heading",
  "codeBlock",
  "blockquote",
  "bulletList",
  "orderedList",
  "taskList",
])

const TURN_INTO = [
  "paragraph",
  "heading-1",
  "heading-2",
  "heading-3",
  "bullet-list",
  "ordered-list",
  "task-list",
  "quote",
  "code-block",
]

type Target = { node: PMNode | null; pos: number }

/**
 * The handle beside the hovered block: `+` adds a block below through the
 * `/` menu; the grip drags the block, and clicking it opens a menu to turn
 * it into another kind, move, duplicate or delete it.
 */
export function BlockHandle({
  editor,
  actions,
}: {
  editor: Editor
  actions: RichTextActions
}) {
  const target = React.useRef<Target>({ node: null, pos: -1 })
  const [menuOpen, setMenuOpen] = React.useState(false)
  const [menuTarget, setMenuTarget] = React.useState<Target | null>(null)

  const openMenu = (open: boolean) => {
    setMenuOpen(open)
    setMenuTarget(open ? target.current : null)
    // Keep the handle on this block while its menu is open.
    if (open) editor.commands.lockDragHandle()
    else editor.commands.unlockDragHandle()
  }

  const current = () => {
    const { node, pos } = target.current
    return node && pos >= 0 ? { node, pos } : null
  }

  const addBelow = () => {
    const block = current()
    if (!block) return
    const { node, pos } = block
    if (node.type.name === "paragraph" && node.content.size === 0) {
      editor
        .chain()
        .focus()
        .setTextSelection(pos + 1)
        .insertContent("/")
        .run()
      return
    }
    const after = pos + node.nodeSize
    editor
      .chain()
      .focus()
      .insertContentAt(after, {
        type: "paragraph",
        content: [{ type: "text", text: "/" }],
      })
      .setTextSelection(after + 2)
      .run()
  }

  /** Selects the whole block so a command converts all of it. */
  const selectBlock = (node: PMNode, pos: number) =>
    editor
      .chain()
      .focus()
      .command(({ tr }) => {
        tr.setSelection(
          TextSelection.between(
            tr.doc.resolve(pos + 1),
            tr.doc.resolve(pos + node.nodeSize - 1)
          )
        )
        return true
      })
      .run()

  const turnInto = (id: string) => {
    const block = current()
    const command = BLOCK_COMMANDS.find((c) => c.id === id)
    if (!block || !command) return
    selectBlock(block.node, block.pos)
    command.run(editor, actions)
  }

  const move = (direction: -1 | 1) => {
    const block = current()
    if (!block) return
    const { node, pos } = block
    editor
      .chain()
      .focus()
      .command(({ tr }) => {
        const $pos = tr.doc.resolve(pos)
        const index = $pos.index()
        const sibling = $pos.parent.maybeChild(index + direction)
        if (!sibling) return false
        tr.delete(pos, pos + node.nodeSize)
        const insertAt =
          direction === -1 ? pos - sibling.nodeSize : pos + sibling.nodeSize
        tr.insert(insertAt, node)
        tr.setSelection(TextSelection.near(tr.doc.resolve(insertAt + 1)))
        tr.scrollIntoView()
        return true
      })
      .run()
  }

  const duplicate = () => {
    const block = current()
    if (!block) return
    editor
      .chain()
      .focus()
      .insertContentAt(block.pos + block.node.nodeSize, block.node.toJSON())
      .run()
  }

  const remove = () => {
    const block = current()
    if (!block) return
    editor
      .chain()
      .focus()
      .deleteRange({ from: block.pos, to: block.pos + block.node.nodeSize })
      .run()
  }

  const turnIntoCommands = BLOCK_COMMANDS.filter(
    (command) =>
      TURN_INTO.includes(command.id) &&
      (!command.requires || hasExtension(editor, command.requires))
  )
  const menuNode = menuTarget?.node ?? null
  const index =
    menuTarget && menuTarget.pos >= 0
      ? editor.state.doc.resolve(menuTarget.pos).index()
      : 0
  const siblings = editor.state.doc.childCount

  return (
    <DragHandle
      editor={editor}
      className="drag-handle z-20"
      onNodeChange={({ node, pos }) => {
        target.current = { node, pos }
      }}
    >
      <div className="flex items-center gap-px pr-1.5">
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Add a block below"
          onClick={addBelow}
          className="text-subtle hover:text-foreground"
        >
          <Icon icon="plus" />
        </Button>
        <div className="relative">
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Drag to move, or click for block options"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => openMenu(true)}
            className="cursor-grab text-subtle hover:text-foreground active:cursor-grabbing"
          >
            <Icon icon="grip" />
          </Button>
          {/* Anchors the menu, which opens on click rather than on the
              mousedown that starts a drag. */}
          <DropdownMenu open={menuOpen} onOpenChange={openMenu}>
            <DropdownMenuTrigger
              nativeButton={false}
              render={
                <span
                  aria-hidden="true"
                  tabIndex={-1}
                  className="pointer-events-none absolute inset-0"
                />
              }
            />
            <DropdownMenuContent
              side="bottom"
              align="start"
              className="min-w-52"
              finalFocus={() => editor.view.dom as HTMLElement}
            >
              {menuNode && CONVERTIBLE.has(menuNode.type.name) && (
                <>
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger>
                      <Icon icon="refresh" />
                      Turn into
                    </DropdownMenuSubTrigger>
                    <DropdownMenuSubContent className="min-w-48">
                      {turnIntoCommands.map((command) => (
                        <DropdownMenuItem
                          key={command.id}
                          onClick={() => turnInto(command.id)}
                        >
                          <Icon icon={command.icon} />
                          {command.label}
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                  <DropdownMenuSeparator />
                </>
              )}
              <DropdownMenuGroup>
                <DropdownMenuItem
                  disabled={index === 0}
                  onClick={() => move(-1)}
                >
                  <Icon icon={ArrowUp02Icon} />
                  Move up
                </DropdownMenuItem>
                <DropdownMenuItem
                  disabled={index >= siblings - 1}
                  onClick={() => move(1)}
                >
                  <Icon icon={ArrowDown02Icon} />
                  Move down
                </DropdownMenuItem>
                <DropdownMenuItem onClick={duplicate}>
                  <Icon icon={Copy01Icon} />
                  Duplicate
                </DropdownMenuItem>
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuItem variant="destructive" onClick={remove}>
                <Icon icon={Delete02Icon} />
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </DragHandle>
  )
}
