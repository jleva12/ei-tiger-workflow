import "./file-preview.css"

import * as React from "react"
import { renderAsync } from "docx-preview"

import { hardenLinks, readableSymbols } from "./content"
import {
  BarDivider,
  DownloadButton,
  PageControl,
  PreviewFrame,
  PreviewLoading,
  PreviewProblem,
  ZoomControl,
} from "./frame"
import { readFailure, usePreviewTask, type ViewProps } from "./preview"
import { stepZoom, useZoomKeys, type ZoomChange, type ZoomFit } from "./zoom"
import { useFitZoom, usePageTracking } from "./layout"

/**
 * A Word document laid out as its pages, with its own fonts, styles,
 * headers, footers, footnotes, tables and images; pages break where Word
 * last broke them. Embedded HTML parts and tracked changes aren't drawn,
 * and links open in a new tab (script links don't at all).
 */
export default function WordView({ data, toolbarEnd, onDownload }: ViewProps) {
  const scrollRef = React.useRef<HTMLDivElement>(null)
  const bodyRef = React.useRef<HTMLDivElement>(null)
  const stylesRef = React.useRef<HTMLDivElement>(null)

  const rendered = usePreviewTask(async () => {
    const body = bodyRef.current
    const styles = stylesRef.current
    if (!body || !styles) throw new Error("The preview went away")
    body.replaceChildren()
    styles.replaceChildren()
    await renderAsync(data, body, styles, {
      className: "docx",
      inWrapper: true,
      breakPages: true,
      ignoreLastRenderedPageBreak: false,
      experimental: true,
      // Data URLs: nothing to revoke when the document closes.
      useBase64URL: true,
      renderHeaders: true,
      renderFooters: true,
      renderFootnotes: true,
      renderEndnotes: true,
      // HTML parts would run in this page; changes and comments are drafts.
      renderAltChunks: false,
      renderChanges: false,
      renderComments: false,
    })
    hardenLinks(body)
    readableSymbols(styles, body)
    const sections = [...body.querySelectorAll<HTMLElement>("section.docx")]
    return {
      pages: sections.length,
      // The widest page (a landscape section is wider), laid out at 100%.
      width: Math.max(0, ...sections.map((section) => section.offsetWidth)),
    }
  }, [data])

  const pages = rendered.value?.pages ?? 0
  const { scale, fit, zoom } = useFitZoom(
    scrollRef,
    rendered.value?.width ? { width: rendered.value.width } : undefined
  )
  const page = usePageTracking(scrollRef, "section.docx", pages)

  const frameRef = React.useRef<HTMLDivElement>(null)
  useZoomKeys(frameRef, (direction) =>
    zoom(
      direction === 0 ? { fit: "auto" } : { scale: stepZoom(scale, direction) }
    )
  )

  const goTo = (next: number) => {
    const sections =
      bodyRef.current?.querySelectorAll<HTMLElement>("section.docx")
    sections?.[next - 1]?.scrollIntoView({ block: "start" })
  }

  return (
    <div ref={frameRef} className="flex min-h-0 min-w-0 flex-1 flex-col">
      <PreviewFrame
        toolbarEnd={toolbarEnd}
        desk
        toolbar={
          pages > 0 && (
            <>
              <PageControl page={page} count={pages} onPage={goTo} />
              <BarDivider />
              <ZoomControl
                scale={scale}
                fit={fit}
                fits={FITS}
                onChange={(change: ZoomChange) => zoom(change)}
              />
            </>
          )
        }
      >
        <div
          ref={scrollRef}
          tabIndex={0}
          role="document"
          aria-label="Document pages"
          className="absolute inset-0 overflow-auto outline-none"
        >
          <div ref={stylesRef} hidden />
          <div ref={bodyRef} data-slot="docx-pages" style={{ zoom: scale }} />
        </div>
        {rendered.error ? (
          <div className="absolute inset-0 bg-muted">
            <PreviewProblem
              kind="word"
              tone="danger"
              title="Couldn't show this document"
              description={readFailure("word", rendered.error)}
              actions={<DownloadButton onDownload={onDownload} />}
            />
          </div>
        ) : (
          rendered.loading && (
            <div className="absolute inset-0 bg-muted">
              <PreviewLoading kind="word" label="Laying out the pages…" />
            </div>
          )
        )}
      </PreviewFrame>
    </div>
  )
}

const FITS: ZoomFit[] = ["auto", "width"]
