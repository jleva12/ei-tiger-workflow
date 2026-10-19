import * as React from "react"

/** The test chat's size, in rem so the text-size preference scales it. */
export type TestPanelSize = { width: number; height: number }

const MIN_SIZE: TestPanelSize = { width: 20, height: 20 }
// The panel sits 1rem in from the viewport's sides and bottom, and below
// the 4rem header, which keeps the Test button that closes it in view.
const VIEWPORT_INSET: TestPanelSize = { width: 2, height: 5 }

export const remPx = () =>
  parseFloat(getComputedStyle(document.documentElement).fontSize) || 16

const clampSize = ({ width, height }: TestPanelSize): TestPanelSize => {
  const rem = remPx()
  const fit = (value: number, min: number, max: number) =>
    Math.round(Math.max(min, Math.min(value, max)) * 4) / 4
  return {
    width: fit(
      width,
      MIN_SIZE.width,
      window.innerWidth / rem - VIEWPORT_INSET.width
    ),
    height: fit(
      height,
      MIN_SIZE.height,
      window.innerHeight / rem - VIEWPORT_INSET.height
    ),
  }
}

/**
 * The test chat's size and a way to change it; `null` is its default,
 * full height beside the canvas. It's this viewer's own setting (`key`),
 * remembered in this browser; without storage it's the default every time.
 */
export function useTestPanelSize(key: string) {
  const [size, setSize] = React.useState<TestPanelSize | null>(() => {
    try {
      const stored: unknown = JSON.parse(
        window.localStorage.getItem(key) ?? "null"
      )
      if (typeof stored !== "object" || stored === null) return null
      const { width, height } = stored as Record<string, unknown>
      if (typeof width !== "number" || typeof height !== "number") return null
      return clampSize({ width, height })
    } catch {
      return null
    }
  })
  const change = React.useCallback(
    (next: TestPanelSize | null, { save = true }: { save?: boolean } = {}) => {
      const settled = next && clampSize(next)
      setSize(settled)
      if (!save) return
      try {
        if (settled) {
          window.localStorage.setItem(key, JSON.stringify(settled))
        } else {
          window.localStorage.removeItem(key)
        }
      } catch {
        // Not remembered; it still applies until the page closes.
      }
    },
    [key]
  )
  return [size, change] as const
}
