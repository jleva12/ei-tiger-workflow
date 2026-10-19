import * as React from "react"
import type { Editor } from "@tiptap/react"
import { FloatingMenu } from "@tiptap/react/menus"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { toast } from "@/components/ui/toast"
import { Icon } from "../icon"
import { AIResponsePreview, type AIPreviewProps } from "./ai-preview"
import { BlockHandle } from "./block-handle"
import { EditorBubbleMenu } from "./bubble-menus"
import { RichTextEditorContext, useRichTextEditorContext } from "./context"
import { EditorContextMenu } from "./context-menu"
import { isAlive } from "./core/lifecycle"
import { RichTextEditorContent } from "./editor-content"
import { IMAGE_TYPES, insertFiles } from "./files"
import { FindReplaceBar } from "./find-replace"
import { InsertDialog } from "./insert-dialog"
import { OutlinePanel } from "./outline"
import { EditorStatusBar } from "./status-bar"
import { EditorToolbar } from "./toolbar"
import type {
  BubbleMenuConfig,
  ContextMenuConfig,
  EditorController,
  EditorError,
  EditorSlotProps,
  EditorUiHandlers,
  EditorViewState,
  InsertDialogRequest,
  MenuItem,
  ToolbarConfig,
  ToolbarItem,
} from "./types"
import {
  useRichTextEditor,
  type UseRichTextEditorOptions,
} from "./use-rich-text-editor"
import "./rich-text-editor.css"

/** Replacements for the editor's main UI areas. */
export interface RichTextEditorComponents {
  /** Replaces the toolbar; build it with `useEditorActions`. */
  Toolbar?: React.ComponentType<
    EditorSlotProps & { items?: ToolbarItem[]; position: "top" | "bottom" }
  >
  /** Replaces the right-click menu; it must render `children` (the text). */
  ContextMenu?: React.ComponentType<
    EditorSlotProps & { items?: MenuItem[]; children: React.ReactNode }
  >
  /** Replaces the AI panel. */
  AIPreview?: React.ComponentType<AIPreviewProps>
  /** Replaces the word and character counts. */
  StatusBar?: React.ComponentType<EditorSlotProps & { maxLength?: number }>
}

export interface RichTextEditorProps extends Omit<
  UseRichTextEditorOptions,
  "ui" | "attributes"
> {
  /** Classes for the frame (border, radius, height). */
  className?: string
  /** Classes for the scrolling text area. */
  contentClassName?: string
  minHeight?: number | string
  maxHeight?: number | string

  /** `false` hides it; defaults to shown while editable. */
  toolbar?: boolean | ToolbarConfig
  /** `false` leaves the browser's own menu. */
  contextMenu?: boolean | ContextMenuConfig
  bubbleMenu?: boolean | BubbleMenuConfig
  components?: RichTextEditorComponents
  /** No toolbar, menus, panels or counts: just the text. */
  headless?: boolean
  /** No field frame, for showing saved content. */
  bare?: boolean

  /** Submits the Markdown with a native form under this name. */
  name?: string
  id?: string
  "aria-label"?: string
  "aria-labelledby"?: string
  "aria-describedby"?: string
  "aria-invalid"?: boolean
  ref?: React.Ref<EditorController>
}

/** The controller a `ref` receives. */
export type RichTextEditorRef = EditorController

// Errors the person should hear about; the rest go to `onError` only.
const TOASTED = new Set<EditorError["code"]>([
  "clipboard",
  "upload-failed",
  "unsafe-content",
  "action-failed",
])

const TOAST_TITLES: Partial<Record<EditorError["code"], string>> = {
  clipboard: "Clipboard unavailable",
  "upload-failed": "Image not added",
  "unsafe-content": "Some links were removed",
  "action-failed": "That didn't work",
}

const DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024

const toCss = (value: number | string | undefined) =>
  typeof value === "number" ? `${value}px` : value

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

const enabledOf = <T extends { enabled?: boolean }>(
  value: boolean | T | undefined,
  fallback: boolean
): { enabled: boolean; config: T | undefined } =>
  typeof value === "boolean"
    ? { enabled: value, config: undefined }
    : { enabled: value?.enabled ?? fallback, config: value }

/**
 * A Markdown rich text editor on Tiptap: Markdown in (`value` or
 * `defaultValue`) and out (`onChange`), a configurable toolbar, selection
 * and right-click menus and a `/` menu that all run one action registry, a
 * controller through `ref`, and optional AI from a callback you supply.
 */
export function RichTextEditor(props: RichTextEditorProps) {
  const {
    readOnly = false,
    disabled = false,
    toolbar,
    contextMenu,
    bubbleMenu,
    components = {},
    headless = false,
    bare = false,
    name,
    className,
    contentClassName,
    minHeight,
    maxHeight,
    maxLength,
    ai: aiConfig,
    ref,
  } = props
  const editable = !readOnly && !disabled

  const fileInputRef = React.useRef<HTMLInputElement>(null)
  const hiddenInputRef = React.useRef<HTMLInputElement>(null)
  const [scrollElement, setScrollElement] =
    React.useState<HTMLDivElement | null>(null)
  const [dialog, setDialog] = React.useState<InsertDialogRequest | null>(null)
  const [view, setView] = React.useState<EditorViewState>({
    find: false,
    outline: false,
    fullscreen: false,
    focusMode: false,
  })
  const viewRef = React.useRef(view)
  React.useLayoutEffect(() => {
    viewRef.current = view
  }, [view])

  const latest = React.useRef(props)
  React.useLayoutEffect(() => {
    latest.current = props
  })

  // Stable for the component's lifetime.
  const [ui] = React.useState<EditorUiHandlers>(() => {
    const toggle = (key: keyof EditorViewState) => (open?: boolean) =>
      setView((was) => ({ ...was, [key]: open ?? !was[key] }))
    return {
      openDialog: setDialog,
      pickImages: () => fileInputRef.current?.click(),
      toggleFind: toggle("find"),
      toggleOutline: toggle("outline"),
      toggleFullscreen: toggle("fullscreen"),
      toggleFocusMode: toggle("focusMode"),
      askAI: () => {},
      viewState: () => viewRef.current,
    }
  })
  const [onError] = React.useState(() => (error: EditorError) => {
    latest.current.onError?.(error)
    if (TOASTED.has(error.code)) {
      toast.add({
        title: TOAST_TITLES[error.code] ?? "Editor",
        description: error.message,
        type: error.code === "unsafe-content" ? "warning" : "error",
      })
    } else if (!latest.current.onError) {
      console.warn(`[RichTextEditor] ${error.message}`)
    }
  })
  const [onChange] = React.useState(() => (markdown: string) => {
    if (hiddenInputRef.current) hiddenInputRef.current.value = markdown
    latest.current.onChange?.(markdown)
  })

  const instance = useRichTextEditor({
    ...props,
    onChange,
    onError,
    attributes: editorAttributes(props, editable),
    ui,
  })
  const { editor, controller, features } = instance
  React.useImperativeHandle(ref, () => controller, [controller])

  // A controlled value also reaches the hidden form field.
  React.useEffect(() => {
    if (props.value !== undefined && hiddenInputRef.current) {
      hiddenInputRef.current.value = props.value
    }
  }, [props.value])

  // Full screen owns the page: no scrolling behind it.
  React.useEffect(() => {
    if (!view.fullscreen) return
    const root = document.documentElement
    const previous = root.style.overflow
    root.style.overflow = "hidden"
    return () => {
      root.style.overflow = previous
    }
  }, [view.fullscreen])

  // StrictMode and concurrent retries can render once more with an editor
  // Tiptap has already destroyed; the replacement arrives with the next render.
  if (!isAlive(editor)) return null

  const toolbarSetup = enabledOf(toolbar, editable)
  const contextSetup = enabledOf(contextMenu, true)
  const bubbleSetup = enabledOf(bubbleMenu, true)
  const showToolbar = !headless && toolbarSetup.enabled
  const toolbarPosition = toolbarSetup.config?.position ?? "top"
  const showStatus =
    !headless && (features.characterCount || maxLength !== undefined)
  const showAI = !headless && Boolean(aiConfig?.enabled)
  const slot = { controller, editor }
  const Toolbar = components.Toolbar
  const StatusBar = components.StatusBar
  const CustomContextMenu = components.ContextMenu

  const toolbarNode = showToolbar ? (
    Toolbar ? (
      <Toolbar
        {...slot}
        items={toolbarSetup.config?.items}
        position={toolbarPosition}
      />
    ) : (
      <EditorToolbar
        items={toolbarSetup.config?.items}
        position={toolbarPosition}
        className={toolbarSetup.config?.className}
      />
    )
  ) : null

  const text = (
    <div
      ref={setScrollElement}
      data-slot="rich-text-scroll"
      style={{ minHeight: toCss(minHeight), maxHeight: toCss(maxHeight) }}
      className={cn(
        "min-h-0 min-w-0 flex-1 overflow-y-auto",
        !bare && "min-h-32",
        contentClassName,
        // Full screen gives the content the whole height.
        view.fullscreen && "max-h-none"
      )}
    >
      <RichTextEditorContent
        focusMode={view.focusMode}
        bare={bare}
        className={cn(
          // A readable measure when the editor fills the screen.
          view.fullscreen && "mx-auto max-w-[52rem] py-6"
        )}
      />
    </div>
  )

  const contextItems = contextSetup.config?.items
  const textWithMenu =
    headless || !contextSetup.enabled ? (
      text
    ) : CustomContextMenu ? (
      <CustomContextMenu {...slot} items={contextItems}>
        {text}
      </CustomContextMenu>
    ) : (
      <EditorContextMenu
        items={contextItems}
        className="flex min-h-0 min-w-0 flex-1 flex-col"
      >
        {text}
      </EditorContextMenu>
    )

  return (
    <RichTextEditorContext.Provider value={instance}>
      <div
        // useEditorState keeps reading a replaced editor until its next
        // transaction, so the parts that use it remount with each instance.
        key={editor.instanceId}
        data-slot="rich-text-editor"
        data-fullscreen={view.fullscreen || undefined}
        data-disabled={disabled || undefined}
        data-readonly={readOnly || undefined}
        inert={disabled}
        onKeyDown={(event) => {
          if (
            event.key === "Escape" &&
            view.fullscreen &&
            !event.defaultPrevented
          ) {
            ui.toggleFullscreen(false)
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
          view.fullscreen &&
            "fixed inset-0 z-50 rounded-none border-0 ring-0 has-[.ProseMirror-focused]:ring-0",
          className
        )}
      >
        {toolbarPosition === "top" && toolbarNode}
        {view.find && features.findReplace && (
          <FindReplaceBar
            editor={editor}
            onClose={() => ui.toggleFind(false)}
          />
        )}

        <div className="flex min-h-0 flex-1">
          {textWithMenu}
          {view.outline && features.outline && <OutlinePanel editor={editor} />}
        </div>

        {showAI &&
          (components.AIPreview ? (
            <CustomAIPreview Preview={components.AIPreview} />
          ) : (
            <AIResponsePreview />
          ))}
        {showStatus &&
          (StatusBar ? (
            <StatusBar {...slot} maxLength={maxLength} />
          ) : (
            <EditorStatusBar maxLength={maxLength} />
          ))}
        {toolbarPosition === "bottom" && toolbarNode}

        {!headless && editable && bubbleSetup.enabled && scrollElement && (
          <EditorBubbleMenu
            scrollTarget={scrollElement}
            items={bubbleSetup.config?.items}
          />
        )}
        {!headless && editable && features.dragHandle && (
          <BlockHandle editor={editor} />
        )}
        {!headless &&
          editable &&
          features.slashCommands &&
          !features.dragHandle && <AddBlockButton editor={editor} />}

        <InsertDialog
          editor={editor}
          request={dialog}
          ui={() => ui}
          urlPolicy={instance.urlPolicy()}
          onClose={() => setDialog(null)}
        />
        <input
          ref={fileInputRef}
          type="file"
          accept={IMAGE_TYPES.join(",")}
          multiple
          hidden
          tabIndex={-1}
          onChange={(event) => {
            const files = Array.from(event.target.files ?? [])
            event.target.value = ""
            if (!files.length) return
            void insertFiles(editor, files, {
              upload: latest.current.onUpload,
              maxFileSize: latest.current.maxFileSize ?? DEFAULT_MAX_FILE_SIZE,
              urlPolicy: instance.urlPolicy(),
              onError: (message) => onError({ code: "upload-failed", message }),
            })
          }}
        />
        {name && (
          <input
            ref={hiddenInputRef}
            type="hidden"
            name={name}
            defaultValue={props.value ?? props.defaultValue ?? ""}
          />
        )}
      </div>
    </RichTextEditorContext.Provider>
  )
}

/** The `+` beside an empty line, which opens the `/` menu. */
function AddBlockButton({ editor }: { editor: Editor }) {
  return (
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
  )
}

/** Hands a custom AI panel the live AI state. */
function CustomAIPreview({
  Preview,
}: {
  Preview: React.ComponentType<AIPreviewProps>
}) {
  const { ai, editor } = useRichTextEditorContext()
  const state = React.useSyncExternalStore(ai.subscribe, ai.getState)
  return <Preview state={state} ai={ai} editor={editor} />
}

/**
 * Saved Markdown, shown read-only with the editor's typography, without the
 * field frame, toolbar or menus.
 */
export function RichTextView({
  value,
  preset = "full",
  className,
  "aria-label": ariaLabel,
}: {
  value: string
  preset?: RichTextEditorProps["preset"]
  className?: string
  "aria-label"?: string
}) {
  return (
    <RichTextEditor
      value={value}
      preset={preset}
      readOnly
      bare
      toolbar={false}
      contextMenu={false}
      bubbleMenu={false}
      features={{
        characterCount: false,
        dragHandle: false,
        slashCommands: false,
      }}
      className={className}
      aria-label={ariaLabel}
    />
  )
}
