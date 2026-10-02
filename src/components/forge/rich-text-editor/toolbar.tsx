import * as React from "react"
import {
  CodeSquareIcon,
  EraserIcon,
  FullScreenIcon,
  HighlighterIcon,
  LeftToRightListBulletIcon,
  LeftToRightListNumberIcon,
  CheckListIcon,
  ListIndentDecreaseIcon,
  ListIndentIncreaseIcon,
  MinimizeScreenIcon,
  RedoIcon,
  SearchReplaceIcon,
  SourceCodeIcon,
  SubscriptIcon,
  SuperscriptIcon,
  TableOfContentsIcon,
  TextAlignCenterIcon,
  TextAlignJustifyCenterIcon,
  TextAlignLeftIcon,
  TextAlignRightIcon,
  TextBoldIcon,
  TextColorIcon,
  TextFontIcon,
  TextItalicIcon,
  TextStrikethroughIcon,
  TextUnderlineIcon,
  UndoIcon,
} from "@hugeicons/core-free-icons"
import { useEditorState, type Editor } from "@tiptap/react"

import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
} from "@/components/ui/dropdown-menu"
import { Popover, PopoverContent } from "@/components/ui/popover"
import { Icon } from "../icon"
import { availableCommands, hasExtension, type BlockCommand } from "./commands"
import { downloadText } from "./content"
import {
  SwatchGrid,
  ToolbarAction,
  ToolbarMenuTrigger,
  ToolbarSeparator,
  ToolbarToggle,
} from "./controls"
import { HIGHLIGHT_COLORS, TEXT_COLORS } from "./palette"
import { formatShortcut } from "./shortcuts"
import type { RichTextActions, RichTextFeatures } from "./types"

const BLOCK_TYPES = [
  { value: "paragraph", label: "Text" },
  { value: "h1", label: "Heading 1" },
  { value: "h2", label: "Heading 2" },
  { value: "h3", label: "Heading 3" },
  { value: "h4", label: "Heading 4" },
  { value: "h5", label: "Heading 5" },
  { value: "h6", label: "Heading 6" },
  { value: "blockquote", label: "Quote" },
  { value: "codeBlock", label: "Code block" },
] as const

type BlockType = (typeof BLOCK_TYPES)[number]["value"]

const FONT_FAMILIES = [
  { value: "", label: "Default" },
  { value: "ui-serif, Georgia, Cambria, serif", label: "Serif" },
  {
    value: "ui-monospace, SFMono-Regular, Menlo, monospace",
    label: "Monospace",
  },
]

// rem, so the person's text-size preference still applies.
const FONT_SIZES = [
  { value: "0.75rem", label: "Small" },
  { value: "", label: "Normal" },
  { value: "1rem", label: "Medium" },
  { value: "1.25rem", label: "Large" },
  { value: "1.5rem", label: "Huge" },
]

const LINE_HEIGHTS = [
  { value: "1.3", label: "Tight" },
  { value: "", label: "Normal" },
  { value: "1.9", label: "Relaxed" },
  { value: "2.3", label: "Double" },
]

const ALIGNMENTS = [
  {
    value: "left",
    label: "Align left",
    icon: TextAlignLeftIcon,
    shortcut: "Mod-Shift-l",
  },
  {
    value: "center",
    label: "Align center",
    icon: TextAlignCenterIcon,
    shortcut: "Mod-Shift-e",
  },
  {
    value: "right",
    label: "Align right",
    icon: TextAlignRightIcon,
    shortcut: "Mod-Shift-r",
  },
  {
    value: "justify",
    label: "Justify",
    icon: TextAlignJustifyCenterIcon,
    shortcut: "Mod-Shift-j",
  },
]

function currentBlock(editor: Editor): BlockType {
  for (const level of [1, 2, 3, 4, 5, 6] as const) {
    if (editor.isActive("heading", { level })) return `h${level}`
  }
  if (editor.isActive("codeBlock")) return "codeBlock"
  if (editor.isActive("blockquote")) return "blockquote"
  return "paragraph"
}

function setBlock(editor: Editor, type: BlockType) {
  const chain = editor.chain().focus()
  if (type === "paragraph") {
    // Leave quotes and code blocks for a plain paragraph.
    if (editor.isActive("codeBlock")) chain.toggleCodeBlock()
    else if (editor.isActive("blockquote")) chain.toggleBlockquote()
    chain.setParagraph().run()
  } else if (type === "blockquote") chain.toggleBlockquote().run()
  else if (type === "codeBlock") chain.toggleCodeBlock().run()
  else chain.setHeading({ level: Number(type.slice(1)) as 1 }).run()
}

/** The item type the list commands act on at the selection. */
function listItemType(editor: Editor) {
  return editor.isActive("taskItem") && editor.schema.nodes.taskItem
    ? "taskItem"
    : "listItem"
}

export interface ToolbarState {
  findOpen: boolean
  outlineOpen: boolean
  fullscreen: boolean
  focusMode: boolean
}

/**
 * The fixed formatting bar: history, block type, text style, marks, colour,
 * lists, alignment, the Insert menu, then view tools on the right.
 */
export function RichTextToolbar({
  editor,
  features,
  actions,
  view,
  onFocusModeChange,
}: {
  editor: Editor
  features: RichTextFeatures
  actions: RichTextActions
  view: ToolbarState
  onFocusModeChange: (on: boolean) => void
}) {
  const state = useEditorState({
    editor,
    selector: ({ editor }) => {
      if (editor.isDestroyed) return null
      const textStyle = editor.getAttributes("textStyle")
      const itemType = listItemType(editor)
      return {
        editable: editor.isEditable,
        canUndo: editor.can().undo(),
        canRedo: editor.can().redo(),
        block: currentBlock(editor),
        bold: editor.isActive("bold"),
        italic: editor.isActive("italic"),
        underline: editor.isActive("underline"),
        strike: editor.isActive("strike"),
        code: editor.isActive("code"),
        subscript: editor.isActive("subscript"),
        superscript: editor.isActive("superscript"),
        bulletList: editor.isActive("bulletList"),
        orderedList: editor.isActive("orderedList"),
        taskList: editor.isActive("taskList"),
        inCodeBlock: editor.isActive("codeBlock"),
        canSink: editor.can().sinkListItem(itemType),
        canLift: editor.can().liftListItem(itemType),
        color: (textStyle.color as string | undefined) ?? null,
        fontFamily: (textStyle.fontFamily as string | undefined) ?? "",
        fontSize: (textStyle.fontSize as string | undefined) ?? "",
        lineHeight: (textStyle.lineHeight as string | undefined) ?? "",
        highlight: editor.isActive("highlight"),
        highlightColor:
          (editor.getAttributes("highlight").color as string | undefined) ??
          null,
        align: (["center", "right", "justify"].find((align) =>
          editor.isActive({ textAlign: align })
        ) ?? "left") as string,
        invisible: hasExtension(editor, "invisibleCharacters")
          ? editor.storage.invisibleCharacters.visibility()
          : false,
      }
    },
  })

  const [colorOpen, setColorOpen] = React.useState(false)
  const commands = React.useMemo(() => availableCommands(editor), [editor])
  // Only while React swaps a discarded editor out (see rich-text-editor.tsx).
  if (!state) return null
  const disabled = !state.editable
  const marksDisabled = disabled || state.inCodeBlock
  const chain = () => editor.chain().focus()
  const alignment =
    ALIGNMENTS.find((a) => a.value === state.align) ?? ALIGNMENTS[0]
  const blockLabel = BLOCK_TYPES.find((b) => b.value === state.block)?.label
  const blockTypes = BLOCK_TYPES.filter(
    (type) =>
      (features.headings || !type.value.startsWith("h")) &&
      (type.value !== "codeBlock" || hasExtension(editor, "codeBlock"))
  )

  const groups = commands.reduce<Record<string, BlockCommand[]>>(
    (acc, command) => {
      ;(acc[command.group] ??= []).push(command)
      return acc
    },
    {}
  )

  const copy = (text: string) => void navigator.clipboard.writeText(text)
  // Menus hand focus back to the text, not their trigger, so typing goes on.
  const toEditor = () => editor.view.dom as HTMLElement

  return (
    <div
      role="toolbar"
      aria-label="Formatting"
      aria-orientation="horizontal"
      data-slot="rich-text-toolbar"
      className="sticky top-0 z-10 flex flex-wrap items-center gap-0.5 rounded-t-[inherit] border-b border-border bg-background px-1.5 py-1"
    >
      <ToolbarAction
        icon={UndoIcon}
        label="Undo"
        shortcut="Mod-z"
        disabled={disabled || !state.canUndo}
        onClick={() => chain().undo().run()}
      />
      <ToolbarAction
        icon={RedoIcon}
        label="Redo"
        shortcut="Mod-Shift-z"
        disabled={disabled || !state.canRedo}
        onClick={() => chain().redo().run()}
      />
      <ToolbarSeparator />

      <DropdownMenu>
        <ToolbarMenuTrigger
          label="Block type"
          value={blockLabel}
          disabled={disabled}
          className="w-[6.75rem] justify-between"
        />
        <DropdownMenuContent finalFocus={toEditor} className="min-w-44">
          <DropdownMenuRadioGroup
            value={state.block}
            onValueChange={(value) => setBlock(editor, value as BlockType)}
          >
            {blockTypes.map((type) => (
              <DropdownMenuRadioItem key={type.value} value={type.value}>
                <span
                  className={
                    type.value.startsWith("h")
                      ? "font-semibold"
                      : type.value === "codeBlock"
                        ? "font-mono"
                        : undefined
                  }
                >
                  {type.label}
                </span>
                {type.value.startsWith("h") && (
                  <DropdownMenuShortcut>
                    {formatShortcut(`Mod-Alt-${type.value.slice(1)}`)}
                  </DropdownMenuShortcut>
                )}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>

      {features.textStyle && (
        <DropdownMenu>
          <ToolbarMenuTrigger
            icon={TextFontIcon}
            label="Font, size and line height"
            disabled={marksDisabled}
            active={Boolean(
              state.fontFamily || state.fontSize || state.lineHeight
            )}
          />
          <DropdownMenuContent finalFocus={toEditor} className="min-w-44">
            <DropdownMenuGroup>
              <DropdownMenuLabel>Font</DropdownMenuLabel>
              <DropdownMenuRadioGroup
                value={state.fontFamily}
                onValueChange={(value) =>
                  value
                    ? chain().setFontFamily(String(value)).run()
                    : chain().unsetFontFamily().run()
                }
              >
                {FONT_FAMILIES.map((font) => (
                  <DropdownMenuRadioItem key={font.label} value={font.value}>
                    <span
                      style={
                        font.value ? { fontFamily: font.value } : undefined
                      }
                    >
                      {font.label}
                    </span>
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel>Size</DropdownMenuLabel>
              <DropdownMenuRadioGroup
                value={state.fontSize}
                onValueChange={(value) =>
                  value
                    ? chain().setFontSize(String(value)).run()
                    : chain().unsetFontSize().run()
                }
              >
                {FONT_SIZES.map((size) => (
                  <DropdownMenuRadioItem key={size.label} value={size.value}>
                    {size.label}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel>Line height</DropdownMenuLabel>
              <DropdownMenuRadioGroup
                value={state.lineHeight}
                onValueChange={(value) =>
                  value
                    ? chain().setLineHeight(String(value)).run()
                    : chain().unsetLineHeight().run()
                }
              >
                {LINE_HEIGHTS.map((height) => (
                  <DropdownMenuRadioItem
                    key={height.label}
                    value={height.value}
                  >
                    {height.label}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
      <ToolbarSeparator />

      <ToolbarToggle
        icon={TextBoldIcon}
        label="Bold"
        shortcut="Mod-b"
        pressed={state.bold}
        disabled={marksDisabled}
        onPressedChange={() => chain().toggleBold().run()}
      />
      <ToolbarToggle
        icon={TextItalicIcon}
        label="Italic"
        shortcut="Mod-i"
        pressed={state.italic}
        disabled={marksDisabled}
        onPressedChange={() => chain().toggleItalic().run()}
      />
      <ToolbarToggle
        icon={TextUnderlineIcon}
        label="Underline"
        shortcut="Mod-u"
        pressed={state.underline}
        disabled={marksDisabled}
        onPressedChange={() => chain().toggleUnderline().run()}
      />
      <ToolbarToggle
        icon={TextStrikethroughIcon}
        label="Strikethrough"
        shortcut="Mod-Shift-s"
        pressed={state.strike}
        disabled={marksDisabled}
        onPressedChange={() => chain().toggleStrike().run()}
      />
      <ToolbarToggle
        icon={SourceCodeIcon}
        label="Inline code"
        shortcut="Mod-e"
        pressed={state.code}
        disabled={marksDisabled}
        onPressedChange={() => chain().toggleCode().run()}
      />
      {features.scripts && (
        <>
          <ToolbarToggle
            icon={SubscriptIcon}
            label="Subscript"
            shortcut="Mod-,"
            pressed={state.subscript}
            disabled={marksDisabled}
            onPressedChange={() => chain().toggleSubscript().run()}
          />
          <ToolbarToggle
            icon={SuperscriptIcon}
            label="Superscript"
            shortcut="Mod-."
            pressed={state.superscript}
            disabled={marksDisabled}
            onPressedChange={() => chain().toggleSuperscript().run()}
          />
        </>
      )}
      {features.textStyle && (
        <Popover open={colorOpen} onOpenChange={setColorOpen}>
          <ToolbarMenuTrigger
            popover
            label="Text colour and highlight"
            disabled={marksDisabled}
            active={Boolean(state.color || state.highlight)}
          >
            <span className="relative grid place-items-center">
              <Icon icon={state.highlight ? HighlighterIcon : TextColorIcon} />
              <span
                aria-hidden="true"
                className="absolute -bottom-1 h-[3px] w-3.5 rounded-full bg-foreground"
                style={{
                  background:
                    state.highlightColor ??
                    state.color ??
                    (state.highlight
                      ? "var(--rte-highlight-yellow)"
                      : undefined),
                }}
              />
            </span>
          </ToolbarMenuTrigger>
          <PopoverContent
            finalFocus={toEditor}
            align="start"
            className="w-auto gap-1 p-1"
          >
            <div className="px-1.5 pt-1 text-2xs text-muted-foreground">
              Text
            </div>
            <SwatchGrid
              label="Text colour"
              kind="text"
              swatches={TEXT_COLORS}
              current={state.color}
              onSelect={(value) => {
                if (value) chain().setColor(value).run()
                else chain().unsetColor().run()
                setColorOpen(false)
              }}
            />
            <div className="px-1.5 pt-1 text-2xs text-muted-foreground">
              Highlight
            </div>
            <SwatchGrid
              label="Highlight"
              kind="highlight"
              swatches={HIGHLIGHT_COLORS}
              current={state.highlight ? state.highlightColor : null}
              onSelect={(value) => {
                if (value) chain().setHighlight({ color: value }).run()
                else chain().unsetHighlight().run()
                setColorOpen(false)
              }}
            />
          </PopoverContent>
        </Popover>
      )}
      <ToolbarAction
        icon="link"
        label="Link"
        shortcut="Mod-k"
        disabled={marksDisabled}
        onClick={() => actions.openDialog({ kind: "link" })}
      />
      <ToolbarAction
        icon={EraserIcon}
        label="Clear formatting"
        disabled={disabled}
        onClick={() => chain().unsetAllMarks().clearNodes().run()}
      />
      <ToolbarSeparator />

      <ToolbarToggle
        icon={LeftToRightListBulletIcon}
        label="Bulleted list"
        shortcut="Mod-Shift-8"
        pressed={state.bulletList}
        disabled={disabled}
        onPressedChange={() => chain().toggleBulletList().run()}
      />
      <ToolbarToggle
        icon={LeftToRightListNumberIcon}
        label="Numbered list"
        shortcut="Mod-Shift-7"
        pressed={state.orderedList}
        disabled={disabled}
        onPressedChange={() => chain().toggleOrderedList().run()}
      />
      {features.taskLists && (
        <ToolbarToggle
          icon={CheckListIcon}
          label="Checklist"
          shortcut="Mod-Shift-9"
          pressed={state.taskList}
          disabled={disabled}
          onPressedChange={() => chain().toggleTaskList().run()}
        />
      )}
      <ToolbarAction
        icon={ListIndentIncreaseIcon}
        label="Indent"
        shortcut="Tab"
        disabled={disabled || !state.canSink}
        onClick={() => chain().sinkListItem(listItemType(editor)).run()}
      />
      <ToolbarAction
        icon={ListIndentDecreaseIcon}
        label="Outdent"
        shortcut="Shift-Tab"
        disabled={disabled || !state.canLift}
        onClick={() => chain().liftListItem(listItemType(editor)).run()}
      />
      {features.alignment && (
        <DropdownMenu>
          <ToolbarMenuTrigger
            icon={alignment.icon}
            label="Alignment"
            disabled={disabled}
          />
          <DropdownMenuContent finalFocus={toEditor} className="min-w-44">
            <DropdownMenuRadioGroup
              value={state.align}
              onValueChange={(value) =>
                chain().setTextAlign(String(value)).run()
              }
            >
              {ALIGNMENTS.map((align) => (
                <DropdownMenuRadioItem key={align.value} value={align.value}>
                  <Icon icon={align.icon} />
                  {align.label}
                  <DropdownMenuShortcut>
                    {formatShortcut(align.shortcut)}
                  </DropdownMenuShortcut>
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
      {commands.length > 0 && (
        <>
          <ToolbarSeparator />
          <DropdownMenu>
            <ToolbarMenuTrigger
              icon="plus"
              label="Insert"
              value="Insert"
              disabled={disabled}
            />
            <DropdownMenuContent
              finalFocus={toEditor}
              className="max-h-96 min-w-56"
            >
              {Object.entries(groups).map(([group, items], index) => (
                <React.Fragment key={group}>
                  {index > 0 && <DropdownMenuSeparator />}
                  <DropdownMenuGroup>
                    <DropdownMenuLabel>{group}</DropdownMenuLabel>
                    {items.map((command) => (
                      <DropdownMenuItem
                        key={command.id}
                        onClick={() => command.run(editor, actions)}
                      >
                        <Icon icon={command.icon} />
                        {command.label}
                        {command.shortcut && (
                          <DropdownMenuShortcut>
                            {formatShortcut(command.shortcut)}
                          </DropdownMenuShortcut>
                        )}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuGroup>
                </React.Fragment>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </>
      )}

      <div className="ml-auto flex items-center gap-0.5">
        {features.findReplace && (
          <ToolbarToggle
            icon={SearchReplaceIcon}
            label="Find and replace"
            shortcut="Mod-f"
            pressed={view.findOpen}
            onPressedChange={() => actions.toggleFind()}
          />
        )}
        {features.outline && features.headings && (
          <ToolbarToggle
            icon={TableOfContentsIcon}
            label="Outline"
            pressed={view.outlineOpen}
            onPressedChange={() => actions.toggleOutline()}
          />
        )}
        {features.fullscreen && (
          <ToolbarAction
            icon={view.fullscreen ? MinimizeScreenIcon : FullScreenIcon}
            label={view.fullscreen ? "Exit full screen" : "Full screen"}
            shortcut="Mod-Shift-Enter"
            onClick={() => actions.toggleFullscreen()}
          />
        )}
        <DropdownMenu>
          <ToolbarMenuTrigger icon="more" label="More" className="px-1" />
          <DropdownMenuContent
            finalFocus={toEditor}
            align="end"
            className="min-w-52"
          >
            <DropdownMenuGroup>
              <DropdownMenuLabel>View</DropdownMenuLabel>
              <DropdownMenuCheckboxItem
                checked={view.focusMode}
                onCheckedChange={(on) => onFocusModeChange(Boolean(on))}
              >
                Focus mode
              </DropdownMenuCheckboxItem>
              {features.invisibleCharacters && (
                <DropdownMenuCheckboxItem
                  checked={state.invisible}
                  onCheckedChange={() =>
                    editor.chain().focus().toggleInvisibleCharacters().run()
                  }
                >
                  Invisible characters
                </DropdownMenuCheckboxItem>
              )}
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel>Export</DropdownMenuLabel>
              <DropdownMenuItem onClick={() => copy(editor.getMarkdown())}>
                <Icon icon="copy" />
                Copy as Markdown
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => copy(editor.getHTML())}>
                <Icon icon={CodeSquareIcon} />
                Copy as HTML
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() =>
                  downloadText(
                    editor.getMarkdown(),
                    "document.md",
                    "text/markdown"
                  )
                }
              >
                <Icon icon="download" />
                Download Markdown
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() =>
                  downloadText(editor.getHTML(), "document.html", "text/html")
                }
              >
                <Icon icon="download" />
                Download HTML
              </DropdownMenuItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  )
}
