import type { Glyph, GlyphIcon, NodeFamily } from "../lib/graph"

/**
 * The drawing of a node, shared by the canvas and the marks beside it
 * (`node-mark.tsx`): a shape per family and a glyph per kind, in a 24-unit
 * box. The canvas draws the shape itself (Cytoscape's node shapes) and the
 * glyph as an image; marks draw both as inline SVG.
 */

export const FAMILIES: NodeFamily[] = [
  "file",
  "type",
  "callable",
  "value",
  "other",
]

/** Cytoscape's shape for each family; the marks' outlines match them. */
export const CANVAS_SHAPES = {
  file: "round-rectangle",
  type: "round-diamond",
  callable: "ellipse",
  value: "round-hexagon",
  other: "round-octagon",
} as const satisfies Record<NodeFamily, string>

// How much of a node its glyph fills: less in a diamond, whose inside is
// smaller.
export const GLYPH_SCALE: Record<NodeFamily, number> = {
  file: 0.62,
  type: 0.5,
  callable: 0.64,
  value: 0.6,
  other: 0.6,
}

// A system stack: glyphs drawn as images can't load the page's web font.
export const GLYPH_FONT =
  "ui-sans-serif, -apple-system, system-ui, 'Segoe UI', Helvetica, Arial, sans-serif"

export const ICON_PATHS: Record<GlyphIcon, string> = {
  // A page with a folded corner.
  file: "M8 4.5h5.5L17 8v11.5H8z M13.5 4.5V8H17",
  // An arrow leaving a corner: something outside the repository.
  external: "M9.5 6.5H17.5V14.5 M17.5 6.5L7 17",
}

/** A glyph's letters fill less of the box the more of them there are. */
export function glyphTextSize(text: string) {
  return text.length === 1 ? 15 : text.length === 2 ? 11.5 : 9
}

/** A glyph image for a Cytoscape node's background, in `ink`. */
export function glyphImage(glyph: Glyph, ink: string) {
  let body: string
  if ("icon" in glyph) {
    body = `<path d="${ICON_PATHS[glyph.icon]}" fill="none" stroke="${ink}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`
  } else {
    const text = glyph.text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
    body = `<text x="12" y="12.5" text-anchor="middle" dominant-baseline="central" font-family="${GLYPH_FONT}" font-size="${glyphTextSize(glyph.text)}" font-weight="650" fill="${ink}">${text}</text>`
  }
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">${body}</svg>`
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
}
