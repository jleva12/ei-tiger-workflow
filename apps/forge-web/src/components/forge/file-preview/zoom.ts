import * as React from "react"

/**
 * Zooms that follow the view's size: `auto` fits the width but never
 * enlarges past a comfortable size, `width` fits the width, `page` a whole
 * page.
 */
export type ZoomFit = "auto" | "width" | "page"

export const PRESETS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3]
export const MIN_ZOOM = 0.1
export const MAX_ZOOM = 4

/** The next preset zoom in (1) or out (-1) from a scale. */
export function stepZoom(scale: number, direction: 1 | -1) {
  const next =
    direction > 0
      ? PRESETS.find((preset) => preset > scale + 0.001)
      : [...PRESETS].reverse().find((preset) => preset < scale - 0.001)
  return (
    next ??
    Math.min(Math.max(scale * (direction > 0 ? 1.25 : 0.8), MIN_ZOOM), MAX_ZOOM)
  )
}

export type ZoomChange = { scale: number } | { fit: ZoomFit }

export const isMac = () =>
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform)

/**
 * ⌘/Ctrl with +, − and 0 zoom the preview instead of the page while focus
 * is inside it (0 goes back to the automatic zoom).
 */
export function useZoomKeys(
  target: React.RefObject<HTMLElement | null>,
  onZoom: (direction: 1 | -1 | 0) => void
) {
  const latest = React.useRef(onZoom)
  React.useEffect(() => {
    latest.current = onZoom
  })
  React.useEffect(() => {
    const element = target.current
    if (!element) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return
      const direction =
        event.key === "=" || event.key === "+"
          ? 1
          : event.key === "-" || event.key === "_"
            ? -1
            : event.key === "0"
              ? 0
              : undefined
      if (direction === undefined) return
      event.preventDefault()
      latest.current(direction)
    }
    element.addEventListener("keydown", onKeyDown)
    return () => element.removeEventListener("keydown", onKeyDown)
  }, [target])
}
