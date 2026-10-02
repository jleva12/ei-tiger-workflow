// The lazy wrappers: importing this folder doesn't pull Tiptap into a page's
// bundle until an editor renders.
export { RichTextEditor, RichTextView } from "./lazy"
export type { RichTextEditorProps } from "./rich-text-editor"
export type {
  MentionItem,
  MentionSource,
  RichTextEditorHandle,
  RichTextFeatures,
  RichTextFormat,
  RichTextPreset,
  RichTextValue,
  UploadHandler,
} from "./types"
