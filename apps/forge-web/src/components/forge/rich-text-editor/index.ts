// The lazy wrappers: importing this folder doesn't pull Tiptap into a page's
// bundle until an editor renders. Nothing here imports Tiptap; for a custom
// UI on the editor, import from "./headless" instead.
export { RichTextEditor, RichTextView } from "./lazy"
export type {
  RichTextEditorComponents,
  RichTextEditorProps,
  RichTextEditorRef,
} from "./rich-text-editor"
export type { AIPreviewProps } from "./ai-preview"
export { DEFAULT_AI_ACTIONS } from "./ai/ai-defaults"
export type {
  AIActionDefinition,
  AIExecuteOptions,
  AIRequest,
  AIResult,
  AIResultMode,
  AIState,
  AIStatus,
  AIStreamEvent,
  EditorAIConfig,
} from "./ai/ai-types"
export type {
  ActionSource,
  BubbleMenuConfig,
  ContentOptions,
  ContextMenuConfig,
  EditorAction,
  EditorActionContext,
  EditorBookmark,
  EditorController,
  EditorError,
  EditorErrorCode,
  EditorFeatures,
  EditorPreset,
  EditorRange,
  EditorSelection,
  EditorSlotProps,
  MentionItem,
  MentionSource,
  MenuItem,
  ToolbarConfig,
  ToolbarItem,
  UploadHandler,
  UrlPolicy,
} from "./types"
