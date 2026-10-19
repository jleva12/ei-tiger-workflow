import type { JSONContent } from "@tiptap/core"

import type { UrlPolicy } from "../types.ts"

const LINK_SCHEMES = new Set(["http:", "https:", "mailto:", "tel:"])
const IMAGE_SCHEMES = new Set(["http:", "https:", "blob:"])
// Nodes whose children are inline (an unsafe image inside one becomes text).
const TEXTBLOCKS = new Set(["paragraph", "heading"])
// Raster formats only: SVG can carry script when opened on its own.
const DATA_IMAGE =
  /^data:image\/(?:png|jpe?g|gif|webp|avif);base64,[a-z\d+/=\s]+$/i

/** The scheme of an absolute URL (`https:`), or null for a relative one. */
function scheme(url: string) {
  // Browsers ignore control characters and whitespace inside schemes
  // (`java\nscript:`), so they don't count as a way around the check.
  // eslint-disable-next-line no-control-regex
  const compact = url.replace(/[\x00-\x20\x7f]/g, "")
  const match = /^([a-z][a-z\d+.-]*):/i.exec(compact)
  return match ? `${match[1].toLowerCase()}:` : null
}

/**
 * The default policy: links may be web, mail, phone or relative URLs;
 * images may be web or blob URLs, or inline PNG, JPEG, GIF, WebP or AVIF.
 * `javascript:`, `vbscript:`, `data:` links and the like are refused.
 */
export const defaultUrlPolicy: UrlPolicy = (url, kind) => {
  const value = url.trim()
  if (!value) return false
  const found = scheme(value)
  if (kind === "image" && found === "data:") return DATA_IMAGE.test(value)
  if (found === null) return true
  return (kind === "link" ? LINK_SCHEMES : IMAGE_SCHEMES).has(found)
}

/**
 * Removes what the policy refuses from parsed content: unsafe links lose the
 * link but keep their text, unsafe images become their alt text. Returns the
 * cleaned content and how many things were removed.
 */
export function sanitizeContent(
  content: JSONContent,
  policy: UrlPolicy = defaultUrlPolicy
): { content: JSONContent; removed: number } {
  let removed = 0

  const visit = (node: JSONContent, inText: boolean): JSONContent | null => {
    if (node.type === "image") {
      const src = String(node.attrs?.src ?? "")
      if (!policy(src, "image")) {
        removed += 1
        const alt = String(node.attrs?.alt ?? "").trim()
        if (!alt) return null
        const text = { type: "text", text: alt }
        return inText ? text : { type: "paragraph", content: [text] }
      }
    }
    let next = node
    if (node.marks?.length) {
      const marks = node.marks.filter((mark) => {
        if (mark.type !== "link") return true
        const ok = policy(String(mark.attrs?.href ?? ""), "link")
        if (!ok) removed += 1
        return ok
      })
      if (marks.length !== node.marks.length) next = { ...next, marks }
    }
    if (node.content) {
      const textblock = TEXTBLOCKS.has(node.type ?? "")
      const children = node.content
        .map((child) => visit(child, textblock))
        .filter((child): child is JSONContent => child !== null)
      next = { ...next, content: children }
    }
    return next
  }

  return {
    content: visit(content, false) ?? { type: "doc", content: [] },
    removed,
  }
}
