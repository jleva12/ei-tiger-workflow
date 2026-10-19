import * as React from "react"

import {
  remPx,
  type TestPanelSize,
} from "@/features/agents/lib/test-panel-size"

const STEP = 1

/**
 * The test chat's top-left corner, dragged to make it wider or taller (as
 * the assistant's is). Arrow keys move it a rem at a time, and a double
 * click (or Enter) puts it back to its default.
 */
export function TestPanelResizer({
  panel,
  onSizeChange,
}: {
  panel: React.RefObject<HTMLElement | null>
  onSizeChange: (
    size: TestPanelSize | null,
    options?: { save?: boolean }
  ) => void
}) {
  const drag = React.useRef<{
    pointerId: number
    x: number
    y: number
    rem: number
    start: TestPanelSize
    moved: boolean
  } | null>(null)
  const [dragging, setDragging] = React.useState(false)

  const shown = (): TestPanelSize | null => {
    const element = panel.current
    if (!element) return null
    const rect = element.getBoundingClientRect()
    const rem = remPx()
    return { width: rect.width / rem, height: rect.height / rem }
  }

  // The panel is pinned at its bottom-right, so moving the corner up and
  // left makes it bigger.
  const sizeAt = (
    start: NonNullable<typeof drag.current>,
    event: React.PointerEvent
  ): TestPanelSize => ({
    width: start.start.width + (start.x - event.clientX) / start.rem,
    height: start.start.height + (start.y - event.clientY) / start.rem,
  })

  const end = () => {
    drag.current = null
    setDragging(false)
  }

  return (
    <button
      type="button"
      aria-label="Resize the test chat"
      data-dragging={dragging || undefined}
      className="absolute top-0 left-0 z-10 size-6 cursor-nwse-resize touch-none rounded-tl-(--radius-card) border-t-2 border-l-2 border-transparent transition-colors outline-none [clip-path:polygon(0_0,100%_0,100%_6px,6px_6px,6px_100%,0_100%)] group-hover/test-panel:border-foreground/15 hover:border-foreground/40 focus-visible:border-ring data-dragging:border-foreground/40 motion-reduce:transition-none"
      onPointerDown={(event) => {
        const start = shown()
        if (event.button !== 0 || !start) return
        // No text selection while dragging.
        event.preventDefault()
        event.currentTarget.setPointerCapture(event.pointerId)
        drag.current = {
          pointerId: event.pointerId,
          x: event.clientX,
          y: event.clientY,
          rem: remPx(),
          start,
          moved: false,
        }
        setDragging(true)
      }}
      onPointerMove={(event) => {
        const start = drag.current
        if (!start || start.pointerId !== event.pointerId) return
        if (
          !start.moved &&
          event.clientX === start.x &&
          event.clientY === start.y
        )
          return
        start.moved = true
        onSizeChange(sizeAt(start, event), { save: false })
      }}
      onPointerUp={(event) => {
        const start = drag.current
        if (!start || start.pointerId !== event.pointerId) return
        event.currentTarget.releasePointerCapture(event.pointerId)
        if (start.moved) onSizeChange(sizeAt(start, event))
        end()
      }}
      onPointerCancel={(event) => {
        const start = drag.current
        if (!start || start.pointerId !== event.pointerId) return
        // Keeps where it got to.
        if (start.moved) {
          const reached = shown()
          if (reached) onSizeChange(reached)
        }
        end()
      }}
      onDoubleClick={() => onSizeChange(null)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault()
          onSizeChange(null)
          return
        }
        const step = event.shiftKey ? STEP * 4 : STEP
        const change = (
          {
            ArrowLeft: [step, 0],
            ArrowRight: [-step, 0],
            ArrowUp: [0, step],
            ArrowDown: [0, -step],
          } as Record<string, [number, number]>
        )[event.key]
        const current = shown()
        if (!change || !current) return
        event.preventDefault()
        onSizeChange({
          width: current.width + change[0],
          height: current.height + change[1],
        })
      }}
    />
  )
}
