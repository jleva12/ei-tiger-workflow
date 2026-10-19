import * as React from "react"
import { isTauri } from "@tauri-apps/api/core"

/*
 * The window's own title bar. On macOS the app draws under it
 * (tauri.conf.json: `titleBarStyle` Overlay, no title): the traffic lights
 * sit at the start of the app's title strip (`trafficLightPosition`), and
 * the window moves by its frame and top bars. Elsewhere the system's title
 * bar stays.
 *
 * Moving it is Tauri's: an element with `data-tauri-drag-region="deep"`
 * drags the window from anywhere in it but its buttons, links and fields,
 * and a double click zooms it, as a title bar does.
 */

/** Tauri's mark for a part of the page the window moves by. */
export const DRAG_REGION = { "data-tauri-drag-region": "deep" } as const

/**
 * Whether the screen draws under the title bar: in the app on macOS, or in
 * a browser with `?titlebar=overlay`, to see that layout while developing.
 */
export const OVERLAY_TITLE_BAR: boolean = (() => {
  try {
    if (
      new URLSearchParams(window.location.search).get("titlebar") === "overlay"
    )
      return true
  } catch {
    // No address to read: not a browser.
  }
  return isTauri() && /Macintosh|Mac OS X/.test(navigator.userAgent)
})()

/** The chat page's own top bars, which the window moves by too. */
const TOP_BARS = '[data-slot="topbar"], [data-slot="sidebar-brand"]'

/**
 * Lets the window move by the open chat page's top bars (its title bar,
 * its conversations' header). They're the shared chat page's, so they're
 * marked as the press reaches them, before Tauri's own handler (on the
 * document, as it bubbles) reads the mark.
 */
export function useTopBarsMoveWindow(enabled: boolean) {
  React.useEffect(() => {
    if (!enabled) return
    const mark = (event: MouseEvent) => {
      const target = event.target
      if (!(target instanceof Element)) return
      target.closest(TOP_BARS)?.setAttribute("data-tauri-drag-region", "deep")
    }
    document.addEventListener("mousedown", mark, true)
    document.addEventListener("mouseup", mark, true)
    return () => {
      document.removeEventListener("mousedown", mark, true)
      document.removeEventListener("mouseup", mark, true)
    }
  }, [enabled])
}
