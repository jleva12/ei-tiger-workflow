import * as React from "react"
import { cn } from "cn"
import mermaid from "mermaid"
import { Flowchart01Icon, SourceCodeIcon } from "@hugeicons/core-free-icons"

import { readText } from "./content"
import {
  BarDivider,
  DownloadButton,
  ModeSwitch,
  PreviewFrame,
  PreviewLoading,
  PreviewProblem,
  ZoomControl,
} from "./frame"
import { usePreviewTask, type ViewProps } from "./preview"
import { stepZoom, useZoomKeys } from "./zoom"
import { useDarkTheme, useDragScroll, useFitZoom } from "./layout"
import { Lines } from "./text-view"

type Mode = "diagram" | "source"

// Mermaid keeps one configuration for the page, so drawings take turns.
let queue: Promise<unknown> = Promise.resolve()
let drawings = 0

function draw(source: string, dark: boolean) {
  const turn = queue.then(async () => {
    const font = getComputedStyle(document.body).fontFamily
    mermaid.initialize({
      startOnLoad: false,
      // Labels are sanitized and click handlers never bound.
      securityLevel: "strict",
      suppressErrorRendering: true,
      theme: dark ? "dark" : "neutral",
      fontFamily: font,
      themeVariables: { fontFamily: font },
    })
    drawings += 1
    const { svg } = await mermaid.render(
      `file-preview-mermaid-${drawings}`,
      source
    )
    // Its size at 100%, from the drawing's own coordinates.
    const box = /viewBox="[-\d.]+ [-\d.]+ ([\d.]+) ([\d.]+)"/.exec(svg)
    return {
      svg,
      size: box ? { width: Number(box[1]), height: Number(box[2]) } : undefined,
    }
  })
  queue = turn.catch(() => undefined)
  return turn
}

/**
 * A Mermaid diagram drawn in the app's theme, fitted to the view; zoom in
 * and drag to look around. Its source is a switch away, and shown with
 * why when it doesn't parse.
 */
export default function MermaidView({
  data,
  name,
  toolbarEnd,
  onDownload,
}: ViewProps) {
  const dark = useDarkTheme()
  const text = usePreviewTask(() => readText(data), [data])
  const source = text.value?.text ?? ""
  const drawn = usePreviewTask(
    async () => (source.trim() ? draw(source, dark) : undefined),
    [source, dark]
  )
  const [mode, setMode] = React.useState<Mode>("diagram")
  const lines = React.useMemo(
    () => source.replace(/\r\n?/g, "\n").replace(/\n$/, "").split("\n"),
    [source]
  )

  const scrollRef = React.useRef<HTMLDivElement>(null)
  const canvasRef = React.useRef<HTMLDivElement>(null)
  const natural = drawn.value?.size

  // The desk's padding and the card's around the drawing.
  const { scale, fit, zoom } = useFitZoom(scrollRef, natural, {
    autoFit: "page",
    gutter: 124,
  })
  // The drawing goes in as Mermaid made it (sanitized, in strict mode)...
  React.useLayoutEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    canvas.innerHTML = drawn.value?.svg ?? ""
    const svg = canvas.querySelector("svg")
    if (svg) svg.style.maxWidth = "none"
  }, [drawn.value, mode])
  // ...sized to the zoom.
  React.useLayoutEffect(() => {
    const svg = canvasRef.current?.querySelector("svg")
    if (!svg || !natural) return
    svg.setAttribute("width", String(natural.width * scale))
    svg.setAttribute("height", String(natural.height * scale))
  }, [drawn.value, mode, natural, scale])

  const dragging = useDragScroll(scrollRef)
  const frameRef = React.useRef<HTMLDivElement>(null)
  useZoomKeys(frameRef, (direction) =>
    zoom(
      direction === 0 ? { fit: "auto" } : { scale: stepZoom(scale, direction) }
    )
  )

  if (text.loading)
    return (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewLoading kind="mermaid" label="Reading the diagram…" />
      </PreviewFrame>
    )
  if (text.error || !text.value)
    return (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewProblem
          kind="mermaid"
          title="Couldn't read this diagram"
          description={text.error?.message ?? "It isn't a text file."}
          actions={<DownloadButton onDownload={onDownload} />}
        />
      </PreviewFrame>
    )

  const broken = drawn.error !== undefined
  const shown: Mode = broken ? "source" : mode
  return (
    <div ref={frameRef} className="flex min-h-0 min-w-0 flex-1 flex-col">
      <PreviewFrame
        toolbarEnd={toolbarEnd}
        desk={shown === "diagram"}
        className="flex flex-col"
        toolbar={
          <>
            <ModeSwitch
              label="Show"
              value={shown}
              onChange={setMode}
              options={[
                {
                  value: "diagram",
                  label: "Diagram",
                  icon: Flowchart01Icon,
                  disabled: broken,
                },
                { value: "source", label: "Source", icon: SourceCodeIcon },
              ]}
            />
            {shown === "diagram" && natural && (
              <>
                <BarDivider />
                <ZoomControl
                  scale={scale}
                  fit={fit}
                  fits={["auto", "width", "page"]}
                  onChange={zoom}
                />
              </>
            )}
          </>
        }
      >
        {broken && (
          <p
            role="alert"
            className="shrink-0 border-b border-danger-border bg-danger-surface px-4 py-2 font-mono text-2xs whitespace-pre-wrap text-danger-foreground"
          >
            {`This diagram doesn't parse, so its source is shown. ${drawn.error?.message ?? ""}`.trim()}
          </p>
        )}
        <div className="relative min-h-0 flex-1">
          {shown === "diagram" ? (
            <div
              ref={scrollRef}
              tabIndex={0}
              role="img"
              aria-label={`Diagram: ${name}`}
              className={cn(
                "absolute inset-0 grid overflow-auto p-8 outline-none",
                dragging ? "cursor-grabbing select-none" : "cursor-grab"
              )}
            >
              <div
                ref={canvasRef}
                data-slot="mermaid-diagram"
                className="m-auto rounded-(--radius-card) bg-background p-6 shadow-(--shadow-paper) ring-1 ring-border"
              />
              {drawn.loading && (
                <div className="absolute inset-0 bg-muted">
                  <PreviewLoading kind="mermaid" label="Drawing the diagram…" />
                </div>
              )}
            </div>
          ) : (
            <Lines lines={lines} wrap />
          )}
        </div>
      </PreviewFrame>
    </div>
  )
}
