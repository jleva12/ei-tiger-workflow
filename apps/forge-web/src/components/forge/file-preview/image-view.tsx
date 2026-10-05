import "./file-preview.css"

import * as React from "react"
import { cn } from "cn"

import {
  BarDivider,
  BarText,
  DownloadButton,
  PreviewFrame,
  PreviewProblem,
  ZoomControl,
} from "./frame"
import { useDragScroll, useFitZoom } from "./layout"
import type { ViewProps } from "./preview"
import { stepZoom, useZoomKeys } from "./zoom"

/**
 * An image, fitted to the view (never enlarged past its own size), on a
 * checkerboard where it's transparent; zoom in and drag to look around.
 * SVGs show as images, where their scripts never run.
 */
export default function ImageView({
  data,
  name,
  toolbarEnd,
  onDownload,
}: ViewProps) {
  const [natural, setNatural] = React.useState<{
    width: number
    height: number
  }>()
  const [failed, setFailed] = React.useState(false)
  const scrollRef = React.useRef<HTMLDivElement>(null)
  const { scale, fit, zoom } = useFitZoom(scrollRef, natural, {
    autoFit: "page",
    gutter: 48,
  })
  const dragging = useDragScroll(scrollRef)
  const frameRef = React.useRef<HTMLDivElement>(null)
  useZoomKeys(frameRef, (direction) =>
    zoom(
      direction === 0 ? { fit: "auto" } : { scale: stepZoom(scale, direction) }
    )
  )
  // The file's own URL, let go when the image goes.
  const source = React.useCallback(
    (image: HTMLImageElement | null) => {
      if (!image) return
      const url = URL.createObjectURL(data)
      image.src = url
      return () => URL.revokeObjectURL(url)
    },
    [data]
  )

  if (failed)
    return (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewProblem
          kind="image"
          tone="danger"
          title="Couldn't show this image"
          description="The browser can't decode it. It may be damaged, or in a format this browser doesn't support."
          actions={<DownloadButton onDownload={onDownload} />}
        />
      </PreviewFrame>
    )

  return (
    <div ref={frameRef} className="flex min-h-0 min-w-0 flex-1 flex-col">
      <PreviewFrame
        toolbarEnd={toolbarEnd}
        desk
        toolbar={
          natural && (
            <>
              <BarText>
                {natural.width.toLocaleString()} ×{" "}
                {natural.height.toLocaleString()} px
              </BarText>
              <BarDivider />
              <ZoomControl
                scale={scale}
                fit={fit}
                fits={["auto", "width", "page"]}
                onChange={zoom}
              />
            </>
          )
        }
      >
        <div
          ref={scrollRef}
          tabIndex={0}
          className={cn(
            "absolute inset-0 grid overflow-auto p-6 outline-none",
            dragging ? "cursor-grabbing select-none" : "cursor-grab"
          )}
        >
          <img
            ref={source}
            alt={name}
            draggable={false}
            onLoad={(event) =>
              setNatural({
                width: event.currentTarget.naturalWidth || 1,
                height: event.currentTarget.naturalHeight || 1,
              })
            }
            onError={() => setFailed(true)}
            className="checkerboard m-auto max-w-none shadow-(--shadow-paper) ring-1 ring-border"
            style={
              natural
                ? {
                    width: natural.width * scale,
                    height: natural.height * scale,
                  }
                : { visibility: "hidden" }
            }
          />
        </div>
      </PreviewFrame>
    </div>
  )
}
