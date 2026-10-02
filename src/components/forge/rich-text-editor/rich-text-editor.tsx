import * as React from "react"
import {
  EditorContent,
  useEditor,
  useEditorState,
  type Editor,
  type JSONContent,
} from "@tiptap/react"
import { FloatingMenu } from "@tiptap/react/menus"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { toast } from "@/components/ui/toast"
import { Icon } from "../icon"
import { BlockHandle } from "./block-handle"
import { RichTextBubbleMenus } from "./bubble-menus"
import { readContent, sameContent, writeContent } from "./content"
import { createExtensions, type EditorHandlers } from "./extensions"
import { resolveFeatures } from "./features"
import { AUDIO_TYPES, IMAGE_TYPES, insertFiles } from "./files"
import { FindReplaceBar } from "./find-replace"
import { InsertDialog } from "./insert-dialog"
import { OutlinePanel } from "./outline"
import { RichTextToolbar } from "./toolbar"
import type {
  InsertDialogRequest,
  MentionSource,
  RichTextEditorHandle,
  RichTextFeatures,
  RichTextFormat,
  RichTextPreset,
  RichTextValue,
  UploadHandler,
} from "./types"
import "./rich-text-editor.css"

/** The value props, typed by format: strings for HTML and Markdown, JSON otherwise. */
type ValueProps =
  | {
      format?: "html" | "markdown"
      value?: string
      defaultValue?: string
      onChange?: (value: string, editor: Editor) => void
    }
  | {
      format: "json"
      value?: JSONContent
      defaultValue?: JSONContent
      onChange?: (value: JSONContent, editor: Editor) => void
    }

export type RichTextEditorProps = ValueProps & {
  /** Which feature groups are on; `features` overrides single ones. */
  preset?: RichTextPreset
  features?: Partial<RichTextFeatures>
  placeholder?: string
  /** Shows the content without editing; the toolbar hides unless `toolbar`. */
  readOnly?: boolean
  disabled?: boolean
  autoFocus?: boolean | "start" | "end"
  /** The most characters the document may hold; typing stops there. */
  maxLength?: number
  /** Show the fixed toolbar. Defaults to on while editable. */
  toolbar?: boolean
  /** Show word and character counts. Defaults to on except in `minimal`. */
  showCount?: boolean
  /** Enables `@` mentions, suggesting what the source returns. */
  mentions?: MentionSource
  /** Stores added files and returns their URLs; without it they're inlined. */
  onUpload?: UploadHandler
  /** Largest file that can be added, in bytes. Default 10 MB. */
  maxFileSize?: number
  /** Submits the value with a native form under this name. */
  name?: string
  id?: string
  "aria-label"?: string
  "aria-labelledby"?: string
  "aria-describedby"?: string
  "aria-invalid"?: boolean
  onFocus?: (editor: Editor) => void
  onBlur?: (editor: Editor) => void
  /** Called once the editor exists, with the Tiptap instance. */
  onReady?: (editor: Editor) => void
  /** Classes for the frame (border, radius, height). */
  className?: string
  /** Classes for the scrolling content area (min/max height). */
  contentClassName?: string
  /** Render without the field frame, for showing saved content. */
  bare?: boolean
  ref?: React.Ref<RichTextEditorHandle>
}

const DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024

/**
 * A complete rich text field on Tiptap: toolbar, selection and table menus,
 * `/` commands, block handles, find and replace, an outline, full screen,
 * uploads, embeds, formulas, and HTML, Markdown or JSON values. Use it
 * controlled (`value` + `onChange`) or uncontrolled (`defaultValue`).
 */
export function RichTextEditor(props: RichTextEditorProps) {
  const {
    format = "html",
    value,
    defaultValue,
    preset = "full",
    features: featureOverrides,
    readOnly = false,
    disabled = false,
    autoFocus = false,
    maxLength,
    toolbar,
    showCount,
    mentions,
    name,
    className,
    contentClassName,
    bare = false,
    ref,
  } = props

  const featuresKey = JSON.stringify(resolveFeatures(preset, featureOverrides))
  const features = React.useMemo<RichTextFeatures>(
    () => JSON.parse(featuresKey),
    [featuresKey]
  )
  const editable = !readOnly && !disabled

  // Everything the editor's callbacks need, current as of the last render.
  const latest = React.useRef({ props, format, editable })
  React.useLayoutEffect(() => {
    latest.current = { props, format, editable }
  })

  const fileInputRef = React.useRef<HTMLInputElement>(null)
  const hiddenInputRef = React.useRef<HTMLInputElement>(null)
  const [scrollElement, setScrollElement] =
    React.useState<HTMLDivElement | null>(null)
  const [dialog, setDialog] = React.useState<InsertDialogRequest | null>(null)
  const [findOpen, setFindOpen] = React.useState(false)
  const [outlineOpen, setOutlineOpen] = React.useState(false)
  const [fullscreen, setFullscreen] = React.useState(false)
  const [focusMode, setFocusMode] = React.useState(false)

  const fullscreenRef = React.useRef(fullscreen)
  React.useLayoutEffect(() => {
    fullscreenRef.current = fullscreen
  }, [fullscreen])

  // Stable for the editor's lifetime; callbacks read the latest props.
  const [handlers] = React.useState<EditorHandlers>(() => ({
    actions: {
      openDialog: (request) => setDialog(request),
      pickFiles: (kind) => {
        const input = fileInputRef.current
        if (!input) return
        input.accept = (kind === "image" ? IMAGE_TYPES : AUDIO_TYPES).join(",")
        input.click()
      },
      toggleFind: (open) => setFindOpen((was) => open ?? !was),
      toggleOutline: (open) => setOutlineOpen((was) => open ?? !was),
      toggleFullscreen: (open) => setFullscreen((was) => open ?? !was),
    },
    insertFiles: (editor, files, pos) =>
      void insertFiles(editor, files, {
        upload: latest.current.props.onUpload,
        maxFileSize: latest.current.props.maxFileSize ?? DEFAULT_MAX_FILE_SIZE,
        pos,
        onError: (message) =>
          toast.add({
            title: "File not added",
            description: message,
            type: "error",
          }),
      }),
    isEditable: () => latest.current.editable,
    placeholder: () => latest.current.props.placeholder ?? "Write something…",
    mentions: (query, signal) =>
      latest.current.props.mentions?.(query, signal) ?? [],
    isFullscreen: () => fullscreenRef.current,
  }))
  const { actions } = handlers

  const hasMentions = Boolean(mentions)
  const extensions = React.useMemo(
    () =>
      createExtensions({
        features,
        maxLength,
        mentions: hasMentions,
        handlers,
      }),
    [features, maxLength, hasMentions, handlers]
  )

  const initial = value ?? defaultValue
  const lastValue = React.useRef<RichTextValue | undefined>(initial)

  const editor = useEditor(
    {
      extensions,
      content: initial ?? "",
      contentType: typeof initial === "string" ? format : "json",
      editable,
      autofocus: autoFocus === true ? "end" : autoFocus || false,
      editorProps: {
        attributes: editorAttributes(props, editable),
      },
      onUpdate: ({ editor }) => {
        const { props, format } = latest.current
        const next = readContent(editor, format)
        lastValue.current = next
        if (hiddenInputRef.current) {
          hiddenInputRef.current.value = serialize(next)
        }
        ;(
          props.onChange as
            ((value: RichTextValue, editor: Editor) => void) | undefined
        )?.(next, editor)
      },
      onFocus: ({ editor }) => latest.current.props.onFocus?.(editor),
      onBlur: ({ editor }) => latest.current.props.onBlur?.(editor),
      onCreate: ({ editor }) => latest.current.props.onReady?.(editor),
    },
    [extensions]
  )

  // A new `value` from outside replaces the document; our own echo doesn't.
  React.useEffect(() => {
    if (value === undefined) return
    if (sameContent(value, lastValue.current)) return
    lastValue.current = value
    if (sameContent(value, readContent(editor, format))) return
    writeContent(editor, value, format)
    if (hiddenInputRef.current) hiddenInputRef.current.value = serialize(value)
  }, [editor, value, format])

  React.useEffect(() => {
    editor.setEditable(editable, false)
  }, [editor, editable])

  const attributesKey = JSON.stringify(editorAttributes(props, editable))
  React.useEffect(() => {
    editor.setOptions({
      editorProps: { attributes: JSON.parse(attributesKey) },
    })
  }, [editor, attributesKey])

  React.useImperativeHandle(
    ref,
    (): RichTextEditorHandle => ({
      editor,
      focus: (position = "end") => editor.commands.focus(position),
      clear: () => editor.commands.clearContent(true),
      getValue: ((valueFormat?: RichTextFormat) =>
        readContent(
          editor,
          valueFormat ?? format
        )) as RichTextEditorHandle["getValue"],
      setValue: (next, valueFormat) => {
        writeContent(editor, next, valueFormat ?? format)
        lastValue.current = readContent(editor, format)
      },
    }),
    [editor, format]
  )

  // Full screen owns the page: no scrolling behind it.
  React.useEffect(() => {
    if (!fullscreen) return
    const root = document.documentElement
    const previous = root.style.overflow
    root.style.overflow = "hidden"
    return () => {
      root.style.overflow = previous
    }
  }, [fullscreen])

  // StrictMode and concurrent retries can render once more with an editor
  // Tiptap has already destroyed (no schema, no storage); the replacement
  // arrives with the next render.
  if (editor.isDestroyed) return null

  const showToolbar = toolbar ?? editable
  const outlineAvailable = features.outline && features.headings
  const counting =
    showCount ?? (preset !== "minimal" || maxLength !== undefined)

  return (
    <div
      // useEditorState keeps reading a replaced editor until its next
      // transaction, so the parts that use it remount with each instance.
      key={editor.instanceId}
      data-slot="rich-text-editor"
      data-fullscreen={fullscreen || undefined}
      data-disabled={disabled || undefined}
      inert={disabled}
      onKeyDown={(event) => {
        if (event.key === "Escape" && fullscreen && !event.defaultPrevented) {
          setFullscreen(false)
        }
      }}
      className={cn(
        // No container query here: containment would re-anchor the
        // fixed-position menus inside to this frame.
        "relative flex min-w-0 flex-col text-foreground",
        !bare &&
          "rounded-(--radius-control) border border-border bg-background transition-[color,box-shadow] duration-200 has-[.ProseMirror-focused]:border-ring has-[.ProseMirror-focused]:ring-3 has-[.ProseMirror-focused]:ring-ring/30",
        !bare &&
          props["aria-invalid"] &&
          "border-destructive ring-3 ring-destructive/20 has-[.ProseMirror-focused]:border-destructive has-[.ProseMirror-focused]:ring-destructive/20",
        disabled && "cursor-not-allowed opacity-50",
        fullscreen &&
          "fixed inset-0 z-50 rounded-none border-0 ring-0 has-[.ProseMirror-focused]:ring-0",
        className
      )}
    >
      {showToolbar && (
        <RichTextToolbar
          editor={editor}
          features={features}
          actions={actions}
          view={{ findOpen, outlineOpen, fullscreen, focusMode }}
          onFocusModeChange={setFocusMode}
        />
      )}
      {findOpen && features.findReplace && (
        <FindReplaceBar editor={editor} onClose={() => setFindOpen(false)} />
      )}

      <div className="flex min-h-0 flex-1">
        <div
          ref={setScrollElement}
          data-slot="rich-text-scroll"
          className={cn(
            "min-h-0 min-w-0 flex-1 overflow-y-auto",
            !bare && "min-h-32",
            contentClassName,
            // Full screen gives the content the whole height.
            fullscreen && "max-h-none"
          )}
        >
          <EditorContent
            editor={editor}
            data-editable={editable || undefined}
            data-drag-handle={(editable && features.dragHandle) || undefined}
            data-focus-mode={focusMode || undefined}
            className={cn(
              "rich-text relative min-h-[inherit]",
              bare && "[&_.ProseMirror]:p-0",
              // A readable measure when the editor fills the screen.
              fullscreen && "mx-auto max-w-[52rem] py-6"
            )}
          />
        </div>
        {outlineOpen && outlineAvailable && <OutlinePanel editor={editor} />}
      </div>

      {counting && <CountFooter editor={editor} maxLength={maxLength} />}

      {editable && scrollElement && (
        <RichTextBubbleMenus
          editor={editor}
          actions={actions}
          scrollTarget={scrollElement}
        />
      )}
      {editable && features.dragHandle && (
        <BlockHandle editor={editor} actions={actions} />
      )}
      {editable && features.slashCommands && !features.dragHandle && (
        <FloatingMenu
          editor={editor}
          options={{ placement: "left", offset: 6 }}
          className="z-20"
        >
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Insert a block"
            onClick={() => editor.chain().focus().insertContent("/").run()}
            className="text-subtle hover:text-foreground"
          >
            <Icon icon="plus" />
          </Button>
        </FloatingMenu>
      )}

      <InsertDialog
        editor={editor}
        request={dialog}
        actions={actions}
        onClose={() => setDialog(null)}
      />
      <input
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        tabIndex={-1}
        onChange={(event) => {
          const files = Array.from(event.target.files ?? [])
          event.target.value = ""
          if (files.length) handlers.insertFiles(editor, files)
        }}
      />
      {name && (
        <input
          ref={hiddenInputRef}
          type="hidden"
          name={name}
          defaultValue={serialize(initial ?? "")}
        />
      )}
    </div>
  )
}

function serialize(value: RichTextValue) {
  return typeof value === "string" ? value : JSON.stringify(value)
}

function editorAttributes(props: RichTextEditorProps, editable: boolean) {
  const attributes: Record<string, string> = {
    role: "textbox",
    "aria-multiline": "true",
  }
  if (props.id) attributes.id = props.id
  if (props["aria-label"]) attributes["aria-label"] = props["aria-label"]
  if (props["aria-labelledby"]) {
    attributes["aria-labelledby"] = props["aria-labelledby"]
  }
  if (props["aria-describedby"]) {
    attributes["aria-describedby"] = props["aria-describedby"]
  }
  if (props["aria-invalid"]) attributes["aria-invalid"] = "true"
  if (!editable) attributes["aria-readonly"] = "true"
  return attributes
}

/** Words and characters, and the limit when there is one. */
function CountFooter({
  editor,
  maxLength,
}: {
  editor: Editor
  maxLength?: number
}) {
  const count = useEditorState({
    editor,
    selector: ({ editor }) =>
      editor.isDestroyed
        ? null
        : {
            characters: editor.storage.characterCount.characters(),
            words: editor.storage.characterCount.words(),
          },
  })
  if (!count) return null
  const near = maxLength !== undefined && count.characters >= maxLength * 0.9
  return (
    <div
      data-slot="rich-text-count"
      className="flex items-center justify-end gap-3 border-t border-border px-3 py-1 text-2xs text-subtle tabular-nums"
    >
      <span>
        {count.words} {count.words === 1 ? "word" : "words"}
      </span>
      <span
        aria-live={near ? "polite" : undefined}
        className={cn(near && "text-destructive")}
      >
        {maxLength !== undefined
          ? `${count.characters} / ${maxLength} characters`
          : `${count.characters} ${count.characters === 1 ? "character" : "characters"}`}
      </span>
    </div>
  )
}

/**
 * Saved rich text, shown read-only with the editor's typography. Pass the
 * same `format` it was saved in.
 */
export function RichTextView(
  props: ValueProps & { className?: string; "aria-label"?: string }
) {
  return (
    <RichTextEditor
      {...(props as RichTextEditorProps)}
      readOnly
      bare
      toolbar={false}
      showCount={false}
      features={{ dragHandle: false, slashCommands: false }}
    />
  )
}
