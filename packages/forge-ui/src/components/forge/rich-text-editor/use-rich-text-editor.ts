import * as React from "react"
import type { AnyExtension } from "@tiptap/core"
import { useEditor, type Editor } from "@tiptap/react"

import { createAIActions } from "./ai/ai-actions"
import type { AIController } from "./ai/ai-controller"
import type { ActionRegistry } from "./core/action-registry"
import { createBuiltInActions } from "./core/builtin-actions"
import { resolveFeatures } from "./core/features"
import { isAlive } from "./core/lifecycle"
import { createEditorExtensions } from "./extensions"
import { EditorRuntime } from "./runtime"
import type {
  EditorAction,
  EditorAIConfig,
  EditorController,
  EditorError,
  EditorFeatures,
  EditorPreset,
  EditorSelection,
  EditorUiHandlers,
  MentionSource,
  ResolvedEditorFeatures,
  UploadHandler,
  UrlPolicy,
} from "./types"

export interface UseRichTextEditorOptions {
  /** Markdown, controlled. Don't pass it with `defaultValue`. */
  value?: string
  /** Markdown the editor starts with, uncontrolled. */
  defaultValue?: string
  /** The document as Markdown, after edits and successful programmatic changes. */
  onChange?: (markdown: string) => void
  /** Wait this many ms after typing stops before `onChange` (it still flushes on blur). Default 0. */
  onChangeDebounce?: number

  readOnly?: boolean
  disabled?: boolean
  autoFocus?: boolean | "start" | "end"
  placeholder?: string

  /** Which features start on; `features` overrides single ones. Default `standard`. */
  preset?: EditorPreset
  features?: EditorFeatures
  /** The most characters the document may hold; typing stops there. */
  maxLength?: number

  /** Commands for toolbars, menus, `/` and `executeAction`. */
  actions?: EditorAction[]
  /** More Tiptap extensions. Keep the array stable (module scope or useMemo). */
  extensions?: AnyExtension[]
  ai?: EditorAIConfig

  /** Enables `@` mentions, suggesting what the source returns. */
  mentions?: MentionSource
  /** Stores added images and returns their URLs; without it they're inlined. */
  onUpload?: UploadHandler
  /** Largest image that can be added, in bytes. Default 10 MB. */
  maxFileSize?: number
  /** Which link and image URLs are allowed. */
  urlPolicy?: UrlPolicy

  onFocus?: () => void
  onBlur?: () => void
  onSelectionChange?: (selection: EditorSelection) => void
  onError?: (error: EditorError) => void
  /** Called once, when the editor is ready, with its controller. */
  onReady?: (controller: EditorController) => void

  /** Attributes for the editable element (role, aria-*). */
  attributes?: Record<string, string>
  /** The UI the built-in actions open; a headless editor gets fallbacks. */
  ui?: Partial<EditorUiHandlers>
}

export interface RichTextEditorInstance {
  editor: Editor
  controller: EditorController
  registry: ActionRegistry
  ai: AIController
  features: ResolvedEditorFeatures
  ui: () => EditorUiHandlers
  urlPolicy: () => UrlPolicy
}

/**
 * The editor without its UI: the Tiptap instance, its controller, the action
 * registry and AI, with Markdown in and out. `RichTextEditor` is built on
 * it; use it directly for a fully custom interface, rendering
 * `RichTextEditorProvider` and `RichTextEditorContent` from `./headless`.
 */
export function useRichTextEditor(
  options: UseRichTextEditorOptions
): RichTextEditorInstance {
  const {
    value,
    defaultValue,
    readOnly = false,
    disabled = false,
    autoFocus = false,
    preset = "standard",
    features: featureOverrides,
    maxLength,
    actions,
    extensions: customExtensions,
    ai: aiConfig,
    mentions,
    urlPolicy,
    attributes,
  } = options

  const featuresKey = JSON.stringify(resolveFeatures(preset, featureOverrides))
  const features = React.useMemo<ResolvedEditorFeatures>(
    () => JSON.parse(featuresKey),
    [featuresKey]
  )
  const editable = !readOnly && !disabled

  // Stable for the component's lifetime; it reads the latest options.
  const [runtime] = React.useState(() => new EditorRuntime(options))
  React.useLayoutEffect(() => {
    runtime.setOptions(options)
  })
  const { registry, controller, ai, handlers, ui } = runtime

  // Built-in and AI actions follow the features and the AI setup; the
  // app's own follow the `actions` prop.
  const aiKey = JSON.stringify([
    aiConfig?.enabled,
    aiConfig?.allowCustomPrompts,
    aiConfig?.actions?.map(({ id, label, requiresSelection, description }) => [
      id,
      label,
      requiresSelection,
      description,
    ]),
  ])
  React.useLayoutEffect(() => {
    registry.setBuiltIns([
      ...createBuiltInActions(features, ui),
      ...createAIActions(ai, ui),
    ])
  }, [registry, ai, ui, features, aiKey])
  React.useLayoutEffect(() => {
    registry.setCustom(actions ?? [])
  })

  const hasMentions = Boolean(mentions)
  const extensions = React.useMemo(
    () =>
      createEditorExtensions({
        features,
        maxLength,
        mentions: hasMentions,
        urlPolicy,
        handlers,
        extensions: customExtensions,
      }),
    [features, maxLength, hasMentions, urlPolicy, handlers, customExtensions]
  )

  // A rebuilt editor (new features) starts from the current document.
  const content = runtime.currentContent(value ?? defaultValue ?? "")

  const editor = useEditor(
    {
      extensions,
      content,
      contentType: "markdown",
      // The editor only renders in the browser (it loads lazily there).
      immediatelyRender: true,
      editable,
      autofocus: autoFocus === true ? "end" : autoFocus || false,
      shouldRerenderOnTransaction: false,
      // Without Markdown shortcuts, typography and emoticons still convert.
      enableInputRules: features.markdownShortcuts
        ? true
        : ["typography", "emoji"],
      editorProps: { attributes: attributes ?? {} },
      onUpdate: () => runtime.sync.changed(),
      onSelectionUpdate: () => runtime.selectionChanged(),
      onBeforeCreate: ({ editor }) => runtime.setEditor(editor),
      onFocus: () => runtime.focused(),
      onBlur: () => runtime.blurred(),
    },
    [extensions]
  )
  // StrictMode can build an editor twice and keep either: the controller
  // follows the one React renders, not the last one built.
  runtime.setEditor(editor)

  // Once per component, not per rebuilt editor: the controller outlives them.
  React.useEffect(() => {
    runtime.notifyReady()
  }, [runtime])

  const bothValues = value !== undefined && defaultValue !== undefined
  React.useEffect(() => {
    if (!bothValues) return
    runtime.onError({
      code: "invalid-props",
      message:
        "Pass either value (controlled) or defaultValue (uncontrolled), not both; value wins.",
    })
  }, [bothValues, runtime])

  // A new `value` from outside replaces the document; our own echo doesn't.
  React.useEffect(() => {
    if (value !== undefined && isAlive(editor)) runtime.receive(editor, value)
  }, [editor, value, runtime])

  React.useEffect(() => {
    editor.setEditable(editable, false)
  }, [editor, editable])

  const attributesKey = JSON.stringify(attributes ?? {})
  React.useEffect(() => {
    editor.setOptions({
      editorProps: { attributes: JSON.parse(attributesKey) },
    })
  }, [editor, attributesKey])

  React.useEffect(() => () => runtime.dispose(), [runtime])

  return {
    editor,
    controller,
    registry,
    ai,
    features,
    ui,
    urlPolicy: runtime.policy,
  }
}
