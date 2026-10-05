import "./file-preview.css"

import * as React from "react"
import { cn } from "cn"
import {
  PptxViewer,
  RECOMMENDED_ZIP_LIMITS,
  type SlideHandle,
} from "@aiden0z/pptx-renderer"
import { SidebarLeftIcon } from "@hugeicons/core-free-icons"
import pdfModuleUrl from "pdfjs-dist/build/pdf.min.mjs?url"
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url"

import {
  BarButton,
  BarDivider,
  DownloadButton,
  PageControl,
  PreviewFrame,
  PreviewLoading,
  PreviewProblem,
  ZoomControl,
} from "./frame"
import { useViewSize } from "./layout"
import { readFailure, type ViewProps } from "./preview"
import { stepZoom, useZoomKeys, type ZoomChange, type ZoomFit } from "./zoom"

// Slides wider than this aren't made bigger by the automatic zoom.
const AUTO_WIDTH = 1040
// The desk around the slides.
const GUTTER = 32

/**
 * A PowerPoint deck as its slides, one under another, drawn from the file's
 * own shapes, text, tables, charts and images; slides are drawn as they
 * scroll into view. Thumbnails down the side jump to a slide. Script links
 * don't open; web links open in a new tab.
 */
export default function SlidesView({
  data,
  toolbarEnd,
  onDownload,
}: ViewProps) {
  const scrollRef = React.useRef<HTMLDivElement>(null)
  const slidesRef = React.useRef<HTMLDivElement>(null)
  const [viewer, setViewer] = React.useState<PptxViewer>()
  const [error, setError] = React.useState<Error>()
  const [slide, setSlide] = React.useState(0)
  const [fit, setFit] = React.useState<ZoomFit | null>("auto")
  const [picked, setPicked] = React.useState(1)
  const [thumbnails, setThumbnails] = React.useState(true)

  // Each file is a new view (FilePreview remounts it), so this runs once.
  React.useEffect(() => {
    const container = slidesRef.current
    const scroll = scrollRef.current
    if (!container || !scroll) return
    const controller = new AbortController()
    let opened: PptxViewer | undefined
    PptxViewer.open(data, container, {
      signal: controller.signal,
      zipLimits: RECOMMENDED_ZIP_LIMITS,
      lazySlides: true,
      lazyMedia: true,
      fitMode: "contain",
      scrollContainer: scroll,
      renderMode: "list",
      listOptions: { windowed: true, initialSlides: 3, batchSize: 4 },
      // For SmartArt and pasted artwork stored as PDF previews.
      pdfjs: { moduleUrl: pdfModuleUrl, workerUrl: pdfWorkerUrl },
      onSlideChange: (index) => setSlide(index),
    }).then(
      (next) => {
        opened = next
        if (controller.signal.aborted) next.destroy()
        else setViewer(next)
      },
      (problem: unknown) => {
        if (!controller.signal.aborted)
          setError(
            problem instanceof Error ? problem : new Error(String(problem))
          )
      }
    )
    return () => {
      controller.abort()
      opened?.destroy()
      container.replaceChildren()
    }
  }, [data])

  // How wide the slides can be: the desk less its gutters.
  const width = Math.max(0, useViewSize(scrollRef).width - GUTTER * 2)

  const natural = viewer?.slideWidth ?? 0
  // Fits size the slides' column (the renderer fills it); a picked zoom
  // draws them at that scale of their own size.
  const column =
    fit === "auto" ? Math.min(width, AUTO_WIDTH) : fit ? width : undefined
  const scale = natural ? (column !== undefined ? column / natural : picked) : 1

  React.useEffect(() => {
    if (!viewer) return
    if (fit) {
      void viewer.setZoom(100).then(() => viewer.setFitMode("contain"))
    } else {
      void viewer.setFitMode("none").then(() => viewer.setZoom(picked * 100))
    }
  }, [viewer, fit, picked])

  const zoom = React.useCallback((change: ZoomChange) => {
    if ("fit" in change) {
      setFit(change.fit)
    } else {
      setFit(null)
      setPicked(change.scale)
    }
  }, [])

  const frameRef = React.useRef<HTMLDivElement>(null)
  useZoomKeys(frameRef, (direction) =>
    zoom(
      direction === 0 ? { fit: "auto" } : { scale: stepZoom(scale, direction) }
    )
  )

  const count = viewer?.slideCount ?? 0
  const goTo = (index: number) =>
    void viewer?.goToSlide(index, { behavior: "instant", block: "start" })

  return (
    <div ref={frameRef} className="flex min-h-0 min-w-0 flex-1 flex-col">
      <PreviewFrame
        toolbarEnd={toolbarEnd}
        desk
        toolbar={
          count > 0 && (
            <>
              <BarButton
                label={thumbnails ? "Hide slides" : "Show slides"}
                icon={SidebarLeftIcon}
                pressed={thumbnails}
                onClick={() => setThumbnails((shown) => !shown)}
                className="@max-[720px]/preview:hidden"
              />
              <BarDivider />
              <PageControl
                page={slide + 1}
                count={count}
                noun="slide"
                onPage={(next) => goTo(next - 1)}
              />
              <BarDivider />
              <ZoomControl
                scale={scale}
                fit={fit}
                fits={["auto", "width"]}
                onChange={zoom}
              />
            </>
          )
        }
        className="flex"
      >
        {viewer && thumbnails && (
          <Thumbnails viewer={viewer} current={slide} onPick={goTo} />
        )}
        <div className="relative min-w-0 flex-1">
          <div
            ref={scrollRef}
            tabIndex={0}
            role="document"
            aria-label="Slides"
            data-slot="pptx-slides"
            className="absolute inset-0 overflow-auto outline-none"
            style={{ padding: `${GUTTER - 8}px ${GUTTER}px` }}
          >
            <div
              ref={slidesRef}
              className="mx-auto"
              style={{ width: column ?? "fit-content" }}
            />
          </div>
          {error ? (
            <div className="absolute inset-0 bg-muted">
              <PreviewProblem
                kind="slides"
                tone="danger"
                title="Couldn't show this deck"
                description={readFailure("slides", error)}
                actions={<DownloadButton onDownload={onDownload} />}
              />
            </div>
          ) : (
            !viewer && (
              <div className="absolute inset-0 bg-muted">
                <PreviewLoading kind="slides" label="Drawing the slides…" />
              </div>
            )
          )}
        </div>
      </PreviewFrame>
    </div>
  )
}

const THUMB_WIDTH = 132

/**
 * The deck's slides, small, down the left: drawn as they scroll into view
 * and let go as they leave; the current one outlined and kept in view.
 */
function Thumbnails({
  viewer,
  current,
  onPick,
}: {
  viewer: PptxViewer
  current: number
  onPick: (index: number) => void
}) {
  const listRef = React.useRef<HTMLOListElement>(null)
  const ratio = viewer.slideHeight / viewer.slideWidth || 9 / 16
  const hidden = viewer.presentationData?.slides.map((slide) => slide.hidden)

  React.useEffect(() => {
    listRef.current
      ?.querySelector(`[data-thumb="${current}"]`)
      ?.scrollIntoView({ block: "nearest" })
  }, [current])

  React.useEffect(() => {
    const list = listRef.current
    if (!list) return
    const handles = new Map<number, SlideHandle | { dispose: () => void }>()
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const target = entry.target as HTMLElement
          const index = Number(target.dataset.index)
          if (entry.isIntersecting && !handles.has(index)) {
            const handle = viewer.renderThumbnailToContainer(index, target, {
              width: THUMB_WIDTH,
            })
            if (handle) handles.set(index, handle)
          } else if (!entry.isIntersecting && handles.has(index)) {
            handles.get(index)?.dispose()
            handles.delete(index)
            target.replaceChildren()
          }
        }
      },
      { root: list, rootMargin: "300px 0px" }
    )
    for (const frame of list.querySelectorAll("[data-index]"))
      observer.observe(frame)
    return () => {
      observer.disconnect()
      for (const handle of handles.values()) handle.dispose()
    }
  }, [viewer])

  return (
    <ol
      ref={listRef}
      aria-label="Slides"
      className="flex w-[11.5rem] shrink-0 flex-col gap-3 overflow-y-auto border-r bg-background px-3 py-4 @max-[720px]/preview:hidden"
    >
      {Array.from({ length: viewer.slideCount }, (_, index) => (
        <li key={index} data-thumb={index} className="flex gap-2">
          <span
            className={cn(
              "w-4 shrink-0 pt-0.5 text-right text-2xs tabular-nums",
              index === current ? "text-foreground" : "text-subtle"
            )}
          >
            {index + 1}
          </span>
          <button
            type="button"
            aria-label={`Slide ${index + 1}${hidden?.[index] ? " (hidden)" : ""}`}
            aria-current={index === current || undefined}
            onClick={() => onPick(index)}
            className={cn(
              "relative overflow-hidden rounded-(--radius-chip) bg-white ring-1 ring-border transition-shadow outline-none",
              index === current
                ? "ring-2 ring-foreground/70"
                : "hover:ring-foreground/30",
              hidden?.[index] && "opacity-50"
            )}
            style={{ width: THUMB_WIDTH, height: THUMB_WIDTH * ratio }}
          >
            <span
              data-index={index}
              aria-hidden="true"
              className="pointer-events-none absolute inset-0"
            />
          </button>
        </li>
      ))}
    </ol>
  )
}
