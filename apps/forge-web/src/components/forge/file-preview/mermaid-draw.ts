import mermaid from "mermaid"

/*
 * Drawing Mermaid source as an SVG in the app's theme, safely: labels are
 * sanitized and click handlers never bound. The file preview and rich
 * answers' diagrams both draw with it.
 */

// Mermaid keeps one configuration for the page, so drawings take turns.
let queue: Promise<unknown> = Promise.resolve()
let drawings = 0

export function drawMermaid(source: string, dark: boolean) {
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
