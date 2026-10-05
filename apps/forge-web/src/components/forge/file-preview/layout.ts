import * as React from "react"

import { MAX_ZOOM, MIN_ZOOM, type ZoomChange, type ZoomFit } from "./zoom"

const clamp = (scale: number) => Math.min(Math.max(scale, MIN_ZOOM), MAX_ZOOM)

/**
 * Zoom for pages laid out in the browser (Word pages, a diagram, an image):
 * `auto` fits the view up to 100% (its width, or with `autoFit: "page"` all
 * of it), `width` fits the width at any size, `page` fits the whole thing;
 * a picked scale stays put. Fits follow the view as it resizes. `natural`
 * is the content's size at 100%, once it's known.
 */
export function useFitZoom(
  scrollRef: React.RefObject<HTMLElement | null>,
  natural: { width: number; height?: number } | undefined,
  {
    gutter = 64,
    autoFit = "width",
  }: { gutter?: number; autoFit?: "width" | "page" } = {}
) {
  const [fit, setFit] = React.useState<ZoomFit | null>("auto")
  const [picked, setPicked] = React.useState(1)
  const view = useViewSize(scrollRef)

  let scale = picked
  if (fit && natural?.width && view.width) {
    const byWidth = (view.width - gutter) / natural.width
    const byHeight = natural.height
      ? (view.height - gutter) / natural.height
      : byWidth
    const byPage = Math.min(byWidth, byHeight)
    scale =
      fit === "auto"
        ? Math.min(1, autoFit === "page" ? byPage : byWidth)
        : fit === "width"
          ? byWidth
          : byPage
  } else if (fit) {
    scale = 1
  }
  scale = clamp(scale)

  const zoom = React.useCallback((change: ZoomChange) => {
    if ("fit" in change) {
      setFit(change.fit)
    } else {
      setFit(null)
      setPicked(clamp(change.scale))
    }
  }, [])

  return { scale, fit, zoom }
}

/** An element's inner size, as it resizes. */
export function useViewSize(ref: React.RefObject<HTMLElement | null>) {
  const [size, setSize] = React.useState({ width: 0, height: 0 })
  React.useLayoutEffect(() => {
    const element = ref.current
    if (!element) return
    // Observing reports the size it starts at, before the first paint.
    const observer = new ResizeObserver(() =>
      setSize({ width: element.clientWidth, height: element.clientHeight })
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [ref])
  return size
}

/**
 * The page (1-based) at the top of a scrolling view of pages matching
 * `selector`: the last one whose top has passed a third of the way down.
 */
export function usePageTracking(
  scrollRef: React.RefObject<HTMLElement | null>,
  selector: string,
  count: number
) {
  const [page, setPage] = React.useState(1)
  React.useEffect(() => {
    const element = scrollRef.current
    if (!element || count === 0) return
    let frame = 0
    const read = () => {
      frame = 0
      const top = element.getBoundingClientRect().top
      const line = top + element.clientHeight / 3
      const pages = element.querySelectorAll<HTMLElement>(selector)
      let current = 1
      pages.forEach((each, index) => {
        if (each.getBoundingClientRect().top <= line) current = index + 1
      })
      setPage(current)
    }
    const onScroll = () => {
      frame ||= requestAnimationFrame(read)
    }
    read()
    element.addEventListener("scroll", onScroll, { passive: true })
    return () => {
      element.removeEventListener("scroll", onScroll)
      cancelAnimationFrame(frame)
    }
  }, [scrollRef, selector, count])
  return page
}

/**
 * Drag with the primary button to scroll a view, like a hand tool, when its
 * content is larger than it. Text selection and controls inside are left be.
 */
export function useDragScroll(scrollRef: React.RefObject<HTMLElement | null>) {
  const [dragging, setDragging] = React.useState(false)
  React.useEffect(() => {
    const element = scrollRef.current
    if (!element) return
    let start: { x: number; y: number; left: number; top: number } | undefined
    const down = (event: PointerEvent) => {
      if (event.button !== 0 || event.pointerType === "touch") return
      if ((event.target as HTMLElement).closest("button, a, input, textarea"))
        return
      const scrollable =
        element.scrollWidth > element.clientWidth ||
        element.scrollHeight > element.clientHeight
      if (!scrollable) return
      start = {
        x: event.clientX,
        y: event.clientY,
        left: element.scrollLeft,
        top: element.scrollTop,
      }
      element.setPointerCapture(event.pointerId)
      setDragging(true)
    }
    const move = (event: PointerEvent) => {
      if (!start) return
      element.scrollLeft = start.left - (event.clientX - start.x)
      element.scrollTop = start.top - (event.clientY - start.y)
    }
    const up = (event: PointerEvent) => {
      if (!start) return
      start = undefined
      element.releasePointerCapture(event.pointerId)
      setDragging(false)
    }
    element.addEventListener("pointerdown", down)
    element.addEventListener("pointermove", move)
    element.addEventListener("pointerup", up)
    element.addEventListener("pointercancel", up)
    return () => {
      element.removeEventListener("pointerdown", down)
      element.removeEventListener("pointermove", move)
      element.removeEventListener("pointerup", up)
      element.removeEventListener("pointercancel", up)
    }
  }, [scrollRef])
  return dragging
}

/** Whether the app is dark now, following the theme as it changes. */
export function useDarkTheme() {
  const read = () => document.documentElement.classList.contains("dark")
  const [dark, setDark] = React.useState(read)
  React.useEffect(() => {
    const observer = new MutationObserver(() => setDark(read()))
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    })
    return () => observer.disconnect()
  }, [])
  return dark
}
