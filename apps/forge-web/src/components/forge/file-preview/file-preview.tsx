import * as React from "react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  DownloadButton,
  PreviewFrame,
  PreviewLoading,
  PreviewProblem,
} from "./frame"
import { type ViewProps } from "./preview"
import {
  formatName,
  noPreviewApp,
  previewKindOf,
  type PreviewKind,
} from "./kinds"

// Each renderer and its library load with the first file of their kind:
// pdf.js, docx-preview, the PPTX renderer, SheetJS, Mermaid and Markdown are
// too heavy for pages that only might show a file.
const VIEWS: Record<
  Exclude<PreviewKind, "none">,
  React.LazyExoticComponent<React.ComponentType<ViewProps>>
> = {
  pdf: React.lazy(() => import("./pdf-view")),
  word: React.lazy(() => import("./word-view")),
  slides: React.lazy(() => import("./slides-view")),
  sheet: React.lazy(() => import("./sheet-view")),
  markdown: React.lazy(() => import("./markdown-view")),
  text: React.lazy(() => import("./text-view")),
  mermaid: React.lazy(() => import("./mermaid-view")),
  image: React.lazy(() => import("./image-view")),
}

export type FilePreviewProps = {
  /** The file's name; its extension picks how it's shown. */
  name: string
  /** Its media type, for a name whose extension says nothing. */
  mediaType?: string
  /** The file; undefined while it's on its way. */
  data?: Blob
  /** 0–1 of the file received while it's on its way, when known. */
  progress?: number
  /** Its size, to show how much has arrived. */
  size?: number
  /** Why the file couldn't be fetched. */
  error?: string | { message: string } | null
  /** Fetch it again, after an error. */
  onRetry?: () => void
  /** Offered wherever it can't be shown: no preview, or a damaged file. */
  onDownload?: () => void
  /** The host's controls, at the end of the viewer's toolbar. */
  toolbarEnd?: React.ReactNode
  className?: string
}

/**
 * Shows a file in the browser, whatever it is: PDFs page by page with
 * find, Word documents and PowerPoint decks as their pages and slides,
 * workbooks and CSVs as sheets in the data table, Markdown as a document,
 * text with line numbers, Mermaid diagrams drawn, and images. It fills the
 * space its parent gives it (a flex item, `min-h-0`), with a toolbar for
 * the format (pages, zoom, sheets, find) and the host's own controls at
 * its end. Formats a browser can't show get a way to download them.
 *
 * It renders the bytes it's given; fetching them is the host's, which
 * passes `progress` and `error` meanwhile.
 */
export function FilePreview({
  name,
  mediaType,
  data,
  progress,
  size,
  error,
  onRetry,
  onDownload,
  toolbarEnd,
  className,
}: FilePreviewProps) {
  const kind = previewKindOf(name, mediaType)
  let body: React.ReactNode
  if (error) {
    body = (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewProblem
          kind={kind}
          tone="danger"
          title="Couldn't load the file"
          description={typeof error === "string" ? error : error.message}
          actions={
            onRetry && (
              <Button variant="outline" size="sm" onClick={onRetry}>
                <Icon icon="refresh" data-icon="inline-start" />
                Retry
              </Button>
            )
          }
        />
      </PreviewFrame>
    )
  } else if (kind === "none") {
    const app = noPreviewApp(name)
    body = (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewProblem
          kind={kind}
          title={`No preview for ${formatName(name)} files`}
          description={`Browsers can't display this format. Download it to open it${app ? ` in ${app}` : " on your computer"}.`}
          actions={<DownloadButton onDownload={onDownload} />}
        />
      </PreviewFrame>
    )
  } else if (!data) {
    body = (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewLoading
          kind={kind}
          label={progress === undefined ? "Loading the file…" : "Downloading…"}
          progress={progress}
          size={size}
        />
      </PreviewFrame>
    )
  } else {
    const View = VIEWS[kind]
    body = (
      <ViewBoundary
        // A new file starts its view over, even one that failed before.
        key={blobKey(data)}
        fallback={(problem) => (
          <PreviewFrame toolbarEnd={toolbarEnd} desk>
            <PreviewProblem
              kind={kind}
              tone="danger"
              title="Couldn't show the file"
              description={problem.message}
              actions={<DownloadButton onDownload={onDownload} />}
            />
          </PreviewFrame>
        )}
      >
        <React.Suspense
          fallback={
            <PreviewFrame toolbarEnd={toolbarEnd} desk>
              <PreviewLoading kind={kind} />
            </PreviewFrame>
          }
        >
          <View
            data={data}
            name={name}
            mediaType={mediaType}
            toolbarEnd={toolbarEnd}
            onDownload={onDownload}
          />
        </React.Suspense>
      </ViewBoundary>
    )
  }
  return (
    <div
      data-slot="file-preview"
      data-kind={kind}
      className={cn(
        // Its own stacking context: nothing inside (a table's sticky header,
        // a page's layers) paints over what the host lays over it.
        "@container/preview relative isolate flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background",
        className
      )}
    >
      {body}
    </div>
  )
}

// Each Blob its own key, so any new file remounts the view that shows it.
const blobKeys = new WeakMap<Blob, number>()
let nextBlobKey = 0
function blobKey(blob: Blob) {
  let key = blobKeys.get(blob)
  if (key === undefined) {
    key = ++nextBlobKey
    blobKeys.set(blob, key)
  }
  return key
}

/** A renderer that throws while rendering shows why, not a blank page. */
class ViewBoundary extends React.Component<
  {
    fallback: (error: Error) => React.ReactNode
    children: React.ReactNode
  },
  { error?: Error }
> {
  state: { error?: Error } = {}

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error : new Error(String(error)) }
  }

  render() {
    return this.state.error
      ? this.props.fallback(this.state.error)
      : this.props.children
  }
}
