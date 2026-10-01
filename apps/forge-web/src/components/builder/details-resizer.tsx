import * as React from "react"
import { cn } from "cn"

import { DETAILS_WIDTH } from "./utils"

// The panel never takes more than this share of the builder, so the
// canvas keeps room to work in.
const MOST_OF_BUILDER = 0.5

const remPx = () => parseFloat(getComputedStyle(document.documentElement).fontSize) || 16

/**
 * The details panel's left edge, dragged to widen or narrow it. Arrow
 * keys move it a rem at a time, Home and End go to its narrowest and
 * widest, and a double click (or Enter) puts it back to its default.
 */
export function DetailsResizer({
  width,
  onWidthChange,
  className,
}: {
  /** In rem. */
  width: number
  onWidthChange: (rem: number, options?: { save?: boolean; max?: number }) => void
  className?: string
}) {
  const drag = React.useRef<{ x: number; width: number; rem: number; max: number } | null>(null)
  const latest = React.useRef(width)
  const [dragging, setDragging] = React.useState(false)
  React.useEffect(() => {
    latest.current = width
  }, [width])

  // The widest the builder allows, in rem.
  const maxFor = (element: HTMLElement) => {
    const builder = element.parentElement?.parentElement
    const rem = remPx()
    return builder
      ? Math.min(DETAILS_WIDTH.max, (builder.clientWidth * MOST_OF_BUILDER) / rem)
      : DETAILS_WIDTH.max
  }

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize the workflow details"
      aria-valuemin={DETAILS_WIDTH.min}
      aria-valuemax={DETAILS_WIDTH.max}
      aria-valuenow={width}
      aria-valuetext={`${Math.round(width * remPx())} pixels wide`}
      tabIndex={0}
      data-dragging={dragging || undefined}
      className={cn(
        "group/resizer absolute inset-y-0 -left-1 z-10 w-2 cursor-col-resize touch-none outline-none",
        className
      )}
      onPointerDown={(event) => {
        if (event.button !== 0) return
        // No text selection while dragging.
        event.preventDefault()
        event.currentTarget.setPointerCapture(event.pointerId)
        drag.current = {
          x: event.clientX,
          width: latest.current,
          rem: remPx(),
          max: maxFor(event.currentTarget),
        }
        setDragging(true)
      }}
      onPointerMove={(event) => {
        const start = drag.current
        if (!start) return
        // The edge is on the panel's left: moving it left widens it.
        onWidthChange(start.width + (start.x - event.clientX) / start.rem, {
          save: false,
          max: start.max,
        })
      }}
      onPointerUp={(event) => {
        if (!drag.current) return
        event.currentTarget.releasePointerCapture(event.pointerId)
        onWidthChange(latest.current, { max: drag.current.max })
        drag.current = null
        setDragging(false)
      }}
      onPointerCancel={() => {
        drag.current = null
        setDragging(false)
      }}
      onDoubleClick={() => onWidthChange(DETAILS_WIDTH.initial)}
      onKeyDown={(event) => {
        const max = maxFor(event.currentTarget)
        const step = event.shiftKey ? 4 : 1
        const next =
          event.key === "ArrowLeft"
            ? width + step
            : event.key === "ArrowRight"
              ? width - step
              : event.key === "Home"
                ? DETAILS_WIDTH.min
                : event.key === "End"
                  ? max
                  : event.key === "Enter"
                    ? DETAILS_WIDTH.initial
                    : undefined
        if (next === undefined) return
        event.preventDefault()
        onWidthChange(next, { max })
      }}
    >
      {/* Over the panel's hairline: darkens while hovered, dragged or focused. */}
      <span
        aria-hidden="true"
        className="absolute inset-y-0 left-[3px] w-0.5 bg-transparent transition-colors duration-150 group-hover/resizer:bg-foreground/20 group-focus-visible/resizer:bg-foreground/35 group-data-dragging/resizer:bg-foreground/35"
      />
    </div>
  )
}
