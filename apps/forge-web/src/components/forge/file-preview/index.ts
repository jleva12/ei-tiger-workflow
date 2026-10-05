export { FilePreview, type FilePreviewProps } from "./file-preview"
// The viewer's own toolbar controls, for a host's controls beside it.
export {
  BarButton as PreviewButton,
  BarDivider as PreviewDivider,
} from "./frame"
export {
  canPreview,
  extensionOf,
  previewKindOf,
  type PreviewKind,
} from "./kinds"
