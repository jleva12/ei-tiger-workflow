// The editor's parts, for an interface of your own: the hook, the context
// that ties parts to it, the text area and each default UI piece. Unlike
// "./index", this imports Tiptap, so load it where the editor renders.
export {
  useRichTextEditor,
  type RichTextEditorInstance,
  type UseRichTextEditorOptions,
} from "./use-rich-text-editor"
export {
  RichTextEditorContext,
  useEditorActions,
  useRichTextEditorContext,
  useRunAction,
  type ResolvedAction,
  type RichTextEditorContextValue,
} from "./context"
export { RichTextEditorProvider } from "./provider"
export { RichTextEditorContent } from "./editor-content"
export { EditorToolbar } from "./toolbar"
export { EditorBubbleMenu } from "./bubble-menus"
export { EditorContextMenu } from "./context-menu"
export { EditorStatusBar } from "./status-bar"
export { AIResponsePreview, type AIPreviewProps } from "./ai-preview"
export { ActionIcon } from "./controls"
export type { ActionState } from "./core/action-registry"
export { defaultUrlPolicy } from "./core/security"
export { resolveFeatures } from "./core/features"
export {
  DEFAULT_BUBBLE_ITEMS,
  defaultContextMenuItems,
  defaultToolbarItems,
} from "./menu-model"
export * from "./index"
