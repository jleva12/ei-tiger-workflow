/**
 * A design token's colour as rgb(), which a canvas understands; the tokens
 * are oklch() and hex. Painting one pixel converts whatever the browser
 * parses.
 */
export function tokenColor(name: string, fallback: string) {
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim()
  const context = document.createElement("canvas").getContext("2d", {
    willReadFrequently: true,
  })
  if (!value || !context) return fallback
  context.fillStyle = fallback
  context.fillStyle = value
  context.fillRect(0, 0, 1, 1)
  const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data
  return a === 255
    ? `rgb(${r}, ${g}, ${b})`
    : `rgba(${r}, ${g}, ${b}, ${a / 255})`
}
